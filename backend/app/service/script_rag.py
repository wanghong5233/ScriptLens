"""ScriptLens 检索：BM25（一级）+ 关键词兜底 + LLM metadata 挑选。

设计依据见 `docs/04-script-pipeline.md` §4：
- 评分 / 证据 / 任务派发 三条核心链路均不查检索，唯一调用方是 Agent 自由
  对话里的 `locate_scenes_tool`
- 短剧用户的真实查询 95%+ 是角色名 / 集场号 / 关键事件 → BM25 + jieba
  关键词兜底命中率高
- 抽象语义查询（"令人破防的桥段"）走二级兜底：把 ≤ 2000 场的 metadata 列表
  喂给 LLM，让 LLM 自己挑——比 embedding 更稳，且无 ingestion 成本

权限：所有查询强制 `script_id` 过滤；user_id 过滤由 Agent 工具层（拿
agent_state.user_id）保证，本模块只接收已校验的 script_id。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import List, Literal, Optional

import jieba
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


# ============================================================
# 数据类
# ============================================================


RetrievalSource = Literal["bm25", "keyword", "llm_metadata"]


@dataclass
class ScoredScene:
    """检索返回的单条结果。"""

    scene_id: str
    script_id: str
    episode_no: Optional[int]
    scene_no: str
    scene_label: str
    text: str
    score: float
    rank: int
    source: RetrievalSource


# ============================================================
# 主入口
# ============================================================


async def retrieve_scenes(
    *,
    script_id: str,
    query: str,
    top_k: int = 5,
    candidate_pool: int = 20,
    engine: Engine = default_engine,
) -> List[ScoredScene]:
    """三级检索：BM25 → jieba 关键词兜底 → LLM 看全剧 metadata 列表挑。

    Args:
        script_id: 限定剧本范围（避免跨剧本污染）
        query: 用户查询（中文短句）
        top_k: 最终返回条数
        candidate_pool: BM25 召回的候选数（默认 20，>= top_k）

    Returns:
        ScoredScene 列表（≤ top_k 条）。空 query → 返回 []；
        三层兜底全部失败 → 返回 []（不抛错，caller 自己处理"没找到"）。
    """
    query = (query or "").strip()
    if not query or top_k <= 0:
        return []

    # 一级：BM25
    bm25_hits = await asyncio.to_thread(_bm25_query_scenes, script_id, query, candidate_pool, engine)
    if bm25_hits:
        out = bm25_hits[:top_k]
        logger.info(
            "retrieve_scenes script_id=%s query=%r returned=%d source=bm25",
            script_id, query[:30], len(out),
        )
        return out

    # 二级：PG simple tokenizer 对中文长串不稳定，先用 jieba 切词 + ILIKE 兜底。
    keyword_hits = await asyncio.to_thread(_keyword_query_scenes, script_id, query, candidate_pool, engine)
    if keyword_hits:
        out = keyword_hits[:top_k]
        logger.info(
            "retrieve_scenes script_id=%s query=%r returned=%d source=keyword",
            script_id, query[:30], len(out),
        )
        return out

    # 三级：LLM metadata 兜底
    logger.info(
        "retrieve_scenes lexical miss script_id=%s query=%r → fallback to llm_metadata",
        script_id, query[:30],
    )
    try:
        llm_hits = await _llm_pick_scenes(script_id, query, top_k, engine)
    except Exception as exc:
        logger.warning("llm_metadata fallback failed: %s", exc)
        return []

    logger.info(
        "retrieve_scenes script_id=%s query=%r returned=%d source=llm_metadata",
        script_id, query[:30], len(llm_hits),
    )
    return llm_hits


# ============================================================
# 一级：BM25（PG to_tsvector + ts_rank_cd）
# ============================================================


_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _query_terms(query: str, *, max_terms: int = 8) -> List[str]:
    """把用户短句转成中文关键词；保留原句，避免 jieba 切坏专名。"""
    terms: List[str] = []
    seen: set[str] = set()
    for raw in [query, *jieba.lcut(query, cut_all=False)]:
        term = "".join(_TOKEN_RE.findall(raw)).strip()
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= max_terms:
            break
    return terms


def _bm25_query_scenes(
    script_id: str,
    query: str,
    pool: int,
    engine: Engine,
) -> List[ScoredScene]:
    """直接查 `scriptlens.scenes.text`，一次 SQL 拿到 score + 完整字段。

    `plainto_tsquery` 输入先经 jieba 切词，尽量让 PG simple tokenizer 不被中文长串拖垮。

    `idx_scenes_text_fts`（GIN）保证 O(log N) 查询。
    """
    fts_query = " ".join(_query_terms(query)) or query
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT s.id::text AS scene_id,
                       s.script_id::text AS script_id,
                       s.episode_no,
                       s.scene_no,
                       s.scene_label,
                       s.text,
                       ts_rank_cd(
                         to_tsvector('simple', coalesce(s.text, '')),
                         plainto_tsquery('simple', :q)
                       ) AS score
                FROM scriptlens.scenes s
                WHERE s.script_id = :sid
                  AND to_tsvector('simple', coalesce(s.text, '')) @@ plainto_tsquery('simple', :q)
                ORDER BY score DESC
                LIMIT :n
                """
            ),
            {"sid": script_id, "q": fts_query, "n": pool},
        ).all()
    out: List[ScoredScene] = []
    for i, r in enumerate(rows):
        out.append(
            ScoredScene(
                scene_id=r.scene_id,
                script_id=r.script_id,
                episode_no=r.episode_no,
                scene_no=r.scene_no,
                scene_label=r.scene_label or "",
                text=r.text or "",
                score=float(r.score or 0.0),
                rank=i + 1,
                source="bm25",
            )
        )
    return out


