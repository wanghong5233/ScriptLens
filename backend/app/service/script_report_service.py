"""ScriptLens 5 维评分报告生成服务（顶层流水线）。

链路：
  scripts 表读元数据
    → 并行跑 5 维评分
        ├─ opening_hook   (dimension_scorer.score_opening_hook)
        ├─ reward_density (dimension_scorer.score_reward_density 用 reward_events)
        ├─ motivation     (motivation_chain.score_motivation)
        ├─ pacing         (dimension_scorer.score_pacing 用 reward_events)
        └─ risk           (risk_screener.screen_risks)
    → 决策聚合（label / confidence / one_sentence_reason / must_read_scenes）
    → 拼装 PRD §7 schema
    → 落 reports / evidence_refs 表（事务）

为什么不走 ReAct：
  5 维评分是固定流水线，每步输入输出明确，没有"工具试错"诉求。
  ReAct 用在 D2-6 多轮 chat / 改写场景。

不变式：
  1. 任一维度评分异常向上抛错（mark_failed 由调用方处理）
  2. 写库走单事务：先写 reports，再写 evidence_refs；任一失败回滚
  3. 重新生成报告时（reanalyze）：先 DELETE 旧 reports + evidence_refs（CASCADE），再写新的
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.dimension_scorer import (
    ScoreOutput,
    score_opening_hook,
    score_pacing,
    score_reward_density,
)
from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError
from service.script_tools.motivation_chain import MotivationResult, score_motivation
from service.script_tools.reward_extractor import RewardEvent, extract_reward_events
from service.script_tools.risk_screener import RiskResult, screen_risks
from service.script_tools.scene_repo import extract_quote
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


_DIMENSIONS_FIVE = ("opening_hook", "reward_density", "motivation", "pacing", "risk")


@dataclass
class _ScriptMeta:
    script_id: str
    title: str
    total_episodes: int
    total_scenes: int
    user_id: int


@dataclass
class _DimScore:
    """统一形状（5 维通用），用于决策聚合 + 落库。

    rubric §6 失败模式：score / level 在「证据不足」时为 None，整个维度
    照样落库（用户能看见这一维"证据不足"），但不参与 overall_score 均值。
    """

    dimension: str
    score: Optional[int]  # 0-10 or None
    level: Optional[str]  # high|medium|low|high_risk|medium_risk|low_risk|clean or None
    reason: str
    evidence_scene_ids: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 强类型边界：score 必须是 int / None；level 必须是 str / None；reason 必须是 str
        # 任何上游（dimension_scorer / motivation_chain / risk_screener）若把 LLM 文本
        # 没强转就塞进来，这里立刻 fail aloud，比下游决策聚合时 f-string 报奇怪错好定位
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
                raise TypeError(
                    f"_DimScore.score 必须是 int|float|None，"
                    f"dimension={self.dimension} got {type(self.score).__name__}={self.score!r}"
                )
            self.score = int(self.score)
        if self.level is not None and not isinstance(self.level, str):
            raise TypeError(
                f"_DimScore.level 必须是 str|None，"
                f"dimension={self.dimension} got {type(self.level).__name__}={self.level!r}"
            )
        if not isinstance(self.reason, str):
            raise TypeError(
                f"_DimScore.reason 必须是 str，"
                f"dimension={self.dimension} got {type(self.reason).__name__}={self.reason!r}"
            )


# ============================================================
# 单维度入口（D2-5c ReAct `score_dimension_tool` 薄包装的目标）
# ============================================================


async def score_one_dimension(
    *,
    script_id: str,
    dimension: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> Dict:
    """跑单一维度评分（不入库），返回 dict。

    用于 ReAct `score_dimension_tool`：用户在 chat 里说"复核一下 motivation"或者
    "把 risk 重新打一遍"时，工具调本入口而不是整份 `generate_report`。

    Args:
        dimension: opening_hook | reward_density | motivation | pacing | risk

    Returns:
        ```
        {
            "dimension": "...",
            "score": 0-10,
            "level": "high|medium|low|high_risk|...",
            "reason": "...",
            "evidence_scene_ids": ["uuid", ...],   # 用 scene_id 而非 evidence_refs.id
        }
        ```

    Raises:
        ValueError: 未知 dimension / script_id 不存在
        ScoreLLMError: LLM 多次失败
    """
    if dimension not in _DIMENSIONS_FIVE:
        raise ValueError(
            f"unknown dimension: {dimension!r}; must be one of {_DIMENSIONS_FIVE}"
        )
    caller = caller or LlmCaller()
    meta = _load_script_meta(script_id, engine=engine)
    if meta is None:
        raise ValueError(f"script_id={script_id} 不存在")

    if dimension == "opening_hook":
        out = await score_opening_hook(script_id=script_id, caller=caller)
        ds = _to_dim("opening_hook", out)
    elif dimension == "reward_density":
        rev = await extract_reward_events(script_id=script_id, caller=caller)
        out = await score_reward_density(
            script_id=script_id,
            reward_events=rev,
            total_episodes=meta.total_episodes,
            caller=caller,
        )
        ds = _to_dim("reward_density", out)
    elif dimension == "pacing":
        rev = await extract_reward_events(script_id=script_id, caller=caller)
        out = await score_pacing(script_id=script_id, reward_events=rev, caller=caller)
        ds = _to_dim("pacing", out)
    elif dimension == "motivation":
        r = await score_motivation(script_id=script_id, caller=caller)
        ds = _to_dim_from_motivation(r)
    else:  # risk
        r = await screen_risks(script_id=script_id, caller=caller)
        ds = _to_dim_from_risk(r)

    return {
        "dimension": ds.dimension,
        "score": ds.score,
        "level": ds.level,
        "reason": ds.reason,
        "evidence_scene_ids": ds.evidence_scene_ids,
    }


# ============================================================
# 主入口
# ============================================================


async def generate_report(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> Dict:
    """主入口：跑完 5 维评分 + 落库 + 返回完整 report dict。

    返回 dict 直接对应 PRD §7 schema（schemas/script.ReportPayload）。
    """
    t0 = time.perf_counter()
    caller = caller or LlmCaller()

    meta = _load_script_meta(script_id, engine=engine)
    if meta is None:
        raise ValueError(f"script_id={script_id} 不存在")

    logger.info("report.start script_id=%s title=%s episodes=%s scenes=%s",
                meta.script_id, meta.title, meta.total_episodes, meta.total_scenes)

    # 1. 抽 reward 事件（reward_density / pacing 共享）
    reward_events = await extract_reward_events(script_id=script_id, caller=caller)

    # 2. 并行 5 维评分
    open_task = score_opening_hook(script_id=script_id, caller=caller)
    reward_task = score_reward_density(
        script_id=script_id,
        reward_events=reward_events,
        total_episodes=meta.total_episodes,
        caller=caller,
    )
    motiv_task = score_motivation(script_id=script_id, caller=caller)
    pacing_task = score_pacing(script_id=script_id, reward_events=reward_events, caller=caller)
    risk_task = screen_risks(script_id=script_id, caller=caller)

    open_r, reward_r, motiv_r, pacing_r, risk_r = await asyncio.gather(
        open_task, reward_task, motiv_task, pacing_task, risk_task
    )

    dim_scores: List[_DimScore] = [
        _to_dim("opening_hook", open_r),
        _to_dim("reward_density", reward_r),
        _to_dim_from_motivation(motiv_r),
        _to_dim("pacing", pacing_r),
        _to_dim_from_risk(risk_r),
    ]

    # 3. 决策聚合
    decision = await _aggregate_decision(meta, dim_scores, caller)
    must_read_scene_ids = _select_must_read(dim_scores, top_k=3)

    # 4. 拼装 evidence_refs（去重；从所有维度的 evidence_scene_ids 展开）
    evidence_refs_payload = _build_evidence_refs(dim_scores, engine=engine)
    scene_id_to_evi_id = {er["scene_id"]: er["id"] for er in evidence_refs_payload}

    # 5. scorecard 的 evidence_ref_ids 现在是 evidence_refs.id（不是 scene_id）
    scorecard_payload = []
    for ds in dim_scores:
        evi_ids = [scene_id_to_evi_id[sid] for sid in ds.evidence_scene_ids if sid in scene_id_to_evi_id]
        scorecard_payload.append({
            "dimension": ds.dimension,
            "score": ds.score,
            "level": ds.level,
            "reason": ds.reason,
            "evidence_ref_ids": evi_ids,
        })

    # must_read_scene_ids 也映射成 evidence_refs.id（保持 PRD §7 字段一致）
    must_read_evi_ids = [scene_id_to_evi_id[sid] for sid in must_read_scene_ids if sid in scene_id_to_evi_id]

    report_payload = {
        "script_id": meta.script_id,
        "title": meta.title,
        "decision": decision,
        "decision_reason": decision.get("one_sentence_reason", ""),
        "overall_score": _overall_score(dim_scores),
        "summary": decision.get("summary", ""),
        "must_read_scene_ids": must_read_evi_ids,
        "scorecard": scorecard_payload,
        "evidence_refs": evidence_refs_payload,
        "risk_flags": _collect_risk_flags(risk_r),
    }

    # 6. 写库（事务）
    _persist_report(meta.script_id, report_payload, evidence_refs_payload, engine=engine)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "report.done script_id=%s elapsed_ms=%s overall=%s decision=%s",
        meta.script_id, elapsed_ms, report_payload["overall_score"], decision.get("label"),
    )
    return report_payload


# ============================================================
# DB 读取
# ============================================================


def _load_script_meta(script_id: str, *, engine: Engine) -> Optional[_ScriptMeta]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id, user_id, title,
                       COALESCE(total_episodes, 0) AS total_episodes,
                       COALESCE(total_scenes, 0) AS total_scenes
                FROM scriptlens.scripts
                WHERE id = :sid
                """
            ),
            {"sid": script_id},
        ).first()
    if not row:
        return None
    return _ScriptMeta(
        script_id=row.id,
        title=row.title,
        total_episodes=row.total_episodes,
        total_scenes=row.total_scenes,
        user_id=row.user_id,
    )


