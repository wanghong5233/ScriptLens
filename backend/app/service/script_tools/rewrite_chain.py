"""全剧维度改写引擎（docs/10-rewrite-agent.md）。

三个纯函数（不写库，调用方负责持久化）：

  1. select_target_scenes(script_id, dimensions)
        基于 latest report 低分维度证据 + story 信号缺口（节拍/反转密度）
        选出待改场，按 dimension union 去重 → 形成全剧 plan 候选场列表。

  2. propose_plan(script_id, dimensions, scenes, ...) -> RewritePlan
        单次 LLM 调用产 plan tree：每场标 target_dimensions / rationale /
        expected_changes，**不出改写后文本**（让用户先审 plan）。

  3. execute_plan_step(scene_id, target_dimensions, ...) -> RewriteResult
        单场 LLM 改写：复用 ProposeRewriteTool 那条久经检验的 prompt（前情后续场
        摘要 + 人物表 + 整剧概要），但维度参数改为 list 支持「一场为多维度同时改」。

业内对照（docs/10 §3）：Cursor Composer / Copilot Workspace / 抖音文心剧本助手
全部走 Plan-then-Execute；本文件只承担「文本生成」，调用方（rewrite_scene_tool /
兼容别名 propose_dimension_rewrite_tool）负责把改写结果落到 scriptlens.scenes.text
并更新 state.modified_files。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.dimension_scorer import (
    STORY_KEY_BEATS,
    STORY_TWIST_EVENT_TYPES,
    STORY_TWIST_PER_EP_MID_HIGH,
    STORY_TWIST_PER_EP_MID_LOW,
)
from service.script_tools.llm_caller import (
    LlmCaller,
    ModelTier,
    ScoreLLMError,
    TokenBudget,
)
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


# 阅文五力（docs/08）；compliance 不参与改写（合规违规走人工复审，docs/10 §7 不变式）
_DIMENSIONS_FIVE = ("story", "character", "concept", "emotion", "pacing")

_DIM_LABELS_ZH = {
    "story": "故事力",
    "character": "人物力",
    "concept": "题材力",
    "emotion": "情感力",
    "pacing": "叙事力",
}

# plan / execute 阶段共享的上下文窗口（与 ProposeRewriteTool 保持同款）
_CONTEXT_WINDOW = 2
_SCENE_DIGEST_CHARS = 180
_CHARACTERS_TOP_N = 12

# plan tree 上限：太多场会撑爆 LLM 输出 + 用户也审不过来
_MAX_PLAN_STEPS = 12
_STORY_KEY_BEATS = STORY_KEY_BEATS
_STORY_TWIST_HL_TYPES = set(STORY_TWIST_EVENT_TYPES)
_STORY_TWIST_PER_EP_TARGET = {
    "medium": STORY_TWIST_PER_EP_MID_LOW,   # 对齐 score_story mid_low 门槛（2 -> 4）
    "high": STORY_TWIST_PER_EP_MID_HIGH,    # 对齐 score_story mid_high 门槛（4 -> 7）
}


# ============================================================
# 数据结构
# ============================================================


@dataclass
class PlanStep:
    """plan tree 的一个 step，对应「改这一场，目标这几个维度」。"""

    scene_id: str
    scene_label: str
    episode_no: Optional[int]
    scene_no: Optional[str]
    target_dimensions: List[str]
    rationale: str  # 为什么改这场（≤80 字）
    expected_changes: str  # 预期改写要点（≤120 字）
    current_excerpt: str = ""  # 改前节选（≤200 字，让用户审 plan 时不必跳到编辑器）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "scene_label": self.scene_label,
            "episode_no": self.episode_no,
            "scene_no": self.scene_no,
            "target_dimensions": list(self.target_dimensions),
            "rationale": self.rationale,
            "expected_changes": self.expected_changes,
            "current_excerpt": self.current_excerpt,
        }


@dataclass
class RewritePlan:
    """LLM 给出的全剧改写计划，等待用户审。"""

    dimensions: List[str]
    overall_summary: str  # 一句话整体改写思路（≤120 字）
    steps: List[PlanStep] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimensions": list(self.dimensions),
            "overall_summary": self.overall_summary,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class RewriteResult:
    """单场改写产出。"""

    scene_id: str
    scene_label: str
    target_dimensions: List[str]
    original_text: str
    rewritten_text: str
    rationale: str  # ≤150 字

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "scene_label": self.scene_label,
            "target_dimensions": list(self.target_dimensions),
            "original_text": self.original_text,
            "rewritten_text": self.rewritten_text,
            "rationale": self.rationale,
        }


# ============================================================
# 1. select_target_scenes
# ============================================================


def select_target_scenes(
    *,
    script_id: str,
    dimensions: List[str],
    max_scenes: int = _MAX_PLAN_STEPS,
    engine: Engine = default_engine,
) -> List[Dict[str, Any]]:
    """选择需要改写的代表场（按信号缺口驱动，而不是固定条数）。

    数据来源：reports.report_json.evaluation.dimensions[].evidence_ref_ids
              → evidence_refs.scene_id。score < 7 的维度才算短板。

    设计原则（第一性）：
      1) 先覆盖“评分证据指出的问题场”（evidence_ref_ids 全量去重）；
      2) 对 story 维度，额外按“下一档阈值缺口”补结构位：
         - 缺关键节拍：补对应结构位场（开场/激励/中点/高潮/收束）
         - 反转密度不足：补“无反转集”的收束场（优先后段集）
      3) 若候选超出 max_scenes，按“策略场 > 多维重叠场 > 分数缺口更大”裁剪。

    Returns:
        list of dict，每条形如：
          {
            "scene_id": "...",
            "scene_label": "...",
            "episode_no": int | None,
            "scene_no": str | None,
            "text": str,                       # 改写时要喂进 LLM
            "matched_dimensions": ["story", ...],  # 哪些维度选中此场
            "dim_reasons": {dim: reason 首句}
          }
    """
    dims = [d for d in dimensions if d in _DIMENSIONS_FIVE]
    if not dims:
        return []

    def _to_numeric_score(raw: Any) -> Optional[float]:
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        return None

    def _add_scene_candidate(
        *,
        per_scene: Dict[str, Dict[str, Any]],
        scene_id: str,
        dim: str,
        dim_reason: str,
        scene_hint: Optional[Dict[str, Any]] = None,
        strategy_boost: int = 0,
    ) -> None:
        scene_key = str(scene_id or "").strip()
        if not scene_key:
            return
        bucket = per_scene.setdefault(
            scene_key,
            {
                "scene_id": scene_key,
                "scene_label": (scene_hint or {}).get("scene_label") if scene_hint else "",
                "episode_no": (scene_hint or {}).get("episode_no") if scene_hint else None,
                "scene_no": (scene_hint or {}).get("scene_no") if scene_hint else None,
                "matched_dimensions": [],
                "dim_reasons": {},
                "_strategy_boost": 0,
            },
        )
        if dim not in bucket["matched_dimensions"]:
            bucket["matched_dimensions"].append(dim)
        if dim and dim_reason and (dim not in bucket["dim_reasons"] or strategy_boost > 0):
            bucket["dim_reasons"][dim] = dim_reason
        if strategy_boost > 0:
            bucket["_strategy_boost"] = max(int(bucket.get("_strategy_boost") or 0), strategy_boost)

    def _collect_present_beats(beat_sheet_payload: Any) -> set[str]:
        present: set[str] = set()
        if not isinstance(beat_sheet_payload, dict):
            return present
        for act in beat_sheet_payload.get("acts") or []:
            if not isinstance(act, dict):
                continue
            for beat in act.get("beats") or []:
                if not isinstance(beat, dict):
                    continue
                beat_type = str(beat.get("type") or "").strip()
                anchor = str(beat.get("anchor_scene_id") or "").strip()
                if beat_type in _STORY_KEY_BEATS and anchor:
                    present.add(beat_type)
        return present

    def _pick_structure_scene_ids(
        missing_beats: List[str],
        ordered_scene_rows: List[Dict[str, Any]],
    ) -> List[str]:
        if not ordered_scene_rows:
            return []
        ids = [str(row.get("id") or "") for row in ordered_scene_rows if str(row.get("id") or "").strip()]
        if not ids:
            return []
        # 优先按“集边界”取结构位：解释成本更低，比纯百分位更贴合短剧阅读节奏。
        episode_to_scene_ids: Dict[int, List[str]] = {}
        for row in ordered_scene_rows:
            sid = str(row.get("id") or "").strip()
            ep_raw = row.get("episode_no")
            if not sid or not isinstance(ep_raw, int):
                continue
            episode_to_scene_ids.setdefault(int(ep_raw), []).append(sid)

        beat_to_scene_id: Dict[str, str] = {}
        if episode_to_scene_ids:
            eps = sorted(episode_to_scene_ids.keys())
            first_ep = eps[0]
            mid_ep = eps[len(eps) // 2]
            last_ep = eps[-1]
            climax_ep = eps[-2] if len(eps) >= 3 else eps[-1]
            beat_to_scene_id = {
                "opening": episode_to_scene_ids[first_ep][0],
                "inciting": episode_to_scene_ids[first_ep][-1],
                "midpoint": episode_to_scene_ids[mid_ep][-1],
                "climax": episode_to_scene_ids[climax_ep][-1],
                "closing": episode_to_scene_ids[last_ep][-1],
            }
        else:
            n = len(ids)
            beat_to_index = {
                "opening": 0,
                "inciting": max(0, min(n - 1, int(math.floor(n * 0.12)))),
                "midpoint": max(0, min(n - 1, int(math.floor(n * 0.5)))),
                "climax": max(0, min(n - 1, int(math.floor(n * 0.82)))),
                "closing": n - 1,
            }
            beat_to_scene_id = {
                beat: ids[idx] for beat, idx in beat_to_index.items()
            }
        out: List[str] = []
        seen: set[str] = set()
        for beat in missing_beats:
            sid = beat_to_scene_id.get(beat)
            if sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
        return out

    def _episode_last_scene_ids(ordered_scene_rows: List[Dict[str, Any]]) -> Dict[int, str]:
        out: Dict[int, str] = {}
        for row in ordered_scene_rows:
            ep_raw = row.get("episode_no")
            if not isinstance(ep_raw, int):
                continue
            sid = str(row.get("id") or "").strip()
            if not sid:
                continue
            out[int(ep_raw)] = sid
        return out

    with engine.connect() as conn:
        report_row = conn.execute(
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

        if report_row is None:
            return []

        payload = report_row["report_json"]
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                logger.warning("report_json parse failed for script %s", script_id)
                return []
        if not isinstance(payload, dict):
            return []

        evaluation = payload.get("evaluation") or {}
        eval_dims: List[Dict[str, Any]] = evaluation.get("dimensions") or []
        if not eval_dims:
            scorecard = payload.get("scorecard") or []
            eval_dims = scorecard if isinstance(scorecard, list) else []

        evidence_refs: List[Dict[str, Any]] = payload.get("evidence_refs") or []
        evi_by_id: Dict[str, Dict[str, Any]] = {
            str(ref.get("id")): ref for ref in evidence_refs if ref.get("id")
        }

        scripts_meta = conn.execute(
            text(
                """
                SELECT COALESCE(total_episodes, 0) AS total_episodes
                FROM scriptlens.scripts
                WHERE id = :sid
                """
            ),
            {"sid": script_id},
        ).mappings().first() or {}
        total_episodes = int(scripts_meta.get("total_episodes") or 0)

        all_scene_rows = conn.execute(
            text(
                """
                SELECT id::text AS id, episode_no, scene_no, scene_label, text
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                """
            ),
            {"sid": script_id},
        ).mappings().all()
        scene_full_map: Dict[str, Dict[str, Any]] = {str(row["id"]): dict(row) for row in all_scene_rows}
        scene_order_index: Dict[str, int] = {
            str(row["id"]): idx for idx, row in enumerate(all_scene_rows)
        }

        per_scene: Dict[str, Dict[str, Any]] = {}
        dim_score_map: Dict[str, Optional[float]] = {}
        for entry in eval_dims:
            dim = str(entry.get("key") or entry.get("dimension") or "")
            if dim not in dims:
                continue
            score_num = _to_numeric_score(entry.get("score"))
            dim_score_map[dim] = score_num
            if score_num is None or score_num >= 7:
                continue
            ref_ids = entry.get("evidence_ref_ids") or []
            if not isinstance(ref_ids, list) or not ref_ids:
                continue
            reason_first = _first_sentence(str(entry.get("reason") or ""))

            for ref_id in ref_ids:
                evi = evi_by_id.get(str(ref_id))
                if evi is None:
                    continue
                scene_id = str(evi.get("scene_id") or "")
                if not scene_id:
                    continue
                _add_scene_candidate(
                    per_scene=per_scene,
                    scene_id=scene_id,
                    dim=dim,
                    dim_reason=reason_first,
                    scene_hint=evi,
                )

        # story 维度的“信号缺口驱动”补场：
        # - 结构缺口：缺哪个 beat 就补哪个结构位
        # - 密度缺口：按“到下一档阈值还差多少反转”补无反转集的收束场
        if "story" in dims:
            story_entry = next(
                (
                    item for item in eval_dims
                    if str(item.get("key") or item.get("dimension") or "").strip() == "story"
                ),
                None,
            )
            story_score = _to_numeric_score((story_entry or {}).get("score"))
            if story_score is not None and story_score < 7 and all_scene_rows:
                beat_sheet_payload = payload.get("beat_sheet") or {}
                present_beats = _collect_present_beats(beat_sheet_payload)
                missing_beats = [beat for beat in _STORY_KEY_BEATS if beat not in present_beats]

                highlights = payload.get("highlights") or []
                twist_scene_ids: set[str] = set()
                if isinstance(highlights, list):
                    for hl in highlights:
                        if not isinstance(hl, dict):
                            continue
                        hl_type = str(hl.get("type") or "").strip()
                        sid = str(hl.get("scene_id") or "").strip()
                        if hl_type in _STORY_TWIST_HL_TYPES and sid:
                            twist_scene_ids.add(sid)

                if total_episodes <= 0:
                    episodes_seen = {
                        int(row["episode_no"])
                        for row in all_scene_rows
                        if isinstance(row.get("episode_no"), int)
                    }
                    total_episodes = len(episodes_seen)
                total_episodes = max(1, total_episodes)

                next_band = "medium" if story_score < 4 else "high"
                target_ratio = _STORY_TWIST_PER_EP_TARGET[next_band]
                required_twists = int(math.ceil(target_ratio * total_episodes))
                missing_twists = max(0, required_twists - len(twist_scene_ids))

                structure_scene_ids = _pick_structure_scene_ids(missing_beats, [dict(r) for r in all_scene_rows])
                for sid in structure_scene_ids:
                    _add_scene_candidate(
                        per_scene=per_scene,
                        scene_id=sid,
                        dim="story",
                        dim_reason=f"补关键节拍（缺 {','.join(missing_beats)}）",
                        scene_hint=scene_full_map.get(sid),
                        strategy_boost=30,
                    )

                if missing_twists > 0:
                    episode_to_last_scene = _episode_last_scene_ids([dict(r) for r in all_scene_rows])
                    scene_to_episode: Dict[str, int] = {}
                    for row in all_scene_rows:
                        sid = str(row.get("id") or "").strip()
                        ep = row.get("episode_no")
                        if sid and isinstance(ep, int):
                            scene_to_episode[sid] = int(ep)
                    twist_episodes = {
                        scene_to_episode[sid]
                        for sid in twist_scene_ids
                        if sid in scene_to_episode
                    }
                    all_episodes_sorted = sorted(episode_to_last_scene.keys(), reverse=True)
                    no_twist_episodes = [ep for ep in all_episodes_sorted if ep not in twist_episodes]
                    for ep in no_twist_episodes[:missing_twists]:
                        sid = episode_to_last_scene.get(ep)
                        if not sid:
                            continue
                        _add_scene_candidate(
                            per_scene=per_scene,
                            scene_id=sid,
                            dim="story",
                            dim_reason=(
                                f"补反转密度（当前 {len(twist_scene_ids)}/{total_episodes}，"
                                f"下一档需至少 {required_twists}）"
                            ),
                            scene_hint=scene_full_map.get(sid),
                            strategy_boost=20,
                        )

        if not per_scene:
            return []

    out: List[Dict[str, Any]] = []
    for sid in per_scene.keys():
        bucket = per_scene[sid]
        sc = scene_full_map.get(sid)
        if sc is None:
            # report 引用了但 scene 已删 —— 跳过，警告下，不让 plan 整体崩
            logger.warning("rewrite plan: scene_id=%s not found in scenes table", sid)
            continue
        bucket["text"] = sc.get("text") or ""
        if not bucket["scene_label"]:
            bucket["scene_label"] = sc.get("scene_label") or ""
        if bucket["episode_no"] is None:
            bucket["episode_no"] = sc.get("episode_no")
        if bucket["scene_no"] is None:
            bucket["scene_no"] = sc.get("scene_no")
        out.append(bucket)

    # 候选超限时，按“策略补场 > 多维重叠 > 维度缺口更大”优先，再回到剧本顺序。
    def _scene_priority(item: Dict[str, Any]) -> float:
        strategy_boost = int(item.get("_strategy_boost") or 0)
        matched_dims = [str(dim) for dim in item.get("matched_dimensions") or []]
        dim_overlap = len(set(matched_dims))
        gap_score = 0.0
        for dim in matched_dims:
            dim_score = dim_score_map.get(dim)
            if dim_score is None:
                gap_score += 2.0
            else:
                gap_score += max(0.0, 7.0 - float(dim_score))
        return float(strategy_boost) + dim_overlap * 8.0 + gap_score

    out.sort(
        key=lambda item: (
            -_scene_priority(item),
            scene_order_index.get(str(item.get("scene_id") or ""), 10**9),
        )
    )
    trimmed = out[:max_scenes]
    trimmed.sort(
        key=lambda b: scene_order_index.get(str(b.get("scene_id") or ""), 10**9)
    )
    for item in trimmed:
        item.pop("_strategy_boost", None)
    return trimmed


# ============================================================
# 2. propose_plan
# ============================================================


_PLAN_PROMPT = """你是中文短剧资深编剧主笔。基于「全剧诊断报告」+「候选低分场清单」，输出一份**全剧改写计划**。

