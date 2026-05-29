# ============================================================
# DEPRECATED — release/v1-mvp (2026-05-29)
# ============================================================
#
# 本文件属于已废弃的「整剧抽情节打标签 → rubric/signal/aggregator
# 评分」流水线（Batch3 体系）。release/v1-mvp 已切回 self-contained
# 6 维规则评分，主流程入口：
#   - service/script_tools/dimension_scorer.py
#   - service/script_report_service.py（generate_report）
# 当前已不再调用本模块任何函数。
#
# 保留原因：避免 git history 大面积污染、便于必要时回收实现细节。
# 清理时机：下次 cleanup PR 统一删除（含本文件、其测试、CLI 入口
# 与 score_registry/rubric_sets/v3.yaml 等配套资产）。
#
# 不要在本文件内再做任何功能性修改。如需新评分能力，请扩展
# dimension_scorer.py。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.extractor_common import persist_episode_tags
from utils.database import engine as default_engine

_HOOK_PLOT_VALUES = {
    "identity_reveal",
    "reversal",
    "betrayal",
    "secret_exposure",
    "conflict_escalation",
    "emotional_choice",
    "forced_marriage",
}
_HOOK_PAYOFF_VALUES = {"cliffhanger", "reveal_power", "face_slapping", "counterattack"}


@dataclass(frozen=True)
class PaidBreakPositionResult:
    script_id: str
    episode_no: int
    position: str
    anchor_idx: int | None
    unit_count: int
    reason: str


def _pick_position(anchor_idx: int, total_units: int) -> str:
    if total_units <= 0:
        return "none"
    ratio = anchor_idx / total_units
    if ratio >= 0.85:
        return "ep_end"
    if ratio >= 0.60:
        return "ep_two_third"
    if ratio >= 0.30:
        return "ep_mid"
    return "none"


def infer_paid_break_position(script_id: str, episode_no: int, *, engine: Engine = default_engine) -> PaidBreakPositionResult:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    pu.id::text AS plot_unit_id,
                    pu.idx AS idx,
                    put.dim AS dim,
                    put.value AS value
                FROM scriptlens.plot_units pu
                LEFT JOIN scriptlens.plot_unit_tags put
                    ON put.plot_unit_id = pu.id
                   AND put.dim IN ('plot_hook', 'payoff_type')
                WHERE pu.script_id = :sid
                  AND pu.episode_no = :ep
                ORDER BY pu.idx
                """
            ),
            {"sid": script_id, "ep": episode_no},
        ).mappings().all()

    if not rows:
        return PaidBreakPositionResult(
            script_id=script_id,
            episode_no=episode_no,
            position="none",
            anchor_idx=None,
            unit_count=0,
            reason="no_plot_units",
        )

    unit_ids: list[str] = []
    hook_unit_idx: set[int] = set()
    for row in rows:
        idx = int(row.get("idx") or 0)
        pid = str(row.get("plot_unit_id") or "")
        if pid and (not unit_ids or unit_ids[-1] != pid):
            unit_ids.append(pid)
        dim = str(row.get("dim") or "")
        value = str(row.get("value") or "")
        if dim == "plot_hook" and value in _HOOK_PLOT_VALUES:
            hook_unit_idx.add(idx)
        if dim == "payoff_type" and value in _HOOK_PAYOFF_VALUES:
            hook_unit_idx.add(idx)

    unit_count = len(unit_ids)
    if not hook_unit_idx:
        return PaidBreakPositionResult(
            script_id=script_id,
            episode_no=episode_no,
            position="none",
            anchor_idx=None,
            unit_count=unit_count,
            reason="no_hook_signal",
        )

    anchor_idx = max(hook_unit_idx)
    position = _pick_position(anchor_idx, unit_count)
    return PaidBreakPositionResult(
        script_id=script_id,
        episode_no=episode_no,
        position=position,
        anchor_idx=anchor_idx,
        unit_count=unit_count,
        reason="hook_anchor",
    )


def persist_paid_break_position(
    script_id: str,
    episode_no: int,
    *,
    tag_set_ver: str,
    prompt_ver: str,
    model_ver: str = "rule:paid_break_position:v1",
    source: str = "rule",
    engine: Engine = default_engine,
) -> PaidBreakPositionResult:
    result = infer_paid_break_position(script_id, episode_no, engine=engine)
    persist_episode_tags(
        script_id=script_id,
        episode_no=episode_no,
        values_by_dim={"paid_break_position": result.position},
        tag_set_ver=tag_set_ver,
        prompt_ver=prompt_ver,
        model_ver=model_ver,
        source=source,
        confidence=None,
        evidence_by_dim={
            "paid_break_position": {
                "method": "rule",
                "reason": result.reason,
                "anchor_idx": result.anchor_idx,
                "unit_count": result.unit_count,
            }
        },
        clear_existing=True,
        engine=engine,
    )
    return result


def persist_paid_break_positions_for_episodes(
    script_id: str,
    episode_nos: Iterable[int],
    *,
    tag_set_ver: str,
    prompt_ver: str,
    model_ver: str = "rule:paid_break_position:v1",
    source: str = "rule",
    engine: Engine = default_engine,
) -> list[PaidBreakPositionResult]:
    out: list[PaidBreakPositionResult] = []
    for episode_no in episode_nos:
        out.append(
            persist_paid_break_position(
                script_id,
                int(episode_no),
                tag_set_ver=tag_set_ver,
                prompt_ver=prompt_ver,
                model_ver=model_ver,
                source=source,
                engine=engine,
            )
        )
    return out
