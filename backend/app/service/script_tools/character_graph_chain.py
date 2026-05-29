"""人物关系图：共现矩阵 + LLM 关系分类。

共现负责「哪些人重要、哪些人有关」，LLM 只负责「关系是什么」。
"""

from __future__ import annotations

import itertools
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from service.script_tools.scene_repo import Scene, get_all_scenes
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


@dataclass
class CharacterNode:
    id: str
    name: str
    role: str = "support"
    motivation: str = ""
    goal: str = ""
    obstacle: str = ""
    first_scene_id: Optional[str] = None
    appearance_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "motivation": self.motivation,
            "goal": self.goal,
            "obstacle": self.obstacle,
            "first_scene_id": self.first_scene_id,
            "appearance_count": self.appearance_count,
        }


@dataclass
class CharacterEdge:
    source_id: str
    target_id: str
    type: str = "ally"
    weight: float = 0.0
    polarity: str = "mixed"

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "type": self.type,
            "weight": self.weight,
            "polarity": self.polarity,
        }


@dataclass
class CharacterGraph:
    nodes: List[CharacterNode] = field(default_factory=list)
    edges: List[CharacterEdge] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }


_RELATION_TYPES = {"family", "romance", "rival", "ally", "authority", "deception", "mentor"}
_POLARITIES = {"positive", "negative", "mixed"}
_ROLES = {"protagonist", "antagonist", "support", "minor"}

# 真实人物名过滤：剧本里常见的非人物条目（道具、动作描写、群体角色）。
# 这套黑名单同时被 relationship_candidate_generator 使用，所以保持模块级常量。
_ACTION_NAME_PATTERN = re.compile(
    r"(上前|迎上|走出|看向|转头|回头|冷喝|点头|挥手|指着|皱眉|扫视|目光|"
    r"翻白眼|站起|伸出|拍了拍|握住|反手|一脚|趴倒|坐下|起身|惊讶|着急|疑惑|不屑|"
    r"做出|手势|表情|动作|的样子|地说|的人)"
)
_GENERIC_CHARACTER_NAMES = {
    "出场人物",
    "电话",
    "空镜",
    "电子提示音",
    "保镖",
    "保镖若干",
    "保镖若干。",
    "宾客",
    "宾客若干",
    "工作人员",
    "小弟",
    "护士",
    "服务员",
    "店员",
}


def is_real_character_name(name: str) -> bool:
    """判定 name 是否像真实人物（剔除道具、动作描写残片、通用群体角色）。

    供 character_graph_chain 的 resolver baseline 路径和
    relationship_candidate_generator 共享，保持单一入口避免规则发散。
    """
    text = str(name or "").strip()
    if not text:
        return False
    if text in _GENERIC_CHARACTER_NAMES:
        return False
    if re.fullmatch(r"场景\d+", text):
        return False
    if re.fullmatch(r"(同学|宾客)\d+", text):
        return False
    if len(text) > 8:
        return False
    return not bool(_ACTION_NAME_PATTERN.search(text))

_SYSTEM_PROMPT = """你是中文短剧人物关系分析师。

共现统计已经告诉你哪些人物重要、哪些人物关系紧密。你只需要补充：
1. 每个主要人物的 role / motivation / goal / obstacle
2. 每条关系边的 type / polarity

关系 type 定义：
- family：血缘、婚姻、收养、家族伦理关系
- romance：恋爱、暧昧、婚恋欲望、情感吸引
- rival：竞争、敌对、目标冲突、资源争夺
- ally：同盟、协作、互相帮助、利益一致
- authority：上下级、控制、支配、雇佣、权力压迫
- deception：欺骗、隐瞒、利用、身份伪装
- mentor：师徒、引导、保护、提携

关系 polarity 使用 signed social network 的三分法，不要把 mixed 当“不确定”：
- positive：合作、保护、信任、利益一致或稳定支持
- negative：敌对、压制、背叛、威胁、竞争或目标冲突
- mixed：同一对人物同时存在正负证据，例如亲密但对立、同盟但互相利用、家人但强冲突、爱情和伤害并存

判定规则：
- 只要主要行为是伤害/压制/背叛，即使有亲缘或旧情，也优先 negative
- 只要主要行为是保护/扶持/合作，即使有轻微争吵，也优先 positive
- 只有当正负行为都推动剧情、且都不是边缘细节时，才用 mixed

不要编造没有根据的人物。输出短句，面向编剧和选品人员。

输出契约（必须严格遵守）：
- 只能输出**一个 JSON 对象**，不要 markdown / 代码块 / 解释 / 多余前后缀。
- motivation / goal / obstacle 各 ≤30 字，避免长理由把整体输出撑爆。
"""

