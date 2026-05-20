"""ScriptLens 阅文五力评分报告生成服务（顶层流水线，docs/08-evaluation-framework.md）。

链路：
  scripts 表读元数据
    → 阶段 A：并行跑基础信号（reward_events / coverage_card / beat_sheet /
              character_graph / motivation_decisions）
    → 阶段 B：基于阶段 A 信号并行跑五力评分 + 合规审核
        ├─ story      (dimension_scorer.score_story 用 beat_sheet + reward_events)
        ├─ character  (dimension_scorer.score_character 用 motivation + character_graph)
        ├─ concept    (dimension_scorer.score_concept 用 coverage_card + 头部场景扫描)
        ├─ emotion    (dimension_scorer.score_emotion 用 reward_events)
        ├─ pacing     (dimension_scorer.score_pacing 用 scenes + reward_events)
        └─ compliance (risk_screener.screen_risks，独立字段不进 scorecard)
    → 决策聚合（label / confidence / one_sentence_reason / must_read_scenes）
    → 拼装 ReportPayload（scorecard 五力 + compliance 独立 + must_read 来自 beat_sheet）
    → 落 reports / evidence_refs 表（事务）

不变式：
  1. compliance 不参与 overall_score；high_risk 时 decision label 强制 not_recommended
  2. 任一维度规则评分需要的上游信号缺失 → 该维 score=None / level=None / reason 写明缺什么
  3. 写库走单事务：先写 reports，再写 evidence_refs；任一失败回滚
  4. 重新生成报告时（reanalyze）：先 DELETE 旧 reports + evidence_refs（CASCADE），再写新的
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
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
    STORY_KEY_BEATS,
    STORY_TWIST_EVENT_TYPES,
    STORY_TWIST_PER_EP_HIGH,
    STORY_TWIST_PER_EP_MID_HIGH,
    STORY_TWIST_PER_EP_MID_LOW,
    score_character,
    score_concept,
    score_emotion,
    score_pacing,
    score_story,
)
from service.script_tools.evaluation_chain import build_evaluation_payload
from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from service.script_tools.motivation_chain import score_motivation
from service.script_tools.pacing_aggregator import aggregate_pacing_curve
from service.script_tools.reward_extractor import RewardEvent, extract_reward_events
from service.script_tools.risk_screener import RiskHit, RiskResult, screen_risks
from service.script_tools.scene_repo import (
    EVIDENCE_QUOTE_MAX_LEN,
    LLM_EVIDENCE_MAX_LEN,
    SCENE_SUMMARY_MAX_LEN,
    extract_quote,
    get_scene,
)
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


# 阅文五力（docs/08-evaluation-framework.md §3）；compliance 独立不在此元组
_DIMENSIONS_FIVE = ("story", "character", "concept", "emotion", "pacing")


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

    用于 ReAct `score_dimension_tool`：用户在 chat 里说"复核一下故事力"或者
    "把合规审核重新跑一遍"时，工具调本入口而不是整份 `generate_report`。

    Args:
        dimension: story | character | concept | emotion | pacing | compliance

    Returns:
        ```
        {
            "dimension": "...",
            "score": 0-10,
            "level": "high|medium|low|high_risk|medium_risk|low_risk|clean",
            "reason": "...",
            "evidence_scene_ids": ["uuid", ...],
        }
        ```

    Raises:
        ValueError: 未知 dimension / script_id 不存在
        ScoreLLMError: LLM 多次失败
    """
    valid = (*_DIMENSIONS_FIVE, "compliance")
    if dimension not in valid:
        raise ValueError(f"unknown dimension: {dimension!r}; must be one of {valid}")
    caller = caller or LlmCaller()
    meta = _load_script_meta(script_id, engine=engine)
    if meta is None:
        raise ValueError(f"script_id={script_id} 不存在")

    # 读取 latest report 的该维度基线分，供前端/Agent 做稳定的 before-vs-after 对照。
    # 注意：基线只是最近一次报告快照，不代表“改写前固定版本”；对话层应结合操作时间解释。
    baseline_score: Optional[int] = None
    baseline_level: Optional[str] = None
    baseline_reason: Optional[str] = None
    baseline_evidence_scene_ids: List[str] = []
    with engine.connect() as conn:
        baseline_row = conn.execute(
            text(
                """
                SELECT report_json
                FROM scriptlens.reports
                WHERE script_id = :sid
                ORDER BY generated_at DESC
                LIMIT 1
                """
            ),
            {"sid": script_id},
        ).mappings().first()
    if baseline_row is not None:
        payload = baseline_row.get("report_json")
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        if isinstance(payload, dict):
            scorecard = payload.get("scorecard")
            if isinstance(scorecard, list):
                for item in scorecard:
                    if not isinstance(item, dict):
                        continue
                    item_dim = str(item.get("dimension") or "").strip()
                    if item_dim != dimension:
                        continue
                    raw_score = item.get("score")
                    if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
                        baseline_score = int(raw_score)
                    raw_level = item.get("level")
                    if isinstance(raw_level, str):
                        baseline_level = raw_level
                    raw_reason = item.get("reason")
                    if isinstance(raw_reason, str):
                        baseline_reason = raw_reason
                    raw_refs = item.get("evidence_ref_ids")
                    if isinstance(raw_refs, list):
                        baseline_ref_ids = {str(ref) for ref in raw_refs if str(ref).strip()}
                        evidence_refs = payload.get("evidence_refs")
                        if isinstance(evidence_refs, list):
                            baseline_evidence_scene_ids = [
                                str(ref.get("scene_id"))
                                for ref in evidence_refs
                                if isinstance(ref, dict)
                                and str(ref.get("id") or "") in baseline_ref_ids
                                and str(ref.get("scene_id") or "").strip()
                            ]
                    break

    if dimension == "compliance":
        r = await screen_risks(script_id=script_id, caller=caller)
        ds = _to_dim_from_risk(r)
        return {
            "dimension": "compliance",
            "score": ds.score,
            "level": ds.level,
            "reason": ds.reason,
            "evidence_scene_ids": ds.evidence_scene_ids,
            "baseline": {
                "score": baseline_score,
                "level": baseline_level,
                "reason": baseline_reason,
                "evidence_scene_ids": baseline_evidence_scene_ids,
            },
        }

    # 五力评分需要先跑上游基础信号，按 dimension 按需触发
    reward_events = await extract_reward_events(script_id=script_id, caller=caller)
    story_diagnostics: Optional[Dict[str, Any]] = None

    if dimension == "story":
        beat_sheet = await extract_beat_sheet(
            script_id=script_id, reward_events=reward_events, caller=caller, engine=engine,
        )
        out = score_story(
            beat_sheet=beat_sheet,
            reward_events=reward_events,
            total_episodes=meta.total_episodes,
        )
        story_diagnostics = _build_story_diagnostics(
            beat_sheet=beat_sheet,
            reward_events=reward_events,
            total_episodes=meta.total_episodes,
            score=out.score,
        )
    elif dimension == "character":
        motiv, cgraph = await asyncio.gather(
            score_motivation(script_id=script_id, caller=caller),
            extract_character_graph(script_id=script_id, caller=caller, engine=engine),
        )
        out = score_character(motivation_result=motiv, character_graph=cgraph)
    elif dimension == "concept":
        coverage = await extract_coverage_card(
            script_id=script_id, caller=caller, engine=engine,
        )
        out = score_concept(coverage_card=coverage, script_id=script_id, engine=engine)
    elif dimension == "emotion":
        out = score_emotion(
            reward_events=reward_events,
            total_episodes=meta.total_episodes,
        )
    else:  # pacing
        out = score_pacing(
            script_id=script_id, reward_events=reward_events, engine=engine,
        )

    ds = _to_dim(dimension, out)
    result = {
        "dimension": ds.dimension,
        "score": ds.score,
        "level": ds.level,
        "reason": ds.reason,
        "evidence_scene_ids": ds.evidence_scene_ids,
        "baseline": {
            "score": baseline_score,
            "level": baseline_level,
            "reason": baseline_reason,
            "evidence_scene_ids": baseline_evidence_scene_ids,
        },
    }
    if story_diagnostics is not None:
        result["diagnostics"] = story_diagnostics
    return result