# ============================================================
# 维度结果统一化
# ============================================================


def _to_dim(dimension: str, output: ScoreOutput) -> _DimScore:
    return _DimScore(
        dimension=dimension,
        score=output.score,
        level=output.level,
        reason=output.reason,
        evidence_scene_ids=list(output.evidence_ref_ids),
    )


def _to_dim_from_motivation(r: MotivationResult) -> _DimScore:
    return _DimScore(
        dimension="motivation",
        score=r.score,
        level=r.level,
        reason=r.reason,
        evidence_scene_ids=list(r.evidence_ref_ids),
    )


def _to_dim_from_risk(r: RiskResult) -> _DimScore:
    return _DimScore(
        dimension="risk",
        score=r.score,
        level=r.level,  # high_risk | medium_risk | low_risk | clean
        reason=r.reason,
        evidence_scene_ids=list(r.evidence_ref_ids),
    )


# ============================================================
# 决策聚合
# ============================================================


_DECISION_PROMPT = """你是中文短剧选品总监。下面是某剧的 5 维评分。
任务：基于这 5 维的分 / level / reason，给出**整体判断**：

| 标签 | 触发条件 |
|---|---|
| recommend_continue | 5 维平均 ≥7.5 且 risk 不为 high_risk |
| cautious_continue | 5 维平均 5-7.5 / 个别维度低分但 risk 可控 |
| not_recommended | 5 维平均 <5 / risk=high_risk / 多维同时低分 |

【5 维评分】
{scores_block}

【整体均分】{overall_score}
【风险等级】{risk_level}

输出 JSON：
{{
  "label": "recommend_continue|cautious_continue|not_recommended",
  "confidence": "high|medium|low",
  "one_sentence_reason": "<≤60 字，对选品 / 编剧 / 审核三类用户都有信息量>",
  "summary": "<3-5 句剧本概览，必须基于 5 维 reason，不准编造未提到的事件>"
}}"""


