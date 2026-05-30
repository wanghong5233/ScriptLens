"""ScriptLens · 人物物料一体化抽取链路（release/v1-mvp 主路径）。

入口
====

::

    entities = await resolve_entities(script_id=..., engine=...)
    persist_entities(entities, script_id=..., engine=...)

    bios = await write_bios_concurrent(
        entities,
        scenes=scenes,
        caller=caller,
        semaphore_size=4,
    )
    persist_bios(bios, engine=...)

设计原则
========

- **自包含**：仅依赖 ``scene_repo`` / ``llm_caller``。**不**引用任何已废弃
  模块（``character_entity_resolver`` / ``relationship_candidate_generator`` /
  ``tag_pipeline`` 等）；后续 cleanup PR 删除废弃模块时本文件零牵连。
- **职责单一**：
    - ``resolve_entities``  —— 纯本地（频率 + Jaro-Winkler + 严格包含），
      不调 LLM。alias 归一阈值见 ``_MERGE_THRESHOLD_HARD`` /
      ``_MERGE_THRESHOLD_SOFT``。
    - ``write_bios_concurrent`` —— 每人一次 LLM call，``asyncio.Semaphore``
      限并发 + ``gather(return_exceptions=True)`` 单点容错。
    - 持久化（``persist_entities`` / ``persist_bios``）独立函数，便于上层
      ``script_report_service`` 在事务边界内复用。
- **id-space 一致**：``CharacterEntity.id`` 是 UUID，写入 character_entities
  表后被 character_graph_chain 的 resolver baseline 路径直接消费，
  ``CharacterBio.character_id`` = entity.id，前端三处（关系图节点 / characters /
  character_bios）共享同一 id-space。

字段语义对齐 docs/prompt.jpg 与 docs/2026-05-29-剧本到分镜-高光集锦投放-需求
与方案.md §5.1（物料层）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.character_graph_chain import is_real_character_name
from service.script_tools.llm_caller import (
    LlmCaller,
    ModelTier,
    ScoreLLMError,
    TokenBudget,
)
from service.script_tools.scene_repo import Scene
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


# ============================================================
# 字段字数 soft / hard 上限（替换 v1-mvp 的 _short_str(..., 40) 魔法数）
# ============================================================
#
# 设计依据（2026-05 采样）：
#   - Sudowrite Story Bible "Personality" 字段：实测样例 80~150 字段落
#   - Novelcrafter Codex "Description"：80~250 字常见
#   - docs/prompt.jpg 用例：identity 约 30-50 字、persona 约 40-80 字
#   - 短剧实测剧本片段长度：单段台词 60~200 字
#
# 取数原则：
#   - soft 是 LLM "推荐输出长度"——前端按段落渲染时呼吸感正好
#   - hard 是真截断阈值——超过会触发 _clamp_text 在最近句号边界回退
#   - 任何字段允许"原样略长"，只要 ≤ hard，不像 v1-mvp 强切到 soft
#
# 命名常量替代 inline 数字。新增字段必须在此表加一行带依据的 (soft, hard)，
# 调用点禁止 inline magic number。

class BioFieldLimits:
    """人物小传字段字符长度 (soft, hard) 限制。"""

    # 三段身份：当前社会身份 / 隐藏身份 / 出身或前世身份
    # 短剧高频结构（双重身份/穿越/伪装），需要写"21世纪美妆博主，穿越后为
    # 大靖七皇子。初代守护者之女，四象秘钥继承者。"这种 30~80 字。
    IDENTITY = (80, 160)

    # appearance.age / height / build / facial 单字段
    AGE = (40, 80)
    HEIGHT = (40, 80)
    BUILD = (60, 120)
    FACIAL = (80, 160)

    # signature_props 单条标志物
    SIG_PROP = (30, 60)

    # outfit 子字段：material / palette / form
    OUTFIT_MATERIAL = (50, 100)
    OUTFIT_PALETTE = (50, 100)
    OUTFIT_FORM = (80, 160)

    # 性格段落：表面人设 / 真实内核
    # 业界标准：Sudowrite "Personality in one paragraph"——一段话比短句有效
    PERSONA_SURFACE = (120, 240)
    PERSONA_CORE = (120, 240)

    # 核心弱点 / 软肋
    WEAKNESS = (80, 160)

    # 成长弧光：从 XX 到 XX + 触发事件
    ARC_LIGHT = (160, 320)

    # 说话风格（v2 新增，对齐 Sudowrite Dialogue Style）
    DIALOGUE_STYLE = (120, 240)

    # 经典台词原文 quote：宽松，避免被截断破坏完整性
    # 短剧单段台词 60~200 字常见
    QUOTE = (200, 360)

    # relations_summary 每条关系一句话
    RELATION_SENTENCE = (80, 160)

    # notable_scenes 每条 behavior：该角色在该场做了什么
    NOTABLE_SCENE_BEHAVIOR = (120, 240)


# 数量上限（业界采样 + 实测）。这些数字有合理依据，不是魔法数：
#   - signature_props max 4：标志物再多视觉/T2I prompt 会糊
#   - catchphrases max 5：docs/prompt.jpg 明确说"3-5 句"
#   - relations_summary max 6：短剧主要关系网通常 3-5 个，留 1 条余量
#   - notable_scenes max 3：典型 setup / midpoint / climax 三段式

_MAX_SIG_PROPS = 4
_MAX_CATCHPHRASES = 5
_MAX_RELATIONS = 6
_MAX_NOTABLE_SCENES = 3


_SENTENCE_BREAK_RE = re.compile(r"[。！？!?；;\.\?\!]+")


def _clamp_text(value: Any, limits: Tuple[int, int]) -> str:
    """语义边界友好截断，替代 v1 的 ``s[:max-1] + "…"``。

    规则：
      - len ≤ soft：原样返回
      - soft < len ≤ hard：原样返回（仅 logger.debug 记录"偏长"）
      - len > hard：在 [soft-buffer, hard] 内找最后一个句末标点（。！？!?；;.?!），
        截到该标点（含）；找不到则硬截到 hard 后补 "…"。Python str 是
        codepoint 数组，硬截不会拆开 UTF-8 字节，但会破坏中文语义——所以
        优先走句末标点回退。
    """
    s = str(value or "").strip()
    soft, hard = limits
    if not s or len(s) <= soft:
        return s
    if len(s) <= hard:
        if len(s) > int(soft * 1.3):
            logger.debug("character_pipeline: field length %d exceeds soft %d (still <= hard %d)", len(s), soft, hard)
        return s

    # 超 hard：在 [soft, hard] 区间找最后一个句末标点
    candidate = s[: hard + 1]
    matches = list(_SENTENCE_BREAK_RE.finditer(candidate))
    if matches:
        for m in reversed(matches):
            if m.end() >= soft:
                return s[: m.end()].rstrip()
    # 兜底硬截
    return s[:hard].rstrip() + "…"


# ============================================================
# 数据类
# ============================================================


@dataclass
class CharacterEntity:
    """人物实体（character_entities 行的内存形态）。

    id 为 UUID，作为下游 graph / bios / relationships 共用 id-space 的锚点。
    """

    id: str
    script_id: str
    canonical_name: str
    aliases: List[str] = field(default_factory=list)
    role: str = "support"  # protagonist / antagonist / support / minor
    appearance_count: int = 0  # 出场场次数
    first_scene_id: Optional[str] = None
    mention_count: int = 0  # 提及次数（>= appearance_count）

    def all_names(self) -> List[str]:
        """canonical_name + aliases，用于 scene 匹配。"""
        return [self.canonical_name, *self.aliases]

    def to_chain_dict(self) -> Dict[str, Any]:
        """转成 character_graph_chain._build_from_resolver 期望的字典形态。

        字段名对齐 chain 的契约（id / name / aliases / appearance_count）；
        archetype / role_in_arc / arc_type / agency_level v1-mvp 不抽，留空让
        chain 内部 _initial_role 走"index==0 → protagonist"的兜底逻辑。
        """
        return {
            "id": self.id,
            "name": self.canonical_name,
            "aliases": list(self.aliases),
            "archetype": "",
            "role_in_arc": "",
            "arc_type": "",
            "agency_level": "",
            "appearance_count": self.appearance_count,
        }


@dataclass
class CharacterBio:
    """人物小传（character_bios 行的内存形态）。

    字段对齐 docs/prompt.jpg 五段式 + alembic 08 扩展（dialogue_style / notable_scenes）。
    详细字段语义见 alembic 07 / 08 注释。
    """

    id: str
    script_id: str
    character_id: str
    identity_present: str = ""
    identity_hidden: str = ""
    identity_origin: str = ""
    appearance: Dict[str, Any] = field(default_factory=dict)
    persona_surface: str = ""
    persona_core: str = ""
    weakness: str = ""
    arc_light: str = ""
    dialogue_style: str = ""
    catchphrases: List[Dict[str, Any]] = field(default_factory=list)
    relations_summary: List[Dict[str, Any]] = field(default_factory=list)
    notable_scenes: List[Dict[str, Any]] = field(default_factory=list)
    bio_ver: str = "v2"
    source: str = "llm"
    evidence: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# resolve_entities：纯本地 alias 归一
# ============================================================


# 阈值取舍（基于短剧人名特征）：
# - 0.92 起合：相似度极高才直接合并，避免"鹿乔" / "鹿鸣"误合
# - "包含关系"作为强证据：A 是 B 子串且都有效 → 通常是 alias（"鹿鸣于" / "鹿鸣于OS"）
# - 不调 LLM：第一版纯本地。后续若发现误合/漏合再加 LLM 仲裁。
_MERGE_THRESHOLD_HARD = 0.92
_CONTAIN_BONUS_THRESHOLD = 2  # 长度 ≥2 才允许"包含关系"合并（避免"于"匹配"鹿鸣于"）


def _normalize_name(raw: str) -> str:
    """剔除括号注释、首尾标点、常见说话标记。"""
    name = (raw or "").strip()
    if not name:
        return ""
    name = re.sub(r"[（(].*?[)）]", "", name)
    name = name.strip("：:、,，。.!！?？·-—_/\\\"'`* \t")
    return name.strip()


def _jaro_similarity(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    len1, len2 = len(s1), len(s2)
    match_distance = max(len1, len2) // 2 - 1
    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0
    for i in range(len1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    transpositions = 0
    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    transpositions //= 2
    return (matches / len1 + matches / len2 + (matches - transpositions) / matches) / 3.0


def _jaro_winkler(s1: str, s2: str, scaling: float = 0.1) -> float:
    jaro = _jaro_similarity(s1, s2)
    prefix_len = 0
    for ch1, ch2 in zip(s1, s2):
        if ch1 != ch2:
            break
        prefix_len += 1
        if prefix_len == 4:
            break
    return jaro + prefix_len * scaling * (1 - jaro)


def _name_similarity(a: str, b: str) -> float:
    """两个名字的相似度。

    包含关系（"鹿鸣于" 之于 "鹿鸣于OS"）是短剧 alias 的最强证据，直接给
    高分。但要求两个名字都至少 _CONTAIN_BONUS_THRESHOLD 字，避免单字
    误合。
    """
    if a == b:
        return 1.0
    if (
        len(a) >= _CONTAIN_BONUS_THRESHOLD
        and len(b) >= _CONTAIN_BONUS_THRESHOLD
        and (a in b or b in a)
    ):
        return 0.95
    return _jaro_winkler(a, b)


def _role_of(rank: int, scene_count: int, top_scene_count: int) -> str:
    """按出场排序推断 role 初值（LLM 后续 enrichment 会覆盖）。"""
    if rank == 0:
        return "protagonist"
    if rank == 1 and scene_count >= max(2, int(top_scene_count * 0.5)):
        return "antagonist"
    if scene_count >= 2:
        return "support"
    return "minor"


def _collect_name_stats(scenes: List[Scene]) -> Tuple[Dict[str, int], Dict[str, set], Dict[str, str]]:
    """从 scenes 收集每个候选人名的（提及次数 / 出场场次 / 首场 id）。"""
    name_freq: Dict[str, int] = {}
    name_scenes: Dict[str, set] = {}
    name_first_scene: Dict[str, str] = {}
    for scene in scenes:
        seen_in_scene: set = set()
        for raw in scene.characters or []:
            name = _normalize_name(raw)
            if not name or not is_real_character_name(name):
                continue
            name_freq[name] = name_freq.get(name, 0) + 1
            seen_in_scene.add(name)
        for name in seen_in_scene:
            name_scenes.setdefault(name, set()).add(scene.id)
            name_first_scene.setdefault(name, scene.id)
    return name_freq, name_scenes, name_first_scene


def _cluster_aliases(name_freq: Dict[str, int]) -> List[List[str]]:
    """按相似度把名字聚成 cluster；canonical_name 留给上层选。

    遍历顺序：按 freq 降序——高频名优先吸纳低频别名（"鹿鸣于" 出场 200 次
    会先成为 cluster 锚点，"鹿鸣于OS" 30 次再被吸纳进去）。
    """
    clusters: List[List[str]] = []
    sorted_names = sorted(name_freq.keys(), key=lambda n: (-name_freq[n], len(n)))
    for name in sorted_names:
        merged = False
        for cluster in clusters:
            if _name_similarity(name, cluster[0]) >= _MERGE_THRESHOLD_HARD:
                cluster.append(name)
                merged = True
                break
        if not merged:
            clusters.append([name])
    return clusters


def cooccurrence_candidate_relationships(
    entities: List[CharacterEntity],
    scenes: List[Scene],
    *,
    max_edges: int = 30,
    min_jaccard: float = 0.12,
) -> List[Dict[str, Any]]:
    """共现矩阵 → 候选关系边，给 character_graph_chain 当 baseline edges。

    返回字典字段对齐 chain ``_build_from_resolver`` 期望：
    ``a_id`` / ``b_id`` / ``type`` / ``polarity``。type 占位 "ally"、polarity
    占位 "mixed"——chain 的 LLM enrichment 阶段会按场景上下文重写这两个字段
    （chain ``_apply_edge_enrichment`` 在校验合法值后覆盖）；漏写的边保持
    占位，前端渲染为浅色 ally/mixed 边，不至于"图谱有节点没边"。

    Jaccard 归一避免"一场宴会同框 6 人 → 6 人两两共现都=1.0"塌成一坨。
    阈值 0.12 沿用 ``character_graph_chain._cooccurrence_graph`` 的实践值。
    """
    if not entities or not scenes:
        return []

    # name → entity_id（含 alias），用于把 scene.characters 的原始名映射回 UUID
    name_to_id: Dict[str, str] = {}
    for entity in entities:
        for name in entity.all_names():
            cleaned = _normalize_name(name)
            if cleaned:
                name_to_id[cleaned] = entity.id

    appearance: Dict[str, int] = {e.id: 0 for e in entities}
    cooccur: Dict[Tuple[str, str], int] = {}
    for scene in scenes:
        ids_in_scene: List[str] = []
        seen_in_scene: set = set()
        for raw in scene.characters or []:
            name = _normalize_name(raw)
            ent_id = name_to_id.get(name)
            if ent_id and ent_id not in seen_in_scene:
                seen_in_scene.add(ent_id)
                ids_in_scene.append(ent_id)
        for ent_id in ids_in_scene:
            appearance[ent_id] = appearance.get(ent_id, 0) + 1
        for i in range(len(ids_in_scene)):
            for j in range(i + 1, len(ids_in_scene)):
                a, b = sorted((ids_in_scene[i], ids_in_scene[j]))
                key = (a, b)
                cooccur[key] = cooccur.get(key, 0) + 1

    scored: List[Tuple[float, str, str, int]] = []
    for (a, b), count in cooccur.items():
        union = appearance.get(a, 0) + appearance.get(b, 0) - count
        if union <= 0:
            continue
        weight = round(count / union, 3)
        if weight < min_jaccard:
            continue
        scored.append((weight, a, b, count))
    scored.sort(reverse=True)
    scored = scored[:max_edges]

    return [
        {
            "a_id": a,
            "b_id": b,
            "type": "ally",  # chain LLM enrichment 会按场景重写
            "polarity": "mixed",
        }
        for _weight, a, b, _count in scored
    ]


async def resolve_entities(
    *,
    script_id: str,
    scenes: List[Scene],
    max_entities: int = 30,
) -> List[CharacterEntity]:
    """从 scenes 中抽出归一化的人物实体列表。

    返回顺序：按出场场次降序（与 _role_of 的 rank 一致）。
    """
    if not scenes:
        return []
    name_freq, name_scenes, name_first_scene = _collect_name_stats(scenes)
    if not name_freq:
        return []

    clusters = _cluster_aliases(name_freq)

    # 给每个 cluster 选 canonical_name（freq 最高、其次更短）+ 聚合统计
    canonicals: List[Tuple[str, List[str], int, set, str]] = []
    for cluster in clusters:
        canonical = sorted(cluster, key=lambda n: (-name_freq[n], len(n)))[0]
        aliases = sorted({n for n in cluster if n != canonical})
        mention_count = sum(name_freq.get(n, 0) for n in cluster)
        scene_set: set = set()
        first_scene: Optional[str] = None
        for n in cluster:
            scene_set.update(name_scenes.get(n, set()))
            cand_first = name_first_scene.get(n)
            if cand_first and (first_scene is None):
                first_scene = cand_first
        canonicals.append((canonical, aliases, mention_count, scene_set, first_scene or ""))

    # 按 scene_count 降序排（主角 = scene_count 最高），裁剪 max_entities
    canonicals.sort(key=lambda item: (-len(item[3]), -item[2], len(item[0])))
    canonicals = canonicals[:max_entities]

    top_scene_count = max((len(item[3]) for item in canonicals), default=1)
    entities: List[CharacterEntity] = []
    for rank, (canonical, aliases, mention_count, scene_set, first_scene) in enumerate(canonicals):
        entities.append(
            CharacterEntity(
                id=str(uuid.uuid4()),
                script_id=script_id,
                canonical_name=canonical,
                aliases=aliases,
                role=_role_of(rank, len(scene_set), top_scene_count),
                appearance_count=len(scene_set),
                first_scene_id=first_scene or None,
                mention_count=mention_count,
            )
        )
    return entities


# ============================================================
# write_bios_concurrent：每人一次 LLM
# ============================================================


_BIO_SYSTEM_PROMPT = """你是中文短剧人物小传写作助手。给定一个角色及其登场场景片段，
按 docs/prompt.jpg 的"五段式 + 说话风格 + 关键场景"七段结构输出该角色的小传。

