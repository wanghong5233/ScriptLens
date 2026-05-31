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

from pydantic import BaseModel, ConfigDict, Field, conlist, field_validator
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

# Summary 上限。前端 v3.7.5 已允许多行完整展示；120 字足够覆盖 2~3 条动作行，
# 只在极端超长时才在分句边界截断（见 _smart_truncate_summary），不再 mid-char 硬切。
_SUMMARY_MAX_LEN = 120

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

# v3.7.5：检测「type 前缀 + 角色名 + 冒号 + 台词」反模式，例如：
#   - "高潮：姜栀枝：系统！老子说..."
#   - "开端：裴鹤年：你是谁"
# 这种 summary 把节拍类型 + 一句对白原文当总结，比省略号还垃圾，必须 reject。
# 触发条件：以 type_zh 前缀打头，且后面包含**第 2 个冒号**（角色台词分隔符）。
_TYPE_PREFIX_PLUS_DIALOGUE_RE = re.compile(
    rf"^{_TYPE_PREFIX}[:：].{{0,12}}[\S][:：]"
)
# v3.7.5：单纯以 type_zh 前缀打头的 summary（不管后面是什么）也属于低质——
# 前端 BeatChip Tag 已显示节拍类型，再加"高潮："是冗余。LLM prompt §6 已明令禁止。
_TYPE_PREFIX_HEAD_RE = re.compile(rf"^{_TYPE_PREFIX}[:：]")


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


# v3.7.5 (2026-05-31): W2.1 schema validation。LLM 偶尔会把 beats 字段写成数字
# (-1 / 1.1) 或单 dict，触发 rule fallback 让用户看到垃圾 rule summary。
# 启用 Pydantic schema → 校验失败时 LlmCaller 自动用「schema + 错误反馈」
# 重试 1 次，把 schema 错从根本上修掉，rule fallback 路径只剩"LLM 完全 5xx"
# 这种极端情况。


class _BeatLLM(BaseModel):
    seq: int = Field(ge=1)
    type: str
    summary: str


class _BeatActLLM(BaseModel):
    act: int = Field(ge=1, le=3)
    title: str = ""
    beats: conlist(_BeatLLM, min_length=1)  # type: ignore[valid-type]

    # v3.7.5b：LLM 最常见的 schema 错误是把单个 beat 写成 dict 而不是 list。
    # 在 Pydantic 校验**之前**拦截：dict → [dict]，避免 list_type 报错 + 二次 repair。
    # 业内做法：Instructor / OpenAI structured outputs 的 coercion 容错。
    @field_validator("beats", mode="before")
    @classmethod
    def _coerce_beats_to_list(cls, v):
        if isinstance(v, dict):
            return [v]
        return v


class _BeatActsPayload(BaseModel):
    acts: conlist(_BeatActLLM, min_length=1, max_length=3)  # type: ignore[valid-type]

    # 同样的容错：acts 偶尔被 LLM 写成单个 dict。
    @field_validator("acts", mode="before")
    @classmethod
    def _coerce_acts_to_list(cls, v):
        if isinstance(v, dict):
            return [v]
        return v

    # v3.7.5: 给 repair prompt 提供 minimal valid example（业内 show-don't-tell 实践）。
    # LLM 看到这个具体例子比看 JSON Schema 嵌套结构更容易模仿正确。
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "acts": [
                    {
                        "act": 1,
                        "title": "开局",
                        "beats": [
                            {"seq": 1, "type": "opening", "summary": "姜栀枝伪装柔弱混入修罗场"}
                        ],
                    },
                    {
                        "act": 2,
                        "title": "发展",
                        "beats": [
                            {"seq": 2, "type": "midpoint", "summary": "身份反转：姜栀枝揭开真面目"}
                        ],
                    },
                    {
                        "act": 3,
                        "title": "收束",
                        "beats": [
                            {"seq": 3, "type": "climax", "summary": "陆沉舟揭穿赵鑫身份击败对手"}
                        ],
                    },
                ]
            }
        }
    )


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
   - 所有 beat：**8-120 字之间**，写完整概括句（含人物 + 动作 + 后果，不要半句截断）
   - climax / closing 必须 ≥ 12 字
