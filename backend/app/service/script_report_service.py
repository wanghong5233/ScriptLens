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
from typing import Any, Awaitable, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_progress_tracker import tracker as progress_tracker
from service.script_tools.beat_chain import BeatSheet, extract_beat_sheet
from service.script_tools.character_graph_chain import CharacterGraph, extract_character_graph
from service.script_tools.coverage_chain import CoverageCard, extract_coverage_card
from service.script_tools.dimension_scorer import (
    ScoreOutput,
    score_opening_hook,
    score_pacing,
    score_reward_density,
)
from service.script_tools.evaluation_chain import build_evaluation_payload
from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError
from service.script_tools.motivation_chain import MotivationResult, score_motivation
from service.script_tools.pacing_aggregator import aggregate_pacing_curve
from service.script_tools.reward_extractor import RewardEvent, extract_reward_events
from service.script_tools.risk_screener import RiskResult, screen_risks
from service.script_tools.scene_repo import extract_quote, get_scene
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


_DIMENSION_LABEL_CN = {
    "opening_hook": "开场钩子",
    "reward_density": "爽点密度",
    "motivation": "动机自洽",
    "pacing": "节奏控制",
    "risk": "审核风险",
}


async def _optional_chain(name: str, coro: Awaitable[Any]) -> Any:
    """报告扩展链可降级为 null；只吞已知业务失败。"""
    try:
        return await coro
    except (ScoreLLMError, ValueError) as exc:
        logger.warning("%s failed and will be stored as null: %s", name, exc)
        return None


def _select_beat_anchor_scenes(beat_sheet: Optional[BeatSheet], *, top_k: int) -> List[str]:
    if beat_sheet is None:
        return []
    priority = {
        "opening": 0,
        "inciting": 1,
        "midpoint": 2,
        "climax": 3,
        "closing": 4,
        "twist": 5,
        "reward": 6,
    }
    beats = [
        beat
        for act in beat_sheet.acts
        for beat in act.beats
        if beat.anchor_scene_id
    ]
    beats.sort(key=lambda b: priority.get(b.type, 99))
    out: List[str] = []
    seen: set[str] = set()
    for beat in beats:
        if beat.anchor_scene_id in seen:
            continue
        seen.add(beat.anchor_scene_id)
        out.append(beat.anchor_scene_id)
        if len(out) >= top_k:
            break
    return out