小传服务两类下游：
  (1) 前端"人物详情"弹窗展示给编剧 —— 字段以**段落**为基本形态，禁短句堆砌
  (2) 高光集锦 / 分镜环节拼 T2I/视频 prompt —— 外貌必须**可机读结构化**

== 总则 ==
- 只输出**一个 JSON 对象**，不带 markdown / 代码块 / 解释 / 多余前后缀。
- 信息缺失的字段填空字符串 "" 或空数组 []，**绝不编造**。剧本没提就是没提，
  不要凭空补"180cm"、"黑色"、"复仇之女"。**字段宁可空，也不要造词凑齐**。
- 字段以**自然段**形式撰写：persona_surface / persona_core / weakness / arc_light /
  dialogue_style 推荐 60~150 字一段，不要短到三五个字凑一行。

== 身份字段提取纪律（重要，最容易出错的环节）==
identity_present 是必填项；identity_hidden / identity_origin 默认应该是空字符串，
只有剧本里有明确文本支撑才允许填。

  identity_present （当前社会身份）
  - 从剧本的人物介绍 / 场景上下文 / 称谓中提炼当前角色身份。
  - 例：「应聘女佣」「医院护士长」「公司新晋助理」「家族继承人」。
  - 至少 30 字以上的简短自然段，把身份+处境一起写。

  identity_hidden （隐藏身份 / 伪装身份）—— 默认空
  仅在剧本明确出现以下线索之一时才填，否则严格保留 ""：
    - "假装是某某" / "其实是另一个身份" / "化名 XX"
    - 角色明确为"卧底""特工""复仇者潜伏""易容""扮成女佣"等
    - 文中其他角色对该角色的身份发出质疑、错认或揭穿
  ❌ 反例：把"暗恋裴鹤年的深情者"当成 identity_hidden（这是性格不是身份）；
  把"恶毒女配"当成 identity_hidden（这是 identity_present 的本职定位）。
  绝大多数现代剧 / 都市剧 / 职场剧 identity_hidden 都应该是 ""。

  identity_origin （出身 / 前世 / 真实身份）—— 默认空
  仅在剧本是穿越 / 重生 / 失忆 / 失散 / 大家族失散身世题材，且有明确文本支撑时才填：
    - "上一世我..." / "穿越前是 21 世纪..." / "其实你才是 X 家的真千金"
    - "母亲临终告诉我，我们家族原本..."
  ❌ 反例：把"小镇出生的普通女孩"当 origin（这是普通现代剧背景，不算 origin）。
  绝大多数现代剧 identity_origin 都应该是 ""。

