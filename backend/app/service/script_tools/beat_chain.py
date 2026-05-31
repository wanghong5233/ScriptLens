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
import re
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

# Summary 下限：太短的 LLM 输出（< 8 字）几乎一定是 type 标签残留，需要 reject。
_SUMMARY_MIN_LEN = 8

# 单场上下文截断。LLM prompt 控 token，不影响 anchor 选取。
_SCENE_TEXT_LIMIT = 600

# v3.7.4: 候选锚点上限改为数据驱动。
#
# 业内对照：
#   - Save the Cat 15-beat sheet（电影 120 分钟基准）—— 15 个核心节拍
#   - Truby 22 Building Blocks —— 22 步法
#   - Linda Aronson 短剧节拍密度调研：每 6-10 分钟 1 个核心 beat（短剧 1 集 ~ 6 分钟）
#
# 我们按场数计算，避免 100 集长剧硬塞 12 个候选导致 act 后半段全空：
#   - n_scenes ≤ 30   : 6 个候选（act1:1, act2:3, act3:2）
#   - n_scenes 30-80  : 9 个候选（act1:2, act2:5, act3:2）
#   - n_scenes 80-200 : 12 个候选（act1:2, act2:7, act3:3）
#   - n_scenes ≥ 200  : 15 个候选（act1:3, act2:9, act3:3）
_CANDIDATES_BY_SCALE: List[Tuple[int, int]] = [
    (30, 6),
    (80, 9),
    (200, 12),
]
_MAX_CANDIDATES_UPPER = 15  # 极长剧上限

# 每幕 beat 数量上限（LLM prompt 看到的，**数据驱动**）。
# 短剧不强套 Save the Cat 15 节拍，但 act2（发展）总是节拍最密集的段，
# 应该给更多空间；act1（开局）和 act3（收束）相对紧凑。
def _max_beats_per_act(n_scenes: int) -> Dict[int, int]:
    """根据剧本场数算每幕 beat 数上限（数据驱动，非硬编码）。

    短剧场景密度典型：30 场 = 5 集，100 场 = 20 集，300 场 = 60 集。
    经验配比：act1 ≈ 1/4 容量，act2 ≈ 1/2，act3 ≈ 1/4。
    """
    if n_scenes <= 30:
        return {1: 2, 2: 3, 3: 2}
    if n_scenes <= 80:
        return {1: 2, 2: 4, 3: 2}
    if n_scenes <= 200:
        return {1: 3, 2: 5, 3: 3}
    return {1: 3, 2: 6, 3: 3}


def _candidate_cap(n_scenes: int) -> int:
    """根据剧本场数算候选锚点总上限。"""
    for ceil, cap in _CANDIDATES_BY_SCALE:
        if n_scenes <= ceil:
            return cap
    return _MAX_CANDIDATES_UPPER

