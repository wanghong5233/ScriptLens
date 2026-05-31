"""把 ScoringContext 压缩成 LLM 可读的"剧本素材摘要"。

设计要点（2026-05-31 LLM-first scoring 翻盘）：
- scoring 主链 5 维并行调 LLM judge，每维都需要相同的剧本素材；
  本摘要一次性算好、所有维度共享，避免重复拼装。
- 摘要必须**事实驱动**：场景原文摘录 / 已抽取事件（reward / cliffhanger /
  beat / character_graph）/ 物理度量（场景密度 / 室外比 / 同框人数等）。
- 不写任何评价词、不写任何 rule 推断的结论；LLM 自己看事实评分。
- token 预算：≤ 6K（中文 ≈ 9K 字符），单维 prompt 总 token 控制在 8K 内。

输出形态：纯文本（多段 markdown-like 区块），方便直接拼进 prompt。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from service.scoring.dimensions._common import (
    first_episode_scenes,
    scenes_by_episode,
)
from service.scoring.framework import ScoringContext

if TYPE_CHECKING:
    from service.script_tools.cliffhanger_extractor import CliffhangerEvent
    from service.script_tools.reward_extractor import RewardEvent
    from service.script_tools.scene_repo import Scene


# ============================================================
# 摘要参数（不是业务魔法数字，是"喂 LLM 多少字够它判断"的工程经验值）
# ============================================================

_MAX_FIRST_SCENE_CHARS = 1200      # 首集首场全文
_MAX_PRE_HOOK_SCENE_CHARS = 400    # 前 3 场每场截取
_PRE_HOOK_SCENE_COUNT = 3
_MAX_EP_END_SCENE_CHARS = 350      # 集末场景每场截取
_EP_END_SAMPLE_COUNT = 8           # 集末采样集数
_REWARD_SAMPLE_PER_TYPE = 3        # 每类 reward 采样多少条
_REWARD_CLAIM_MAX_CHARS = 100
_CLIFF_PER_TYPE_SAMPLE = 3
_CLIFF_CLAIM_MAX_CHARS = 100
_TOP_CHARACTER_COUNT = 6
_COMPLIANCE_HIT_SAMPLE = 8


# ============================================================
# 公共入口
# ============================================================


def build_script_summary(ctx: ScoringContext) -> str:
    """把 ScoringContext 转成 LLM-ready 的剧本素材摘要文本。

    各段顺序：元信息 → 节拍三幕 → 首集首场 → 前 3 场 → 集末采样 → reward 事件 →
    cliffhanger 事件 → 主要人物 / 关系 → 制作可行性度量 → 合规命中。
    """
    parts: list[str] = []
    parts.append(_section_meta(ctx))
    parts.append(_section_beat_sheet(ctx))
    parts.append(_section_first_scene(ctx))
    parts.append(_section_pre_hook_chain(ctx))
    parts.append(_section_episode_end_sample(ctx))
    parts.append(_section_reward_events(ctx))
    parts.append(_section_cliffhanger_events(ctx))
    parts.append(_section_characters(ctx))
    parts.append(_section_producibility_metrics(ctx))
    parts.append(_section_compliance_hits(ctx))
    return "\n\n".join(p for p in parts if p)


# ============================================================
# 各 section
# ============================================================


def _section_meta(ctx: ScoringContext) -> str:
    scenes = ctx.scenes or []
    ep_set = {s.episode_no for s in scenes if s.episode_no is not None}
    return (
        "## 剧本元信息\n"
        f"- script_id: {ctx.script_id}\n"
        f"- 总集数（声明）: {ctx.total_episodes}\n"
        f"- 总场景数: {len(scenes)}\n"
        f"- 实际出现集数: {len(ep_set)}（min={min(ep_set, default='-')}, "
        f"max={max(ep_set, default='-')}）"
    )


def _section_beat_sheet(ctx: ScoringContext) -> str:
    bs = ctx.beat_sheet
    if not bs or not getattr(bs, "acts", None):
        return "## 节拍三幕（beat_sheet）\n- 未抽取到节拍数据。"
    lines = ["## 节拍三幕（beat_sheet，由 LLM 上游抽取）"]
    for act in bs.acts:
        act_no = getattr(act, "act_no", getattr(act, "no", "?"))
        act_label = getattr(act, "label", getattr(act, "name", ""))
        lines.append(f"- 第 {act_no} 幕 {act_label}")
        beats = getattr(act, "beats", []) or []
        for b in beats:
            btype = getattr(b, "type", getattr(b, "beat_type", ""))
            bsum = (getattr(b, "summary", "") or "").strip().replace("\n", " ")
            ep_no = getattr(b, "episode_no", None)
            ep_tag = f" [集 {ep_no}]" if ep_no else ""
            lines.append(f"  · {btype}{ep_tag}: {bsum[:80]}")
    return "\n".join(lines)


def _section_first_scene(ctx: ScoringContext) -> str:
    scenes = first_episode_scenes(ctx.scenes)
    if not scenes:
        return "## 首集首场\n- 无场景数据。"
    s = scenes[0]
    text = (s.text or "").strip().replace("\r\n", "\n")
    return (
        "## 首集首场全文（用户 8 秒决策窗口的判断素材）\n"
        f"- scene_id={s.id} scene_no={s.scene_no} label={s.scene_label}\n"
        f"- 人物: {', '.join(s.characters or []) or '(未识别)'}\n"
        f"- 原文摘录（≤ {_MAX_FIRST_SCENE_CHARS} 字）:\n"
        f"```\n{text[:_MAX_FIRST_SCENE_CHARS]}\n```"
    )


def _section_pre_hook_chain(ctx: ScoringContext) -> str:
    scenes = first_episode_scenes(ctx.scenes)
    if len(scenes) <= 1:
        return ""
    lines = [f"## 前 {_PRE_HOOK_SCENE_COUNT} 场钩子链素材"]
    for s in scenes[1:_PRE_HOOK_SCENE_COUNT]:
        text = (s.text or "").strip().replace("\r\n", "\n")
        lines.append(
            f"- scene_no={s.scene_no} label={s.scene_label}\n"
            f"  人物: {', '.join(s.characters or []) or '(未识别)'}\n"
            f"  摘录: {text[:_MAX_PRE_HOOK_SCENE_CHARS]}"
        )
    return "\n".join(lines)


def _section_episode_end_sample(ctx: ScoringContext) -> str:
    eps_map = scenes_by_episode(ctx.scenes)
    if not eps_map:
        return ""
    ep_nos = sorted(eps_map.keys())
    if len(ep_nos) > _EP_END_SAMPLE_COUNT:
        step = max(1, len(ep_nos) // _EP_END_SAMPLE_COUNT)
        sampled = ep_nos[::step][:_EP_END_SAMPLE_COUNT]
        if ep_nos[-1] not in sampled:
            sampled.append(ep_nos[-1])
    else:
        sampled = ep_nos

    lines = [
        f"## 集末场景采样（采样 {len(sampled)} 集，用于判断每集留钩与节奏）"
    ]
    for ep in sampled:
        last_scene = eps_map[ep][-1]
        text = (last_scene.text or "").strip().replace("\r\n", "\n")
        lines.append(
            f"- 第 {ep} 集集末 scene_no={last_scene.scene_no} "
            f"label={last_scene.scene_label}\n"
            f"  摘录: {text[:_MAX_EP_END_SCENE_CHARS]}"
        )
    return "\n".join(lines)


def _section_reward_events(ctx: ScoringContext) -> str:
    rewards: list["RewardEvent"] = list(ctx.reward_events or [])
    if not rewards:
        return "## Reward 事件\n- 上游 reward_extractor 未召回任何爽点事件。"

    per_type: dict[str, list["RewardEvent"]] = defaultdict(list)
    for r in rewards:
        per_type[r.event_type].append(r)

    total = len(rewards)
    eps_set = {r.episode_no for r in rewards if r.episode_no is not None}
    avg_per_ep = total / max(1, len(eps_set))
    coverage = len(eps_set) / max(1, ctx.total_episodes or len(eps_set))

    lines = [
        "## Reward 事件（爽点 / 反转 / 打脸 / 揭露等，已 LLM 二级判定 + verbatim 校验）",
        f"- 总事件数: {total}，覆盖集数 {len(eps_set)} / "
        f"{ctx.total_episodes or '?'}（{coverage:.0%}），均每集 {avg_per_ep:.2f}",
        "- 按类型分布:",
    ]
    for etype, items in sorted(per_type.items(), key=lambda x: -len(x[1])):
        lines.append(f"  · {etype}: {len(items)} 条")

    lines.append("- 各类样本（用户可读 claim）:")
    for etype, items in sorted(per_type.items(), key=lambda x: -len(x[1])):
        for r in items[:_REWARD_SAMPLE_PER_TYPE]:
            claim = (r.claim or "").strip()[:_REWARD_CLAIM_MAX_CHARS]
            ep_tag = f"第 {r.episode_no} 集" if r.episode_no else "?集"
            lines.append(f"  · [{etype}][{ep_tag}] {claim}")
    return "\n".join(lines)


def _section_cliffhanger_events(ctx: ScoringContext) -> str:
    cliffs: list["CliffhangerEvent"] = list(ctx.cliffhangers or [])
    if not cliffs:
        return "## Cliffhanger 事件\n- 上游 cliffhanger_extractor 未召回任何集末留钩。"

    per_type: dict[str, list["CliffhangerEvent"]] = defaultdict(list)
    for c in cliffs:
        per_type[c.cliff_type].append(c)

    eps_with_cliff = {c.episode_no for c in cliffs if c.episode_no is not None}
    coverage = len(eps_with_cliff) / max(1, ctx.total_episodes or len(eps_with_cliff))

    lines = [
        "## Cliffhanger 事件（集末留钩，已 LLM 二级判定 + verbatim quote 校验）",
        f"- 总事件数: {len(cliffs)}，集末有留钩的集数 {len(eps_with_cliff)} / "
        f"{ctx.total_episodes or '?'}（{coverage:.0%}）",
        "- 按 cliff_type 分布:",
    ]
    for ctype, items in sorted(per_type.items(), key=lambda x: -len(x[1])):
        cn = items[0].cliff_type_cn if items else ctype
        lines.append(f"  · {ctype}（{cn}）: {len(items)} 条")

    lines.append("- 各类样本（用户可读 claim + verbatim 引文）:")
    for ctype, items in sorted(per_type.items(), key=lambda x: -len(x[1])):
        cn = items[0].cliff_type_cn if items else ctype
        for c in items[:_CLIFF_PER_TYPE_SAMPLE]:
            claim = (c.claim or "").strip()[:_CLIFF_CLAIM_MAX_CHARS]
            quote = (c.quote_verbatim or "").strip().replace("\n", " ")[:80]
            quote_tag = f"「{quote}」" if c.quote_verified and quote else ""
            lines.append(
                f"  · [{cn}][第 {c.episode_no} 集] {claim} {quote_tag}"
            )
    return "\n".join(lines)


def _section_characters(ctx: ScoringContext) -> str:
    cg = ctx.character_graph
    if not cg or not getattr(cg, "nodes", None):
        return "## 主要人物 / 关系\n- 未抽取到人物图谱。"

    nodes = list(cg.nodes)
    nodes.sort(key=lambda n: -(getattr(n, "appearance_count", 0) or 0))
    top = nodes[:_TOP_CHARACTER_COUNT]
    name_by_id = {n.id: getattr(n, "name", n.id) for n in nodes}

    lines = ["## 主要人物（按出场次数 top）"]
    for n in top:
        role = getattr(n, "role", "")
        appear = getattr(n, "appearance_count", 0)
        goal = (getattr(n, "goal", "") or "").strip()[:60]
        obstacle = (getattr(n, "obstacle", "") or "").strip()[:60]
        lines.append(
            f"- {n.name}（{role}，出场 {appear}）"
            f" 目标: {goal or '?'} | 阻碍: {obstacle or '?'}"
        )

    edges = list(getattr(cg, "edges", []) or [])
    top_ids = {n.id for n in top}
    rel_edges = [e for e in edges if e.source_id in top_ids and e.target_id in top_ids]
    if rel_edges:
        lines.append("- 主要关系:")
        for e in rel_edges[:12]:
            src = name_by_id.get(e.source_id, e.source_id)
            tgt = name_by_id.get(e.target_id, e.target_id)
            lines.append(f"  · {src} -[{e.type}/{e.polarity}]-> {tgt}")
    return "\n".join(lines)


def _section_producibility_metrics(ctx: ScoringContext) -> str:
    scenes = ctx.scenes or []
    if not scenes:
        return "## 制作可行性度量\n- 无场景数据。"

    eps_map = scenes_by_episode(scenes)
    avg_scenes_per_ep = len(scenes) / max(1, len(eps_map) or ctx.total_episodes or 1)

    max_concurrent = 0
    for s in scenes:
        c = len(s.characters or [])
        if c > max_concurrent:
            max_concurrent = c

    char_in_eps: dict[str, set[int]] = defaultdict(set)
    for s in scenes:
        if s.episode_no is None:
            continue
        for c in s.characters or []:
            char_in_eps[c].add(s.episode_no)
    cross_ep_chars = sum(1 for eps in char_in_eps.values() if len(eps) >= 2)

    total_dialogue_lines = 0
    for s in scenes:
        if not s.text:
            continue
        total_dialogue_lines += sum(
            1 for ln in s.text.splitlines() if ln.strip()
        )
    avg_dialogue_per_scene = total_dialogue_lines / max(1, len(scenes))

    lines = [
        "## 制作可行性度量（物理量，用于 PRODUCIBILITY 判断）",
        f"- 平均每集场景数: {avg_scenes_per_ep:.2f}（场景越多 = AI 生成成本越高）",
        f"- 单场最大同时在场角色峰值: {max_concurrent}（峰值越高 = 多角色一致性越烧钱）",
        f"- 跨集复现角色数（出现在 ≥ 2 集）: {cross_ep_chars}（数量大 = 角色 LoRA 复用必要）",
        f"- 平均每场对白行数: {avg_dialogue_per_scene:.1f}（对白密度高 = 嘴型一致性烧钱）",
    ]

    label_counter: Counter[str] = Counter()
    for s in scenes:
        if s.scene_label:
            label_counter[s.scene_label] += 1
    top_labels = label_counter.most_common(8)
    if top_labels:
        lines.append("- 场景类型 top-8（用于判断特殊场景占比）:")
        for label, n in top_labels:
            lines.append(f"  · {label}: {n} 场")
    return "\n".join(lines)


def _section_compliance_hits(ctx: ScoringContext) -> str:
    comp = ctx.compliance
    if not comp:
        return "## 合规命中\n- 未运行合规扫描。"
    hits = list(getattr(comp, "hits", []) or [])
    if not hits:
        return (
            "## 合规命中\n"
            f"- tier={getattr(comp, 'tier', '?')} status={getattr(comp, 'status', '?')}：未命中任何关键词。"
        )

    lines = [
        f"## 合规命中（tier={getattr(comp, 'tier', '?')} "
        f"status={getattr(comp, 'status', '?')} score={getattr(comp, 'score', '?')}）",
    ]
    for h in hits[:_COMPLIANCE_HIT_SAMPLE]:
        if not isinstance(h, dict):
            continue
        level = h.get("level", "?")
        category = h.get("category", "?")
        term = h.get("matched_term", "")
        excerpt = (h.get("excerpt") or "").strip().replace("\n", " ")[:80]
        lines.append(f"- [{level}][{category}] 命中「{term}」: {excerpt}")
    if len(hits) > _COMPLIANCE_HIT_SAMPLE:
        lines.append(f"- ... 还有 {len(hits) - _COMPLIANCE_HIT_SAMPLE} 条未列出")
    return "\n".join(lines)


__all__ = ["build_script_summary"]