# ============================================================
# 二级：jieba 关键词兜底（不改 DB schema）
# ============================================================


def _keyword_query_scenes(
    script_id: str,
    query: str,
    pool: int,
    engine: Engine,
) -> List[ScoredScene]:
    """PG FTS miss 后，用 jieba 关键词 ILIKE 做保守召回。

    这是 Agent 自由检索路径的兜底，不参与评分链路；比直接让 LLM metadata
    盲选更可靠，尤其是角色名、地点、事件词这类中文短查询。
    """
    terms = _query_terms(query)
    if not terms:
        return []

    where_parts: List[str] = []
    score_parts: List[str] = []
    params: dict[str, object] = {"sid": script_id, "n": pool}
    for i, term in enumerate(terms):
        key = f"term_{i}"
        params[key] = f"%{term}%"
        where_parts.append(f"s.text ILIKE :{key}")
        score_parts.append(f"CASE WHEN s.text ILIKE :{key} THEN 1 ELSE 0 END")

    score_expr = " + ".join(score_parts)
    stmt = text(
        f"""
        SELECT s.id::text AS scene_id,
               s.script_id::text AS script_id,
               s.episode_no,
               s.scene_no,
               s.scene_label,
               s.text,
               ({score_expr})::float AS score
        FROM scriptlens.scenes s
        WHERE s.script_id = :sid
          AND ({" OR ".join(where_parts)})
        ORDER BY score DESC, s.episode_no NULLS LAST, s.scene_no
        LIMIT :n
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt, params).all()

    out: List[ScoredScene] = []
    for i, r in enumerate(rows):
        out.append(
            ScoredScene(
                scene_id=r.scene_id,
                script_id=r.script_id,
                episode_no=r.episode_no,
                scene_no=r.scene_no,
                scene_label=r.scene_label or "",
                text=r.text or "",
                score=float(r.score or 0.0),
                rank=i + 1,
                source="keyword",
            )
        )
    return out


# ============================================================
# 三级：LLM metadata 兜底
# ============================================================


_LLM_PICK_SYSTEM_PROMPT = (
    "你是剧本场景定位助手。用户给一个查询，你需要在「全剧场景元数据列表」里挑出最相关的几场。\n"
    "判定相关性时主要看：场景标题（scene_label，含时空：客厅/夜内/沈宅 等）、出场人物、场号编排。\n"
    "你看不到正文，所以只挑 metadata 上看起来匹配的；不要自己脑补剧情。\n"
    "严格输出 JSON：{\"scene_ids\": [\"<scene_id>\", ...], \"reason\": \"<一句话解释你为什么挑这几场>\"}"
)


async def _llm_pick_scenes(
    script_id: str,
    query: str,
    top_k: int,
    engine: Engine,
) -> List[ScoredScene]:
    """BM25 miss 时的兜底：把全剧 scene metadata 喂 LLM，让 LLM 挑 top_k。

    成本估算：2000 场 × 50 字 metadata ≈ 100KB ≈ 30K token；MINI 档 LLM 单次调用
    成本远低于 1500 次 embedding API call（即便后者只跑一次 ingestion）。
    """
    metas = _fetch_all_scene_metadata(script_id, engine=engine)
    if not metas:
        return []

    meta_lines = [
        f"- {m['scene_id']}|{m['scene_no']}|{m['scene_label']}|人物:{','.join(m['characters'][:5])}"
        for m in metas
    ]
    user_prompt = (
        f"用户查询：{query}\n\n"
        f"全剧场景元数据列表（共 {len(metas)} 场，每行：scene_id|scene_no|scene_label|人物）：\n"
        + "\n".join(meta_lines)
        + f"\n\n请挑出最相关的 {top_k} 场，按相关性排序。"
    )

    from service.script_tools.llm_caller import LlmCaller, ModelTier

    caller = LlmCaller()
    resp = await caller.call_json(
        prompt=user_prompt,
        tier=ModelTier.MINI,  # 轻任务，省 token
        system_message=_LLM_PICK_SYSTEM_PROMPT,
        max_tokens=512,
    )
    parsed = resp.parsed if hasattr(resp, "parsed") else None
    if not isinstance(parsed, dict):
        logger.warning("llm_metadata returned non-dict: %r", parsed)
        return []
    raw_ids = parsed.get("scene_ids") or []
    if not isinstance(raw_ids, list):
        return []

    valid_ids: dict[str, int] = {}  # scene_id → rank
    seen: set = set()
    for i, sid in enumerate(raw_ids):
        s = str(sid).strip()
        if s and s not in seen:
            seen.add(s)
            valid_ids[s] = i + 1
        if len(valid_ids) >= top_k:
            break

    if not valid_ids:
        return []

    rows = _fetch_scenes_by_ids(list(valid_ids.keys()), engine=engine)
    out: List[ScoredScene] = []
    for sid, rank in valid_ids.items():
        sc = rows.get(sid)
        if sc is None:
            continue
        out.append(
            ScoredScene(
                scene_id=sid,
                script_id=sc["script_id"],
                episode_no=sc["episode_no"],
                scene_no=sc["scene_no"],
                scene_label=sc["scene_label"] or "",
                text=sc["text"] or "",
                # llm_metadata 路径没有真实"分数"，用 1/rank 充当排序信号
                score=1.0 / rank,
                rank=rank,
                source="llm_metadata",
            )
        )
    return out


def _fetch_all_scene_metadata(script_id: str, *, engine: Engine) -> List[dict]:
    """拉全剧 scene metadata（不含 text，控制 token）。"""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS scene_id, episode_no, scene_no, scene_label, characters
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no
                """
            ),
            {"sid": script_id},
        ).mappings().all()
    return [
        {
            "scene_id": r["scene_id"],
            "episode_no": r["episode_no"],
            "scene_no": r["scene_no"] or "",
            "scene_label": r["scene_label"] or "",
            "characters": list(r["characters"] or []),
        }
        for r in rows
    ]


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