== 外貌字段边界（避免性格 / 行为混入，但要尽力提取）==
外貌只描述"长什么样"，不描述"做什么"或"内心如何"。

**重要平衡**：宁可"恰当地写"也不要"为了避免错误而全空"。剧本里**只要有任何一句**
关于该角色长相 / 装束 / 年龄 / 身高 / 配饰的描写，就**必须**抽取到对应字段。
不要因为怕越界就把所有字段都空——LLM 必须先做"提取"，再做"边界判断"。

  age：年龄段（"二十出头"/"四十中年"/"少年"）。
    ✅ 提取信号：剧本说"她不过二十二三的年纪"、"少年人"、"中年男子" → 写入。
    ⚪ 仅在剧本通篇毫无年龄暗示时留空。

  height：身高（"高挑"/"约 168cm"/"中等身材"/"个子小巧"）。
    ✅ 提取信号：剧本提到身高对比、"她比他矮一头"、"高挑的身影" → 写入。
    ⚪ 剧本未涉及身高时留空。

  build：体型（"纤细"/"健硕"/"丰腴"/"瘦削挺拔"/"骨架小巧"）。
    ✅ 提取信号：体型直接描写。
    ❌ 排除：行为或性格倾向（"行动带狡黠感"、"动作敏捷"、"举止得体" 都不是体型）。

  facial：脸型 / 五官 / 肤色 / 眼形 / 胡须 / 长期眼神形态 等"长得啥样"。
    ✅ 提取信号："清秀端正、眼神冷静"、"圆脸娃娃音、一对梨涡"、"剑眉星目、肤色偏冷白"。
    ❌ 排除：表情变化（"表情多变"、"从无辜到撒娇切换自如" 是性格 / 演技，不是面部特征）。

  signature_props：在剧本里**反复出现** 3 次或以上的物件 / 道具 / 配饰。
    ✅ 正确：贯穿多场的折扇、随身的小药瓶、永远夹着的简历夹、口含的棒棒糖。
    ❌ 不要：单次出现的杯子、随手拿的纸巾、一次冲突里使用过的扳手。

  outfit.material / palette / form：从剧本对该角色的服装描写中归纳。
    ✅ 提取信号：剧本只要提到该角色穿的"白色棉麻女佣装"、"玄黑曳地长袍"、"修身西装"等
       任一描述就要拆出 material（棉麻 / 丝绸 / 西装料）/ palette（白 / 玄黑 / 深蓝）
       / form（女佣装 / 曳地长袍 / 修身西装）三段。
    ❌ 排除：从动作或称谓推断（"恶毒女配" 不等于 "红色礼服"，没说就不要写）。

  外貌字段优先来源：角色第一次出场段、明确的"角色描写"段、反复提及的服装段。
  **不要从动作/台词/性格推断长相**。剧本如确实毫无外貌描写，可以留空，但**应该是少数情况**——
  正常的剧本至少会有第一次出场的人物速写。**全空 = 多半是漏抽，请重新检查场景片段**。