【目标维度】（阅文五力，docs/08）
{dims_block}

【整剧基调】
{script_overview}

【候选低分场清单（已按集场排序）】
{candidates_block}

任务：为每个候选场列出**一条改写指令**（不要写改写后的文本，只写「要改什么 / 为什么」）。

硬约束：
1. 每场 target_dimensions 必须是【目标维度】的子集；同一场可同时挂多个维度（如该场既是反转点又是钩子）
2. rationale ≤ 80 字，回答「为什么改这场」
3. expected_changes ≤ 120 字，回答「具体改什么」（如：把宁卓蟒袍玉带换成现代西装入殓打戏 / 第 1 段补一句穿越触发台词 / 删掉重复的家世铺垫）
4. 不要凭空加新人物 / 新主线；plan 只动**已有候选场**
5. 输出顺序保持候选清单的集场顺序

输出严格 JSON（不要 markdown 代码块包裹）：
{{
  "overall_summary": "<≤120 字的整体改写思路：哪几条线 / 哪几集是重点 / 整剧基调如何调整>",
  "steps": [
    {{
      "scene_id": "<候选清单里的 scene_id>",
      "target_dimensions": ["story", "concept"],
      "rationale": "<为什么改这场 ≤80 字>",
      "expected_changes": "<具体改什么 ≤120 字>"
    }}
  ]
}}"""


async def propose_plan(
    *,
    script_id: str,
    dimensions: List[str],
    scenes: Optional[List[Dict[str, Any]]] = None,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> RewritePlan:
    """LLM 单调用产出全剧改写 plan tree（不出改写文本，让用户先审）。

    Args:
        scenes: select_target_scenes 的输出。None 时内部自动 select。
    """
    dims = [d for d in dimensions if d in _DIMENSIONS_FIVE]
    if not dims:
        raise ValueError(
            f"dimensions must be a non-empty subset of {_DIMENSIONS_FIVE}; got {dimensions!r}"
        )

    if scenes is None:
        scenes = select_target_scenes(
            script_id=script_id, dimensions=dims, engine=engine
        )

    if not scenes:
        return RewritePlan(
            dimensions=dims,
            overall_summary="当前剧本在所选维度上没有 score<7 的明显短板场，无须 plan-level 改写。",
            steps=[],
        )

    overview = _load_script_overview(script_id, engine=engine)
    dims_block = "\n".join(
        f"- {d}（{_DIM_LABELS_ZH.get(d, d)}）" for d in dims
    )
    candidates_block = _format_candidates(scenes)

    prompt = _PLAN_PROMPT.format(
        dims_block=dims_block,
        script_overview=overview,
        candidates_block=candidates_block,
    )

    caller = caller or LlmCaller()
    try:
        resp = await caller.call_json(
            prompt,
            tier=ModelTier.PRIMARY,
            temperature=0.3,
            max_tokens=TokenBudget.COVERAGE_CARD,  # plan 输出和 coverage 量级相当
        )
    except ScoreLLMError as e:
        logger.warning("propose_plan LLM failed: %s", e)
        raise

    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    overall_summary = str(parsed.get("overall_summary") or "").strip()
    raw_steps = parsed.get("steps") or []

    scene_index = {s["scene_id"]: s for s in scenes}
    steps: List[PlanStep] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("scene_id") or "").strip()
        if sid not in scene_index:
            # LLM 编了一个候选清单外的 scene_id，丢弃，警告
            logger.warning("propose_plan: LLM returned unknown scene_id=%s, dropped", sid)
            continue
        target_dims_raw = raw.get("target_dimensions") or []
        target_dims = [
            d for d in target_dims_raw if isinstance(d, str) and d in dims
        ]
        if not target_dims:
            target_dims = list(scene_index[sid].get("matched_dimensions") or dims[:1])

        cand = scene_index[sid]
        excerpt = (cand.get("text") or "").strip().replace("\r\n", "\n")
        if len(excerpt) > 200:
            excerpt = excerpt[:200] + "…"

        steps.append(
            PlanStep(
                scene_id=sid,
                scene_label=str(cand.get("scene_label") or ""),
                episode_no=cand.get("episode_no"),
                scene_no=cand.get("scene_no"),
                target_dimensions=target_dims,
                rationale=_truncate(str(raw.get("rationale") or ""), 80),
                expected_changes=_truncate(str(raw.get("expected_changes") or ""), 120),
                current_excerpt=excerpt,
            )
        )

    if not steps:
        # LLM 一个 step 都没给 —— 兜底：用 select 的候选作为 step（rationale 取 dim_reason）
        for cand in scenes:
            target_dims = list(cand.get("matched_dimensions") or [])
            reasons = cand.get("dim_reasons") or {}
            rationale = "；".join(reasons.get(d, "") for d in target_dims if reasons.get(d))
            excerpt = (cand.get("text") or "").strip()[:200]
            steps.append(
                PlanStep(
                    scene_id=cand["scene_id"],
                    scene_label=str(cand.get("scene_label") or ""),
                    episode_no=cand.get("episode_no"),
                    scene_no=cand.get("scene_no"),
                    target_dimensions=target_dims,
                    rationale=_truncate(rationale, 80),
                    expected_changes="按维度短板针对性优化",
                    current_excerpt=excerpt,
                )
            )

    if not overall_summary:
        overall_summary = (
            f"针对 {'/'.join(_DIM_LABELS_ZH.get(d, d) for d in dims)} 共 {len(steps)} 场进行定向改写。"
        )

    return RewritePlan(
        dimensions=dims,
        overall_summary=overall_summary,
        steps=steps,
    )


# ============================================================
# 3. execute_plan_step
# ============================================================


_EXECUTE_PROMPT = """你是中文短剧资深编剧。请基于「整剧上下文 + 目标场原文 + 多维度改写指令」对单场做改写。