def _build_story_diagnostics(
    *,
    beat_sheet: Optional[BeatSheet],
    reward_events: List[RewardEvent],
    total_episodes: int,
    score: Optional[int],
) -> Dict[str, Any]:
    """给 story 评分返回结构化诊断，便于解释“为什么没涨分”。"""
    episodes = int(total_episodes or 0)
    if episodes <= 0:
        episodes = len({ev.episode_no for ev in reward_events if ev.episode_no is not None}) or 1
    episodes = max(1, episodes)

    present_beats: set[str] = set()
    if beat_sheet is not None:
        for act in beat_sheet.acts:
            for beat in act.beats:
                if beat.type in STORY_KEY_BEATS and beat.anchor_scene_id:
                    present_beats.add(beat.type)
    missing_beats = [b for b in STORY_KEY_BEATS if b not in present_beats]

    twist_count = sum(1 for ev in reward_events if ev.event_type in STORY_TWIST_EVENT_TYPES)
    twist_per_ep = twist_count / episodes

    if score is None or score < 4:
        target_band = "medium"
        target_ratio = STORY_TWIST_PER_EP_MID_LOW
    elif score < 7:
        target_band = "high"
        target_ratio = STORY_TWIST_PER_EP_MID_HIGH
    else:
        target_band = "top"
        target_ratio = STORY_TWIST_PER_EP_HIGH
    required_twists = int(math.ceil(target_ratio * episodes))
    missing_twists = max(0, required_twists - twist_count)

    return {
        "version": "story_v1",
        "total_episodes": episodes,
        "twist_count": twist_count,
        "twist_per_episode": round(twist_per_ep, 4),
        "present_beats": sorted(present_beats),
        "missing_beats": missing_beats,
        "next_band": {
            "target": target_band,
            "twist_per_episode_threshold": target_ratio,
            "required_twist_count": required_twists,
            "missing_twists": missing_twists,
        },
        "estimated_min_rewrite_scenes_for_next_band": missing_twists + len(missing_beats),
    }


