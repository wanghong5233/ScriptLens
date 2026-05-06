from __future__ import annotations

import re
from typing import Iterable, List, Tuple, Dict, Any, Set
from core.config import settings
from service.core.ingestion.constants import is_multimodal_metadata
from service.core.ingestion.interfaces import ParsedBlock, Chunker
from utils.get_logger import log


# ---- chunk 质量过滤（learned from LlamaIndex / RAGFlow / Anthropic Contextual Retrieval）----
#
# 核心原则：垃圾进、垃圾出。在入库前掐断低信息密度的 chunk，避免：
# (1) 召回阶段把垃圾块挤掉真正相关的内容；
# (2) 右侧引文面板出现「只有 URL/标题」这种没有阅读价值的卡片；
# (3) LLM 上下文被噪声稀释。
#
# 三层过滤：结构性黑名单 → 内容信息密度 → 孤立标题归并。

_URL_RE = re.compile(r"https?://\S+")
_PURE_URL_RE = re.compile(r"^\s*https?://\S+\s*$")
_HEADING_TYPES: Set[str] = {"title", "heading", "header", "h1", "h2", "h3", "h4", "h5", "h6"}
_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]{2,}")


def _substantive_word_count(text: str) -> int:
    """去掉 URL / Markdown 标题前缀后统计「像词」的 token 数，用于过滤伪正文块。"""
    stripped = _URL_RE.sub(" ", (text or "").strip())
    stripped = re.sub(r"^#{1,6}\s+", "", stripped, flags=re.MULTILINE)
    stripped = re.sub(r"[*_`]+", " ", stripped)
    return len(_TOKEN_RE.findall(stripped.lower()))


def _canonical_title_key(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"^#{1,6}\s+", "", t)
    t = re.sub(r"[*_`]+", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" .:;，。、")


def _drop_logical_types() -> Set[str]:
    raw = str(getattr(settings, "SM_CHUNK_DROP_LOGICAL_TYPES", "") or "")
    items = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return items


def _block_type(block: ParsedBlock) -> str:
    md = block.metadata or {}
    return str(md.get("logical_type") or md.get("element_type") or "").strip().lower()


