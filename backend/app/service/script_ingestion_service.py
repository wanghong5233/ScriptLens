"""ScriptLens 剧本摄取服务（顶层编排）。

链路：本地文件 → load_script_paragraphs → segment_script →
generate_embedding (DashScope) → ScriptPgVectorWriter.

调用方：
- `router/script_rt.py POST /api/scripts/upload` （HTTP 入口；走两阶段异步）
- `eval/_e2e_dryrun.py` （离线 e2e 验证；走单阶段 ingest）

不变式：
- 单个剧本的 scenes + chunks 在一个事务里写完，要么全成要么全无
- 任何步骤异常都向上抛，不静默吞错（fail-fast）
- 解析/切分耗时大头时，可以传 `progress_cb` 让上游推 SSE 进度

两种调用模式：
1. **同步单阶段** —— `ingest()`：一次性 INSERT 三表 + status='ready'，dryrun / CLI 用
2. **异步两阶段** —— `start_pending()` + `run_ingestion()`：HTTP upload 立即返回
   `script_id` + status='pending'，BackgroundTask 跑完 INSERT scenes/chunks 并
   UPDATE status='ready'；任何阶段异常 → mark_failed，前端可见 failure_reason
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from service.core.ingestion.script_loader import (
    UnsupportedScriptFormatError,
    load_script_paragraphs,
)
from service.core.ingestion.script_pgvector_writer import (
    ScriptPgVectorWriter,
    WrittenScene,
)
from service.core.ingestion.script_segmenter import (
    SegmentResult,
    segment_script,
)

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    script_id: str
    title: str
    source_format: str
    total_episodes: int
    total_scenes: int
    total_chars: int
    embedded_scenes: int
    fallback_strategy: Optional[str]
    parsing_warnings: List[str]
    written_scenes: List[WrittenScene]


class ScriptIngestionService:
    """三步走：load → segment → embed+persist。"""

    def __init__(
        self,
        *,
        writer: Optional[ScriptPgVectorWriter] = None,
        embed_fn: Optional[Callable[[List[str]], List[List[float]]]] = None,
    ) -> None:
        self.writer = writer or ScriptPgVectorWriter()
        # embed_fn 默认懒加载，避免 e2e dryrun 依赖 DashScope 配置
        self._embed_fn = embed_fn

    # ============================================================
    # 同步单阶段（dryrun / CLI 离线用）
    # ============================================================

    def ingest(
        self,
        *,
        file_path: Path,
        user_id: int,
        title: Optional[str] = None,
        progress_cb: Optional[Callable[[str, dict], None]] = None,
    ) -> IngestResult:
        """同步主入口；任一阶段失败立即抛错。三表一次性 INSERT + status='ready'。"""
        t0 = time.perf_counter()
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))

        suffix = file_path.suffix.lower().lstrip(".")
        title = title or file_path.stem

        paragraphs, seg, embeddings = self._load_segment_embed(
            file_path=file_path,
            progress_cb=progress_cb,
        )
        embedded_count = sum(1 for v in embeddings if v is not None)

        if progress_cb:
            progress_cb("persisting", {"scenes": seg.total_scenes})
        script_id, written = self.writer.insert_script_with_scenes(
            user_id=user_id,
            title=title,
            source_format=suffix,
            raw_storage_path=str(file_path),
            total_episodes=seg.total_episodes,
            scenes=seg.scenes,
            scene_embeddings=embeddings,
        )

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("ingest.done script_id=%s elapsed_ms=%s", script_id, elapsed_ms)
        if progress_cb:
            progress_cb("done", {"script_id": script_id, "elapsed_ms": elapsed_ms})

        return IngestResult(
            script_id=script_id,
            title=title,
            source_format=suffix,
            total_episodes=seg.total_episodes,
            total_scenes=seg.total_scenes,
            total_chars=seg.total_chars,
            embedded_scenes=embedded_count,
            fallback_strategy=seg.fallback_strategy,
            parsing_warnings=seg.parsing_warnings,
            written_scenes=written,
        )

    # ============================================================
    # 异步两阶段（HTTP upload 用）
    # ============================================================

    def start_pending(
        self,
        *,
        file_path: Path,
        user_id: int,
        title: Optional[str] = None,
    ) -> str:
        """阶段一：file 已落盘 → INSERT scripts(status='pending') → 返回 script_id。

        立即返回，不做解析/embedding。后续由 `run_ingestion(script_id, file_path)`
        在 BackgroundTask 里跑完整链路。
        """
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))
        suffix = file_path.suffix.lower().lstrip(".")
        title = title or file_path.stem
        return self.writer.create_pending_script(
            user_id=user_id,
            title=title,
            source_format=suffix,
            raw_storage_path=str(file_path),
        )

    def run_ingestion(
        self,
        *,
        script_id: str,
        file_path: Path,
        progress_cb: Optional[Callable[[str, dict], None]] = None,
    ) -> IngestResult:
        """阶段二：跑完整链路并 UPDATE status='ready'；失败时 mark_failed 后抛错。

        BackgroundTask 入口。失败 reason 会落到 scripts.failure_reason，前端能看到。
        """
        t0 = time.perf_counter()
        try:
            self.writer.update_status(script_id, "parsing")
            paragraphs, seg, embeddings = self._load_segment_embed(
                file_path=file_path,
                progress_cb=progress_cb,
            )
            embedded_count = sum(1 for v in embeddings if v is not None)

            self.writer.update_status(script_id, "indexing")
            if progress_cb:
                progress_cb("persisting", {"scenes": seg.total_scenes})
            written = self.writer.complete_script_with_scenes(
                script_id=script_id,
                total_episodes=seg.total_episodes,
                scenes=seg.scenes,
                scene_embeddings=embeddings,
            )
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            logger.exception("run_ingestion failed script_id=%s reason=%s", script_id, reason)
            try:
                self.writer.mark_failed(script_id, reason)
            except Exception:
                logger.exception("mark_failed also failed script_id=%s", script_id)
            raise

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("run_ingestion.done script_id=%s elapsed_ms=%s", script_id, elapsed_ms)
        if progress_cb:
            progress_cb("done", {"script_id": script_id, "elapsed_ms": elapsed_ms})

        return IngestResult(
            script_id=script_id,
            title=file_path.stem,
            source_format=file_path.suffix.lower().lstrip("."),
            total_episodes=seg.total_episodes,
            total_scenes=seg.total_scenes,
            total_chars=seg.total_chars,
            embedded_scenes=embedded_count,
            fallback_strategy=seg.fallback_strategy,
            parsing_warnings=seg.parsing_warnings,
            written_scenes=written,
        )

    # ============================================================
    # 内部：load + segment + embed（不写库）
    # ============================================================

    def _load_segment_embed(
        self,
        *,
        file_path: Path,
        progress_cb: Optional[Callable[[str, dict], None]] = None,
    ) -> tuple[List[str], SegmentResult, List[Optional[List[float]]]]:
        if progress_cb:
            progress_cb("loading", {"file": str(file_path)})
        paragraphs = load_script_paragraphs(file_path)
        if not paragraphs:
            raise ValueError(f"剧本解析后段落为空：{file_path.name}")
        logger.info("ingest.loaded paragraphs=%s file=%s", len(paragraphs), file_path.name)

        if progress_cb:
            progress_cb("segmenting", {"paragraphs": len(paragraphs)})
        seg: SegmentResult = segment_script(paragraphs)
        if not seg.scenes:
            raise ValueError(f"剧本切分后无场景：{file_path.name}")
        logger.info(
            "ingest.segmented scenes=%s eps=%s fallback=%s file=%s",
            seg.total_scenes,
            seg.total_episodes,
            seg.fallback_strategy,
            file_path.name,
        )

        if progress_cb:
            progress_cb("embedding", {"scenes": seg.total_scenes})
        scene_texts = [s.text for s in seg.scenes]
        embeddings = self._embed_batch(scene_texts)
        logger.info(
            "ingest.embedded count=%s/%s",
            sum(1 for v in embeddings if v is not None),
            len(scene_texts),
        )
        return paragraphs, seg, embeddings

    def _embed_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """对 N 段文本调用 embedding；失败的位置返回 None（不阻断整批）。"""
        if not texts:
            return []
        embed_fn = self._embed_fn or _default_embed_fn
        try:
            vecs = embed_fn(texts)
        except Exception as e:
            logger.error("embedding batch failed: %s", e)
            raise
        out: List[Optional[List[float]]] = []
        for i in range(len(texts)):
            v = vecs[i] if i < len(vecs) else None
            if isinstance(v, list) and v:
                out.append(v)
            else:
                out.append(None)
        return out


def _default_embed_fn(texts: List[str]) -> List[List[float]]:
    """默认走 ScholarMind 现成 generate_embedding（DashScope text-embedding-v3）。"""
    from service.core.rag.nlp.model import generate_embedding

    return generate_embedding(texts) or []