== 经典台词挑选标准（catchphrases）==
"经典台词" ≠ "随便从角色台词里抓 3-5 句"。挑选标准（满足任一）：
  ① 自我宣言：角色直接表态自己是谁、为何而来、要做什么
     例："我要做女佣是因为我羡慕你有一个幸福的家。"
  ② 暴露内核：揭示角色真实动机 / 隐藏情感 / 长久执念的关键句
     例："八岁那年好梦破碎，我的余生只有复仇。"
  ③ 关系定调：第一次见面 / 关键转折 / 决裂时对另一关键角色说的话
     例："从今天起我们再不是兄妹。"
  ④ 反复呼应：角色在剧中多次重复或变体重复的口头禅 / 标志句

❌ 拒绝：
  - 纯粗口、骂人："我操"、"妈的"、"真不要脸" —— 除非这是该角色本职性格的核心符号
  - 单字呼喊："乔颜！"、"啊！"、"快跑！" —— 没有信息量
  - 八卦闲话、应酬场面话、纯解释剧情的旁白
  - 主角随口的"嗯"、"好的"、"哦"

宁可少选几句也不要凑数：找到 3 句有价值的就只输出 3 条，找不到就输出空数组。
不要输出 5 句"无价值台词"凑齐。

== scene_id 纪律 ==
catchphrases[].scene_id、notable_scenes[].scene_id 必须**逐字符抄入**用户消息
【该角色登场的场景片段】里以 [scene_id=xxxxxxxx-xxxx-...] 出现过的真实 UUID。
不记得就留空字符串 ""，**绝不要**编造 "scene-01" / "S1" 类占位。
catchphrases 即使 scene_id 留空，台词本身依然有价值；notable_scenes 必须配真实 scene_id。

== 关系字段（relations_summary）==
other_id 必须是【其他主要角色 id 表】里给定的真实 id；没有可写的关系就空数组，
不要凑数。每条 sentence 一句话概括"两人关系本质 + 当前阶段"。