async def _aggregate_decision(
    meta: _ScriptMeta,
    dim_scores: List[_DimScore],
    caller: LlmCaller,
) -> Dict:
    overall = _overall_score(dim_scores)  # Optional[float]
    risk_dim = next((d for d in dim_scores if d.dimension == "risk"), None)
    risk_level = (risk_dim.level if risk_dim and risk_dim.level else "clean")

    # _overall_score 应当只返回 float 或 None；任何其他类型都是上游 bug，
    # 这里 fail aloud 把它打成可定位的错误而不是 f-string 抛 ValueError
    if overall is not None and not isinstance(overall, (int, float)):
        raise TypeError(
            f"_overall_score 返回值类型异常 type={type(overall).__name__} value={overall!r}；"
            f"dim_scores={[(d.dimension, type(d.score).__name__, d.score) for d in dim_scores]}"
        )
    if not isinstance(risk_level, str):
        raise TypeError(
            f"risk_level 类型异常 type={type(risk_level).__name__} value={risk_level!r}；"
            f"risk_dim={risk_dim!r}"
        )

    # 拼分项：证据不足维度显式标注
    score_lines = []
    for d in dim_scores:
        if d.score is None:
            score_lines.append(f"- {d.dimension}: 证据不足 — {d.reason}")
        else:
            score_lines.append(
                f"- {d.dimension}: {d.score}/10 ({d.level}) — {d.reason}"
            )
    scores_block = "\n".join(score_lines)

    overall_text = f"{overall:.1f}" if overall is not None else "证据不足（≥3 维无评分）"
    prompt = _DECISION_PROMPT.format(
        scores_block=scores_block,
        overall_score=overall_text,
        risk_level=risk_level,
    )
    # 决策聚合 LLM 失败 → 用规则法兜底（label/confidence 来自规则；reason 拼提示）
    # 这里**不是**评分 fail aloud，是"决策卡"层面的 graceful degradation：
    # rubric/PRD 没要求决策卡也必须 LLM，规则法在工业上完全合理（rubric §3 + §3.5 已经确定 5 维分数）
    parsed: Dict = {}
    try:
        resp = await caller.call_json(
            prompt, tier=ModelTier.PRIMARY, temperature=0.2, max_tokens=512
        )
        if isinstance(resp.parsed, dict):
            parsed = resp.parsed
    except ScoreLLMError as e:
        logger.warning("decision aggregation LLM failed, falling back to rule-based: %s", e)

    label = str(parsed.get("label") or _rule_label(overall, risk_level))
    if label not in ("recommend_continue", "cautious_continue", "not_recommended"):
        label = _rule_label(overall, risk_level)
    confidence = str(parsed.get("confidence") or _rule_confidence(dim_scores))
    if confidence not in ("high", "medium", "low"):
        confidence = _rule_confidence(dim_scores)

    fallback_reason = (
        f"5 维评分证据不足，风险等级 {risk_level}"
        if overall is None
        else f"5 维均分 {overall:.1f}，风险等级 {risk_level}"
    )
    one_sentence_reason = str(parsed.get("one_sentence_reason") or fallback_reason)[:120]
    summary = str(parsed.get("summary") or "（暂无概览）")[:600]

    return {
        "label": label,
        "confidence": confidence,
        "one_sentence_reason": one_sentence_reason,
        "summary": summary,
    }