_PROMPT = """下面是人物共现统计和部分原文场景。请补充人物关系图信息。

【主要人物】
{nodes_block}

【候选关系边】
{edges_block}

【场景样本】
{scenes_block}

输出 JSON（只能是一个 JSON 对象，不要任何额外文字）：
{{
  "nodes": [
    {{"id": "<node_id>", "role": "protagonist|antagonist|support|minor", "motivation": "≤30字", "goal": "≤30字", "obstacle": "≤30字"}}
  ],
  "edges": [
    {{"source_id": "<node_id>", "target_id": "<node_id>", "type": "family|romance|rival|ally|authority|deception|mentor", "polarity": "positive|negative|mixed"}}
  ]
}}
"""


async def extract_character_graph(
    *,
    script_id: str,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
    max_nodes: int = 12,
    max_edges: int = 30,
    characters: Optional[List[Dict]] = None,
    relationships: Optional[List[Dict]] = None,
) -> CharacterGraph:
    """抽取人物关系图。

    两条数据源路径：

    1. **resolver baseline**（优先）—— 当调用方传入 ``characters`` /
       ``relationships`` 时，节点取 ``character_entities`` 行（已合并 alias、
       归一 canonical_name），边取 ``character_relationships`` 行（已包含
       LLM 判定过的 type / polarity）。节点 id = character_entities.id（UUID），
       与报告 payload 里的 ``characters[]`` 同 id-space，前端可以跨 tab 联动。
    2. **scene 共现 fallback** —— 仅在 resolver 数据不可用时使用，节点 id
       是从 name 算出来的 slug，无法和 characters 表对齐，但能保证旧脚本
       /未跑 resolver 流水线的剧本仍出图。

    两条路径都走 LLM enrichment 补 motivation / goal / obstacle；resolver
    baseline 路径下边的 type / polarity 已存在，enrichment 仅在 LLM 给出
    合法值时覆盖。
    """
    caller = caller or LlmCaller()
    scenes = get_all_scenes(script_id=script_id, engine=engine)
    if not scenes:
        return CharacterGraph()

    nodes, raw_edges = _build_from_resolver(
        characters or [], relationships or [], max_nodes=max_nodes, max_edges=max_edges
    )
    if not nodes:
        nodes, raw_edges = _cooccurrence_graph(
            scenes, max_nodes=max_nodes, max_edges=max_edges
        )
    if not nodes:
        return CharacterGraph()

    # 收集每个 node 的 canonical_name 和 aliases，给 _sample_scenes_block 用做
    # scene.characters 匹配。resolver 路径下 node.id 是 UUID 和 scene 的 raw 名字
    # 无法直接比对，必须靠 name 集合做模糊匹配。fallback 路径下 aliases 为空，
    # 仍然能按 node.name 自身匹配回原 scene。
    node_aliases: Dict[str, List[str]] = {}
    if characters:
        char_by_id = {str(c.get("id") or ""): c for c in characters}
        for node in nodes:
            entry = char_by_id.get(node.id)
            if entry is None:
                continue
            raw_aliases = entry.get("aliases") or []
            if isinstance(raw_aliases, list):
                node_aliases[node.id] = [str(a) for a in raw_aliases if str(a).strip()]

    # LLM enrichment 是「锦上添花」：motivation / goal / obstacle 完全依赖 LLM；
    # role / type / polarity 在 resolver 路径下已有默认，LLM 只在给出合法值时覆盖。
    # enrichment 失败时不该让整张图消失——退化成「只有基线的图」远比「图整个没了」对用户更友好。
    try:
        return await _enrich_graph(nodes, raw_edges, scenes, caller, node_aliases)
    except ScoreLLMError as exc:
        logger.exception(
            "character_graph_chain: LLM enrichment 失败，降级返回基线图（保留 %d 节点 / %d 边）: %s",
            len(nodes), len(raw_edges), exc,
        )
        return CharacterGraph(nodes=nodes, edges=raw_edges)