def _is_low_information_text(text: str) -> bool:
    """信息密度过低的文本：纯 URL、过短、字符过于重复、纯标点。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return True

    min_chars = max(int(getattr(settings, "SM_CHUNK_MIN_INFORMATION_CHARS", 30) or 30), 0)
    if len(cleaned) < min_chars:
        return True

    if bool(getattr(settings, "SM_CHUNK_DROP_PURE_URL", True)):
        if _PURE_URL_RE.match(cleaned):
            return True
        url_ratio = float(getattr(settings, "SM_CHUNK_URL_CHAR_RATIO_MAX", 0.65) or 0.65)
        url_ratio = min(max(url_ratio, 0.1), 0.99)
        url_chars = sum(len(m.group(0)) for m in _URL_RE.finditer(cleaned))
        if url_chars >= url_ratio * max(len(cleaned), 1):
            return True

    min_words = max(int(getattr(settings, "SM_CHUNK_MIN_SUBSTANTIVE_WORDS", 5) or 0), 0)
    if min_words > 0 and len(cleaned) <= int(
        getattr(settings, "SM_CHUNK_SUBSTANTIVE_CHECK_MAX_CHARS", 8000) or 8000
    ):
        if _substantive_word_count(cleaned) < min_words:
            return True

    min_unique = max(int(getattr(settings, "SM_CHUNK_MIN_UNIQUE_CHARS", 20) or 20), 0)
    if len(set(cleaned)) < min_unique:
        return True

    # 字母 + 数字占比 < 30% 视为纯符号 / 公式 / 噪声
    alnum = sum(1 for c in cleaned if c.isalnum())
    if len(cleaned) > 0 and alnum / len(cleaned) < 0.3:
        return True

    return False


def _should_index_block(block: ParsedBlock, *, drop_set: Set[str]) -> Tuple[bool, str]:
    """判断单个 block 是否应该进入 chunk 索引。

    Returns:
        (keep, reason)；keep=False 时 reason 用于日志统计。
    """
    md = block.metadata or {}

    # 多模态块（图/表）由 indexer 单独处理，chunker 一律放行
    if is_multimodal_metadata(md):
        return True, "multimodal"

    btype = _block_type(block)
    if btype in drop_set:
        return False, f"blacklist:{btype}"

    # structure_path 也可能直接指向 references 节
    spath = str(md.get("structure_path") or "").lower()
    if "references" in spath or "bibliography" in spath:
        return False, "blacklist:structure_path"

    text = (block.text or "").strip()

    if _is_low_information_text(text):
        return False, "low_information"

    # 孤立的短标题：结构信息已写入下游 chunk 的 structure_title 元数据，
    # 标题独立成 chunk 既无阅读价值又会占召回名额。
    if (
        bool(getattr(settings, "SM_CHUNK_DROP_ISOLATED_HEADING", True))
        and btype in _HEADING_TYPES
        and len(text) < 120
    ):
        return False, f"isolated_heading:{btype}"

    return True, "keep"


def _filter_blocks(blocks: List[ParsedBlock]) -> Tuple[List[ParsedBlock], Dict[str, int]]:
    """应用 chunk 质量过滤，返回 (kept_blocks, dropped_stats)。"""
    if not bool(getattr(settings, "SM_CHUNK_QUALITY_FILTER_ENABLED", True)):
        return blocks, {}
    drop_set = _drop_logical_types()
    kept: List[ParsedBlock] = []
    dropped_stats: Dict[str, int] = {}
    for blk in blocks:
        keep, reason = _should_index_block(blk, drop_set=drop_set)
        if keep:
            kept.append(blk)
        else:
            dropped_stats[reason] = dropped_stats.get(reason, 0) + 1
    return kept, dropped_stats


def post_filter_chunks_for_embedding(
    chunks: List[ParsedBlock],
    *,
    document_title: str | None,
) -> Tuple[List[ParsedBlock], Dict[str, int]]:
    """嵌入前的最后一道门：再审一次结构与信息密度；去掉与正文标题重复的复述块。"""
    if not bool(getattr(settings, "SM_CHUNK_POST_FILTER_ENABLED", True)):
        return chunks, {}

    drop_set = _drop_logical_types()
    title_key = _canonical_title_key(document_title) if document_title else ""
    kept: List[ParsedBlock] = []
    stats: Dict[str, int] = {}
    for c in chunks or []:
        ok, reason = _should_index_block(c, drop_set=drop_set)
        if not ok:
            bucket = f"post:{reason}"
            stats[bucket] = stats.get(bucket, 0) + 1
            continue
        if title_key and len(title_key) >= 12:
            body = _canonical_title_key(c.text or "")
            if body and body == title_key:
                stats["post:title_duplicate_body"] = stats.get("post:title_duplicate_body", 0) + 1
                continue
        kept.append(c)
    return kept, stats


def _normalize_page_range(value: Any, fallback: Any = None) -> List[int]:
    pages: List[int] = []
    candidates = []
    if value is not None:
        candidates.append(value)
    if fallback is not None:
        candidates.append(fallback)
    for cand in candidates:
        if cand is None:
            continue
        if isinstance(cand, int):
            pages.append(int(cand))
        elif isinstance(cand, list):
            for item in cand:
                try:
                    pages.append(int(item))
                except Exception:
                    continue
        elif isinstance(cand, (tuple, set)):
            for item in cand:
                try:
                    pages.append(int(item))
                except Exception:
                    continue
    seen: List[int] = []
    for p in pages:
        if p not in seen:
            seen.append(p)
    return seen


def _merge_page_ranges(metas: List[Dict[str, Any]]) -> List[int]:
    combined: List[int] = []
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        rng = _normalize_page_range(meta.get("page_range"), meta.get("page"))
        for p in rng:
            if p not in combined:
                combined.append(p)
    return combined


def _merge_short_chunks(chunks: List[ParsedBlock]) -> List[ParsedBlock]:
    """
    历史遗留函数：在结构化重构后不再合并短块，仅负责清理空文本。
    （保留函数签名，以兼容旧逻辑调用。）
    """
    if not chunks:
        return []
    return [c for c in chunks if (c.text or "").strip()]


def _is_multimodal_block(block: ParsedBlock) -> bool:
    return is_multimodal_metadata(block.metadata)


def _produce_chunk(
    block: ParsedBlock,
    text: str,
    index: int,
    total: int,
    start: int,
    end: int,
    override_metadata: Dict[str, Any] | None = None,
) -> ParsedBlock:
    """
    生成 chunk 时，完整保留所有结构化元数据，确保数据管道完整性。
    
    保留的关键元数据：
    - 结构信息: structure_path, structure_title, logical_type, element_type
    - 位置信息: page_range, page, bbox_list
    - 文档信息: document_title, document_name, doi
    - 分块信息: structure_chunk_index, structure_chunk_total, offset_start, offset_end
    - 其他: source, alignment_status, parser_engine, 等等
    """
    md = dict(block.metadata or {})
    if override_metadata:
        md.update(override_metadata)

    # 页码范围处理
    pages = _normalize_page_range(md.get("page_range"), md.get("page"))
    if pages:
        md["page_range"] = pages
        md.setdefault("page", pages[0])

    # 分块索引信息
    md["structure_chunk_index"] = index - 1  # 从 0 开始，方便前端显示
    md["structure_chunk_total"] = total
    md["offset_start"] = start
    md["offset_end"] = end
    
    # 结构化元数据（确保存在）
    md.setdefault("logical_type", (block.metadata or {}).get("logical_type"))
    md.setdefault("element_type", md.get("logical_type") or (block.metadata or {}).get("element_type"))
    md.setdefault("structure_path", (block.metadata or {}).get("structure_path"))
    if block.metadata.get("structure_title"):
        md.setdefault("structure_title", block.metadata.get("structure_title"))
    if block.metadata.get("title"):
        md.setdefault("title", block.metadata.get("title"))
    
    # 位置信息（bbox_list）- 关键！用于前端精确定位
    if block.metadata.get("bbox_list"):
        md.setdefault("bbox_list", block.metadata.get("bbox_list"))
    
    # 对齐状态和来源信息
    if block.metadata.get("alignment_status"):
        md.setdefault("alignment_status", block.metadata.get("alignment_status"))
    if block.metadata.get("source"):
        md.setdefault("source", block.metadata.get("source"))
    if block.metadata.get("parser_engine"):
        md.setdefault("parser_engine", block.metadata.get("parser_engine"))

    return ParsedBlock(text=text, metadata=md)


class RecursiveCharacterChunker(Chunker):
    """递归字符分块器（兜底方案）
    
    学术 RAG 最佳实践：
    - target_chars: 800 (约 512 tokens，适合学术论文的段落长度)
    - overlap: 100 (约 12.5%，保证上下文连续性)
    """
    def __init__(self, target_chars: int = 800, overlap: int = 100) -> None:
        self.target_chars = target_chars
        self.overlap = overlap

    def _min_chunk_chars(self) -> int:
        return max(int(getattr(settings, "SM_CHUNK_MIN_CHARS", 200)), 100)

    def _find_chunk_boundary(self, text: str, start: int, preferred_end: int) -> int:
        """
        在首选终点附近寻找更自然的切分点（段落/句末），否则退回到首选位置。
        """
        length = len(text)
        preferred_end = min(length, max(start + self._min_chunk_chars(), preferred_end))
        back_window = max(int(getattr(settings, "SM_CHUNK_BREAK_BACK_WINDOW", 180)), 50)
        forward_window = max(int(getattr(settings, "SM_CHUNK_BREAK_FORWARD_WINDOW", 120)), 20)
        markers = ["\n\n", "\n", "。", "！", "？", ".", "!", "?"]

        # 1) 向后（优先选择靠近目标长度的断点）
        search_start = max(start + 1, preferred_end - back_window)
        snippet = text[search_start:preferred_end]
        for marker in markers:
            idx = snippet.rfind(marker)
            if idx != -1 and (search_start + idx) > start:
                return search_start + idx + len(marker)

        # 2) 向前（若后向没有命中，则允许稍超出目标长度）
        search_end = min(length, preferred_end + forward_window)
        snippet = text[preferred_end:search_end]
        for marker in markers:
            idx = snippet.find(marker)
            if idx != -1:
                pos = preferred_end + idx + len(marker)
                if pos - start >= self._min_chunk_chars():
                    return pos

        # 3) 兜底：直接使用首选终点
        return preferred_end

    def chunk(self, *, blocks: Iterable[ParsedBlock]) -> List[ParsedBlock]:
        # 结构优先：先按结构块迭代，内部再做长度切分
        block_list: List[ParsedBlock] = [b for b in blocks if (b.text or "").strip()]
        # 入库前的质量门：黑名单类型（references / footer / author_bio）、
        # 低信息密度（纯 URL / 过短 / 重复字符）、孤立短标题统统过滤。
        block_list, dropped_stats = _filter_blocks(block_list)
        if dropped_stats:
            try:
                log.info(
                    "RecursiveCharacterChunker.quality_filter dropped=%s",
                    dropped_stats,
                )
            except Exception:
                pass
        if getattr(settings, "SM_SEMANTIC_CHUNKING_ENABLED", False):
            # 从配置读取 SOTA 参数
            target = getattr(settings, "SM_CHUNK_TARGET_CHARS", 800)
            min_chars = getattr(settings, "SM_CHUNK_MIN_CHARS", 200)
            max_chars = getattr(settings, "SM_CHUNK_MAX_CHARS", 1200)
            sim_threshold = getattr(settings, "SM_SEMANTIC_SIMILARITY_THRESHOLD", 0.72)
            
            try:
                log.info(
                    f"SemanticAwareChunker enabled: input_blocks={len(block_list)} "
                    f"target={target} min={min_chars} max={max_chars} sim_threshold={sim_threshold}"
                )
            except Exception:
                pass
            chunks = SemanticAwareChunker(
                target_chars=target,
                min_chunk_chars=min_chars,
                max_chunk_chars=max_chars,
                similarity_threshold=sim_threshold
            ).chunk(blocks=block_list)
            try:
                log.info(
                    f"SemanticAwareChunker output_chunks={len(chunks)}"
                )
            except Exception:
                pass
            return chunks
        results: List[ParsedBlock] = []
        for block in block_list:
            text = (block.text or "").strip()
            if not text:
                continue
            # Chunker 不负责过滤多模态块，只负责切分
            # 多模态块直接保留，由 Indexer 统一过滤
            if _is_multimodal_block(block):
                results.append(
                    _produce_chunk(
                        block=block,
                        text=text,
                        index=1,
                        total=1,
                        start=0,
                        end=len(text),
                    )
                )
                continue
            pieces = self._split_block(text)
            total = len(pieces)
            for idx, (chunk_text, start, end) in enumerate(pieces, start=1):
                results.append(
                    _produce_chunk(
                        block=block,
                        text=chunk_text,
                        index=idx,
                        total=total,
                        start=start,
                        end=end,
                    )
                )

        results = _merge_short_chunks(results)

        try:
            log.info(
                f"RecursiveCharacterChunker output_chunks={len(results)} input_blocks={len(block_list)} target_chars={self.target_chars}"
            )
        except Exception:
            pass
        return results

    def _split_block(self, text: str) -> List[Tuple[str, int, int]]:
        """按 target/overlap 在单个结构块内部切分，返回 (chunk_text, start, end) 列表。"""
        pieces: List[Tuple[str, int, int]] = []
        if not text:
            return pieces
        start = 0
        length = len(text)
        target = max(self.target_chars, 200)
        overlap = min(self.overlap, target - 50) if target > 50 else 0
        while start < length:
            preferred_end = min(length, start + target)
            boundary = self._find_chunk_boundary(text, start, preferred_end)
            chunk_txt = text[start:boundary].strip()
            if chunk_txt:
                pieces.append((chunk_txt, start, boundary))
            if boundary >= length:
                break
            next_start = boundary - overlap
            if next_start <= start:
                next_start = boundary
            start = next_start
        return pieces



class SemanticAwareChunker(Chunker):
    """基于句向量相似度突变的语义感知分块（学术 RAG SOTA 方案）
    
    核心策略：
    - 先按句子切分（保留语义完整性）
    - 计算相邻句向量余弦相似度
    - 相似度低于阈值或累计长度达到上限时切块
    
    学术 RAG 最佳实践参数：
    - target_chars: 800 (约 512 tokens，适配 OpenAI/Qwen embedding 最佳窗口)
    - min_chunk_chars: 200 (约 128 tokens，避免碎片化)
    - max_chunk_chars: 1200 (约 768 tokens，硬上限防止溢出)
    - similarity_threshold: 0.72 (学术文本主题连贯性强，阈值适中)
    """

    def __init__(
        self, 
        target_chars: int = 800, 
        min_chunk_chars: int = 200,
        max_chunk_chars: int = 1200,
        similarity_threshold: float = 0.72
    ) -> None:
        self.target_chars = target_chars
        self.min_chunk_chars = min_chunk_chars
        self.max_chunk_chars = max_chunk_chars
        self.similarity_threshold = similarity_threshold

    def _split_sentences(self, text: str) -> List[str]:
        # 简易句切分（兼容中英）
        import re
        s = re.split(r"(?<=[。！？!?.])\s+|\n+", text.strip())
        return [t.strip() for t in s if t and t.strip()]

    def _split_block_sliding(self, block: ParsedBlock) -> List[ParsedBlock]:
        """滑窗兜底：当块长度远超 max_chunk_chars 时使用固定窗口切分。"""
        text = (block.text or "").strip()
        if not text:
            return []

        target = getattr(settings, "SM_CHUNK_TARGET_CHARS", self.target_chars)
        overlap = getattr(settings, "SM_CHUNK_OVERLAP_CHARS", 150)
        target = max(target, 400)  # IEEE 首段较长，兜底窗口稍大
        overlap = max(min(overlap, target // 3), 50)

        raw_pieces: List[Tuple[str, int, int]] = []
        start = 0
        length = len(text)
        while start < length:
            end = min(length, start + target)
            chunk_txt = text[start:end]
            raw_pieces.append((chunk_txt, start, end))
            if end >= length:
                break
            start = end - overlap
            if start < 0:
                start = 0

        total = len(raw_pieces)
        return [
            _produce_chunk(block=block, text=chunk_txt, index=idx + 1, total=total, start=piece_start, end=piece_end)
            for idx, (chunk_txt, piece_start, piece_end) in enumerate(raw_pieces)
        ]

    def _embed(self, sents: List[str]) -> List[List[float]]:
        # 复用已有 Embedder（本地或API），以确保维度一致
        try:
            from service.core.ingestion.embedder import SimpleAPIEmbedder
            emb = SimpleAPIEmbedder()
            # 复用其内部批处理接口：构造伪 chunks
            chunks = [ParsedBlock(text=si, metadata={}) for si in sents]
            recs = emb.embed(chunks=chunks)
            return [r.get("vector") or [] for r in recs]
        except Exception:
            return [[] for _ in sents]

    def _cos(self, a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 1.0
        import math
        da = math.sqrt(sum(x * x for x in a))
        db = math.sqrt(sum(x * x for x in b))
        if da == 0 or db == 0:
            return 1.0
        dot = sum(x * y for x, y in zip(a, b))
        return max(-1.0, min(1.0, dot / (da * db)))

    def _is_semantic_split_candidate(self, block: ParsedBlock) -> bool:
        """Only long prose-like layout blocks should pay the sentence-embedding cost."""

        metadata = block.metadata or {}
        block_type = str(
            metadata.get("logical_type") or metadata.get("element_type") or ""
        ).strip().lower()
        non_prose_types = {
            "title",
            "heading",
            "header",
            "footer",
            "table",
            "tablechunk",
            "image",
            "figure",
            "code",
            "equation",
            "formula",
            "link",
            "reference",
            "caption",
        }
        return block_type not in non_prose_types

    def _semantic_split_block(self, block: ParsedBlock) -> List[ParsedBlock]:
        """Split one long prose block by adjacent sentence embedding similarity."""

        text = (block.text or "").strip()
        if not text:
            return []
        sents = self._split_sentences(text)
        if not sents:
            return self._split_block_sliding(block)
        if len(sents) == 1 and len(text) > self.max_chunk_chars:
            return self._split_block_sliding(block)

        embs = self._embed(sents)
        buf: List[str] = []
        buf_vecs: List[List[float]] = []
        last_vec: List[float] | None = None
        chunk_payloads: List[Tuple[str, Dict[str, Any]]] = []

        def _flush_buffer() -> None:
            if not buf:
                return
            override_md: Dict[str, Any] = {}
            if buf_vecs:
                try:
                    dim = len(buf_vecs[0])
                    acc = [0.0] * dim
                    for vv in buf_vecs:
                        if len(vv) == dim:
                            for j in range(dim):
                                acc[j] += float(vv[j])
                    override_md["pre_embedding"] = [x / max(len(buf_vecs), 1) for x in acc]
                except Exception:
                    pass
            chunk_payloads.append(("\n".join(buf), override_md))

        for i, sent in enumerate(sents):
            if not buf:
                buf.append(sent)
                first_vec = embs[i] if i < len(embs) else None
                if isinstance(first_vec, list) and first_vec:
                    buf_vecs.append(first_vec)
                    last_vec = first_vec
                else:
                    last_vec = None
                continue

            cur_vec = embs[i] if i < len(embs) else None
            sim = self._cos(last_vec or [], cur_vec or [])
            buf_len = sum(len(x) for x in buf)
            next_len = buf_len + 1 + len(sent)
            force_split = next_len >= self.max_chunk_chars
            soft_split = (next_len >= self.target_chars) and (sim < self.similarity_threshold)
            can_split = (buf_len >= self.min_chunk_chars) or force_split

            if can_split and (force_split or soft_split):
                _flush_buffer()
                buf = [sent]
                buf_vecs = [cur_vec] if isinstance(cur_vec, list) and cur_vec else []
            else:
                buf.append(sent)
                if isinstance(cur_vec, list) and cur_vec:
                    buf_vecs.append(cur_vec)
            last_vec = cur_vec

        if buf:
            _flush_buffer()

        total = len(chunk_payloads)
        return [
            _produce_chunk(
                block=block,
                text=chunk_text,
                index=idx,
                total=total,
                start=0,
                end=len(chunk_text),
                override_metadata=override_md,
            )
            for idx, (chunk_text, override_md) in enumerate(chunk_payloads, start=1)
        ]
    
    def _chunk_block_level(self, blocks: List[ParsedBlock]) -> List[ParsedBlock]:
        """Hybrid layout-aware mode: preserve layout blocks, semantically split long prose."""

        valid_blocks = [b for b in blocks if (b.text or "").strip()]
        if not valid_blocks:
            return []
        # 关键质量门：LlamaParse / Unstructured / MinerU 输出的 layout blocks 里
        # 会混入 references / heading / 纯 URL / 作者简介等无信息块；如果直接保留
        # 为 chunk，会出现「右侧引文卡片只有一行 URL 或一个标题」的脏数据。
        # 这里在 hybrid layout-aware 路径入口统一过滤，与 RecursiveCharacterChunker
        # 入口的过滤策略保持一致。
        valid_blocks, dropped_stats = _filter_blocks(valid_blocks)
        if dropped_stats:
            try:
                log.info(
                    "SemanticAwareChunker.hybrid_layout.quality_filter dropped=%s",
                    dropped_stats,
                )
            except Exception:
                pass
        if not valid_blocks:
            return []

        final_results: List[ParsedBlock] = []
        preserved_blocks = 0
        semantic_split_blocks = 0
        semantic_split_chunks = 0
        sliding_split_blocks = 0
        layout_semantic_enabled = bool(getattr(settings, "SM_LAYOUT_SEMANTIC_SPLIT_ENABLED", True))
        semantic_min_chars = max(
            int(getattr(settings, "SM_LAYOUT_SEMANTIC_MIN_CHARS", self.min_chunk_chars)),
            self.min_chunk_chars,
        )
        for block in valid_blocks:
            text = (block.text or "").strip()
            if not text:
                continue

            # Chunker 不负责过滤多模态块，只负责切分
            # 多模态块直接保留，由 Indexer 统一过滤
            if _is_multimodal_block(block):
                final_results.append(
                    _produce_chunk(
                        block=block,
                        text=text,
                        index=1,
                        total=1,
                        start=0,
                        end=len(text),
                    )
                )
                preserved_blocks += 1
                continue

            if (
                layout_semantic_enabled
                and len(text) >= semantic_min_chars
                and self._is_semantic_split_candidate(block)
            ):
                split_chunks = self._semantic_split_block(block)
                final_results.extend(split_chunks)
                semantic_split_blocks += 1
                semantic_split_chunks += len(split_chunks)
                continue

            if len(text) <= self.max_chunk_chars:
                final_results.append(
                    _produce_chunk(
                        block=block,
                        text=text,
                        index=1,
                        total=1,
                        start=0,
                        end=len(text),
                    )
                )
                preserved_blocks += 1
                continue

            split_chunks = self._split_block_sliding(block)
            final_results.extend(split_chunks)
            sliding_split_blocks += 1

        try:
            log.info(
                "SemanticAwareChunker.hybrid_layout: "
                f"input={len(valid_blocks)} output={len(final_results)} "
                f"preserved={preserved_blocks} semantic_blocks={semantic_split_blocks} "
                f"semantic_chunks={semantic_split_chunks} sliding_blocks={sliding_split_blocks} "
                f"semantic_min_chars={semantic_min_chars}"
            )
        except Exception:
            pass
        return final_results

    def chunk(self, *, blocks: Iterable[ParsedBlock]) -> List[ParsedBlock]:
        # 保障可重复遍历与统计
        _blocks: List[ParsedBlock] = list(blocks)
        results: List[ParsedBlock] = []
        try:
            log.info(
                f"SemanticAwareChunker.start blocks={len(_blocks)} target_chars={self.target_chars} sim_threshold={self.similarity_threshold}"
            )
        except Exception:
            pass
        
        # Rich layout parsers already produce citation-aware blocks. Preserve those
        # boundaries first, then apply sentence-level semantic splitting only inside
        # long prose blocks.
        layout_preserving_engines = {"mineru", "llamaparse", "unstructured_api"}
        is_layout_preserving = any(
            (b.metadata or {}).get("parser_engine") in layout_preserving_engines
            for b in _blocks
        )
        
        if is_layout_preserving and getattr(settings, "SM_LAYOUT_AWARE_CHUNKING_ENABLED", True):
            try:
                engines = sorted(
                    {
                        str((b.metadata or {}).get("parser_engine"))
                        for b in _blocks
                        if (b.metadata or {}).get("parser_engine")
                    }
                )
                log.info(
                    f"SemanticAwareChunker.hybrid_layout_mode engines={engines} "
                    f"blocks={len(_blocks)}"
                )
            except Exception:
                pass
            block_level = self._chunk_block_level(_blocks)
            return _merge_short_chunks(block_level)
        
        # 非 layout-aware 路径：句子级语义分块。同样要在入口质量过滤，
        # 否则 PyMuPDF / Unstructured 兜底输出的 references / footer 块
        # 会绕过 _chunk_block_level 的过滤直接进入索引。
        _blocks, dropped_stats = _filter_blocks(_blocks)
        if dropped_stats:
            try:
                log.info(
                    "SemanticAwareChunker.sentence.quality_filter dropped=%s",
                    dropped_stats,
                )
            except Exception:
                pass
        for b in _blocks:
            text = (b.text or "").strip()
            # Chunker 不负责过滤多模态块，只负责切分
            # 多模态块直接保留，由 Indexer 统一过滤
            if not text:
                continue
            sents = self._split_sentences(text)
            if not sents:
                # 当分句失败但文本非空时，回退为单块，确保产出可用 chunk
                try:
                    log.info("SemanticAwareChunker.sents_empty_fallback: using single-block fallback")
                except Exception:
                    pass
                results.append(
                    _produce_chunk(
                        block=b,
                        text=text,
                        index=1,
                        total=1,
                        start=0,
                        end=len(text),
                    )
                )
                continue
            results.extend(self._semantic_split_block(b))
        results = _merge_short_chunks(results)

        try:
            log.info(
                f"SemanticAwareChunker.finish blocks={len(_blocks)} chunks={len(results)}"
            )
        except Exception:
            pass
        return results
