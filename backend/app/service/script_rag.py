"""ScriptLens 简化 RAG（embedding + BM25 → RRF → top-k）。

来源：reuse-matrix §3 决定砍掉 ScholarMind 的六层 RAG（query variants / HyDE /
metadata boost / cross-encoder rerank），只保留两路并行召回 + RRF 合并。

为什么短剧场景不需要那六层：
- 短剧检索目标是「场景」（1 chunk = 1 scene，~500-2000 字），粒度大，召回率高
- 用户提问通常带具体词汇（角色名 / 「打脸」 / 「反转」），lexical 命中率本身就高
- 单部剧 chunks ≤ 2000，TopK=5 召回容错率宽，rerank ROI 低

实施细节：
- embedding 路径：DashScope text-embedding-v3 (1024 维) + pgvector cosine
- BM25 路径：PG `to_tsvector('simple', text)` GIN 索引（alembic 已建）
- RRF：score_d = sum(1 / (k + rank_i)) for each list i; k=60（业界默认）

权限：所有查询强制 user_id 过滤，避免越权读他人剧本场景。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


# ============================================================
# 数据类
# ============================================================


@dataclass
class ScoredScene:
    """RAG 检索返回的单条结果。"""

    scene_id: str
    script_id: str
    episode_no: Optional[int]
    scene_no: str
    scene_label: str
    text: str
    rrf_score: float
    embedding_rank: Optional[int] = None
    bm25_rank: Optional[int] = None


@dataclass
class _RawHit:
    scene_id: str
    rank: int


# ============================================================
# 主入口
# ============================================================


async def retrieve_scenes(
    *,
    script_id: str,
    query: str,
    top_k: int = 5,
    use_embedding: bool = True,
    use_bm25: bool = True,
    candidate_pool: int = 20,
    rrf_k: int = 60,
    engine: Engine = default_engine,
) -> List[ScoredScene]:
    """两路并行召回（embedding + BM25）→ RRF 合并 → top-k。

    Args:
        script_id: 限定剧本范围（避免跨剧本污染）
        query: 用户查询（中文短句）
        top_k: 最终返回条数
        use_embedding: 关闭时退化为纯 BM25（embedding 服务故障兜底）
        use_bm25: 关闭时退化为纯向量
        candidate_pool: 每路召回的候选数（默认 20，足够 RRF 重排）
        rrf_k: RRF 平滑因子（业界默认 60）

    Returns:
        按 RRF 分数倒序的 ScoredScene 列表（≤ top_k 条）。空 query / 双路全关
        / 全部失败 → 返回 []。
    """
    query = (query or "").strip()
    if not query or top_k <= 0:
        return []
    if not (use_embedding or use_bm25):
        logger.warning("retrieve_scenes called with both retrievers disabled")
        return []

    # 并行跑两路（任一失败 → 降级）
    emb_task = _embedding_recall(script_id, query, candidate_pool, engine) if use_embedding else _empty_async()
    bm25_task = _bm25_recall(script_id, query, candidate_pool, engine) if use_bm25 else _empty_async()
    emb_hits, bm25_hits = await asyncio.gather(emb_task, bm25_task, return_exceptions=True)

    if isinstance(emb_hits, Exception):
        logger.warning("embedding recall failed (degrade to BM25): %s", emb_hits)
        emb_hits = []
    if isinstance(bm25_hits, Exception):
        logger.warning("BM25 recall failed (degrade to embedding-only): %s", bm25_hits)
        bm25_hits = []

    if not emb_hits and not bm25_hits:
        return []

    # RRF 融合
    fused = _rrf_fuse(emb_hits, bm25_hits, rrf_k)
    top_ids = [scene_id for scene_id, _ in fused[:top_k]]
    if not top_ids:
        return []

    # 一次性把 top_k 场景文本拉回来
    scenes_by_id = _fetch_scenes_by_ids(top_ids, engine=engine)

    out: List[ScoredScene] = []
    emb_rank_map = {h.scene_id: h.rank for h in emb_hits}
    bm25_rank_map = {h.scene_id: h.rank for h in bm25_hits}
    for scene_id, rrf_score in fused[:top_k]:
        sc = scenes_by_id.get(scene_id)
        if sc is None:
            continue
        out.append(
            ScoredScene(
                scene_id=scene_id,
                script_id=sc["script_id"],
                episode_no=sc["episode_no"],
                scene_no=sc["scene_no"],
                scene_label=sc["scene_label"] or "",
                text=sc["text"] or "",
                rrf_score=rrf_score,
                embedding_rank=emb_rank_map.get(scene_id),
                bm25_rank=bm25_rank_map.get(scene_id),
            )
        )
    logger.info(
        "retrieve_scenes script_id=%s query=%r returned=%s",
        script_id,
        query[:30],
        len(out),
    )
    return out


# ============================================================
# embedding 召回（pgvector cosine）
# ============================================================


async def _embedding_recall(
    script_id: str,
    query: str,
    pool: int,
    engine: Engine,
) -> List[_RawHit]:
    """调 generate_embedding（同步函数）→ pgvector cosine top-N。"""
    vec = await asyncio.to_thread(_embed_query, query)
    if not vec:
        return []
    vec_literal = "[" + ",".join(f"{float(v):.6f}" for v in vec) + "]"
    return await asyncio.to_thread(_embedding_sql, script_id, vec_literal, pool, engine)


def _embed_query(query: str) -> List[float]:
    from service.core.rag.nlp.model import generate_embedding

    vecs = generate_embedding(query)
    if not vecs:
        return []
    if isinstance(vecs[0], list):
        return vecs[0]
    return list(vecs)


def _embedding_sql(
    script_id: str,
    vec_literal: str,
    pool: int,
    engine: Engine,
) -> List[_RawHit]:
    """ivfflat cosine 检索；返回 (scene_id, rank)。"""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.scene_id::text AS scene_id
                FROM scriptlens.script_chunks c
                WHERE c.script_id = :sid AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> CAST(:vec AS vector)
                LIMIT :n
                """
            ),
            {"sid": script_id, "vec": vec_literal, "n": pool},
        ).all()
    return [_RawHit(scene_id=r.scene_id, rank=i + 1) for i, r in enumerate(rows)]