def _build_from_resolver(
    characters: List[Dict],
    relationships: List[Dict],
    *,
    max_nodes: int,
    max_edges: int,
) -> Tuple[List[CharacterNode], List[CharacterEdge]]:
    """从 character_entities + character_relationships 表数据构造基线节点/边。

    输入字段约定（与 script_report_service._load_characters /
    _load_character_relationships 输出一致）：

      characters[i]    : id, name, archetype, role_in_arc, arc_type,
                          agency_level, appearance_count
      relationships[i] : a_id, b_id, type, polarity

    返回的 ``CharacterNode.id`` 是 character_entities.id（UUID），保证和
    report payload 里的 characters[] / character_relationships[] 同 id-space。
    """
    valid_chars = [
        c
        for c in characters
        if str(c.get("id") or "").strip()
        and is_real_character_name(str(c.get("name") or ""))
    ]
    if not valid_chars:
        return [], []
    valid_chars.sort(
        key=lambda c: (
            -int(c.get("appearance_count") or 0),
            str(c.get("name") or ""),
        )
    )
    top = valid_chars[:max_nodes]
    node_ids = {str(c.get("id") or "") for c in top}

    nodes: List[CharacterNode] = []
    for idx, character in enumerate(top):
        nodes.append(
            CharacterNode(
                id=str(character.get("id") or ""),
                name=str(character.get("name") or ""),
                role=_initial_role(character, idx),
                appearance_count=int(character.get("appearance_count") or 0),
            )
        )

    edge_by_pair: Dict[Tuple[str, str], CharacterEdge] = {}
    for rel in relationships:
        a, b = str(rel.get("a_id") or ""), str(rel.get("b_id") or "")
        if not a or not b or a == b:
            continue
        if a not in node_ids or b not in node_ids:
            continue
        rel_type = str(rel.get("type") or "").strip()
        if rel_type not in _RELATION_TYPES:
            continue
        polarity = str(rel.get("polarity") or "").strip()
        if polarity not in _POLARITIES:
            polarity = "mixed"
        key = tuple(sorted((a, b)))  # type: ignore[assignment]
        if key in edge_by_pair:
            # 已有同对边，保留 type 非默认的那条
            continue
        edge_by_pair[key] = CharacterEdge(
            source_id=a,
            target_id=b,
            type=rel_type,
            polarity=polarity,
            # 表里没强度信号，给中等默认；前端用 weight 控制连线粗细，太低会看不见
            weight=0.5,
        )

    edges = list(edge_by_pair.values())[:max_edges]
    return nodes, edges


def _initial_role(character: Dict, index: int) -> str:
    """基于 character_entities 的语义字段推断节点 role 初值。

    LLM enrichment 会在 _apply_node_enrichment 阶段覆盖；这里给出一个
    确定性的兜底，保证 LLM 失败时主角/反派还能区分。
    """
    role_in_arc = str(character.get("role_in_arc") or "").lower()
    archetype = str(character.get("archetype") or "").lower()
    agency_level = str(character.get("agency_level") or "").lower()
    if index == 0 or role_in_arc == "actor":
        return "protagonist"
    if role_in_arc == "blocker" or "villain" in archetype:
        return "antagonist"
    if role_in_arc in {"helper", "mentor", "catalyst"} or agency_level == "high":
        return "support"
    return "minor"