6. summary 必须包含「人物 + 动作 + 后果」三要素之一，**严禁纯标签 / 场景头残留**：
   - ❌ 错误反例（这些会被自动丢弃 / 视为低质量）：
     * "开端：关键场"               ← 纯 type + 标签
     * "中点反转：办公室日内"        ← scene heading 残留
     * "高潮：卧室日内"             ← scene heading 残留
     * "收束：帝王居，日内"          ← 同上
     * "节拍"                     ← 纯类型词
     * "高潮：姜栀枝：系统！他们怎么回事" ← **抄一句台词原文**（最严重的反模式）
     * "开端：裴鹤年：你是谁"        ← 同上，抄对白
   - ⚠ **summary 严禁照搬剧本的对白原文（"角色：台词内容"），也严禁以「开端／开场／中点／高潮／收束／反转／爽点」+ 冒号开头**——前端节拍卡片已经会显示节拍类型 Tag，summary 是用来回答「这一拍承担什么故事功能」的概括句，不是带类型标签的台词复述。
   - ✅ 正确范例：
     * opening：「姜栀枝伪装柔弱混入修罗场，引出反派注意」
     * inciting：「裴鹤年误以为姜栀枝暗恋自己十年，主动出击」
     * midpoint：「身份反转：姜栀枝揭开真面目，全员震惊」
     * climax：「陆沉舟揭穿赵鑫身份，腾龙宴公开击败对手」
     * closing：「双向救赎：陆沉舟向叶云浅公开求婚」
7. 严禁输出场景头关键词「日内 / 日外 / 夜内 / 夜外 / 内景 / 外景」。
8. 输出**一个 JSON 对象**，不要 markdown / 代码块 / 解释。
9. **schema 硬性约束**（违反任意一条都会被 reject）：
   - 顶层 ``acts`` 必须是数组，恰好 3 个元素（act=1, 2, 3 各一个）
   - 每个 act 里的 ``beats`` **必须是 JSON 数组**（即使只有 1 个 beat 也要写成 ``[{{...}}]``）；
     **禁止**把 ``beats`` 写成数字（``"beats": 3``）、字符串（``"beats": "..."``）、
     或单个对象（``"beats": {{...}}``）—— 永远是数组
   - 每个 beat 是对象，必须含 ``seq``（整数）、``type``（字符串）、``summary``（字符串）3 个字段