def _rule_label(overall: Optional[float], risk_level: str) -> str:
    """规则兜底决策标签（决策卡 LLM 失败 / overall 缺失时使用）。"""
    if risk_level == "high_risk":
        return "not_recommended"
    if overall is None:
        # 评分证据不足，不能贸然 recommend；偏保守
        return "cautious_continue"
    if overall >= 7.5:
        return "recommend_continue"
    if overall >= 5.0:
        return "cautious_continue"
    return "not_recommended"


def _rule_confidence(dim_scores: List[_DimScore]) -> str:
    """有效维度（score 非 None）+ 含 evidence 的占比 → confidence 档位。"""
    valid = [d for d in dim_scores if d.score is not None]
    if not valid:
        return "low"
    has_evidence = sum(1 for d in valid if d.evidence_scene_ids)
    if len(valid) == len(dim_scores) and has_evidence == len(valid):
        return "high"
    if has_evidence >= 3:
        return "medium"
    return "low"


def _overall_score(dim_scores: List[_DimScore]) -> Optional[float]:
    """5 维均分。**只算有 score 的维度**；少于 3 维有 score 时返回 None（rubric §6）。"""
    valid: List[float] = []
    for d in dim_scores:
        if d.score is None:
            continue
        # 双保险：_DimScore.__post_init__ 已强转，但有些上游可能 bypass dataclass 路径
        if not isinstance(d.score, (int, float)) or isinstance(d.score, bool):
            logger.error(
                "_overall_score skip dim=%s score 类型异常: %s=%r",
                d.dimension,
                type(d.score).__name__,
                d.score,
            )
            continue
        valid.append(float(d.score))
    if len(valid) < 3:
        return None
    return round(sum(valid) / len(valid), 1)