== 关键场景（notable_scenes）==
选 1-3 场该角色"做了关键动作 / 暴露内核 / 关系反转 / 计划启动 / 计划失败"的场。
behavior 一两句话写**该角色在该场做了什么**（不是场景梗概，不是该场所有人做的事）。
找不到合适的就空数组。
"""


_BIO_FEW_SHOT_JSON_EXAMPLE = """{
  "identity_present": "应聘进入豪门的女佣",
  "identity_hidden": "为父翻案的复仇者，伪造背景接近仇家。",
  "identity_origin": "原是受害家族独女，八岁那年家破人亡后改名换姓潜伏多年。",
  "appearance": {
    "age": "二十出头",
    "height": "约 168cm，纤细匀称",
    "build": "偏瘦但行动有力量感",
    "facial": "清秀端正，眉眼平时低顺，独处时眼神锋利、隐含愤怒",
    "signature_props": ["简历夹", "棒棒糖"],
    "outfit": {
      "material": "棉麻与羊毛混搭",
      "palette": "灰黑冷色为主",
      "form": "朴素女佣制服 + 利落通勤版型"
    }
  },
  "persona_surface": "在雇主面前是温顺谦卑、几乎看不见情绪的求职者，说话轻声、垂眼听命，与豪宅成员保持安全距离。表面人设服务于"无害"印象。",
  "persona_core": "她真正的内核是背负血债、长期自我训练的复仇者：理性冷静、计划周密、对仇家家族每个成员的弱点都有清单。出于本能压抑情感波动。",
  "weakness": "对家人旧案的执念过深；一旦看到仇家与孩子温情互动会瞬间动摇，可能为不必要的目标暴露身份。",
  "arc_light": "从"完美隐忍的复仇机器"到"敢直面父亲遗物、承认自己也想拥有家"的人。触发点：发现仇家少爷其实并非凶手，旧案另有真凶。",
  "dialogue_style": "对外说话短促、礼貌、句末多带"是"、"好的"，几乎不用形容词；独处或被逼急了切换成长句，常引用八年前家族旧事，语气冷而刻意压抑。",
  "catchphrases": [
    {"quote": "你忘了这一切的错都赖你，谁让你禁不住诱惑呢。", "scene_id": "<请把【场景片段】里 [scene_id=xxx] 的真实 UUID 抄到这里；不记得就留空字符串>"},
    {"quote": "我要做女佣是因为我羡慕你有一个幸福的家，我想加入这个家。", "scene_id": ""},
    {"quote": "八岁那年好梦破碎，我的余生只有复仇。", "scene_id": ""}
  ],
  "relations_summary": [
    {"other_id": "<必须是【其他主要角色 id 表】里的真实 id>", "sentence": "视其为杀父仇人，长期以女佣身份贴身潜伏并搜集证据。"},
    {"other_id": "<必须是【其他主要角色 id 表】里的真实 id>", "sentence": "表面恭顺、暗中持续试探其底线，是首阶段最重要的目标。"}
  ],
  "notable_scenes": [
    {"scene_id": "<同 catchphrases 的 scene_id 纪律：抄真实 UUID 或留空>", "behavior": "首次踏入豪宅面试，在女主人面前演完温顺求职者，独处时取出母亲遗留的简历夹凝视，确认任务开始。"}
  ]
}"""


_BIO_USER_PROMPT = """【目标角色】
canonical_name: {canonical_name}
别名: {aliases_block}
出场场次: {appearance_count}
角色 id: {character_id}

【其他主要角色 id 表】（relations_summary.other_id 必须从这里选；没合适的就空数组）
{other_chars_block}

【该角色登场的场景片段】
（每段以 [scene_id=<UUID>] 开头，catchphrases[].scene_id / notable_scenes[].scene_id 只能逐字符抄这里出现过的 UUID）

{scenes_block}

【few-shot 风格示例】（仅参考写法和结构，**不要照抄具体人物内容**；scene_id 注意按上述纪律处理）
{few_shot_json}

【关于上述示例的重要提示】
该示例是"潜伏复仇"双重身份剧，所以 identity_hidden 和 identity_origin 都填了。
**如果你正在写的目标角色不是这种剧（普通都市剧 / 现代职场 / 校园 / 古装本色），
identity_hidden 和 identity_origin 必须留空字符串 ""，不要模仿示例硬填。**
判断方法：剧本里**没有**明确出现"伪装/假身份/化名/上一世/穿越前/真千金"等字眼时，
两个字段一律留空。

输出契约（再次强调）：
- 只输出**一个 JSON 对象**。
- 段落字段写自然段（约 60~150 字），不要拆成短句堆砌；信息缺失留 "" 或 []。
- scene_id 只能从【场景片段】给出的 UUID 抄入，不要编造 "scene-01" 类占位。
- 严格遵守 system 里的"身份留空纪律"和"外貌字段边界"，不要为了凑齐而造词。

