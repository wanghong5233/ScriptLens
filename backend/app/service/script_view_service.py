"""ScriptLens 报告视图派生服务。

GET /api/scripts/{id}/view 的核心逻辑：

  1. 透传 reports.report_json（v4 投资决策评分 verdict / 5 维 + 合规 gate）
  2. 派生 `rewrite_seeds`（C-3c 起暂为 []，后续从 v4 top_improvements 派生）
  3. 派生 `task_status`：从 script_operations 表派生每个 (scene_id, dimension)
     上的改写任务状态（详见 docs/03-system-mental-model.md §8）

视角切换由前端「行动」segment 的三张 Persona Action Card 实装，
ViewResponse 不返回 card 结构、不带 role 参数（详见 docs/09-action-lens.md）。

报告本身（reports.report_json）保持不变；rewrite_seeds / task_status 是
**视图层派生**，不污染持久化层。
"""

from __future__ import annotations

import logging
from typing import Dict, List

from schemas.script import (
    ReportPayload,
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
        summary=report.summary or report.decision_reason,
        compliance=report.compliance,
        drama_tags=list(report.drama_tags or []),
        plot_units=list(report.plot_units or []),
        characters=list(report.characters or []),
        character_relationships=list(report.character_relationships or []),
        character_bios=list(report.character_bios or []),
        must_read_scene_ids=list(report.must_read_scene_ids or []),
        risk_flags=list(report.risk_flags or []),
        evidence_refs=list(report.evidence_refs or []),
        highlights=list(report.highlights or []),
        coverage_card=report.coverage_card,
        beat_sheet=report.beat_sheet,
        character_graph=report.character_graph,
        pacing_curve=report.pacing_curve,
        rewrite_seeds=rewrite_seeds,
        task_status=task_status,
        # W1.3 (2026-05-31)：透传报告级 provenance 元数据给前端。
        meta=report.meta,
        # Wave C-1 / D (2026-05-31) / C-3c：v4 投资决策评分主链路字段透传给前端。
        verdict=report.verdict,
        investment_score=report.investment_score,
        evaluation_v4=report.evaluation_v4,
        top_improvements=list(report.top_improvements or []),
    )


def _derive_rewrite_seeds(report: ReportPayload, *, max_seeds: int = 3) -> List[RewriteSeed]:
    """Wave C-3c：rewrite_seeds 不再从 v3 scorecard / evaluation 派生。

    历史实现基于 v3 `scorecard.signal_refs.evidence_refs[].scene_id` 提取低分维度
    的代表场景，作为"最值得改的 N 场"卡片。Wave C-3c 删除 v3 字段后，等效信息
    应改走 v4 `report.top_improvements`（按 dim × signal × score_gap 排序）。

    暂时返回 []：前端 ScriptViewResponseDTO.rewrite_seeds 仍保留字段，但内容
    自然为空。后续独立 PR 把 top_improvements 派生成场景级 rewrite seeds 后再填。

    max_seeds 保留参数以最小化调用方 BC 改动；后续 PR 一并清理。
    """
    _ = (report, max_seeds)
    return []