# ============================================================
# BM25 召回（PG to_tsvector + ts_rank_cd）
# ============================================================


async def _bm25_recall(
    script_id: str,
    query: str,
    pool: int,
    engine: Engine,
) -> List[_RawHit]:
    return await asyncio.to_thread(_bm25_sql, script_id, query, pool, engine)


def _bm25_sql(script_id: str, query: str, pool: int, engine: Engine) -> List[_RawHit]:
    """PG `to_tsvector('simple', text)` 全文检索。

    用 `plainto_tsquery('simple', :q)` 做 query 解析（不分词，按空白切；中文文本
    `'simple'` 配置实际是按字切分，足够短剧关键词命中场景）。
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.scene_id::text AS scene_id,
                       ts_rank_cd(
                         to_tsvector('simple', coalesce(c.text, '')),
                         plainto_tsquery('simple', :q)
                       ) AS score
                FROM scriptlens.script_chunks c
                WHERE c.script_id = :sid
                  AND to_tsvector('simple', coalesce(c.text, '')) @@ plainto_tsquery('simple', :q)
                ORDER BY score DESC
                LIMIT :n
                """
            ),
            {"sid": script_id, "q": query, "n": pool},
        ).all()
    return [_RawHit(scene_id=r.scene_id, rank=i + 1) for i, r in enumerate(rows)]


# ============================================================
# RRF 融合
# ============================================================


def _rrf_fuse(
    emb_hits: List[_RawHit],
    bm25_hits: List[_RawHit],
    rrf_k: int,
) -> List[tuple[str, float]]:
    """RRF: score(d) = Σ 1 / (rrf_k + rank_i(d))。

    返回按 score 倒序的 (scene_id, score) 列表。
    """
    scores: dict[str, float] = {}
    for hits in (emb_hits, bm25_hits):
        for h in hits:
            scores[h.scene_id] = scores.get(h.scene_id, 0.0) + 1.0 / (rrf_k + h.rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


# ============================================================
# 批量回填场景文本
# ============================================================


def _fetch_scenes_by_ids(scene_ids: List[str], *, engine: Engine) -> dict:
    if not scene_ids:
        return {}
    stmt = text(
        """
        SELECT id::text AS id, script_id::text AS script_id,
               episode_no, scene_no, scene_label, text
        FROM scriptlens.scenes
        WHERE id::text IN :ids
        """
    ).bindparams(bindparam("ids", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(stmt, {"ids": scene_ids}).mappings().all()
    return {r["id"]: dict(r) for r in rows}


async def _empty_async() -> List[_RawHit]:
    return []
