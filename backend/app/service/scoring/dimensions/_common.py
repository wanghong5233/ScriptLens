"""scoring v4 dimension 计算共享工具。

只放纯函数 / 数据归一化，不引入业务字面量（所有关键词从 rubric_loader.load_keywords 取）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Optional, TypeVar

from service.scoring.framework import (
    SignalResult,
    SignalSource,
    SignalStatus,
)

if TYPE_CHECKING:
    from service.scoring.rubric_loader import SignalConfig
    from service.script_tools.scene_repo import Scene


T = TypeVar("T")


def required_param(cfg: "SignalConfig", key: str, caster: Any) -> Any:
    """从 signal_cfg.params 取必填字段，缺失则抛 KeyError（zero-default 策略）。

    所有阈值 / 关键词 / 窗口大小都必须在 YAML 里显式配置，不允许 Python 给默认值。
    """
    if key not in cfg.params:
        raise KeyError(
            f"signal {cfg.key!r} 缺失必填 param: {key!r}，请在 rubric YAML 补齐"
        )
    return caster(cfg.params[key])


def make_signal(
    *,
    key: str,
    source: SignalSource,
    score: float,
    tier: str,
    raw_value: Optional[float] = None,
    evidence_ref_ids: Optional[list[str]] = None,
    detail: str = "",
    status: SignalStatus = SignalStatus.COMPUTED,
    fallback_reason: Optional[str] = None,
) -> SignalResult:
    """构造 SignalResult 的统一入口。"""
    del tier  # 仅日志用途，已经体现在 score 里；保留参数让调用方代码可读
    return SignalResult(
        key=key,
        source=source,
        status=status,
        score=score,
        raw_value=raw_value,
        evidence_ref_ids=list(evidence_ref_ids or []),
        detail=detail,
        fallback_reason=fallback_reason,
    )


def make_failed_signal(
    key: str,
    source: SignalSource,
    *,
    fallback_reason: str,
    score: float = 0.0,
) -> SignalResult:
    return SignalResult(
        key=key,
        source=source,
        status=SignalStatus.FAILED,
        score=score,
        fallback_reason=fallback_reason,
    )


def make_not_applicable_signal(
    key: str,
    source: SignalSource,
    *,
    reason: str,
    score: float = 0.0,
) -> SignalResult:
    return SignalResult(
        key=key,
        source=source,
        status=SignalStatus.NOT_APPLICABLE,
        score=score,
        fallback_reason=reason,
    )


# ============================================================
# Scene 过滤辅助
# ============================================================


def first_episode_scenes(scenes: Iterable["Scene"]) -> list["Scene"]:
    """返回 episode_no=1 的场景（按 scene_no 排序）；没有 episode_no 时返回全部按顺序。"""
    eps = [s for s in scenes if s.episode_no == 1]
    if eps:
        eps.sort(key=lambda s: _scene_sort_key(s.scene_no))
        return eps
    return list(scenes)


def _scene_sort_key(scene_no: str) -> tuple:
    parts: list[int] = []
    cur = ""
    for ch in scene_no or "":
        if ch.isdigit():
            cur += ch
        else:
            if cur:
                parts.append(int(cur))
                cur = ""
    if cur:
        parts.append(int(cur))
    return tuple(parts) if parts else (0,)


def scenes_by_episode(scenes: Iterable["Scene"]) -> dict[int, list["Scene"]]:
    out: dict[int, list["Scene"]] = {}
    for s in scenes:
        ep = s.episode_no
        if ep is None:
            continue
        out.setdefault(ep, []).append(s)
    for ep in out:
        out[ep].sort(key=lambda s: _scene_sort_key(s.scene_no))
    return out


def normalize_inverse(value: float, low: float, high: float) -> float:
    """把 value 归一化到 [0, 1]，反向（值越低分越高）。

    value <= low → 1.0
    value >= high → 0.0
    """
    if high <= low:
        return 1.0 if value <= low else 0.0
    if value <= low:
        return 1.0
    if value >= high:
        return 0.0
    return 1.0 - (value - low) / (high - low)


def normalize_forward(value: float, low: float, high: float) -> float:
    """正向归一化（值越高分越高）。"""
    if high <= low:
        return 0.0 if value <= low else 1.0
    if value <= low:
        return 0.0
    if value >= high:
        return 1.0
    return (value - low) / (high - low)


def safe_div(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return a / b