async def generate_report(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> Dict:
    """主入口：跑完 5 维评分 + 落库 + 返回完整 report dict。

    返回 dict 直接对应 PRD §7 schema（schemas/script.ReportPayload）。

    可观测性：
      流水线在 6 个关键节点 publish 进度到 progress_tracker，前端通过
      `GET /api/scripts/{id}/progress` 轮询拿到当前阶段 / detail，渲染
      6 阶段时间轴。任何一阶段失败时 finalize(error=...) 让前端能看到红点。
    """
    t0 = time.perf_counter()
    caller = caller or LlmCaller()
    progress_tracker.start(script_id)

    try:
        # ---- 1. 加载剧本元数据 ----------------------------------------------
        progress_tracker.update_stage(script_id, "loading_meta", "running")
        meta = _load_script_meta(script_id, engine=engine)
        if meta is None:
            progress_tracker.update_stage(
                script_id, "loading_meta", "failed", detail="剧本不存在"
            )
            progress_tracker.finalize(script_id, error=f"script_id={script_id} 不存在")
            raise ValueError(f"script_id={script_id} 不存在")
        progress_tracker.update_stage(
            script_id,
            "loading_meta",
            "done",
            detail=f"《{meta.title}》· {meta.total_episodes} 集 / {meta.total_scenes} 场",
        )

        logger.info("report.start script_id=%s title=%s episodes=%s scenes=%s",
                    meta.script_id, meta.title, meta.total_episodes, meta.total_scenes)

        # ---- 2. 抽 reward 事件（reward_density / pacing 共享）---------------
        progress_tracker.update_stage(
            script_id,
            "extracting_rewards",
            "running",
            detail="LLM 通读全剧，识别反转 / 打脸 / 逆袭 / 觉醒事件…",
        )
        reward_events = await extract_reward_events(script_id=script_id, caller=caller)
        progress_tracker.update_stage(
            script_id,
            "extracting_rewards",
            "done",
            detail=f"识别到 {len(reward_events)} 个爽点事件",
        )

        # ---- 3. 并行 5 维评分 -----------------------------------------------
        progress_tracker.update_stage(
            script_id,
            "scoring_dimensions",
            "running",
            detail="并行评估 5 维：开场钩子 / 爽点密度 / 动机自洽 / 节奏控制 / 审核风险",
        )

        completed_dims: List[str] = []

        def _on_dim_done(dim_key: str) -> None:
            completed_dims.append(_DIMENSION_LABEL_CN.get(dim_key, dim_key))
            progress_tracker.update_detail(
                script_id,
                f"已完成 {len(completed_dims)}/5 维（{', '.join(completed_dims)}）",
            )

        async def _run_dim(dim_key: str, coro):
            try:
                return await coro
            finally:
                _on_dim_done(dim_key)

        open_r, reward_r, motiv_r, pacing_r, risk_r = await asyncio.gather(
            _run_dim("opening_hook", score_opening_hook(script_id=script_id, caller=caller)),
            _run_dim("reward_density", score_reward_density(
                script_id=script_id,
                reward_events=reward_events,
                total_episodes=meta.total_episodes,
                caller=caller,
            )),
            _run_dim("motivation", score_motivation(script_id=script_id, caller=caller)),
            _run_dim("pacing", score_pacing(
                script_id=script_id, reward_events=reward_events, caller=caller,
            )),
            _run_dim("risk", screen_risks(script_id=script_id, caller=caller)),
        )

        dim_scores: List[_DimScore] = [
            _to_dim("opening_hook", open_r),
            _to_dim("reward_density", reward_r),
            _to_dim_from_motivation(motiv_r),
            _to_dim("pacing", pacing_r),
            _to_dim_from_risk(risk_r),
        ]
        scored_count = sum(1 for d in dim_scores if d.score is not None)
        progress_tracker.update_stage(
            script_id,
            "scoring_dimensions",
            "done",
            detail=f"5 维完成：{scored_count} 维有评分 · {5 - scored_count} 维证据不足",
        )

        # ---- 4. 决策聚合 + 故事/人物/速览提炼（并行）-------------------------
        progress_tracker.update_stage(
            script_id,
            "aggregating_decision",
            "running",
            detail="LLM 综合 5 维评分生成决策卡 + 一句话理由…",
        )
        progress_tracker.update_stage(
            script_id,
            "extracting_narrative",
            "running",
            detail="生成速览卡、三幕节拍、人物关系图和节奏曲线…",
        )
        decision_task = _aggregate_decision(meta, dim_scores, caller)
        coverage_task = _optional_chain(
            "coverage_chain",
            extract_coverage_card(script_id=script_id, caller=caller, engine=engine),
        )
        beat_task = _optional_chain(
            "beat_chain",
            extract_beat_sheet(
                script_id=script_id, reward_events=reward_events, caller=caller, engine=engine,
            ),
        )
        character_graph_task = _optional_chain(
            "character_graph_chain",
            extract_character_graph(script_id=script_id, caller=caller, engine=engine),
        )
        decision, coverage_card, beat_sheet, character_graph = await asyncio.gather(
            decision_task,
            coverage_task,
            beat_task,
            character_graph_task,
        )
        pacing_curve_payload = aggregate_pacing_curve(
            script_id=script_id,
            reward_events=reward_events,
            engine=engine,
        )
        must_read_scene_ids = _select_beat_anchor_scenes(beat_sheet, top_k=3)
        progress_tracker.update_stage(
            script_id,
            "aggregating_decision",
            "done",
            detail=f"决策：{decision.get('label', '?')} · 置信度 {decision.get('confidence', '?')}",
        )
        progress_tracker.update_stage(
            script_id,
            "extracting_narrative",
            "done",
            detail=(
                f"速览{'已生成' if coverage_card else '降级'} · "
                f"节拍 {len(beat_sheet.acts) if beat_sheet else 0} 幕 · "
                f"人物 {len(character_graph.nodes) if character_graph else 0} 个"
            ),
        )

        # ---- 5. 拼装 evidence_refs ------------------------------------------
        progress_tracker.update_stage(
            script_id,
            "building_evidence",
            "running",
            detail="为每个评分挂载来源场次的原文 quote…",
        )
        evidence_refs_payload = _build_evidence_refs(
            dim_scores,
            extra_scene_ids=must_read_scene_ids,
            engine=engine,
        )
        await _attach_scene_summaries(evidence_refs_payload, caller=caller, engine=engine)
        scene_id_to_evi_id = {er["scene_id"]: er["id"] for er in evidence_refs_payload}

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
        must_read_evi_ids = [scene_id_to_evi_id[sid] for sid in must_read_scene_ids if sid in scene_id_to_evi_id]

        # task.md §三 把"主要看点 / 钩子 / 反转 / 爽点"作为头等公民展示给用户：
        # 这一段把 reward_events + opening_hook 首条 + 已确认的高/中风险 hit 合并成一份 highlights 清单
        opening_dim_score = next((d for d in dim_scores if d.dimension == "opening_hook"), None)
        highlights_payload = _build_highlights(
            reward_events=reward_events,
            opening_dim=opening_dim_score,
            risk_r=risk_r,
            evidence_refs_payload=evidence_refs_payload,
            engine=engine,
        )

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
            "highlights": highlights_payload,
            "coverage_card": coverage_card.to_dict() if coverage_card else None,
            "beat_sheet": beat_sheet.to_dict() if beat_sheet else None,
            "character_graph": character_graph.to_dict() if character_graph else None,
            "pacing_curve": pacing_curve_payload,
            "evaluation": build_evaluation_payload(
                scorecard=scorecard_payload,
                evidence_refs=evidence_refs_payload,
                risk_flags=_collect_risk_flags(risk_r),
            ),
            "risk_flags": _collect_risk_flags(risk_r),
        }
        progress_tracker.update_stage(
            script_id,
            "building_evidence",
            "done",
            detail=(
                f"已挂载 {len(evidence_refs_payload)} 条证据片段 · "
                f"提炼 {len(highlights_payload)} 个看点 / 风险节点"
            ),
        )

        # ---- 6. 写库（事务）-------------------------------------------------
        progress_tracker.update_stage(
            script_id,
            "persisting",
            "running",
            detail="DELETE 旧报告 → INSERT 新报告 + 证据片段（事务）",
        )
        _persist_report(meta.script_id, report_payload, evidence_refs_payload, engine=engine)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        progress_tracker.update_stage(
            script_id,
            "persisting",
            "done",
            detail=f"报告已落库 · 耗时 {elapsed_ms / 1000:.1f}s",
        )
        progress_tracker.finalize(script_id)

        logger.info(
            "report.done script_id=%s elapsed_ms=%s overall=%s decision=%s",
            meta.script_id, elapsed_ms, report_payload["overall_score"], decision.get("label"),
        )
        return report_payload
    except Exception as exc:
        # 把当前阶段标记 failed 并 finalize；前端时间轴上能看到具体哪一步红了
        progress_tracker.finalize(script_id, error=f"{type(exc).__name__}: {exc}")
        raise


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
}}