def _cooccurrence_graph(
    scenes: List[Scene],
    *,
    max_nodes: int,
    max_edges: int,
) -> Tuple[List[CharacterNode], List[CharacterEdge]]:
    appearance: Counter[str] = Counter()
    first_scene: Dict[str, str] = {}
    cooccur: Counter[tuple[str, str]] = Counter()

    for scene in scenes:
        names = [_clean_name(n) for n in scene.characters or []]
        names = [n for n in dict.fromkeys(names) if n]
        for name in names:
            appearance[name] += 1
            first_scene.setdefault(name, scene.id)
        for a, b in itertools.combinations(sorted(names), 2):
            cooccur[(a, b)] += 1

    top_names = [name for name, _count in appearance.most_common(max_nodes)]
    top_set = set(top_names)
    if not top_names:
        return [], []

    # Jaccard 归一：cooccur(a,b) / (appear(a) + appear(b) - cooccur(a,b))。
    # 避免「一场宴会同框 6 人 → 6 人两两共现都=1.0」导致的家族节点全塌成一点。
    edges: List[CharacterEdge] = []
    for (a, b), count in cooccur.most_common(max_edges * 4):
        if a not in top_set or b not in top_set:
            continue
        union = appearance[a] + appearance[b] - count
        if union <= 0:
            continue
        weight = round(count / union, 3)
        if weight < 0.12:
            continue
        edges.append(
            CharacterEdge(
                source_id=_node_id(a),
                target_id=_node_id(b),
                weight=weight,
            )
        )
        if len(edges) >= max_edges:
            break

    nodes = [
        CharacterNode(
            id=_node_id(name),
            name=name,
            first_scene_id=first_scene.get(name),
            appearance_count=appearance[name],
        )
        for name in top_names
    ]
    return nodes, edges


async def _enrich_graph(
    nodes: List[CharacterNode],
    edges: List[CharacterEdge],
    scenes: List[Scene],
    caller: LlmCaller,
    node_aliases: Optional[Dict[str, List[str]]] = None,
) -> CharacterGraph:
    node_by_id = {node.id: node for node in nodes}
    edge_by_pair = {_edge_key(edge.source_id, edge.target_id): edge for edge in edges}

    resp = await caller.call_json(
        prompt=_PROMPT.format(
            nodes_block="\n".join(
                f"- {node.id}|{node.name}|出场{node.appearance_count}场" for node in nodes
            ),
            edges_block="\n".join(
                f"- {edge.source_id}|{edge.target_id}|共现权重{edge.weight}" for edge in edges
            ),
            scenes_block=_sample_scenes_block(scenes, nodes, node_aliases or {}),
        ),
        tier=ModelTier.PRIMARY,
        system_message=_SYSTEM_PROMPT,
        temperature=0.2,
        max_tokens=TokenBudget.CHARACTER_GRAPH,
    )
    parsed = resp.parsed if isinstance(resp.parsed, dict) else None
    if parsed is None:
        raise ScoreLLMError(
            f"character_graph_chain: LLM 返回非 JSON object（type={type(resp.parsed).__name__}，"
            f"raw 前 200 字：{(getattr(resp, 'raw', '') or '')[:200]!r}）"
        )

    _apply_node_enrichment(nodes, parsed.get("nodes"), node_by_id)
    _apply_edge_enrichment(edges, parsed.get("edges"), edge_by_pair)
    return CharacterGraph(nodes=nodes, edges=edges)


def _apply_node_enrichment(
    nodes: List[CharacterNode],
    raw: object,
    node_by_id: Dict[str, CharacterNode],
) -> None:
    if not isinstance(raw, list):
        return
    for item in raw:
        if not isinstance(item, dict):
            continue
        node = node_by_id.get(str(item.get("id") or "").strip())
        if node is None:
            continue
        role = str(item.get("role") or "").strip()
        if role in _ROLES:
            node.role = role
        node.motivation = _short(item.get("motivation"), 30)
        node.goal = _short(item.get("goal"), 30)
        node.obstacle = _short(item.get("obstacle"), 30)

    if nodes:
        nodes[0].role = "protagonist" if nodes[0].role == "support" else nodes[0].role


