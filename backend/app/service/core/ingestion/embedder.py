from __future__ import annotations

import time
from typing import Dict, Iterable, List
from service.core.ingestion.interfaces import ParsedBlock, Embedder
from service.core.rag.nlp.model import generate_embedding
import logging


class DummyEmbedder(Embedder):
    """
    占位嵌入器：返回零向量，便于先打通接口。
    后续替换为本地/云端真实嵌入器。
    """

    def __init__(self, dim: int = 768) -> None:
        self.dim = dim

    def embed(self, *, chunks: Iterable[ParsedBlock]) -> List[Dict[str, object]]:
        vec = [0.0] * self.dim
        records: List[Dict[str, object]] = []
        for c in chunks:
            records.append({
                "text": c.text,
                "vector": vec,
                "metadata": c.metadata,
            })
        return records


class SimpleAPIEmbedder(Embedder):
    """
    简易真实嵌入器：调用现有 generate_embedding(texts) 接口批量生成向量。
    作为第一版可用实现，后续可替换为本地/云端更高性能的实现。
    """

    def __init__(self, batch_size: int = 10) -> None:
        self.batch_size = batch_size

    def embed(self, *, chunks: Iterable[ParsedBlock]) -> List[Dict[str, object]]:
        logger = logging.getLogger("ingestion.embedder")
        started_at = time.perf_counter()
        chunk_list = list(chunks)
        if not chunk_list:
            logger.info("SimpleAPIEmbedder: no texts to embed (0 chunks)")
            return []
        total_chars = sum(len((chunk.text or "")) for chunk in chunk_list)
        # 先读取预嵌入，缺失的再批量请求
        pre_vecs: List[List[float] | None] = []
        missing_indices: List[int] = []
        for i, c in enumerate(chunk_list):
            md = c.metadata or {}
            v = md.get("pre_embedding") if isinstance(md, dict) else None
            if isinstance(v, list) and v and isinstance(v[0], (int, float)):
                pre_vecs.append(v)
            else:
                pre_vecs.append(None)
                missing_indices.append(i)

        logger.info(
            "SimpleAPIEmbedder.start chunks=%s chars=%s preembedded=%s missing=%s batch_size=%s",
            len(chunk_list),
            total_chars,
            len(chunk_list) - len(missing_indices),
            len(missing_indices),
            self.batch_size,
        )
        embeddings: List[List[float] | None] = [None] * len(chunk_list)
        # 批量嵌入缺失部分
        if missing_indices:
            texts_missing = [(chunk_list[i].text or "") for i in missing_indices]
            try:
                gen = generate_embedding(texts_missing) or []
            except Exception as e:
                logger.error(f"SimpleAPIEmbedder: generate_embedding failed: {e}")
                gen = []
            for k, i in enumerate(missing_indices):
                embeddings[i] = gen[k] if k < len(gen) else None

        # 合并预嵌入
        for i in range(len(chunk_list)):
            if pre_vecs[i] is not None:
                embeddings[i] = pre_vecs[i]

        # 兜底：若返回不足，填充空向量
        dim = 1024
        for v in embeddings:
            if isinstance(v, list) and v:
                dim = len(v)
                break
        records: List[Dict[str, object]] = []
        for idx, c in enumerate(chunk_list):
            emb = embeddings[idx] if idx < len(embeddings) else None
            vec = emb if emb is not None else ([0.0] * dim)
            records.append({
                "text": c.text,
                "vector": vec,
                "metadata": c.metadata,
            })
        logger.info(
            "SimpleAPIEmbedder.finish records=%s dim=%s elapsed_ms=%s",
            len(records),
            dim,
            int((time.perf_counter() - started_at) * 1000),
        )
        return records


