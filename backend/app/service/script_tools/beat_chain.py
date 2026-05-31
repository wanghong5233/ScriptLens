"""故事节拍抽取：规则锚点 + LLM 标注的混合方案。

设计原则（v3 重写，2026-05-30）：
  1. **规则层**先从 reward 曲线 + scene_no 进度切出 3 幕骨架，再为每幕预选
     1-2 个候选锚点（opening / inciting / midpoint / twist / climax / closing）。
     候选锚点是真实存在的 scene，**不靠 LLM 凭空写 UUID**。
  2. **LLM 层**仅给候选锚点打 type / 写 summary。LLM 不能新增锚点、不能
     改 anchor 位置。LLM 用 ``seq`` 整数（候选编号）作为引用，UUID 由代码
     在 LLM 出参后映射回。
  3. **rule fallback**：LLM 整段失败时，规则层把候选锚点的 ``scene_label``
     当 summary 直接落库，永远保证 3 幕 ≥ 1 beat，不再出现"0 节拍"屏。
  4. **可解释**：``BeatSheet.source`` 暴露 ``"llm"`` / ``"rule_fallback"`` /
     ``"hybrid"``，前端 / 单测可观察 LLM 是否真的命中。

参考：
  - Gorinski & Lapata 2015《Movie Plot Structure Analysis》—— 用 supervised
    attention 找节拍；规则锚点对应他们的"位置先验"。
  - Save the Cat 15-beat sheet —— 短剧不强套，但 opening / inciting /
    midpoint / climax / closing 五点是行业最大公约数。
  - Aristotle 三段式 + tension curve —— act1 / act2 / act3 的 25/85% 切分
    来自 Field 的《Screenplay》经验值。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from service.script_tools.reward_extractor import RewardEvent
from service.script_tools.scene_repo import Scene, get_all_scenes
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


BeatType = str

# 三幕切分位（Field《Screenplay》经验值，短剧 100 集仍适用）。
# act1: 0..25%；act2: 25..85%；act3: 85..100%。
_ACT1_END_RATIO = 0.25
_ACT2_END_RATIO = 0.85

# 节拍类型白名单。LLM 出 type 不在这里 → 用规则给的 type_hint 兜底。
_ALLOWED_BEATS = {"opening", "inciting", "midpoint", "climax", "closing", "twist", "reward"}

# Summary 上限。50 字与 Field 的 logline 习惯一致；超出截断。
_SUMMARY_MAX_LEN = 50

# 单场上下文截断。LLM prompt 控 token，不影响 anchor 选取。
_SCENE_TEXT_LIMIT = 600

# 给 LLM 看的候选锚点最多 12 个。再多 prompt 太胖，且 act3 不需要太多。
_MAX_CANDIDATES = 12


@dataclass
class BeatNode:
    type: BeatType
    summary: str
    anchor_scene_id: str

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "summary": self.summary,
            "anchor_scene_id": self.anchor_scene_id,
        }


@dataclass
class BeatAct:
    act: int
    title: str
    scene_range: List[str] = field(default_factory=list)
    beats: List[BeatNode] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "act": self.act,
            "title": self.title,
            "scene_range": self.scene_range,
            "beats": [b.to_dict() for b in self.beats],
        }


@dataclass
class BeatSheet:
    acts: List[BeatAct] = field(default_factory=list)
    # observability：调用方可记录 source 用于 BI / 单测
    source: str = "llm"
    fallback_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"acts": [a.to_dict() for a in self.acts]}


@dataclass
class _CandidateAnchor:
    """规则层预选的候选锚点。

    seq: 给 LLM 看的 1-based 整数 ID。LLM 用 seq 引用 anchor，避免抄 UUID 的脆弱链路。
    """

    seq: int
    act: int
    type_hint: BeatType
    scene: Scene


_DEFAULT_ACT_TITLES = {1: "开局", 2: "发展", 3: "收束"}


_SYSTEM_PROMPT = """你是中文短剧剧本统筹，负责把长剧本整理成「三幕故事骨架」。

你的任务是给系统已经预选好的候选锚点写 summary、确认 type。
**不要新增锚点、不要更改锚点位置、不要发明 seq**。

对编剧、选品、审核三类用户都要友好：summary 让人一眼看懂"这一拍承担什么故事功能"，
不要写"主角进入 xxx 场景"这种空话，也不要直接抄一句台词。
"""


_USER_PROMPT = """下面是从剧本中**规则层预选**的候选锚点。请补充 summary 和 type。

【候选锚点】
{candidates_block}