【整剧概要】
{script_overview}

【人物表（出场频次倒序）】
{characters_block}

【前情场次摘要】
{prev_scenes_block}

【目标场原文】（{scene_label}）
---
{scene_text}
---

【后续场次摘要】（已写好的剧情走向，改写时必须呼应）
{next_scenes_block}

【目标改写维度】{target_dimensions_text}

【plan 阶段定下的改写要点】
{expected_changes}

任务：针对**所有目标维度同时优化**，对【目标场原文】整段重写。

硬约束（违反任何一条结果都不可用）：
1. 改写后只输出「目标场」的新文本——不要顺手改前 / 后场
2. 必须沿用上面【人物表】里已存在的人物，可以引用【前情】已发生事件作为铺垫，但不能凭空捏造新人物 / 新核心事件
3. 改写后字数与原文 ±30% 以内
4. 必须与【后续场次】剧情走向自洽
5. 同时承载多个维度时按这个优先级取舍：concept > story > character > emotion > pacing

各维度优化方向（按需取用，不要堆砌）：
- story    : 强化主线推进 / 补反转节点 / 回应已埋伏笔
- character: 给关键决策补可追溯因果（用前情人物关系做铺垫）；打掉 OOC 与扁平化
- concept  : 把题材标识 / 核心卖点钩子前置到本场前 1/3，删冗余铺垫
- emotion  : 加情感钩子或爽点（CP 进展 / 反派败落 / 逆袭）放大情绪密度
- pacing   : 删冗余对白 / 重复信息，节奏前推