def _collect_risk_flags(risk_r: RiskResult) -> List[str]:
    """从 risk hits 收集去重的 category 列表（限制 ≤ 8 个）。"""
    cats: List[str] = []
    seen: set[str] = set()
    for h in risk_r.hits or []:
        # high_risk / medium_risk 必须 LLM 确认；low_risk 直接采信
        if h.level != "low_risk" and not h.confirmed_by_llm:
            continue
        if h.category in seen:
            continue
        seen.add(h.category)
        cats.append(h.category)
        if len(cats) >= 8:
            break
    return cats


# ============================================================
# must_read_scenes
# ============================================================


def _select_must_read(dim_scores: List[_DimScore], top_k: int = 3) -> List[str]:
    """从所有维度的 evidence_scene_ids 里选前 K 个独立 scene。

    选择策略：每维的第一条优先（=该维"最关键"证据）；不够时补第二条。
    """
    out: List[str] = []
    seen: set[str] = set()
    # 先取每维第一条
    for d in dim_scores:
        if not d.evidence_scene_ids:
            continue
        sid = d.evidence_scene_ids[0]
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
            if len(out) >= top_k:
                return out
    # 再补每维第二条
    for d in dim_scores:
        for sid in d.evidence_scene_ids[1:]:
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
                if len(out) >= top_k:
                    return out
    return out


# ============================================================
# evidence_refs 展开
# ============================================================


def _build_evidence_refs(dim_scores: List[_DimScore], *, engine: Engine) -> List[Dict]:
    """收集所有维度提到的 scene_id，去重，逐个调 extract_quote 生成 evidence_ref。"""
    seen_scene_ids: List[str] = []
    seen: set[str] = set()
    scene_to_dimensions: Dict[str, List[str]] = {}
    for d in dim_scores:
        for sid in d.evidence_scene_ids:
            if sid not in seen:
                seen.add(sid)
                seen_scene_ids.append(sid)
            scene_to_dimensions.setdefault(sid, []).append(d.dimension)

    out: List[Dict] = []
    for sid in seen_scene_ids:
        q = extract_quote(scene_id=sid, engine=engine)
        if q is None:
            continue
        evi_id = str(uuid.uuid4())
        # reason 字段：记录哪些维度引用了这一场（让前端能反查"这条证据支撑哪些评分"）
        dims = sorted(set(scene_to_dimensions.get(sid, [])))
        out.append({
            "id": evi_id,
            "scene_id": sid,
            "scene_no": q.get("scene_no"),
            "scene_label": q.get("scene_label"),
            "start_line": q.get("start_line"),
            "end_line": q.get("end_line"),
            "quote": q.get("quote"),
            "reason": f"被 {', '.join(dims)} 维度引用",
            "confidence": "medium",
        })
    return out


# ============================================================
# 落库（事务 + reanalyze 覆盖语义）
# ============================================================


def _persist_report(
    script_id: str,
    report_payload: Dict,
    evidence_refs_payload: List[Dict],
    *,
    engine: Engine,
) -> None:
    """先 DELETE 旧 report（连带 evidence_refs CASCADE），再 INSERT 新的。

    单事务包裹，任一失败回滚。
    """
    report_id = str(uuid.uuid4())
    generated_at = datetime.utcnow()
    with engine.begin() as conn:
        # 旧 report 删除（含 evidence_refs CASCADE）
        conn.execute(
            text("DELETE FROM scriptlens.reports WHERE script_id = :sid"),
            {"sid": script_id},
        )
        # 新 report
        conn.execute(
            text(
                """
                INSERT INTO scriptlens.reports (id, script_id, report_json, generated_at)
                VALUES (:id, :sid, CAST(:payload AS jsonb), :ts)
                """
            ),
            {
                "id": report_id,
                "sid": script_id,
                "payload": json.dumps(report_payload, ensure_ascii=False),
                "ts": generated_at,
            },
        )
        # evidence_refs
        if evidence_refs_payload:
            rows = [
                {
                    "id": er["id"],
                    "report_id": report_id,
                    "scene_id": er["scene_id"],
                    "quote": er["quote"],
                    "reason": er["reason"],
                    "confidence": er.get("confidence", "medium"),
                }
                for er in evidence_refs_payload
            ]
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.evidence_refs
                        (id, report_id, scene_id, quote, reason, confidence)
                    VALUES
                        (:id, :report_id, :scene_id, :quote, :reason, :confidence)
                    """
                ),
                rows,
            )
    report_payload["report_id"] = report_id
    report_payload["generated_at"] = generated_at.isoformat()
    logger.info("report.persisted report_id=%s evidence=%s", report_id, len(evidence_refs_payload))
