from __future__ import annotations

import re
from typing import Any

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
_FRONTEND_RELATION_TYPES = {
    "family",
    "romance",
    "rival",
    "ally",
    "authority",
    "deception",
    "mentor",
}


def is_real_character_name(name: str) -> bool:
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


def build_character_graph(
    *,
    characters: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    max_nodes: int = 12,
) -> dict[str, Any]:
    valid_characters = [
        character
        for character in characters
        if is_real_character_name(str(character.get("name") or ""))
    ]
    sorted_characters = sorted(
        valid_characters,
        key=lambda character: (
            -int(character.get("appearance_count") or 0),
            str(character.get("name") or ""),
        ),
    )[:max_nodes]
    node_ids = {str(character.get("id") or "") for character in sorted_characters}
    nodes = [
        {
            "id": str(character.get("id") or ""),
            "name": str(character.get("name") or ""),
            "role": _graph_role(character, index),
            "motivation": _node_motivation(character),
            "goal": _node_goal(character),
            "obstacle": _node_obstacle(character),
            "first_scene_id": None,
            "appearance_count": int(character.get("appearance_count") or 0),
        }
        for index, character in enumerate(sorted_characters)
        if str(character.get("id") or "")
    ]

    raw_edges: list[dict[str, Any]] = []
    max_strength = 1
    for relationship in relationships:
        source_id = str(relationship.get("a_id") or "")
        target_id = str(relationship.get("b_id") or "")
        if source_id not in node_ids or target_id not in node_ids or source_id == target_id:
            continue
        relation_type = _relation_type(str(relationship.get("type") or ""))
        if relation_type is None:
            continue
        strength = _relation_strength(relationship)
        max_strength = max(max_strength, strength)
        raw_edges.append(
            {
                "source_id": source_id,
                "target_id": target_id,
                "type": relation_type,
                "polarity": _relation_polarity(str(relationship.get("polarity") or "")),
                "strength": strength,
            }
        )

    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in raw_edges:
        left, right = sorted([edge["source_id"], edge["target_id"]])
        key = (left, right)
        current = deduped.get(key)
        if current is None or edge["strength"] > current["strength"]:
            deduped[key] = edge

    edges = []
    for edge in sorted(deduped.values(), key=lambda item: -int(item["strength"])):
        weight = round(max(0.15, min(1.0, int(edge["strength"]) / max_strength)), 4)
        edges.append(
            {
                "source_id": edge["source_id"],
                "target_id": edge["target_id"],
                "type": edge["type"],
                "weight": weight,
                "polarity": edge["polarity"],
            }
        )

    return {"nodes": nodes, "edges": edges[:30]}


def _graph_role(character: dict[str, Any], index: int) -> str:
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


def _node_motivation(character: dict[str, Any]) -> str:
    role = str(character.get("role_in_arc") or "").strip()
    if role == "actor":
        return "推动主线行动"
    if role == "blocker":
        return "阻碍主线目标"
    if role == "helper":
        return "协助主线推进"
    if role == "mentor":
        return "提供关键支持"
    return ""


def _node_goal(character: dict[str, Any]) -> str:
    arc_type = str(character.get("arc_type") or "").strip()
    if arc_type == "power_growth":
        return "完成身份或力量上升"
    if arc_type == "redemption":
        return "完成补偿与修复"
    if arc_type == "tragic_fall":
        return "维持既有利益"
    return ""


def _node_obstacle(character: dict[str, Any]) -> str:
    role = str(character.get("role_in_arc") or "").strip()
    if role == "blocker":
        return "与主线目标冲突"
    if role == "actor":
        return "外部压制与误解"
    return ""


def _relation_type(raw: str) -> str | None:
    value = raw.strip().lower()
    if value in _FRONTEND_RELATION_TYPES:
        return value
    if value == "friendship":
        return "ally"
    if value == "blood_oath":
        return "ally"
    if value == "servant":
        return "authority"
    return None


def _relation_polarity(raw: str) -> str:
    value = raw.strip().lower()
    if value in {"positive", "negative", "mixed"}:
        return value
    return "mixed"


def _relation_strength(relationship: dict[str, Any]) -> int:
    evidence = relationship.get("evidence")
    if isinstance(evidence, dict):
        raw = evidence.get("cooccurrence")
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return 1