【规则】
1. 三幕已固定：1=开局，2=发展，3=收束。
2. 你只能从上面候选 ``seq`` 里挑锚点，**不允许新增 seq、不允许重复 seq、不允许引用没出现的 seq**。
3. ``type`` 必须是 opening/inciting/midpoint/climax/closing/twist/reward 之一；
   候选行已给出 ``type_hint``，没把握就用 ``type_hint``。
4. 每幕 1-3 个 beat。**每一幕至少 1 个 beat**。
5. summary 字数与格式：
   - 普通 beat：≤ 50 字
   - **climax / closing 必须 ≥ 12 字**——这两类是前端速览速读位（钩子/高潮/结局）的内容来源，
     写「关键场」「中点」这种标签型残留 或 「收束：帝王居，日内」这种 scene heading 残留
     会被前端过滤丢弃。写**完整概括句**，例如：
     - climax 好范例：「陆沉舟腾龙宴揭穿质疑，公开青帮龙首身份击败赵鑫」
     - closing 好范例：「陆沉舟向叶云浅公开求婚，全剧以双向救赎收尾」
6. summary 写"这一拍承担的故事功能 + 关键人物动作"，不要直接摘台词，不要写场景头（日内/日外/夜内等）。
7. 输出**一个 JSON 对象**，不要 markdown / 代码块 / 解释。

