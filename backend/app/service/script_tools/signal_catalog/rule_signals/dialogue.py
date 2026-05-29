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

from service.script_tools.signal_catalog import SignalContext, SignalValue, register_signal


def _dialogue_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "：" in line or ":" in line:
            out.append(line)
    return out


@register_signal("dialogue_density", scope="script", source="rule", primary_dim="dialogue")
def compute_dialogue_density(ctx: SignalContext) -> SignalValue:
    values = [value for value in ctx.plot_values("dialogue_density") if value and value != "none"]
    if not values:
        return SignalValue(
            key="dialogue_density",
            value={"dense_ratio": 0.0},
            score=3.0,
            source="rule",
            confidence=0.3,
        )
    dense = sum(1 for value in values if value == "dense")
    moderate = sum(1 for value in values if value == "moderate")
    sparse = sum(1 for value in values if value == "sparse")
    weighted = (dense * 1.0 + moderate * 0.75 + sparse * 0.45) / len(values)
    score = round(max(1.0, min(10.0, weighted * 10.0)), 2)
    return SignalValue(
        key="dialogue_density",
        value={
            "dense_ratio": round(dense / len(values), 4),
            "moderate_ratio": round(moderate / len(values), 4),
            "sparse_ratio": round(sparse / len(values), 4),
        },
        score=score,
        source="rule",
        confidence=0.8,
    )


@register_signal("topic_focus", scope="script", source="rule", primary_dim="dialogue")
def compute_topic_focus(ctx: SignalContext) -> SignalValue:
    qa_values = [value for value in ctx.episode_values("qa_relevance") if value and value != "none"]
    if qa_values:
        good = sum(1 for value in qa_values if value in {"high", "good", "relevant"})
        ratio = good / len(qa_values)
        score = round(max(1.0, min(10.0, ratio * 10.0)), 2)
        return SignalValue(
            key="topic_focus",
            value={"qa_relevant_ratio": round(ratio, 4)},
            score=score,
            source="rule",
            confidence=0.6,
        )

    # fallback: estimate focus by lexical overlap between dialogue lines in the same scene.
    dialogue_lengths = [len(_dialogue_lines(str(scene.get("text") or ""))) for scene in ctx.scenes]
    if not dialogue_lengths:
        return SignalValue(
            key="topic_focus",
            value={"qa_relevant_ratio": 0.0},
            score=3.0,
            source="rule",
            confidence=0.2,
        )
    scenes_with_dialogue = sum(1 for length in dialogue_lengths if length >= 2)
    ratio = scenes_with_dialogue / len(dialogue_lengths)
    score = round(max(1.0, min(10.0, ratio * 10.0)), 2)
    return SignalValue(
        key="topic_focus",
        value={"dialogue_scene_ratio": round(ratio, 4)},
        score=score,
        source="rule",
        confidence=0.45,
    )


@register_signal("voiceover_fit", scope="script", source="rule", primary_dim="dialogue")
def compute_voiceover_fit(ctx: SignalContext) -> SignalValue:
    values = [value for value in ctx.plot_values("voiceover_type") if value]
    if not values:
        return SignalValue(
            key="voiceover_fit",
            value={"narrator_ratio": 0.0},
            score=6.0,
            source="rule",
            confidence=0.3,
        )
    narrator_ratio = sum(1 for value in values if value == "narrator") / len(values)
    mixed_ratio = sum(1 for value in values if value == "mixed") / len(values)
    score = round(max(1.0, min(10.0, (1.0 - narrator_ratio * 0.8 + mixed_ratio * 0.2) * 10.0)), 2)
    return SignalValue(
        key="voiceover_fit",
        value={"narrator_ratio": round(narrator_ratio, 4), "mixed_ratio": round(mixed_ratio, 4)},
        score=score,
        source="rule",
        confidence=0.7,
    )


@register_signal("brevity_ratio", scope="script", source="rule", primary_dim="dialogue")
def compute_brevity_ratio(ctx: SignalContext) -> SignalValue:
    line_lengths: list[int] = []
    for scene in ctx.scenes:
        for line in _dialogue_lines(str(scene.get("text") or "")):
            line_lengths.append(len(line))
    if not line_lengths:
        return SignalValue(
            key="brevity_ratio",
            value={"avg_line_length": 0.0},
            score=3.0,
            source="rule",
            confidence=0.2,
        )
    avg = sum(line_lengths) / len(line_lengths)
    # short drama dialogue usually performs best around 18-36 chars per line.
    if 18 <= avg <= 36:
        score = 9.0
    elif 12 <= avg <= 48:
        score = 7.0
    elif 8 <= avg <= 60:
        score = 5.0
    else:
        score = 3.0
    return SignalValue(
        key="brevity_ratio",
        value={"avg_line_length": round(avg, 3)},
        score=score,
        source="rule",
        confidence=0.75,
    )