# v3.7.2 低质量 summary 检测：LLM 经常输出「X：场景头」类的标签残留（如「开端：关键场」
# 「中点反转：办公室日内」「高潮：卧室日内」），这些不是真正的节拍概括，
# 应该被 reject + fallback 到带剧情信息的 rule summary。
_TYPE_PREFIX = "(开端|开场|钩子|中点|中点反转|二次反转|高潮|收束|结局|爽点|反转|节拍)"
_SCENE_HEAD = "(关键场|过场|普通场|日内|日外|夜内|夜外)"
_BAD_BEAT_SUMMARY_RE = re.compile(
    rf"^{_TYPE_PREFIX}[:：][\s]*[^，。；,;\.]{{0,12}}{_SCENE_HEAD}\s*$"
)
# 兼容更宽松的「X：≤4 字关键词」纯标签形态
_LABEL_ONLY_SUMMARY_RE = re.compile(rf"^{_TYPE_PREFIX}[:：][\s]*\S{{1,8}}\s*$")


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
    # observability：调用方可记录 source 用于 BI / 单测 / 前端降级提示
    # source: "llm" | "hybrid" | "rule_fallback"
    source: str = "llm"
    fallback_reasons: List[str] = field(default_factory=list)
    # W1.9 (2026-05-31)：被规则替换的 beat 数（LLM 输出垃圾 summary 被 reject 后改用
    # rule fallback）。如果 > 0，整体 source 必须降级为 hybrid。
    rule_replaced_beat_count: int = 0

    def to_dict(self) -> dict:
        # W1.9 (2026-05-31)：to_dict 必须含 provenance 字段，否则上游 ChainResult
        # 无法判断是「全 LLM」还是「部分规则补」。旧实现只输出 acts，前端永远显示绿
        # 灯，用户被欺骗。
        return {
            "acts": [a.to_dict() for a in self.acts],
            "source": self.source,
            "fallback_reasons": list(self.fallback_reasons),
            "rule_replaced_beat_count": self.rule_replaced_beat_count,
        }


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
4. **每幕 beat 数量上限（数据驱动）**：act1={max_act1}，act2={max_act2}，act3={max_act3}。
   **每一幕至少 1 个 beat**，不允许超过上限；不需要凑数。
5. summary 字数与格式（**硬性规则，违反会被后端 reject 丢弃**）：
   - 所有 beat：**8-50 字之间**，写完整概括句
   - climax / closing 必须 ≥ 12 字
6. summary 必须包含「人物 + 动作 + 后果」三要素之一，**严禁纯标签 / 场景头残留**：
   - ❌ 错误反例（这些会被自动丢弃）：
     * "开端：关键场"           ← 纯 type + 标签
     * "中点反转：办公室日内"     ← scene heading 残留
     * "高潮：卧室日内"          ← scene heading 残留
     * "收束：帝王居，日内"       ← 同上
     * "节拍"                  ← 纯类型词
   - ✅ 正确范例：
     * opening：「姜栀枝伪装柔弱混入修罗场，引出反派注意」
     * inciting：「裴鹤年误以为姜栀枝暗恋自己十年，主动出击」
     * midpoint：「身份反转：姜栀枝揭开真面目，全员震惊」
     * climax：「陆沉舟揭穿赵鑫身份，腾龙宴公开击败对手」
     * closing：「双向救赎：陆沉舟向叶云浅公开求婚」
