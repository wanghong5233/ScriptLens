"""ScriptLens 报告视图派生服务。

GET /api/scripts/{id}/view 的核心逻辑：

  1. 透传 reports.report_json（scorecard 顺序固定为五力声明序，不按角色重排）
  2. 派生 `rewrite_seeds`：从 score<7 五力维度的第一条 evidence 生成
     「最值得改的 N 场」候选（详见 docs/03-system-mental-model.md §6）
     注：合规违规不进改写候选——合规问题需人工二次审核，不交给 LLM 改写
  3. 派生 `task_status`：从 script_operations 表派生每个 (scene_id, dimension)
     上的改写任务状态（详见 docs/03-system-mental-model.md §8）

视角切换由前端「行动」segment 的三张 Persona Action Card 实装，
ViewResponse 不返回 card 结构、不带 role 参数（详见 docs/09-action-lens.md）。

报告本身（reports.report_json）保持不变；rewrite_seeds / task_status 是
**视图层派生**，不污染持久化层。
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


def build_view(
    *,
    script_id: str,
    user_id: int,
    report: ReportPayload,
) -> ViewResponse:
    """拼装 ViewResponse（含派生的 rewrite_seeds / task_status）。

    调用方应保证已校验剧本归属与 status=ready。
    """
    rewrite_seeds = _derive_rewrite_seeds(report)

    # 派生 task_status：op 表查询失败不让整个 view 接口 500，状态徽章降级为空即可
    task_status: Dict[str, RewriteTaskStatus] = {}
    try:
        raw_status = script_operation_service.get_rewrite_task_status_map(
            script_id=script_id, user_id=user_id,
        )
        task_status = {k: RewriteTaskStatus.model_validate(v) for k, v in raw_status.items()}
    except script_operation_service.OperationError as exc:
        logger.warning("task_status 派生失败 script=%s user=%s err=%s", script_id, user_id, exc)
    except Exception:  # noqa: BLE001
        logger.exception("task_status 派生异常 script=%s user=%s", script_id, user_id)

    return ViewResponse(
        script_id=script_id,
        decision=report.decision,
        overall_score=report.overall_score,
        summary=report.summary or report.decision.summary,
        scorecard=list(report.scorecard),
        compliance=report.compliance,
        must_read_scene_ids=list(report.must_read_scene_ids or []),
        risk_flags=list(report.risk_flags or []),
        evidence_refs=list(report.evidence_refs or []),
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
    """优先使用 Batch 3 improvement_actions 生成 rewrite seeds，兼容旧链路回退。"""
    evaluation = report.evaluation
    if evaluation and evaluation.rewrite_seeds:
        seeds: List[RewriteSeed] = []
        for raw in evaluation.rewrite_seeds[:max_seeds]:
            if not isinstance(raw, dict):
                continue
            evidence_refs = raw.get("evidence_refs") if isinstance(raw.get("evidence_refs"), list) else []
            scene_id = str(raw.get("scene_id") or "").strip()
            scene_label = str(raw.get("scene_label") or "").strip() or None
            if not scene_id and evidence_refs:
                first = evidence_refs[0] if isinstance(evidence_refs[0], dict) else {}
                scene_id = str(first.get("scene_id") or "").strip()
                scene_label = scene_label or str(first.get("scene_label") or "").strip() or None
            if not scene_id:
                continue
            seeds.append(
                RewriteSeed(
                    id=str(raw.get("id") or "").strip() or None,
                    dimension=str(raw.get("dimension") or ""),
                    signal_key=str(raw.get("signal_key") or ""),
                    scene_id=scene_id,
                    scene_label=scene_label,
                    issue=str(raw.get("issue") or "").strip() or "改写优化项",
                    target=str(raw.get("target") or "").strip(),
                    action_steps=[str(item) for item in raw.get("action_steps") or [] if str(item).strip()],
                    evidence_refs=[item for item in evidence_refs if isinstance(item, dict)],
                    estimated_lift=raw.get("estimated_lift") if isinstance(raw.get("estimated_lift"), dict) else {},
                    evidence_ref_id=str(raw.get("evidence_ref_id") or "").strip() or None,
                )
            )
        if seeds:
            return seeds

    if not report.scorecard or not report.evidence_refs:
        return []

    evi_by_id: Dict[str, ReportEvidenceRef] = {ref.id: ref for ref in report.evidence_refs}
    candidates: List[Tuple[float, ReportScorecardItem]] = []
    for sc in report.scorecard:
        if sc.score is None or sc.score >= 7:
            continue
        if not sc.evidence_ref_ids:
            continue
        candidates.append((float(sc.score), sc))
    candidates.sort(key=lambda t: t[0])

    seeds: List[RewriteSeed] = []
    used_scenes: set[str] = set()
    for _, sc in candidates:
        if len(seeds) >= max_seeds:
            break
        evi = next((evi_by_id[rid] for rid in sc.evidence_ref_ids if rid in evi_by_id), None)
        if evi is None or evi.scene_id in used_scenes:
            continue
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