【输出 JSON】
{{
  "acts": [
    {{
      "act": 1,
      "title": "开局",
      "beats": [
        {{"seq": <候选 seq>, "type": "opening|inciting|...", "summary": "≤120字完整概括句"}}
      ]
    }},
    {{"act": 2, "title": "发展", "beats": [{{"seq": ..., "type": "...", "summary": "..."}}]}},
    {{"act": 3, "title": "收束", "beats": [{{"seq": ..., "type": "...", "summary": "..."}}]}}
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
        validate_with=_BeatActsPayload,
        chain_name="beat_chain",
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
        # v3.7.5 (2026-05-31)：LLM 偶尔会写错 `beats` 字段：
        #   - `"beats": {...}`                   ← 漏 wrap 成数组
        #   - `"beats": "..."` 或 `"beats": 3`   ← 完全错的类型
        # 之前直接整幕掉，触发降级。现在做容错：
        #   1. dict → 自动 wrap 成 [dict]，保住该幕至少 1 个 beat
        #   2. number / str / None → 该幕走规则兜底（act{n}_filled_by_rule）
        # 用户策略：尽量不要降级；即使发生降级也不向前端用户暴露，只记录在
        # logger / fallback_reasons 里供后端排查。
        raw_beats = raw.get("beats")
        if isinstance(raw_beats, dict):
            logger.info(
                "beat_chain: act=%s 的 beats 是 dict，自动包成单元素 list 保住该幕",
                act_no,
            )
            raw_beats = [raw_beats]
        elif not isinstance(raw_beats, list):
            logger.warning(
                "beat_chain: act=%s 的 beats 字段不是 list/dict (type=%s value=%r)，跳过该幕，走规则兜底",
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
# 动作行（△ 开头）描述"谁做了什么"，是 rule fallback summary 唯一的合法来源。
# 对白行（角色：台词）虽然是剧情正文，但**抄一句对白当节拍 summary 等于没总结**，
# v3.7.5 起 rule fallback 不再用对白行兜底（产品反馈：用户看到「高潮：姜栀枝：系统！...」
# 一句台词当总结，比省略号还差）。
_ACTION_LINE_RE = re.compile(r"^[△▲■◆●▼]")

# 系统 VO / 字幕 / 旁白 / 画外音不算"谁做了什么"，跳过。
_NARRATION_SPEAKER_RE = re.compile(
    r"^(系统\s*VO|系统|VO|画外音|旁白|字幕|OS|N|narrator)\s*[:：（(]",
    re.IGNORECASE,
)


def _smart_truncate_summary(text: str, max_len: int = _SUMMARY_MAX_LEN) -> str:
    """在 max_len 内尽量于分句标点处截断，避免 mid-char 硬切（v3.7.5c 根因修复）。

    历史 bug：_extract_plot_excerpt 曾硬切 joined[:38]，主要看点 oneliner
    「开场抓人 · …」正好 45 字在半句「姜栀枝强势」处断掉。
    """
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    chunk = s[:max_len]
    for sep in ("；", "，", "。", ";", ",", "、"):
        pos = chunk.rfind(sep)
        # 分句点不能太极端靠前，否则 summary 信息量不足
        if pos >= max(12, max_len // 3):
            trimmed = chunk[:pos].strip()
            if len(trimmed) >= _SUMMARY_MIN_LEN:
                return trimmed
    return chunk[: max_len - 1] + "…"


def _extract_plot_excerpt(scene_text: str) -> Optional[str]:
    """v3.7.5：rule fallback summary 的核心抽取器，只抓 ≥1 行真实动作行。

    设计原则（业内对照：Final Draft scene tagger / Sudowrite plot beat extractor）：
      - 节拍 summary 应该回答「谁做了什么导致了什么」
      - 动作行（△ 开头）是剧本里唯一**描述行为**的文本类型
      - 对白行是台词原文，**抄一句台词当 summary 反而骗用户**
      - 系统 VO / 字幕 / 旁白属于 narration，不是行为

    策略：
      1. 跳过场号 / 集号 / 人物列表 / 场景头 / 空行 / 系统 VO
      2. **只收集动作行**（△/▲/■/◆/●/▼），用「；」拼接成完整动作链
      3. 总长超过 _SUMMARY_MAX_LEN 时，在分句标点处截断（_smart_truncate_summary）
      4. 完全没有动作行 → 返回 None，上层走"场次定位 + 人物"的诚实占位
         （**不再抓对白行兜底**——抄一句台词比占位更糟）
    """
    if not scene_text:
        return None
    action_chunks: List[str] = []
    for raw_line in scene_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _SCENE_HEADER_LINE_RE.match(line):
            continue
        if not _ACTION_LINE_RE.match(line):
            continue
        clean = re.sub(r"^[△▲■◆●▼]\s*", "", line).strip()
        if not clean:
            continue
        if _NARRATION_SPEAKER_RE.match(clean):
            continue
        action_chunks.append(clean)
    if not action_chunks:
        return None
    joined = "；".join(action_chunks)
    joined = re.sub(r"[，。！？；,;.]+$", "", joined).strip()
    if len(joined) < 12:
        return None
    return _smart_truncate_summary(joined)


def _scene_label_summary(scene: Scene, type_hint: BeatType) -> str:
    """v3.7.5 rule fallback summary：去掉 "高潮：" 这类 type 前缀。

    历史问题：之前格式 ``"{type_zh}：{plot}"`` → 渲染成"高潮：姜栀枝：系统！..."
      - 前端 BeatChip 已有 typeLabel Tag（"高潮"／"开端"），summary 再加"高潮："
        就是冗余 + 视觉上像「标签 : 一句台词」
      - 而旧 ``_extract_plot_excerpt`` 抓的是对白行 → 抄的就是台词原文
      - 两个 bug 叠加用户看到的就是垃圾

    v3.7.5 改动：
      1. summary 只放"动作行"（_extract_plot_excerpt 已经收紧）
      2. 不再加 type_zh 前缀（前端 Tag 已显示）
      3. 抓不到动作行 → 诚实占位「集N·场M·人物 待补充」，**前端用户透明**
         （后端 fallback_reasons 仍记录 act{N}_filled_by_rule 供维护排查）
    """
    plot = _extract_plot_excerpt(scene.text or "")
    if plot:
        return plot
    ep = scene.episode_no or "?"
    sc = scene.scene_no or "?"
    if scene.characters:
        chars = "、".join((scene.characters or [])[:2])
        return f"第{ep}集·{sc}场 · {chars}场景"[:_SUMMARY_MAX_LEN]
    return f"第{ep}集·{sc}场 关键场次"[:_SUMMARY_MAX_LEN]


def _is_low_quality_summary(summary: str) -> bool:
    """命中低质量模式（场景头残留 / 纯 type 标签 / type+台词原文）就 reject。

    用于 _enrich_via_llm 输出后过滤——这种 summary 落库会让速览速读位完全失效。
    """
    s = (summary or "").strip()
    if len(s) < _SUMMARY_MIN_LEN:
        return True
    if _BAD_BEAT_SUMMARY_RE.match(s):
        return True
    if _LABEL_ONLY_SUMMARY_RE.match(s):
        return True
    # v3.7.5：「高潮：姜栀枝：系统！...」这种 type 前缀 + 台词原文 → reject
    if _TYPE_PREFIX_PLUS_DIALOGUE_RE.match(s):
        return True
    # v3.7.5：单纯以 type 前缀打头也 reject（如 "开端：xxx"），前端 Tag 已显示类型
    if _TYPE_PREFIX_HEAD_RE.match(s):
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