7. 严禁输出场景头关键词「日内 / 日外 / 夜内 / 夜外 / 内景 / 外景」。
8. 输出**一个 JSON 对象**，不要 markdown / 代码块 / 解释。

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

    # v3.7.4: 每幕 beat 上限按场数算（数据驱动），传给 LLM prompt
    max_beats = _max_beats_per_act(len(scenes))
    logger.info(
        "beat_chain: n_scenes=%d candidate_cap=%d max_beats_per_act=%s",
        len(scenes),
        _candidate_cap(len(scenes)),
        max_beats,
    )

    try:
        sheet = await _enrich_via_llm(candidates, caller, max_beats_per_act=max_beats)
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

    # v3.7.4: 候选锚点上限根据剧本场数算（数据驱动）。
    chosen = chosen[: _candidate_cap(n)]
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
    *,
    max_beats_per_act: Optional[Dict[int, int]] = None,
) -> BeatSheet:
    caps = max_beats_per_act or {1: 3, 2: 5, 3: 3}
    prompt = _USER_PROMPT.format(
        candidates_block=_render_candidates(candidates),
        max_act1=caps.get(1, 3),
        max_act2=caps.get(2, 5),
        max_act3=caps.get(3, 3),
    )
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
    # W1.9：统计 LLM 给了 summary 但被 reject 改用 rule summary 的 beat 数
    rule_replaced_summary_count = 0

    for raw in raw_acts:
        if not isinstance(raw, dict):
            continue
        act_no = raw.get("act")
        if act_no not in (1, 2, 3):
            continue
        # v3.7.2: LLM 偶尔会把 "beats" 写成数字（如 `"beats": 3`）或字符串而非数组。
        # 必须显式 isinstance list 校验，否则会触发 'int' object is not iterable。
        raw_beats = raw.get("beats")
        if not isinstance(raw_beats, list):
            logger.warning(
                "beat_chain: act=%s 的 beats 字段不是 list (type=%s value=%r)，跳过该幕，走规则兜底",
                act_no,
                type(raw_beats).__name__,
                raw_beats if not isinstance(raw_beats, str) else raw_beats[:60],
            )
            continue
        for raw_beat in raw_beats:
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
            # v3.7.2：LLM 给的 summary 命中低质量模式（场景头残留 / 纯 type 标签 / 过短）
            # 直接 reject，回退到带剧情正文的规则 summary，避免速览速读位塞垃圾。
            beat_replaced = False
            if not summary or _is_low_quality_summary(summary):
                if summary:
                    logger.info(
                        "beat_chain: rejected low-quality LLM summary %r (seq=%s type=%s) → rule fallback",
                        summary[:40],
                        seq_int,
                        beat_type,
                    )
                summary = _scene_label_summary(anchor.scene, anchor.type_hint)
                beat_replaced = True
                rule_replaced_summary_count += 1
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
    # W1.9：rule_replaced_beat_count = 「LLM 给了 summary 但被 reject 重写」 +
    # 「整幕缺失 act 补位」。任一 > 0 即整体降级为 hybrid。
    rule_replaced_total = rule_replaced_summary_count + len(
        [r for r in fallback_reasons if r.startswith("act") and r.endswith("_filled_by_rule")]
    )
    if rule_replaced_summary_count > 0:
        fallback_reasons.append(
            f"beats_rule_replaced_summary_count={rule_replaced_summary_count}"
        )
    source = "hybrid" if fallback_reasons else "llm"
    return BeatSheet(
        acts=acts,
        source=source,
        fallback_reasons=fallback_reasons,
        rule_replaced_beat_count=rule_replaced_total,
    )


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


_TYPE_ZH = {
    "opening": "开端",
    "inciting": "钩子",
    "midpoint": "中点反转",
    "twist": "二次反转",
    "climax": "高潮",
    "closing": "收束",
    "reward": "爽点",
}


# v3.7.4: 跳过这些"非剧情"行（场号 / 集号 / 人物列表 / 纯场景头）。
# 实际剧本头部典型：
#   第一集
#   1-1
#   酒店夜内
#   人物：姜栀枝裴鹤年
#   △裴鹤年被蒙着双眼，姜栀枝解开裴鹤年衣服扣子
#   系统VO（字幕）：已锁定载体，宿主灵魂投放中
# 前 4 行都是 metadata，第 5 行（△ 动作行）才是真正的剧情。
_SCENE_HEADER_LINE_RE = re.compile(
    r"^("
    r"第[一二三四五六七八九十百千零0-9]+[集场]\s*$|"  # 第八十四集
    r"\d+-\d+\s*$|"                                  # 84-1
    r"\d+-\d+\s+\S{1,12}\s*$|"                       # 84-1 卧室日内
    r"人物[:：].*$|"                                  # 人物：姜栀枝裴鹤年
    r"场景[:：].*$|"                                  # 场景：办公室
    r"(INT|EXT|S)\.\s*.*$|"                          # INT. 卧室
    r"内景[\s:：].*$|外景[\s:：].*$|"
    r"\S{1,8}(日内|日外|夜内|夜外)\s*$"                # 卧室日内
    r")"
)
# 动作行（△ 开头）和对白行（角色（情绪）：xxx）都是合法的剧情正文起点。
_ACTION_LINE_RE = re.compile(r"^[△▲■◆●▼]")
_DIALOGUE_LINE_RE = re.compile(r"^\S{1,12}(（[^）]*）)?[:：]")