语言要求：
1. 面向剧本创作者、选品、审核人员，不要出现 reward、scene_no、方差、均值、比值、OOC 等工程词。
2. 如需提场次，写「第 X 集第 Y 场」，不要写「10-1」。
3. 结论要解释成创作/审核语言，例如「情绪回报不足」「中段缺少阶段性反转」「角色行为突兀」。"""


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


# ============================================================
# highlights：剧本叙事节点清单（task.md §三 头等公民）
# ============================================================


# RewardEvent.event_type → ReportHighlight.type 的映射
# （前端 enum 与 reward extractor 取值不完全一致，这里做归一化）
_REWARD_TO_HIGHLIGHT_TYPE: Dict[str, str] = {
    "face_slap": "face_slap",
    "reversal": "reversal",
    "revenge": "revenge",
    "romantic_progress": "cp_progress",
    "identity_reveal": "identity_reveal",
    "humiliate_villain": "villain_fall",
    "underdog_rise": "underdog_rise",
    "scheme_exposed": "scheme_exposed",
}


# RewardEvent.event_type → 一句话点题模板（用 evidence 文本截短填进去）
_REWARD_TYPE_HEADLINE = {
    "face_slap": "打脸 / 反转",
    "reversal": "命运反转",
    "revenge": "复仇 / 报应",
    "romantic_progress": "CP 进展",
    "identity_reveal": "身份揭露",
    "humiliate_villain": "反派败落",
    "underdog_rise": "逆袭爆发",
    "scheme_exposed": "阴谋败露",
}


def _trim_oneliner(s: str, max_len: int = 40) -> str:
    """看点 oneliner 长度限制：UI 行高有限，单行 40 字够用。"""
    s = (s or "").strip().replace("\n", " ")
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _build_highlights(
    *,
    reward_events: List[RewardEvent],
    opening_dim: Optional[_DimScore],
    risk_r: RiskResult,
    evidence_refs_payload: List[Dict],
    engine: Engine,
) -> List[Dict]:
    """合并 reward_events + opening_hook 首条证据 + risk hits 为统一的 highlights 清单。

    设计：
      - 同一 scene_id 在 highlights 中只出现一次（按优先级：reward > hook > risk）
      - 缺 scene_label / episode_no / start_line / end_line 时回查 extract_quote
      - oneliner 来源：
          reward → REWARD_TYPE_HEADLINE + evidence 截短拼接（"反转 · {evidence片段}"）
          hook   → "开场强冲突：{evidence片段}"
          risk   → "{category} · {matched_term}"
    """
    # 复用 evidence_refs 已经查好的 scene meta，避免重复查 DB
    meta_by_scene: Dict[str, Dict] = {}
    for er in evidence_refs_payload:
        meta_by_scene[er["scene_id"]] = er

    def _resolve_meta(scene_id: str) -> Optional[Dict]:
        cached = meta_by_scene.get(scene_id)
        if cached:
            return cached
        q = extract_quote(scene_id=scene_id, engine=engine)
        if q is None:
            return None
        meta_by_scene[scene_id] = q
        return q

    out: List[Dict] = []
    used_scenes: set[str] = set()

    # 1) reward_events（按 episode_no/scene_no 已排序）
    for ev in reward_events:
        if ev.scene_id in used_scenes:
            continue
        hl_type = _REWARD_TO_HIGHLIGHT_TYPE.get(ev.event_type)
        if hl_type is None:
            continue
        meta = _resolve_meta(ev.scene_id)
        if meta is None:
            continue
        headline = _REWARD_TYPE_HEADLINE.get(ev.event_type, "看点")
        oneliner = _trim_oneliner(f"{headline} · {ev.evidence}")
        out.append({
            "id": str(uuid.uuid4()),
            "type": hl_type,
            "scene_id": ev.scene_id,
            "episode_no": ev.episode_no if ev.episode_no is not None else meta.get("episode_no"),
            "scene_no": ev.scene_no or meta.get("scene_no"),
            "scene_label": meta.get("scene_label"),
            "start_line": meta.get("start_line"),
            "end_line": meta.get("end_line"),
            "oneliner": oneliner,
            "evidence": ev.evidence,
        })
        used_scenes.add(ev.scene_id)

    # 2) opening_hook 维度的首条证据 → type='hook'
    if opening_dim and opening_dim.evidence_scene_ids:
        first_sid = opening_dim.evidence_scene_ids[0]
        if first_sid not in used_scenes:
            meta = _resolve_meta(first_sid)
            if meta is not None:
                quote = (meta.get("quote") or "").strip()
                oneliner = _trim_oneliner(f"开场强冲突 · {quote}")
                out.append({
                    "id": str(uuid.uuid4()),
                    "type": "hook",
                    "scene_id": first_sid,
                    "episode_no": meta.get("episode_no"),
                    "scene_no": meta.get("scene_no"),
                    "scene_label": meta.get("scene_label"),
                    "start_line": meta.get("start_line"),
                    "end_line": meta.get("end_line"),
                    "oneliner": oneliner,
                    "evidence": quote or None,
                })
                used_scenes.add(first_sid)

    # 3) risk hits：confirmed_by_llm 且 level != low_risk 的高 / 中风险
    for h in risk_r.hits or []:
        if h.scene_id in used_scenes:
            continue
        if not h.confirmed_by_llm:
            continue
        if h.level not in ("high_risk", "medium_risk"):
            continue
        meta = _resolve_meta(h.scene_id)
        if meta is None:
            continue
        oneliner = _trim_oneliner(f"{h.category} · {h.matched_term}")
        out.append({
            "id": str(uuid.uuid4()),
            "type": "risk",
            "scene_id": h.scene_id,
            "episode_no": h.episode_no if h.episode_no is not None else meta.get("episode_no"),
            "scene_no": h.scene_no or meta.get("scene_no"),
            "scene_label": meta.get("scene_label"),
            "start_line": meta.get("start_line"),
            "end_line": meta.get("end_line"),
            "oneliner": oneliner,
            "evidence": h.excerpt,
        })
        used_scenes.add(h.scene_id)

    return out


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


def _build_evidence_refs(
    dim_scores: List[_DimScore],
    *,
    extra_scene_ids: Optional[List[str]] = None,
    engine: Engine,
) -> List[Dict]:
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
    for sid in extra_scene_ids or []:
        if sid not in seen:
            seen.add(sid)
            seen_scene_ids.append(sid)
        scene_to_dimensions.setdefault(sid, []).append("关键场景")

    out: List[Dict] = []
    for sid in seen_scene_ids:
        q = extract_quote(scene_id=sid, engine=engine)
        if q is None:
            continue
        evi_id = str(uuid.uuid4())
        # reason 字段：记录哪些维度 / 故事节拍引用了这一场
        dims = sorted(set(scene_to_dimensions.get(sid, [])))
        out.append({
            "id": evi_id,
            "scene_id": sid,
            "episode_no": q.get("episode_no"),
            "scene_no": q.get("scene_no"),
            "scene_label": q.get("scene_label"),
            "start_line": q.get("start_line"),
            "end_line": q.get("end_line"),
            "quote": q.get("quote"),
            "reason": f"被 {', '.join(dims)} 维度引用",
            "confidence": "medium",
        })
    return out


_SCENE_SUMMARY_PROMPT = """你是中文短剧剧本编辑。下面给你若干个完整场景文本。
任务：为每一场写一句「整场概述」，用于报告里的关键场景卡片。