【输出 JSON】
{{
  "acts": [
    {{
      "act": 1,
      "title": "开局",
      "beats": [
        {{"seq": <候选 seq>, "type": "opening|inciting|...", "summary": "≤50字"}}
      ]
    }},
    {{"act": 2, ...}},
    {{"act": 3, ...}}
  ]
}}
"""


async def extract_beat_sheet(
    *,
    script_id: str,
    reward_events: Optional[List[RewardEvent]] = None,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> BeatSheet:
    """抽取三幕节拍。

    流程：
      1. 取全剧 scenes（已按集→场→行排序）。
      2. 规则层切 3 幕，每幕预选 1-2 个候选锚点。
      3. LLM 用候选锚点 seq 写 summary / 标 type；失败则规则层兜底。
      4. 输出 BeatSheet（永远保证 3 幕，每幕 ≥ 1 beat）。
    """
    caller = caller or LlmCaller()
    scenes = get_all_scenes(script_id=script_id, engine=engine)
    if not scenes:
        raise ValueError(f"script_id={script_id} 没有可分析的场景")

    candidates = _derive_candidate_anchors(scenes, reward_events or [])
    if not candidates:
        # 极端情况：scenes 不足 2 场。直接 rule fallback 给 1 幕 1 beat。
        return _rule_fallback(scenes, reward_events or [], reason="too_few_scenes")

    try:
        sheet = await _enrich_via_llm(candidates, caller)
        return sheet
    except ScoreLLMError as exc:
        logger.warning("beat_chain: LLM enrichment failed, fall back to rule. err=%s", exc)
        return _rule_fallback(scenes, reward_events or [], reason=f"llm_error:{type(exc).__name__}")


# ---------------------------------------------------------------------------
# 规则层：三幕切分 + 候选锚点
# ---------------------------------------------------------------------------


def _derive_candidate_anchors(
    scenes: List[Scene],
    reward_events: List[RewardEvent],
) -> List[_CandidateAnchor]:
    """给三幕预选候选锚点。

    每幕至少 1 个候选；总候选数 ≤ ``_MAX_CANDIDATES``。锚点优先用 reward 峰值
    场，其次用幕内位置（开始/中点/结尾）。
    """
    n = len(scenes)
    if n < 2:
        return []

    # reward score 累计：同一场可能有多个 reward 事件，求和当强度。
    reward_by_scene: Dict[str, float] = defaultdict(float)
    for ev in reward_events:
        sid = getattr(ev, "scene_id", None)
        if sid is None:
            continue
        score = float(getattr(ev, "score", 1.0) or 1.0)
        reward_by_scene[sid] += score

    act1_end = max(1, int(round(n * _ACT1_END_RATIO)))
    act2_end = max(act1_end + 1, int(round(n * _ACT2_END_RATIO)))
    act2_end = min(act2_end, n - 1)

    act1_scenes = scenes[:act1_end]
    act2_scenes = scenes[act1_end:act2_end]
    act3_scenes = scenes[act2_end:]

    chosen: List[Tuple[BeatType, int, Scene]] = []
    seen_ids: set = set()

    def _push(type_hint: BeatType, act: int, scene: Optional[Scene]) -> None:
        if scene is None or scene.id in seen_ids:
            return
        chosen.append((type_hint, act, scene))
        seen_ids.add(scene.id)

    # ---- Act 1 ----
    _push("opening", 1, act1_scenes[0] if act1_scenes else None)
    if len(act1_scenes) >= 2:
        # inciting 取 act1 后半 reward 最高的场（避开 opening 同场）。
        candidate_pool = act1_scenes[1:] or act1_scenes
        _push("inciting", 1, _pick_reward_peak(candidate_pool, reward_by_scene))

    # ---- Act 2 ----
    if act2_scenes:
        mid_anchor = _pick_reward_peak(act2_scenes, reward_by_scene)
        _push("midpoint", 2, mid_anchor)
        # 中点之后还有戏 → 加一个 twist 锚点
        if mid_anchor is not None:
            try:
                mid_idx = act2_scenes.index(mid_anchor)
            except ValueError:
                mid_idx = len(act2_scenes) // 2
            after = act2_scenes[mid_idx + 1 :]
            if after:
                _push("twist", 2, _pick_reward_peak(after, reward_by_scene))

    # ---- Act 3 ----
    if act3_scenes:
        # climax 取 act3 中段 reward 最高场（避免直接是最末场被双计）。
        if len(act3_scenes) >= 2:
            climax_pool = act3_scenes[:-1]
            _push("climax", 3, _pick_reward_peak(climax_pool, reward_by_scene))
        else:
            _push("climax", 3, act3_scenes[0])
        # closing 取最末场
        if len(act3_scenes) >= 1:
            _push("closing", 3, act3_scenes[-1])

    # 兜底：如果哪一幕没拿到任何锚点（场太少），强制各取一场。
    have_acts = {act for _, act, _ in chosen}
    if 1 not in have_acts and act1_scenes:
        _push("opening", 1, act1_scenes[0])
    if 2 not in have_acts and act2_scenes:
        _push("midpoint", 2, act2_scenes[len(act2_scenes) // 2])
    if 3 not in have_acts and act3_scenes:
        _push("closing", 3, act3_scenes[-1])

    chosen = chosen[:_MAX_CANDIDATES]
    return [
        _CandidateAnchor(seq=idx + 1, act=act, type_hint=type_hint, scene=scene)
        for idx, (type_hint, act, scene) in enumerate(chosen)
    ]


def _pick_reward_peak(
    pool: List[Scene],
    reward_by_scene: Dict[str, float],
) -> Optional[Scene]:
    """从 pool 选 reward 累计分最高的场；全 0 时退化到 pool 中点。"""
    if not pool:
        return None
    scored = [(reward_by_scene.get(s.id, 0.0), idx, s) for idx, s in enumerate(pool)]
    scored.sort(key=lambda t: (-t[0], t[1]))
    top_score = scored[0][0]
    if top_score <= 0.0:
        return pool[len(pool) // 2]
    return scored[0][2]


# ---------------------------------------------------------------------------
# LLM 层：给候选锚点写 summary
# ---------------------------------------------------------------------------


async def _enrich_via_llm(
    candidates: List[_CandidateAnchor],
    caller: LlmCaller,
) -> BeatSheet:
    prompt = _USER_PROMPT.format(candidates_block=_render_candidates(candidates))
    resp = await caller.call_json(
        prompt=prompt,
        tier=ModelTier.PRIMARY,
        system_message=_SYSTEM_PROMPT,
        temperature=0.2,
        max_tokens=TokenBudget.BEAT_SHEET,
    )
    parsed = resp.parsed if isinstance(resp.parsed, dict) else None
    if parsed is None:
        raise ScoreLLMError("beat_chain: LLM 返回非 JSON object")

    raw_acts = parsed.get("acts")
    if not isinstance(raw_acts, list):
        raise ScoreLLMError("beat_chain: missing acts list")

    by_seq = {c.seq: c for c in candidates}
    beats_by_act: Dict[int, List[BeatNode]] = {1: [], 2: [], 3: []}
    used_seq: set = set()

    for raw in raw_acts:
        if not isinstance(raw, dict):
            continue
        act_no = raw.get("act")
        if act_no not in (1, 2, 3):
            continue
        for raw_beat in raw.get("beats") or []:
            if not isinstance(raw_beat, dict):
                continue
            seq = raw_beat.get("seq")
            try:
                seq_int = int(seq)
            except (TypeError, ValueError):
                continue
            if seq_int in used_seq:
                continue
            anchor = by_seq.get(seq_int)
            if anchor is None or anchor.act != act_no:
                continue
            beat_type = str(raw_beat.get("type") or "").strip()
            if beat_type not in _ALLOWED_BEATS:
                beat_type = anchor.type_hint
            summary = str(raw_beat.get("summary") or "").strip()
            if not summary:
                summary = _scene_label_summary(anchor.scene, anchor.type_hint)
            beats_by_act[act_no].append(
                BeatNode(
                    type=beat_type,
                    summary=summary[:_SUMMARY_MAX_LEN],
                    anchor_scene_id=anchor.scene.id,
                )
            )
            used_seq.add(seq_int)

    fallback_reasons: List[str] = []
    # 兜底：LLM 漏掉某一幕 / 该幕 0 beat 时，用候选锚点直接补一条。
    for act_no in (1, 2, 3):
        if beats_by_act[act_no]:
            continue
        backup = next((c for c in candidates if c.act == act_no), None)
        if backup is None:
            continue
        beats_by_act[act_no].append(
            BeatNode(
                type=backup.type_hint,
                summary=_scene_label_summary(backup.scene, backup.type_hint),
                anchor_scene_id=backup.scene.id,
            )
        )
        fallback_reasons.append(f"act{act_no}_filled_by_rule")

    acts = _build_acts(candidates, beats_by_act)
    source = "hybrid" if fallback_reasons else "llm"
    return BeatSheet(acts=acts, source=source, fallback_reasons=fallback_reasons)


def _render_candidates(candidates: List[_CandidateAnchor]) -> str:
    blocks: List[str] = []
    for c in candidates:
        scene = c.scene
        text = (scene.text or "")[:_SCENE_TEXT_LIMIT]
        if scene.text and len(scene.text) > _SCENE_TEXT_LIMIT:
            text += "..."
        chars = ",".join((scene.characters or [])[:6])
        blocks.append(
            f"[seq={c.seq}] [act={c.act}] [type_hint={c.type_hint}] "
            f"[第{scene.episode_no or '?'}集 · {scene.scene_no} · {scene.scene_label}] "
            f"[人物:{chars}]\n{text}"
        )
    return "\n\n---\n\n".join(blocks)


def _scene_label_summary(scene: Scene, type_hint: BeatType) -> str:
    """rule fallback summary：场标签 + 节拍类型，控制在 50 字内。"""
    label = (scene.scene_label or "").strip() or "关键场"
    type_zh = {
        "opening": "开端",
        "inciting": "钩子",
        "midpoint": "中点反转",
        "twist": "二次反转",
        "climax": "高潮",
        "closing": "收束",
        "reward": "爽点",
    }.get(type_hint, "节拍")
    summary = f"{type_zh}：{label}"
    return summary[:_SUMMARY_MAX_LEN]


def _build_acts(
    candidates: List[_CandidateAnchor],
    beats_by_act: Dict[int, List[BeatNode]],
) -> List[BeatAct]:
    """根据每幕的 candidates 决定 scene_range，再装配 BeatAct。"""
    acts: List[BeatAct] = []
    for act_no in (1, 2, 3):
        act_anchors = [c for c in candidates if c.act == act_no]
        scene_range: List[str] = []
        if act_anchors:
            scene_range = [act_anchors[0].scene.id, act_anchors[-1].scene.id]
            if scene_range[0] == scene_range[1]:
                scene_range = [scene_range[0]]
        acts.append(
            BeatAct(
                act=act_no,
                title=_DEFAULT_ACT_TITLES[act_no],
                scene_range=scene_range,
                beats=beats_by_act.get(act_no, []),
            )
        )
    return acts


# ---------------------------------------------------------------------------
# rule fallback：LLM 整段不可用时
# ---------------------------------------------------------------------------


def _rule_fallback(
    scenes: List[Scene],
    reward_events: List[RewardEvent],
    *,
    reason: str,
) -> BeatSheet:
    """规则层独立兜底：保证返回 3 幕 ≥ 1 beat 的最小 sheet。"""
    candidates = _derive_candidate_anchors(scenes, reward_events)
    beats_by_act: Dict[int, List[BeatNode]] = {1: [], 2: [], 3: []}
    for c in candidates:
        beats_by_act[c.act].append(
            BeatNode(
                type=c.type_hint,
                summary=_scene_label_summary(c.scene, c.type_hint),
                anchor_scene_id=c.scene.id,
            )
        )

    # 最后一道保险：场太少 → 至少给 act 1 留一个 opening。
    if not any(beats_by_act.values()):
        if scenes:
            beats_by_act[1].append(
                BeatNode(
                    type="opening",
                    summary=_scene_label_summary(scenes[0], "opening"),
                    anchor_scene_id=scenes[0].id,
                )
            )

    acts = _build_acts(candidates, beats_by_act)
    return BeatSheet(acts=acts, source="rule_fallback", fallback_reasons=[reason])
