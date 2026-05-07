"""ScriptLens 报告视图（按角色）派生服务。

GET /api/scripts/{id}/view 的核心逻辑都在这里：

  1. 按角色重排 scorecard 优先级（不重生成评分）
  2. 重选 must_read_scene_ids（把角色优先维度的 evidence 提到前面）
  3. 派生 `rewrite_seeds`：从 score<7 / *_risk / major 维度的第一条 evidence
     生成「最值得改的 N 场」候选（详见 docs/03-system-mental-model.md §6）
  4. 派生 `task_status`：从 script_operations 表派生每个 (scene_id, dimension)
     上的改写任务状态（详见 docs/03-system-mental-model.md §8）

报告本身（reports.report_json）保持不变；rewrite_seeds / task_status 是
**视图层派生**，不污染持久化层（PRD §7 ReportPayload schema 不动）。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from schemas.script import (
    ReportEvidenceRef,
    ReportPayload,
    ReportScorecardItem,
    RewriteSeed,
    RewriteTaskStatus,
    ViewResponse,
)
from service import script_operation_service

logger = logging.getLogger(__name__)


_ROLE_DIMENSION_PRIORITY: Dict[str, Tuple[str, ...]] = {
    # 选品视角：先看钩子能不能抓人，再看爽点密度，最后审核风险兜底
    "selection": ("opening_hook", "reward_density", "risk", "pacing", "motivation"),
    # 编剧视角：动机不立则一切白搭，节奏次之
    "writer": ("motivation", "pacing", "opening_hook", "reward_density", "risk"),
    # 审核视角：风险红线优先
    "review": ("risk", "motivation", "pacing", "opening_hook", "reward_density"),
}


_LOW_RISK_LEVELS = {"high_risk", "medium_risk", "major"}


def get_role_priority(role: str) -> Tuple[str, ...]:
    return _ROLE_DIMENSION_PRIORITY[role]


def is_role_supported(role: str) -> bool:
    return role in _ROLE_DIMENSION_PRIORITY


def supported_roles() -> List[str]:
    return sorted(_ROLE_DIMENSION_PRIORITY.keys())


def build_view(
    *,
    script_id: str,
    user_id: int,
    role: str,
    report: ReportPayload,
) -> ViewResponse:
    """按角色拼装 ViewResponse（包含派生的 rewrite_seeds / task_status）。

    调用方应保证 role 已合法 + 已校验剧本归属与 status=ready。
    """
    priority = get_role_priority(role)

    # 1) 重排 scorecard：把优先维度排在前面，保持其他原顺序
    pri_index = {dim: i for i, dim in enumerate(priority)}
    scorecard_sorted = sorted(
        report.scorecard,
        key=lambda item: pri_index.get(item.dimension, 99),
    )

    # 2) 重选 must_read：把优先维度对应的 evidence_ref_ids 提前
    role_focus_dims = list(priority[:3])  # 前 3 维是 role 真正强关注的
    focused_ref_ids: List[str] = []
    seen_ref: set[str] = set()
    for dim_name in role_focus_dims:
        for sc in report.scorecard:
            if sc.dimension == dim_name:
                for rid in sc.evidence_ref_ids or []:
                    if rid not in seen_ref:
                        seen_ref.add(rid)
                        focused_ref_ids.append(rid)
    # 兜底：重排后不足 3 条时把原始 must_read 补上
    for rid in (report.must_read_scene_ids or []):
        if rid not in seen_ref and len(focused_ref_ids) < 3:
            seen_ref.add(rid)
            focused_ref_ids.append(rid)

    # 3) 派生 rewrite_seeds：低分维度的改写候选
    rewrite_seeds = _derive_rewrite_seeds(report)

    # 4) 派生 task_status：从 script_operations group by 出每个 (scene, dim) 状态
    #    单独 try：op 表查询失败不应让整个 view 接口 500，状态徽章降级为空即可
    task_status: Dict[str, RewriteTaskStatus] = {}
    try:
        raw_status = script_operation_service.get_rewrite_task_status_map(
            script_id=script_id, user_id=user_id,
        )
        task_status = {k: RewriteTaskStatus.model_validate(v) for k, v in raw_status.items()}
    except script_operation_service.OperationError as exc:
        logger.warning("task_status 派生失败 script=%s user=%s err=%s", script_id, user_id, exc)
    except Exception:  # noqa: BLE001
        # 不让派生失败拖垮主接口；记 stack 后降级
        logger.exception("task_status 派生异常 script=%s user=%s", script_id, user_id)

    return ViewResponse(
        script_id=script_id,
        role=role,  # type: ignore[arg-type]
        decision=report.decision,
        overall_score=report.overall_score,
        summary=report.summary or report.decision.summary,
        scorecard=scorecard_sorted,
        must_read_scene_ids=focused_ref_ids[:3],
        risk_flags=report.risk_flags,
        role_focus=role_focus_dims,
        evidence_refs=report.evidence_refs,
        highlights=list(report.highlights or []),
        coverage_card=report.coverage_card,
        beat_sheet=report.beat_sheet,
        character_graph=report.character_graph,
        pacing_curve=list(report.pacing_curve or []),
        evaluation=report.evaluation,
        rewrite_seeds=rewrite_seeds,
        task_status=task_status,
    )


def _derive_rewrite_seeds(report: ReportPayload, *, max_seeds: int = 3) -> List[RewriteSeed]:
    """从报告里按"最值得改"规则挑出 N 个改写候选。

    选择规则（详见 docs/03-system-mental-model.md §6 §10）：
      - 维度入选条件：score 是数字且 <7，或 level ∈ {high_risk, medium_risk, major}
      - 排序：先按是否 *_risk/major（高风险优先）、再按 score 升序
      - 每个入选维度取其第一条 evidence_ref（已被 LLM 标为该维度的 top-1 证据）
      - 同一 scene 不重复（同 scene 多维问题只挑最高优先级那一个，避免噪音）
    """
    if not report.scorecard or not report.evidence_refs:
        return []

    evi_by_id: Dict[str, ReportEvidenceRef] = {ref.id: ref for ref in report.evidence_refs}

    candidates: List[Tuple[int, int, ReportScorecardItem]] = []
    for sc in report.scorecard:
        is_risk_flag = (sc.level or "") in _LOW_RISK_LEVELS
        score_low = sc.score is not None and sc.score < 7
        if not (is_risk_flag or score_low):
            continue
        if not sc.evidence_ref_ids:
            continue  # 没证据的维度不出种子（避免无锚点改写）
        # 排序键：is_risk_flag 优先（0 排前），其次 score 升序（None 视为 99 沉底）
        risk_key = 0 if is_risk_flag else 1
        score_key = sc.score if sc.score is not None else 99
        candidates.append((risk_key, score_key, sc))

    candidates.sort(key=lambda t: (t[0], t[1]))

    seeds: List[RewriteSeed] = []
    used_scenes: set[str] = set()
    for _, _, sc in candidates:
        if len(seeds) >= max_seeds:
            break
        # 取第一条命中 evidence_refs 的 ref_id
        evi = next(
            (evi_by_id[rid] for rid in sc.evidence_ref_ids if rid in evi_by_id),
            None,
        )
        if evi is None:
            continue
        if evi.scene_id in used_scenes:
            continue  # 同一 scene 已经有更高优先级的种子了
        used_scenes.add(evi.scene_id)
        seeds.append(
            RewriteSeed(
                dimension=sc.dimension,
                scene_id=evi.scene_id,
                scene_label=evi.scene_label or evi.scene_no,
                issue=_first_sentence(sc.reason),
                evidence_ref_id=evi.id,
            )
        )
    return seeds


def _first_sentence(text_: str, *, max_len: int = 80) -> str:
    """从一段 reason 里抽第一句作为 issue 一句话点题。

    规则：先按 `\\n` / `。` / `；` 切，取第一段；再按 max_len 截断。
    短剧 reason 通常已经是一句话，这一层主要为兜底。
    """
    if not text_:
        return ""
    chunk = text_.strip()
    for sep in ("\n", "。", "；", "！", "?"):
        if sep in chunk:
            chunk = chunk.split(sep, 1)[0]
            break
    chunk = chunk.strip()
    if len(chunk) > max_len:
        chunk = chunk[: max_len - 1] + "…"
    return chunk