要求：
1. 概述的是这一整场发生了什么，不要摘一句台词。
2. 说明这场的戏剧功能：冲突 / 反转 / 情绪释放 / 风险点 / 人物关系推进。
3. 每条 35-70 字，面向编剧、选品、审核人员，语气自然，不要工程术语。
4. 不要出现 scene_no、reward、方差、JSON 以外的解释。

【场景】
{scenes_block}

输出 JSON：
{{
  "summaries": [
    {{"scene_id": "<scene_id>", "summary": "<整场概述>"}}
  ]
}}"""


def _fallback_scene_summary(text_: str, max_len: int = 70) -> str:
    """LLM 摘要失败时的可读兜底：取前几行实质内容，避免只显示短 quote。"""
    cleaned: List[str] = []
    for raw in (text_ or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith(("人物", "出场", "场景", "地点", "时间")) and ("：" in s[:4] or ":" in s[:4]):
            continue
        cleaned.append(s)
        if len(" ".join(cleaned)) >= max_len:
            break
    summary = " ".join(cleaned).strip()
    if len(summary) > max_len:
        summary = summary[: max_len - 1] + "…"
    return summary


async def _attach_scene_summaries(
    evidence_refs_payload: List[Dict],
    *,
    caller: LlmCaller,
    engine: Engine,
) -> None:
    """给 evidence_refs_payload 原地补 `scene_summary`。

    关键场景卡片需要的是「整场发生了什么」，不是 evidence quote。quote 只用于行级高亮；
    scene_summary 才用于报告阅读。如果 LLM 摘要失败，退化为基于完整 scene.text 的可读截断。
    """
    if not evidence_refs_payload:
        return

    scenes = []
    for er in evidence_refs_payload[:8]:
        scene = get_scene(scene_id=er["scene_id"], engine=engine)
        if scene is None:
            continue
        text_ = (scene.text or "").strip()
        if len(text_) > 1200:
            text_ = text_[:1200] + "…"
        scenes.append((er["scene_id"], text_, _fallback_scene_summary(scene.text)))

    fallback_by_id = {sid: fallback for sid, _text, fallback in scenes}
    for er in evidence_refs_payload:
        er["scene_summary"] = fallback_by_id.get(er["scene_id"]) or er.get("quote")

    if not scenes:
        return

    blocks = [
        f"[scene_id={sid}]\n{text_}"
        for sid, text_, _fallback in scenes
    ]
    prompt = _SCENE_SUMMARY_PROMPT.format(scenes_block="\n\n---\n\n".join(blocks))

    try:
        resp = await caller.call_json(
            prompt,
            tier=ModelTier.MINI,
            temperature=0.2,
            max_tokens=1200,
        )
    except ScoreLLMError as exc:
        logger.warning("scene_summary LLM failed, fallback to extractive summaries: %s", exc)
        return

    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    raw_items = parsed.get("summaries", [])
    if not isinstance(raw_items, list):
        return

    summary_by_id: Dict[str, str] = {}
    allowed = {sid for sid, _text, _fallback in scenes}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("scene_id") or "").strip()
        if sid not in allowed:
            continue
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        if len(summary) > 90:
            summary = summary[:89] + "…"
        summary_by_id[sid] = summary

    for er in evidence_refs_payload:
        sid = er["scene_id"]
        if sid in summary_by_id:
            er["scene_summary"] = summary_by_id[sid]


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