输出严格 JSON（不要 markdown 代码块包裹）：
{{
  "rewritten_text": "<改写后的整段场景文本>",
  "rationale": "<≤150 字，解释你具体做了哪几处改动、用了哪些前情铺垫、为什么这样改能在所选维度提分>"
}}"""


async def execute_plan_step(
    *,
    script_id: str,
    scene_id: str,
    target_dimensions: List[str],
    expected_changes: str = "",
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> RewriteResult:
    """单场改写：LLM 一调用产新场文本 + rationale。

    Raises:
        ValueError: scene_id 不存在 / 不属于 script_id / dimensions 全非法
        ScoreLLMError: LLM 多次失败或返回为空
    """
    dims = [d for d in target_dimensions if d in _DIMENSIONS_FIVE]
    if not dims:
        raise ValueError(
            f"target_dimensions must be a non-empty subset of {_DIMENSIONS_FIVE}; got {target_dimensions!r}"
        )

    ctx = _load_rewrite_context(scene_id, expected_script_id=script_id, engine=engine)
    if ctx is None:
        raise ValueError(f"scene_id {scene_id} not found in script {script_id}")

    scene = ctx["scene"]
    scene_text = scene.get("text") or ""
    if not scene_text.strip():
        raise ValueError(f"scene {scene_id} has empty text")

    target_dims_text = " + ".join(
        f"{d}（{_DIM_LABELS_ZH.get(d, d)}）" for d in dims
    )

    prompt = _EXECUTE_PROMPT.format(
        script_overview=ctx["script_overview"],
        characters_block=ctx["characters_block"],
        prev_scenes_block=ctx["prev_scenes_block"],
        next_scenes_block=ctx["next_scenes_block"],
        scene_label=scene.get("scene_label") or "",
        scene_text=scene_text,
        target_dimensions_text=target_dims_text,
        expected_changes=expected_changes or "（plan 未指定细节，按维度短板自行判断）",
    )

    caller = caller or LlmCaller()
    try:
        resp = await caller.call_json(
            prompt,
            tier=ModelTier.PRIMARY,
            temperature=0.4,
            max_tokens=TokenBudget.REWRITE_EXCERPT,
        )
    except ScoreLLMError as e:
        logger.warning("execute_plan_step LLM failed for scene %s: %s", scene_id, e)
        raise

    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    rewritten = str(parsed.get("rewritten_text") or "").strip()
    rationale = str(parsed.get("rationale") or "").strip()

    if not rewritten:
        raise ScoreLLMError(f"execute_plan_step: LLM returned empty rewritten_text for scene {scene_id}")

    return RewriteResult(
        scene_id=scene_id,
        scene_label=str(scene.get("scene_label") or ""),
        target_dimensions=dims,
        original_text=scene_text,
        rewritten_text=rewritten,
        rationale=rationale or "（LLM 未给出 rationale）",
    )


# ============================================================
# helpers（与 ProposeRewriteTool _load_rewrite_context 同款简化版）
# ============================================================


def _load_script_overview(script_id: str, *, engine: Engine = default_engine) -> str:
    """整剧概要：summary + decision.one_sentence_reason；缺则兜底。"""
    with engine.connect() as conn:
        row = conn.execute(
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
    if row is None:
        return "（暂无诊断报告，仅基于场内容做改写）"
    payload = row["report_json"]
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    if not isinstance(payload, dict):
        return "（报告解析失败，仅基于场内容做改写）"
    summary = str(payload.get("summary") or "").strip()
    decision = payload.get("decision") or {}
    one_line = ""
    if isinstance(decision, dict):
        one_line = str(decision.get("one_sentence_reason") or "").strip()
    coverage = payload.get("coverage_card") or {}
    genre = ""
    if isinstance(coverage, dict):
        gs = coverage.get("genre") or []
        if isinstance(gs, list):
            genre = " / ".join(str(g) for g in gs if g)
    parts = [p for p in (genre and f"题材：{genre}", summary, one_line) if p]
    return "\n".join(parts) if parts else "（暂无整剧概要）"


def _load_rewrite_context(
    scene_id: str,
    *,
    expected_script_id: str,
    engine: Engine = default_engine,
) -> Optional[Dict[str, Any]]:
    """单场改写所需上下文（前后场窗口 + 人物表 + 整剧概要）。"""
    with engine.connect() as conn:
        target = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, text
                FROM scriptlens.scenes
                WHERE id = :sid
                """
            ),
            {"sid": scene_id},
        ).mappings().first()
        if target is None:
            return None
        target_dict = dict(target)
        if target_dict["script_id"] != expected_script_id:
            raise ValueError("scene_id does not belong to current script")

        all_scenes = conn.execute(
            text(
                """
                SELECT id::text AS id, episode_no, scene_no, scene_label,
                       LEFT(text, :digest) AS digest
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                """
            ),
            {"sid": expected_script_id, "digest": _SCENE_DIGEST_CHARS},
        ).mappings().all()

        characters_rows = conn.execute(
            text(
                """
                SELECT character_name, COUNT(*) AS appearances
                FROM (
                    SELECT unnest(characters) AS character_name
                    FROM scriptlens.scenes
                    WHERE script_id = :sid
                ) AS t
                WHERE character_name IS NOT NULL AND character_name <> ''
                GROUP BY character_name
                ORDER BY appearances DESC, character_name ASC
                LIMIT :top_n
                """
            ),
            {"sid": expected_script_id, "top_n": _CHARACTERS_TOP_N},
        ).mappings().all()

    scenes_list = [dict(s) for s in all_scenes]
    target_idx = next(
        (i for i, s in enumerate(scenes_list) if s["id"] == scene_id),
        None,
    )
    if target_idx is None:
        return None

    prev_window = scenes_list[max(0, target_idx - _CONTEXT_WINDOW): target_idx]
    next_window = scenes_list[target_idx + 1: target_idx + 1 + _CONTEXT_WINDOW]

    return {
        "scene": target_dict,
        "script_overview": _load_script_overview(expected_script_id, engine=engine),
        "characters_block": _format_characters([dict(r) for r in characters_rows]),
        "prev_scenes_block": _format_window(prev_window) or "（无前情场次，本场为剧本开端）",
        "next_scenes_block": _format_window(next_window) or "（无后续场次，本场为剧本结尾）",
    }