输出 JSON 的字段结构：
{{
  "identity_present": "<当前社会身份段落，60~120 字。例：「应聘女佣，作为新晋员工初入豪宅，
                       表面恭顺听话、实则与雇主家族保持距离。」>",
  "identity_hidden": "<隐藏身份/伪装身份；**剧本无明确伪装/卧底/假身份线索时严格留空字符串 ''**。
                      不要把"暗恋某某"或"性格刚烈"当 hidden。>",
  "identity_origin": "<出身/前世/真实身份；**仅穿越/重生/失忆/大家族失散题材且有明确文本支撑时填**，
                      其他情况一律留空字符串 ''。普通现代剧主角的"小镇出身"不算 origin。>",
  "appearance": {{
    "age": "<年龄段，如「二十出头」「四十中年」；剧本未提则空>",
    "height": "<身高描述，如「约 168cm」「高挑」「中等身材」；剧本未提则空>",
    "build": "<体型，如「纤细」「健硕」「瘦削挺拔」；**不要写行为或性格**（如「行动带狡黠感」是性格不是体型）>",
    "facial": "<脸型 / 五官 / 肤色 / 眼形 等"长得啥样"；**不要写表情变化**（如「表情多变」是性格表现）>",
    "signature_props": ["<剧本中反复出现 3 次以上的物件/配饰，如「棒棒糖」「简历夹」；单次出现的物品不算>"],
    "outfit": {{
      "material": "<服装材质，从剧本服装描写归纳；未提则空>",
      "palette": "<服装色系，从剧本服装描写归纳；未提则空>",
      "form": "<服装形制风格，从剧本服装描写归纳；未提则空>"
    }}
  }},
  "persona_surface": "<表面人设段落，60~150 字>",
  "persona_core": "<真实内核段落，60~150 字>",
  "weakness": "<核心弱点/软肋段落，60~120 字；无则空>",
  "arc_light": "<成长弧光段落，含「从…到…」+ 触发事件，80~200 字；无则空>",
  "dialogue_style": "<说话风格段落：节奏/语气/用词/口头禅模式，60~150 字；无则空>",
  "catchphrases": [
    {{"quote": "<原文台词，不改写。必须满足"自我宣言/暴露内核/关系定调/反复呼应"任一标准。
              **不要选纯粗口、单字呼喊、应酬场面话**。宁缺勿滥，3 句强于 5 句凑数>",
     "scene_id": "<【场景片段】里出现过的真实 UUID 或空字符串>"}}
  ],
  "relations_summary": [
    {{"other_id": "<其他主要角色 id 表里的真实 id>", "sentence": "<一句话概括两人关系本质 + 当前阶段>"}}
  ],
  "notable_scenes": [
    {{"scene_id": "<【场景片段】里出现过的真实 UUID>",
     "behavior": "<该角色在该场做了什么，1~2 句；不是场景梗概，是该角色的关键动作>"}}
  ]
}}
"""


def _select_scenes_for_entity(
    entity: CharacterEntity,
    scenes: List[Scene],
    *,
    max_scenes: int = 12,
    max_chars_per_scene: int = 1000,
) -> List[Tuple[Scene, str]]:
    """挑选与该角色相关的场景片段，给 LLM 当上下文。

    匹配口径：scene.characters（清洗后）命中 entity.canonical_name 或
    aliases 任一即视为该角色出场。截到前 max_scenes 场，每场截 max_chars
    字，避免单条 prompt 爆 token。
    """
    matchable = {entity.canonical_name, *entity.aliases}
    matchable.discard("")
    if not matchable:
        return []
    out: List[Tuple[Scene, str]] = []
    for scene in scenes:
        scene_names = {_normalize_name(n) for n in (scene.characters or [])}
        scene_names.discard("")
        if not (scene_names & matchable):
            continue
        body = scene.text or ""
        if len(body) > max_chars_per_scene:
            body = body[: max_chars_per_scene - 1] + "…"
        out.append((scene, body))
        if len(out) >= max_scenes:
            break
    return out


def _build_bio_prompt(
    entity: CharacterEntity,
    other_entities: List[CharacterEntity],
    scenes_for_entity: List[Tuple[Scene, str]],
) -> str:
    aliases_block = "、".join(entity.aliases) if entity.aliases else "（无）"
    other_chars_lines = [
        f"- {e.id}|{e.canonical_name}（出场 {e.appearance_count} 场）"
        for e in other_entities
        if e.id != entity.id
    ]
    other_chars_block = "\n".join(other_chars_lines) if other_chars_lines else "（无其他主要角色）"
    scene_blocks: List[str] = []
    for scene, body in scenes_for_entity:
        scene_blocks.append(
            f"[scene_id={scene.id}] [{scene.scene_no}] [{scene.scene_label}]\n{body}"
        )
    scenes_block = "\n\n---\n\n".join(scene_blocks) if scene_blocks else "（无可用场景片段）"
    return _BIO_USER_PROMPT.format(
        canonical_name=entity.canonical_name,
        aliases_block=aliases_block,
        appearance_count=entity.appearance_count,
        character_id=entity.id,
        other_chars_block=other_chars_block,
        scenes_block=scenes_block,
        few_shot_json=_BIO_FEW_SHOT_JSON_EXAMPLE,
    )


def _normalize_appearance(raw: Any) -> Dict[str, Any]:
    """把 LLM 给的 appearance dict 规范化到固定 schema，缺字段留空。

    每个子字段使用 ``BioFieldLimits`` 的命名常量做"语义边界友好截断"——
    既保留段落级描述，也防止 LLM 失控输出 500+ 字撑爆 UI。
    """
    out: Dict[str, Any] = {
        "age": "",
        "height": "",
        "build": "",
        "facial": "",
        "signature_props": [],
        "outfit": {"material": "", "palette": "", "form": ""},
    }
    if not isinstance(raw, dict):
        return out
    out["age"] = _clamp_text(raw.get("age"), BioFieldLimits.AGE)
    out["height"] = _clamp_text(raw.get("height"), BioFieldLimits.HEIGHT)
    out["build"] = _clamp_text(raw.get("build"), BioFieldLimits.BUILD)
    out["facial"] = _clamp_text(raw.get("facial"), BioFieldLimits.FACIAL)

    props = raw.get("signature_props")
    if isinstance(props, list):
        out["signature_props"] = [
            _clamp_text(p, BioFieldLimits.SIG_PROP)
            for p in props
            if str(p or "").strip()
        ][:_MAX_SIG_PROPS]

    outfit = raw.get("outfit") if isinstance(raw.get("outfit"), dict) else {}
    out["outfit"] = {
        "material": _clamp_text(outfit.get("material"), BioFieldLimits.OUTFIT_MATERIAL),
        "palette": _clamp_text(outfit.get("palette"), BioFieldLimits.OUTFIT_PALETTE),
        "form": _clamp_text(outfit.get("form"), BioFieldLimits.OUTFIT_FORM),
    }
    return out


def _normalize_catchphrases(raw: Any, valid_scene_ids: set) -> List[Dict[str, Any]]:
    """规范 catchphrases。scene_id 必须在 ``valid_scene_ids`` 中，否则置空。

    v2 注意：LLM 编造的 fake scene_id（"scene-01" / "S1" 等）会被这里过滤为空，
    前端展示时会显示"未绑定到具体场次"——这是降级行为，比放任 fake id 让
    "点击跳转原文"功能崩坏更安全。但 prompt 已强化纪律，预期实际命中率 >70%。
    """
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        quote = _clamp_text(item.get("quote"), BioFieldLimits.QUOTE)
        if not quote:
            continue
        scene_id_raw = str(item.get("scene_id") or "").strip()
        scene_id = scene_id_raw if scene_id_raw in valid_scene_ids else ""
        out.append({"quote": quote, "scene_id": scene_id})
        if len(out) >= _MAX_CATCHPHRASES:
            break
    return out


def _normalize_relations(raw: Any, valid_other_ids: set) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        other_id = str(item.get("other_id") or "").strip()
        if other_id not in valid_other_ids or other_id in seen:
            continue
        sentence = _clamp_text(item.get("sentence"), BioFieldLimits.RELATION_SENTENCE)
        if not sentence:
            continue
        seen.add(other_id)
        out.append({"other_id": other_id, "sentence": sentence})
        if len(out) >= _MAX_RELATIONS:
            break
    return out


def _normalize_notable_scenes(raw: Any, valid_scene_ids: set) -> List[Dict[str, Any]]:
    """规范 notable_scenes。scene_id 必须真实命中，否则整条丢弃。

    与 catchphrases 不同：catchphrases 即使 scene_id 缺失，原文 quote 仍有
    展示价值；notable_scenes 缺了 scene_id 就丧失"跳转原文"的核心价值，
    保留下来反而干扰用户，直接丢弃更干净。
    """
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        scene_id = str(item.get("scene_id") or "").strip()
        if scene_id not in valid_scene_ids or scene_id in seen:
            continue
        behavior = _clamp_text(item.get("behavior"), BioFieldLimits.NOTABLE_SCENE_BEHAVIOR)
        if not behavior:
            continue
        seen.add(scene_id)
        out.append({"scene_id": scene_id, "behavior": behavior})
        if len(out) >= _MAX_NOTABLE_SCENES:
            break
    return out


def _empty_bio(entity: CharacterEntity, *, reason: str) -> CharacterBio:
    """LLM 失败时的空占位 bio：保 character_id 联表，前端展开态降级提示。"""
    return CharacterBio(
        id=str(uuid.uuid4()),
        script_id=entity.script_id,
        character_id=entity.id,
        appearance=_normalize_appearance(None),
        evidence={"status": "failed", "reason": reason},
    )


async def _write_one_bio(
    entity: CharacterEntity,
    *,
    other_entities: List[CharacterEntity],
    scenes: List[Scene],
    caller: LlmCaller,
) -> CharacterBio:
    scenes_for_entity = _select_scenes_for_entity(entity, scenes)
    if not scenes_for_entity:
        logger.info(
            "character_pipeline: bio skipped (no scenes hit) script=%s name=%s",
            entity.script_id, entity.canonical_name,
        )
        return _empty_bio(entity, reason="no_scenes_hit")

    prompt = _build_bio_prompt(entity, other_entities, scenes_for_entity)
    valid_scene_ids = {scene.id for scene, _body in scenes_for_entity}
    valid_other_ids = {e.id for e in other_entities if e.id != entity.id}
    try:
        resp = await caller.call_json(
            prompt=prompt,
            tier=ModelTier.PRIMARY,
            system_message=_BIO_SYSTEM_PROMPT,
            temperature=0.2,
            max_tokens=TokenBudget.BIO_WRITER,
        )
    except ScoreLLMError as exc:
        logger.warning(
            "character_pipeline: bio LLM failed script=%s name=%s err=%s",
            entity.script_id, entity.canonical_name, exc,
        )
        return _empty_bio(entity, reason=f"llm_error:{type(exc).__name__}")

    parsed = resp.parsed if isinstance(resp.parsed, dict) else None
    if parsed is None:
        return _empty_bio(entity, reason="llm_non_object")

    catchphrases = _normalize_catchphrases(parsed.get("catchphrases"), valid_scene_ids)
    notable_scenes = _normalize_notable_scenes(parsed.get("notable_scenes"), valid_scene_ids)
    return CharacterBio(
        id=str(uuid.uuid4()),
        script_id=entity.script_id,
        character_id=entity.id,
        identity_present=_clamp_text(parsed.get("identity_present"), BioFieldLimits.IDENTITY),
        identity_hidden=_clamp_text(parsed.get("identity_hidden"), BioFieldLimits.IDENTITY),
        identity_origin=_clamp_text(parsed.get("identity_origin"), BioFieldLimits.IDENTITY),
        appearance=_normalize_appearance(parsed.get("appearance")),
        persona_surface=_clamp_text(parsed.get("persona_surface"), BioFieldLimits.PERSONA_SURFACE),
        persona_core=_clamp_text(parsed.get("persona_core"), BioFieldLimits.PERSONA_CORE),
        weakness=_clamp_text(parsed.get("weakness"), BioFieldLimits.WEAKNESS),
        arc_light=_clamp_text(parsed.get("arc_light"), BioFieldLimits.ARC_LIGHT),
        dialogue_style=_clamp_text(parsed.get("dialogue_style"), BioFieldLimits.DIALOGUE_STYLE),
        catchphrases=catchphrases,
        relations_summary=_normalize_relations(parsed.get("relations_summary"), valid_other_ids),
        notable_scenes=notable_scenes,
        bio_ver="v2",
        source="llm",
        evidence={
            "model": resp.model,
            "provider": resp.provider,
            "elapsed_ms": resp.elapsed_ms,
            "scenes_used": len(scenes_for_entity),
            "catchphrases_with_scene_id": sum(1 for c in catchphrases if c.get("scene_id")),
            "notable_scenes_count": len(notable_scenes),
        },
    )


async def write_bios_concurrent(
    entities: List[CharacterEntity],
    *,
    scenes: List[Scene],
    caller: LlmCaller,
    semaphore_size: int = 4,
) -> List[CharacterBio]:
    """并发为每个 entity 产出小传。

    单点失败容忍：单个 entity 的 LLM 失败不影响其他人；调用 ``_empty_bio``
    占位写表，前端展开态显示"小传未生成"。
    """
    if not entities:
        return []
    sem = asyncio.Semaphore(max(1, int(semaphore_size)))

    async def _bounded(entity: CharacterEntity) -> CharacterBio:
        async with sem:
            return await _write_one_bio(
                entity,
                other_entities=entities,
                scenes=scenes,
                caller=caller,
            )

    results = await asyncio.gather(
        *[_bounded(e) for e in entities],
        return_exceptions=True,
    )
    bios: List[CharacterBio] = []
    for entity, result in zip(entities, results):
        if isinstance(result, Exception):
            logger.exception(
                "character_pipeline: bio gather raised script=%s name=%s",
                entity.script_id, entity.canonical_name,
            )
            bios.append(_empty_bio(entity, reason=f"unexpected:{type(result).__name__}"))
        else:
            bios.append(result)
    return bios


# ============================================================
# 持久化
# ============================================================


def persist_entities(
    entities: Iterable[CharacterEntity],
    *,
    script_id: str,
    engine: Engine = default_engine,
) -> None:
    """覆盖式写入 character_entities：先 DELETE 当前 script_id 全部，再 INSERT。

    覆盖语义比 UPSERT 更适合"每次 generate_report 重算实体"的场景：避免
    历史 cluster 残留导致 id-space 漂移；外键 ON DELETE CASCADE 会自动
    清掉旧 character_relationships / character_bios。
    """
    rows = list(entities)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM scriptlens.character_entities WHERE script_id = :sid"),
            {"sid": script_id},
        )
        if not rows:
            return
        for entity in rows:
            evidence = {
                "mention_count": entity.mention_count,
                "scene_count": entity.appearance_count,
                "first_scene_id": entity.first_scene_id,
                "cluster_size": 1 + len(entity.aliases),
            }
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.character_entities
                        (id, script_id, canonical_name, aliases, role, gender, archetype, arc_type,
                         agency_level, tag_set_ver, source, evidence, created_at)
                    VALUES
                        (:id, :script_id, :canonical_name, CAST(:aliases AS jsonb), :role,
                         NULL, NULL, NULL, NULL, :tag_set_ver, :source, CAST(:evidence AS jsonb),
                         NOW())
                    """
                ),
                {
                    "id": entity.id,
                    "script_id": entity.script_id,
                    "canonical_name": entity.canonical_name,
                    "aliases": json.dumps(entity.aliases, ensure_ascii=False),
                    "role": entity.role,
                    "tag_set_ver": "v1-mvp",
                    "source": "rule",
                    "evidence": json.dumps(evidence, ensure_ascii=False),
                },
            )