# ============================================================
# 主入口
# ============================================================


async def _optional_chain(name: str, coro: Awaitable[Any]) -> Any:
    """报告扩展链可降级为 null；只吞已知业务失败。"""
    try:
        return await coro
    except (ScoreLLMError, ValueError) as exc:
        # 用 exception 而不是 warning：LLM 截断 / JSON 解析失败这类问题
        # 非常依赖 traceback 才能定位（之前是静默 warning，用户看不到根因）
        logger.exception("%s failed and will be stored as null: %s", name, exc)
        return None


def _select_beat_anchor_scenes(beat_sheet: Optional[BeatSheet], *, top_k: int) -> List[str]:
    """从 beat_sheet 选 top_k 个最值得用户先看的场。

    业内对照（短剧选品 / 影视投资 deck）：
        - 抖音文心剧本助手 / 快手 StreamLake：钩子 + 反转 + 爽点（reward / twist / climax）
        - 阅文 IP 评级：转折点 + 高密度爽点
        - Final Draft "Story Highlights" / "Hook-Twist-Reward" pitch 模板：reward / twist / climax 优先

    与 task.md §三-1 列点完全对齐：「主要看点 / 钩子 / 反转 / 爽点在哪里」。
    旧版 priority 取 opening / inciting / midpoint = 戏剧理论意义上的开场结构场，
    不是用户决策需要的"爆点"——属于"看了 30 秒不知道值不值得继续读"的反向选场。
    """
    if beat_sheet is None:
        return []
    # 顺序：爽点 > 反转 > 高潮 > 开场钩子 > 激励 > 中点过渡 > 收束
    # 短剧用户的判断顺序 = "有没有爽 → 有没有反转 → 有没有高潮 → 钩不钩人"
    priority = {
        "reward": 0,
        "twist": 1,
        "climax": 2,
        "opening": 3,
        "inciting": 4,
        "midpoint": 5,
        "closing": 6,
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

        # ---- 2. 抽 reward 事件（emotion / pacing / story 共享）---------------
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

        # ---- 3. 阶段 A：并行抽基础信号（速览 / 节拍 / 人物图 / 动机决策 / 合规）----
        progress_tracker.update_stage(
            script_id,
            "extracting_narrative",
            "running",
            detail="生成速览卡、三幕节拍、人物关系图、动机决策、合规审核…",
        )
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
        motivation_task = _optional_chain(
            "motivation_chain",
            score_motivation(script_id=script_id, caller=caller),
        )
        risk_task = screen_risks(script_id=script_id, caller=caller)
        coverage_card, beat_sheet, character_graph, motivation_result, risk_r = (
            await asyncio.gather(
                coverage_task,
                beat_task,
                character_graph_task,
                motivation_task,
                risk_task,
            )
        )
        progress_tracker.update_stage(
            script_id,
            "extracting_narrative",
            "done",
            detail=(
                f"速览{'已生成' if coverage_card else '降级'} · "
                f"节拍 {len(beat_sheet.acts) if beat_sheet else 0} 幕 · "
                f"人物 {len(character_graph.nodes) if character_graph else 0} 个 · "
                f"决策回扫 {len(getattr(motivation_result, 'judged_decisions', None) or [])} 处 · "
                f"合规 {risk_r.level}"
            ),
        )

        # ---- 4. 阶段 B：基于阶段 A 信号并行跑五力评分 ----------------------
        progress_tracker.update_stage(
            script_id,
            "scoring_dimensions",
            "running",
            detail="阅文五力并行评估：故事力 / 人物力 / 题材力 / 情感力 / 叙事力",
        )

        story_r = score_story(
            beat_sheet=beat_sheet,
            reward_events=reward_events,
            total_episodes=meta.total_episodes,
        )
        character_r = score_character(
            motivation_result=motivation_result,
            character_graph=character_graph,
        )
        concept_r = score_concept(
            coverage_card=coverage_card, script_id=script_id, engine=engine,
        )
        emotion_r = score_emotion(
            reward_events=reward_events, total_episodes=meta.total_episodes,
        )
        pacing_r = score_pacing(
            script_id=script_id, reward_events=reward_events, engine=engine,
        )

        dim_scores: List[_DimScore] = [
            _to_dim("story", story_r),
            _to_dim("character", character_r),
            _to_dim("concept", concept_r),
            _to_dim("emotion", emotion_r),
            _to_dim("pacing", pacing_r),
        ]
        scored_count = sum(1 for d in dim_scores if d.score is not None)
        progress_tracker.update_stage(
            script_id,
            "scoring_dimensions",
            "done",
            detail=f"五力完成：{scored_count} 维有评分 · {5 - scored_count} 维证据不足",
        )

        # ---- 5. 决策聚合 + 节奏曲线 ---------------------------------------
        progress_tracker.update_stage(
            script_id,
            "aggregating_decision",
            "running",
            detail="LLM 综合五力评分生成决策卡 + 一句话理由…",
        )
        compliance_dim = _to_dim_from_risk(risk_r)
        decision = await _aggregate_decision(
            meta, dim_scores, compliance_dim=compliance_dim, caller=caller,
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

        # ---- 5. 拼装 evidence_refs ------------------------------------------
        progress_tracker.update_stage(
            script_id,
            "building_evidence",
            "running",
            detail="为每个评分挂载来源场次的原文 quote…",
        )
        # v3.3 line-range anchored citation：所有跳转锚点都是 (scene_id, line_range) 双锚定。
        # - coverage strengths/concerns：前端用 point.evidence_line_range，**不**走 evidence_refs
        # - 评估卡 chip / 三大看点：用 evidence_refs.start_line/end_line（来自 reward / risk LLM 同次输出）
        # - 主要看点 highlights：派生时用 reward / risk 的 evidence_line_range
        reward_lookup: Dict[str, RewardEvent] = {ev.scene_id: ev for ev in reward_events}
        risk_lookup: Dict[str, RiskHit] = {}
        for hit in risk_r.hits or []:
            if not hit.scene_id:
                continue
            if not (hit.excerpt or "").strip():
                continue
            if hit.level != "low_risk" and not hit.confirmed_by_llm:
                continue
            risk_lookup.setdefault(hit.scene_id, hit)

        evidence_refs_payload = _build_evidence_refs(
            dim_scores + [compliance_dim],
            extra_scene_ids=must_read_scene_ids,
            reward_lookup=reward_lookup,
            risk_lookup=risk_lookup,
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
        compliance_payload = {
            "dimension": compliance_dim.dimension,
            "score": compliance_dim.score,
            "level": compliance_dim.level,
            "reason": compliance_dim.reason,
            "evidence_ref_ids": [
                scene_id_to_evi_id[sid]
                for sid in compliance_dim.evidence_scene_ids
                if sid in scene_id_to_evi_id
            ],
        }
        must_read_evi_ids = [scene_id_to_evi_id[sid] for sid in must_read_scene_ids if sid in scene_id_to_evi_id]

        # task.md §三 把"主要看点 / 钩子 / 反转 / 爽点"作为头等公民展示给用户：
        # 这一段把 reward_events + 节拍开场锚点 + 已确认的高/中风险 hit 合并成一份 highlights 清单
        opening_anchor_id = _opening_anchor_scene_id(beat_sheet)
        highlights_payload = _build_highlights(
            reward_events=reward_events,
            opening_anchor_scene_id=opening_anchor_id,
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
            "compliance": compliance_payload,
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


def _to_dim_from_risk(r: RiskResult) -> _DimScore:
    """合规审核独立成 _DimScore 形状，便于复用 evidence 反查；落库到 ReportPayload.compliance。"""
    return _DimScore(
        dimension="compliance",
        score=r.score,
        level=r.level,  # high_risk | medium_risk | low_risk | clean
        reason=r.reason,
        evidence_scene_ids=list(r.evidence_ref_ids),
    )


# ============================================================
# 决策聚合
# ============================================================


# 决策阈值常量。**整个文件只能从这里取数**，不许在 prompt / 规则兜底两处分别写。
# 业内对照（Coverfly / Save the Cat workbook）：>=7.5 推荐立项，5-7.5 谨慎，<5 不立项。
# 高风险硬约束 = `not_recommended`，独立于均分（rubric §4 + §5 已声明）。
DECISION_THRESHOLDS = {
    "recommend": 7.5,
    "cautious": 5.0,
}

_DECISION_PROMPT = """你是中文短剧选品总监。下面是某剧的阅文五力评分 + 合规审核结果。
任务：基于五力 + 合规给出**整体判断**：

| 标签 | 触发条件 |
|---|---|
| recommend_continue | 五力平均 ≥{recommend_threshold} 且 合规不为 high_risk |
| cautious_continue | 五力平均 {cautious_threshold}-{recommend_threshold} / 个别维度低分但合规可控 |
| not_recommended | 五力平均 <{cautious_threshold} / 合规=high_risk / 多维同时低分 |

【五力评分】
{scores_block}

【整体均分（五力等权）】{overall_score}
【合规等级】{risk_level}

输出 JSON：
{{
  "label": "recommend_continue|cautious_continue|not_recommended",
  "confidence": "high|medium|low",
  "one_sentence_reason": "<≤60 字，对选品 / 编剧 / 审核三类用户都有信息量>",
  "summary": "<3-5 句剧本概览，必须基于五力 reason，不准编造未提到的事件>"
}}

语言要求：
1. 面向剧本创作者、选品、审核人员，不要出现 reward、scene_no、方差、均值、比值、OOC 等工程词。
2. 如需提场次，写「第 X 集第 Y 场」，不要写「10-1」。
3. 结论要解释成创作/审核语言，例如「情感回报不足」「中段缺少阶段性反转」「角色行为突兀」。"""


async def _aggregate_decision(
    meta: _ScriptMeta,
    dim_scores: List[_DimScore],
    *,
    compliance_dim: _DimScore,
    caller: LlmCaller,
) -> Dict:
    overall = _overall_score(dim_scores)
    risk_level = compliance_dim.level if compliance_dim.level else "clean"

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
        recommend_threshold=DECISION_THRESHOLDS["recommend"],
        cautious_threshold=DECISION_THRESHOLDS["cautious"],
    )
    # 决策聚合 LLM 失败 → 用规则法兜底（label/confidence 来自规则；reason 拼提示）
    # 这里**不是**评分 fail aloud，是"决策卡"层面的 graceful degradation：
    # rubric/PRD 没要求决策卡也必须 LLM，规则法在工业上完全合理（rubric §3 + §3.5 已经确定 5 维分数）
    parsed: Dict = {}
    try:
        resp = await caller.call_json(
            prompt, tier=ModelTier.PRIMARY, temperature=0.2,
            max_tokens=TokenBudget.DECISION_AGGREGATE,
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
    """规则兜底决策标签（决策卡 LLM 失败 / overall 缺失时使用）。

    阈值与 _DECISION_PROMPT 同源 = `DECISION_THRESHOLDS`。
    """
    if risk_level == "high_risk":
        return "not_recommended"
    if overall is None:
        # 评分证据不足，不能贸然 recommend；偏保守
        return "cautious_continue"
    if overall >= DECISION_THRESHOLDS["recommend"]:
        return "recommend_continue"
    if overall >= DECISION_THRESHOLDS["cautious"]:
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


def _opening_anchor_scene_id(beat_sheet: Optional[BeatSheet]) -> Optional[str]:
    """从 beat_sheet 找开场节拍的 anchor_scene_id，给 highlights 'hook' 用。"""
    if beat_sheet is None:
        return None
    for act in beat_sheet.acts:
        for beat in act.beats:
            if beat.type == "opening" and beat.anchor_scene_id:
                return beat.anchor_scene_id
    return None


def _build_highlights(
    *,
    reward_events: List[RewardEvent],
    opening_anchor_scene_id: Optional[str],
    risk_r: RiskResult,
    evidence_refs_payload: List[Dict],
    engine: Engine,
) -> List[Dict]:
    """合并 reward_events + 开场节拍 + risk hits 为统一的 highlights 清单。

    设计：
      - 同一 scene_id 在 highlights 中只出现一次（按优先级：reward > hook > risk）
      - 缺 scene_label / episode_no / start_line / end_line 时回查 extract_quote
      - oneliner 来源：
          reward → REWARD_TYPE_HEADLINE + evidence 截短拼接（"反转 · {evidence片段}"）
          hook   → "开场抓人 · {evidence片段}"（来自 beat_sheet opening 节拍）
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
    # v3.3：start_line/end_line 优先用 LLM 给的 ev.evidence_line_range；否则退回 meta 行号
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
        line_range = ev.evidence_line_range
        out.append({
            "id": str(uuid.uuid4()),
            "type": hl_type,
            "scene_id": ev.scene_id,
            "episode_no": ev.episode_no if ev.episode_no is not None else meta.get("episode_no"),
            "scene_no": ev.scene_no or meta.get("scene_no"),
            "scene_label": meta.get("scene_label"),
            "start_line": line_range[0] if line_range else meta.get("start_line"),
            "end_line": line_range[1] if line_range else meta.get("end_line"),
            "oneliner": oneliner,
            "evidence": ev.evidence,
        })
        used_scenes.add(ev.scene_id)

    # 2) 开场节拍 anchor_scene_id → type='hook'
    if opening_anchor_scene_id and opening_anchor_scene_id not in used_scenes:
        meta = _resolve_meta(opening_anchor_scene_id)
        if meta is not None:
            quote = (meta.get("quote") or "").strip()
            oneliner = _trim_oneliner(f"开场抓人 · {quote}")
            out.append({
                "id": str(uuid.uuid4()),
                "type": "hook",
                "scene_id": opening_anchor_scene_id,
                "episode_no": meta.get("episode_no"),
                "scene_no": meta.get("scene_no"),
                "scene_label": meta.get("scene_label"),
                "start_line": meta.get("start_line"),
                "end_line": meta.get("end_line"),
                "oneliner": oneliner,
                "evidence": quote or None,
            })
            used_scenes.add(opening_anchor_scene_id)

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
        line_range = h.evidence_line_range
        out.append({
            "id": str(uuid.uuid4()),
            "type": "risk",
            "scene_id": h.scene_id,
            "episode_no": h.episode_no if h.episode_no is not None else meta.get("episode_no"),
            "scene_no": h.scene_no or meta.get("scene_no"),
            "scene_label": meta.get("scene_label"),
            "start_line": line_range[0] if line_range else meta.get("start_line"),
            "end_line": line_range[1] if line_range else meta.get("end_line"),
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
    reward_lookup: Optional[Dict[str, RewardEvent]] = None,
    risk_lookup: Optional[Dict[str, "RiskHit"]] = None,
    engine: Engine,
) -> List[Dict]:
    """收集所有维度提到的 scene_id，去重，按语义优先级生成 evidence_ref。

    v3.3 line-range anchored citation：
    - start_line / end_line **直接用 LLM 同次给的 evidence_line_range**，不再字符匹配反推
    - quote 字段仅作 tooltip 展示文本

    优先级（每条 evidence_ref 的锚点 + 展示文本）：
    1. 该场是 reward 命中 → (line_range, quote) = (reward.evidence_line_range, reward.evidence)
    2. 该场是 risk  命中 → (line_range, quote) = (risk.evidence_line_range,   risk.excerpt)
    3. 都不是 → fallback extract_quote（场内首条非空行的行号 + 文本，仅占位）

    业内对照：GitHub PR review hunk / Cursor codebase index / NotebookLM citation
    都是 (container_id, line_range) 双锚定，quote 字符串只做展示，从不参与定位。
    """
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

    reward_lookup = reward_lookup or {}
    risk_lookup = risk_lookup or {}

    out: List[Dict] = []
    for sid in seen_scene_ids:
        q = extract_quote(scene_id=sid, engine=engine)
        if q is None:
            continue

        final_quote: str = q.get("quote") or ""
        final_start: Optional[int] = q.get("start_line")
        final_end: Optional[int] = q.get("end_line")
        quote_source: str = "fallback_first_line"

        if sid in reward_lookup:
            ev = reward_lookup[sid]
            display_quote = ev.evidence or final_quote
            if len(display_quote) > EVIDENCE_QUOTE_MAX_LEN:
                display_quote = display_quote[: EVIDENCE_QUOTE_MAX_LEN - 1] + "…"
            final_quote = display_quote
            quote_source = f"reward:{ev.event_type}"
            if ev.evidence_line_range is not None:
                final_start, final_end = ev.evidence_line_range
            else:
                logger.warning(
                    "evidence_refs.line_range_missing scene_id=%s source=%s",
                    sid, quote_source,
                )
        elif sid in risk_lookup:
            hit = risk_lookup[sid]
            display_quote = hit.excerpt or final_quote
            if len(display_quote) > EVIDENCE_QUOTE_MAX_LEN:
                display_quote = display_quote[: EVIDENCE_QUOTE_MAX_LEN - 1] + "…"
            final_quote = display_quote
            quote_source = "risk_hit"
            if hit.evidence_line_range is not None:
                final_start, final_end = hit.evidence_line_range
            else:
                logger.warning(
                    "evidence_refs.line_range_missing scene_id=%s source=risk_hit",
                    sid,
                )

        evi_id = str(uuid.uuid4())
        dims = sorted(set(scene_to_dimensions.get(sid, [])))
        out.append({
            "id": evi_id,
            "scene_id": sid,
            "episode_no": q.get("episode_no"),
            "scene_no": q.get("scene_no"),
            "scene_label": q.get("scene_label"),
            "start_line": final_start,
            "end_line": final_end,
            "quote": final_quote,
            "quote_source": quote_source,
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


def _fallback_scene_summary(text_: str, max_len: int = SCENE_SUMMARY_MAX_LEN) -> str:
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
            max_tokens=TokenBudget.SCENE_SUMMARY,
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