def _extract_plot_excerpt(scene_text: str) -> Optional[str]:
    """从 scene.text 抽 ≥ 12 字的剧情正文片段。

    策略：
      1. 按行逐句扫描，跳过场号 / 集号 / 人物列表 / 场景头 / 空行
      2. 命中第 1 个"动作行（△…）"或"对白行（角色：…）"开始累积内容
      3. 累计到 ≥ 12 字时返回，最多取 ≤ 40 字
      4. 没找到合法行 → 返回 None，让上层走更差的 fallback
    """
    if not scene_text:
        return None
    plot_chunks: List[str] = []
    accumulated = 0
    for raw_line in scene_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _SCENE_HEADER_LINE_RE.match(line):
            continue
        # 系统 VO 字幕这种 narration 也算剧情（短剧很常见）
        is_action = bool(_ACTION_LINE_RE.match(line))
        is_dialogue = bool(_DIALOGUE_LINE_RE.match(line))
        is_plot = is_action or is_dialogue or len(line) >= 10
        if not is_plot:
            continue
        # 去掉动作行的标记符号
        clean = re.sub(r"^[△▲■◆●▼]\s*", "", line).strip()
        # 对白行抽取「角色：内容」的角色 + 内容（保留语义）
        clean = re.sub(r"^(\S{1,12})（[^）]*）", r"\1", clean)
        if not clean:
            continue
        plot_chunks.append(clean)
        accumulated += len(clean)
        if accumulated >= 28:
            break
    if not plot_chunks:
        return None
    joined = "；".join(plot_chunks)
    # 去掉句末标点叠加
    joined = re.sub(r"[，。！？；,;.]+$", "", joined).strip()
    if len(joined) < 12:
        return None
    return joined[:38]


def _scene_label_summary(scene: Scene, type_hint: BeatType) -> str:
    """rule fallback summary（v3.7.4 重写）：彻底告别"X：人物 关键场"垃圾格式。

    新策略：
      1. 优先用 ``_extract_plot_excerpt`` 从 scene.text 抽 12-38 字真实剧情
      2. 找不到剧情正文 → 用 type + 第 ep/sc + 人物，但**不带"关键场"死词**
      3. 没人物没正文 → 明确标注「需手动审阅」让运营 / 用户知道这是异常场，
         而不是骗过用户以为这是真节拍
    """
    type_zh = _TYPE_ZH.get(type_hint, "节拍")
    plot = _extract_plot_excerpt(scene.text or "")
    if plot:
        budget = _SUMMARY_MAX_LEN - len(type_zh) - 1
        return f"{type_zh}：{plot[:budget]}"
    # 没抓到剧情正文：给出诚实的"占位"，明确标注异常，便于回查
    ep = scene.episode_no or "?"
    sc = scene.scene_no or "?"
    if scene.characters:
        chars = "、".join((scene.characters or [])[:2])
        return f"{type_zh}（第{ep}集 · {sc}）：{chars} · 待补充"[:_SUMMARY_MAX_LEN]
    return f"{type_zh}（第{ep}集 · {sc}）：场次内容待补充"[:_SUMMARY_MAX_LEN]


def _is_low_quality_summary(summary: str) -> bool:
    """命中低质量模式（场景头残留 / 纯 type 标签）就 reject。

    用于 _enrich_via_llm 输出后过滤——这种 summary 落库会让速览速读位完全失效。
    """
    s = (summary or "").strip()
    if len(s) < _SUMMARY_MIN_LEN:
        return True
    if _BAD_BEAT_SUMMARY_RE.match(s):
        return True
    if _LABEL_ONLY_SUMMARY_RE.match(s):
        return True
    # 句中含 scene heading 关键词且 ≤ 14 字（说明信息量太低）
    if len(s) <= 14 and re.search(r"(日内|日外|夜内|夜外)", s):
        return True
    return False


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