def _format_candidates(scenes: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for sc in scenes:
        title = _scene_title(sc)
        dims = "/".join(sc.get("matched_dimensions") or [])
        reasons = sc.get("dim_reasons") or {}
        reason_parts = [f"{d}: {r}" for d, r in reasons.items() if r]
        reason_text = "；".join(reason_parts) or "（无评分理由）"
        excerpt = (sc.get("text") or "").strip().replace("\n", " ")[:120]
        lines.append(
            f"- scene_id={sc['scene_id']} | {title} | 短板维度={dims} | {reason_text}\n  原文片段：{excerpt}…"
        )
    return "\n".join(lines)


def _format_characters(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "（剧本未抽取出人物，请从前情 / 后续摘要里识别）"
    return "\n".join(
        f"- {r['character_name']}（出场 {r['appearances']} 场）" for r in rows
    )


def _format_window(window: List[Dict[str, Any]]) -> str:
    if not window:
        return ""
    lines = []
    for s in window:
        digest = (s.get("digest") or "").strip().replace("\n", " ")
        lines.append(f"- {_scene_title(s)}：{digest}")
    return "\n".join(lines)


def _scene_title(s: Dict[str, Any]) -> str:
    parts: List[str] = []
    ep = s.get("episode_no")
    if ep is not None:
        parts.append(f"第{ep}集")
    sn = s.get("scene_no")
    if sn is not None and str(sn).strip():
        parts.append(f"第{sn}场")
    label = s.get("scene_label")
    if label:
        parts.append(f"《{label}》")
    return " ".join(parts) if parts else "未命名场"


def _first_sentence(s: str, *, max_len: int = 80) -> str:
    if not s:
        return ""
    chunk = s.strip()
    for sep in ("\n", "。", "；", "！", "?"):
        if sep in chunk:
            chunk = chunk.split(sep, 1)[0]
            break
    chunk = chunk.strip()
    if len(chunk) > max_len:
        chunk = chunk[: max_len - 1] + "…"
    return chunk


def _truncate(s: str, max_len: int) -> str:
    s = (s or "").strip()
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s