def persist_relationships(
    edges: Iterable[Any],
    *,
    script_id: str,
    engine: Engine = default_engine,
) -> None:
    """覆盖式写入 character_relationships。

    入参 ``edges`` 是 character_graph_chain 输出的 ``CharacterEdge`` dataclass
    list（含 source_id / target_id / type / polarity / weight）。entity 的 UUID
    id-space 已由 persist_entities 写入；这里只把 LLM enrichment 后的
    type/polarity 入表，给前端 ``ReportPayload.character_relationships`` 与下游
    选品 / 高光集锦读取。
    """
    rows = list(edges)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM scriptlens.character_relationships WHERE script_id = :sid"),
            {"sid": script_id},
        )
        if not rows:
            return
        seen: set = set()
        for edge in rows:
            src = str(getattr(edge, "source_id", "") or "").strip()
            dst = str(getattr(edge, "target_id", "") or "").strip()
            if not src or not dst or src == dst:
                continue
            key = tuple(sorted((src, dst)))
            if key in seen:
                continue
            seen.add(key)
            rel_type = str(getattr(edge, "type", "") or "ally").strip() or "ally"
            polarity = str(getattr(edge, "polarity", "") or "mixed").strip() or "mixed"
            weight = float(getattr(edge, "weight", 0.0) or 0.0)
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.character_relationships
                        (id, script_id, src_char_id, dst_char_id,
                         relationship_type, polarity, dynamic_arc, triangle,
                         evidence, tag_set_ver, source, created_at)
                    VALUES
                        (:id, :script_id, :src, :dst,
                         :rel_type, :polarity, NULL, NULL,
                         CAST(:evidence AS jsonb), :tag_set_ver, :source, NOW())
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "script_id": script_id,
                    "src": src,
                    "dst": dst,
                    "rel_type": rel_type,
                    "polarity": polarity,
                    "evidence": json.dumps({"weight": weight}, ensure_ascii=False),
                    "tag_set_ver": "v1-mvp",
                    "source": "rule+llm",
                },
            )


