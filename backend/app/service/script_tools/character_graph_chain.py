"""人物关系图：共现矩阵 + LLM 关系分类。

共现负责「哪些人重要、哪些人有关」，LLM 只负责「关系是什么」。
"""

from __future__ import annotations

import itertools
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError
from service.script_tools.scene_repo import Scene, get_all_scenes
from utils.database import engine as default_engine


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

_SYSTEM_PROMPT = """你是中文短剧人物关系分析师。

共现统计已经告诉你哪些人物重要、哪些人物关系紧密。你只需要补充：
1. 每个主要人物的 role / motivation / goal / obstacle
2. 每条关系边的 type / polarity

不要编造没有根据的人物。输出短句，面向编剧和选品人员。
"""

_PROMPT = """下面是人物共现统计和部分原文场景。请补充人物关系图信息。

【主要人物】
{nodes_block}

【候选关系边】
{edges_block}

【场景样本】
{scenes_block}

输出 JSON：
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
) -> CharacterGraph:
    caller = caller or LlmCaller()
    scenes = get_all_scenes(script_id=script_id, engine=engine)
    if not scenes:
        return CharacterGraph()

    nodes, raw_edges = _cooccurrence_graph(scenes, max_nodes=max_nodes, max_edges=max_edges)
    if not nodes:
        return CharacterGraph()

    enriched = await _enrich_graph(nodes, raw_edges, scenes, caller)
    return enriched


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
            scenes_block=_sample_scenes_block(scenes, set(node_by_id)),
        ),
        tier=ModelTier.PRIMARY,
        system_message=_SYSTEM_PROMPT,
        temperature=0.2,
        max_tokens=1600,
    )
    parsed = resp.parsed if isinstance(resp.parsed, dict) else None
    if parsed is None:
        raise ScoreLLMError("character_graph_chain: LLM 返回非 JSON object")

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


def _sample_scenes_block(scenes: List[Scene], node_ids: set[str]) -> str:
    blocks: List[str] = []
    for scene in scenes[:60]:
        names = [_node_id(_clean_name(n)) for n in scene.characters or []]
        if not any(n in node_ids for n in names):
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