def _apply_edge_enrichment(
    edges: List[CharacterEdge],
    raw: object,
    edge_by_pair: Dict[str, CharacterEdge],
) -> None:
    if not isinstance(raw, list):
        return
    for item in raw:
        if not isinstance(item, dict):
            continue
        edge = edge_by_pair.get(_edge_key(str(item.get("source_id") or ""), str(item.get("target_id") or "")))
        if edge is None:
            continue
        rel_type = str(item.get("type") or "").strip()
        polarity = str(item.get("polarity") or "").strip()
        if rel_type in _RELATION_TYPES:
            edge.type = rel_type
        if polarity in _POLARITIES:
            edge.polarity = polarity


def _sample_scenes_block(
    scenes: List[Scene],
    nodes: List[CharacterNode],
    node_aliases: Dict[str, List[str]],
) -> str:
    """挑选涉及主要节点的场景作为 LLM 上下文。

    匹配口径：scene.characters 中的任一原始名（清洗后）命中节点 canonical_name
    或其 alias 集合即视为涉及。这套口径同时支持：

      - resolver 路径（node.id = UUID，必须靠 name+alias 才能匹配 scene 名）
      - fallback 共现路径（node.id 是 name slug，但 node.name 仍是清洗后名字，
        aliases 为空时退化为纯 name 匹配）
    """
    matchable: set[str] = set()
    for node in nodes:
        cleaned = _clean_name(node.name)
        if cleaned:
            matchable.add(cleaned)
        for alias in node_aliases.get(node.id, []):
            cleaned_alias = _clean_name(alias)
            if cleaned_alias:
                matchable.add(cleaned_alias)
    matchable.discard("")
    if not matchable:
        return ""

    blocks: List[str] = []
    for scene in scenes[:60]:
        scene_names = {_clean_name(n) for n in (scene.characters or [])}
        scene_names.discard("")
        if not (scene_names & matchable):
            continue
        text = scene.text or ""
        if len(text) > 500:
            text = text[:500] + "..."
        blocks.append(
            f"[{scene.scene_no}] [{scene.scene_label}] [人物:{','.join(scene.characters[:6])}]\n{text}"
        )
        if len(blocks) >= 12:
            break
    return "\n\n---\n\n".join(blocks)


_GENERIC_NAME_TOKENS = {
    "龙套",
    "群众",
    "众人",
    "所有人",
    "路人",
    "保镖",
    "侍卫",
    "村民",
    "记者",
    "保安",
    "士兵",
    "警察",
    "客人",
    "同学",
    "同事",
    "邻居",
    "护士",
    "医生",
    "司机",
    "服务员",
    "美女",
    "男子",
    "女子",
    "男人",
    "女人",
    "小孩",
    "孩子",
    "老人",
    "旁白",
}

_NAME_FORBIDDEN_RE = re.compile(r"^[\d\W_]+$", re.UNICODE)


def _clean_name(name: str) -> str:
    cleaned = re.sub(r"[（(].*?[）)]", "", name or "").strip()
    cleaned = cleaned.strip("、,，.。;；:：·-—_/\\\"'`!！?？*+~ \t")
    if not cleaned or len(cleaned) < 2 or len(cleaned) > 8:
        # 纯数字 / 单字残片（如 "2"、"3"、"龙套1、2、3" 切坏后的碎片）一律丢
        return ""
    if _NAME_FORBIDDEN_RE.match(cleaned):
        return ""
    if any(
        cleaned == token or cleaned.startswith(token) or cleaned.endswith(token)
        for token in _GENERIC_NAME_TOKENS
    ):
        # 「龙套X」「黑衣男子」「旗袍美女」这种通用角色，剧本图谱意义不大
        return ""
    if re.search(r"\d{1,2}$", cleaned) and len(cleaned) <= 4:
        # "美女1"、"龙套2"、"路人3"、"工人4"——通用角色 + 编号
        return ""
    return cleaned


def _node_id(name: str) -> str:
    return re.sub(r"\W+", "_", name, flags=re.UNICODE).strip("_") or "unknown"


def _edge_key(a: str, b: str) -> str:
    x, y = sorted((a.strip(), b.strip()))
    return f"{x}|{y}"


def _short(value: object, max_len: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