def persist_bios(
    bios: Iterable[CharacterBio],
    *,
    engine: Engine = default_engine,
) -> None:
    """UPSERT 写入 character_bios（按 character_id 唯一索引去重）。

    上层调用顺序保证：persist_entities 已先清旧并写新 entities，对应
    character_id 已存在；本函数走 INSERT ... ON CONFLICT 保留写入语义
    幂等（重新生成报告时直接覆盖）。
    """
    rows = list(bios)
    if not rows:
        return
    with engine.begin() as conn:
        for bio in rows:
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.character_bios
                        (id, script_id, character_id,
                         identity_present, identity_hidden, identity_origin,
                         appearance,
                         persona_surface, persona_core, weakness, arc_light,
                         dialogue_style,
                         catchphrases, relations_summary, notable_scenes,
                         bio_ver, source, evidence, created_at, updated_at)
                    VALUES
                        (:id, :script_id, :character_id,
                         :identity_present, :identity_hidden, :identity_origin,
                         CAST(:appearance AS jsonb),
                         :persona_surface, :persona_core, :weakness, :arc_light,
                         :dialogue_style,
                         CAST(:catchphrases AS jsonb), CAST(:relations_summary AS jsonb),
                         CAST(:notable_scenes AS jsonb),
                         :bio_ver, :source, CAST(:evidence AS jsonb), NOW(), NOW())
                    ON CONFLICT (character_id) DO UPDATE SET
                        identity_present  = EXCLUDED.identity_present,
                        identity_hidden   = EXCLUDED.identity_hidden,
                        identity_origin   = EXCLUDED.identity_origin,
                        appearance        = EXCLUDED.appearance,
                        persona_surface   = EXCLUDED.persona_surface,
                        persona_core      = EXCLUDED.persona_core,
                        weakness          = EXCLUDED.weakness,
                        arc_light         = EXCLUDED.arc_light,
                        dialogue_style    = EXCLUDED.dialogue_style,
                        catchphrases      = EXCLUDED.catchphrases,
                        relations_summary = EXCLUDED.relations_summary,
                        notable_scenes    = EXCLUDED.notable_scenes,
                        bio_ver           = EXCLUDED.bio_ver,
                        source            = EXCLUDED.source,
                        evidence          = EXCLUDED.evidence,
                        updated_at        = NOW()
                    """
                ),
                {
                    "id": bio.id,
                    "script_id": bio.script_id,
                    "character_id": bio.character_id,
                    "identity_present": bio.identity_present,
                    "identity_hidden": bio.identity_hidden,
                    "identity_origin": bio.identity_origin,
                    "appearance": json.dumps(bio.appearance, ensure_ascii=False),
                    "persona_surface": bio.persona_surface,
                    "persona_core": bio.persona_core,
                    "weakness": bio.weakness,
                    "arc_light": bio.arc_light,
                    "dialogue_style": bio.dialogue_style,
                    "catchphrases": json.dumps(bio.catchphrases, ensure_ascii=False),
                    "relations_summary": json.dumps(bio.relations_summary, ensure_ascii=False),
                    "notable_scenes": json.dumps(bio.notable_scenes, ensure_ascii=False),
                    "bio_ver": bio.bio_ver,
                    "source": bio.source,
                    "evidence": json.dumps(bio.evidence, ensure_ascii=False),
                },
            )
