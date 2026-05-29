"""character_graph_chain：resolver baseline 路径的纯单元测试。

LLM enrichment / scene 共现 fallback 走集成测试和 e2e；这里聚焦
``_build_from_resolver`` 与 ``is_real_character_name`` 的合并语义，
确保从 character_graph_builder 收编进来的过滤规则没有丢失。
"""

from service.script_tools import character_graph_chain as cgc


def test_is_real_character_name_filters_tool_npc_and_action_residue() -> None:
    assert cgc.is_real_character_name("萧南")
    assert cgc.is_real_character_name("顾晓月")
    # 通用 NPC / 道具
    assert not cgc.is_real_character_name("电话")
    assert not cgc.is_real_character_name("保镖")
    # 动作描写残片
    assert not cgc.is_real_character_name("保镖做出请的手势")
    assert not cgc.is_real_character_name("管家上前")
    # 编号化群体
    assert not cgc.is_real_character_name("场景3")
    assert not cgc.is_real_character_name("同学2")
    # 边界
    assert not cgc.is_real_character_name("")
    assert not cgc.is_real_character_name("超过八个字的名字算异常")


def test_build_from_resolver_filters_and_orders_by_appearance_count() -> None:
    characters = [
        {
            "id": "c-hero",
            "name": "萧南",
            "role_in_arc": "actor",
            "archetype": "weak_start_hidden_strong",
            "arc_type": "power_growth",
            "agency_level": "high",
            "appearance_count": 30,
        },
        {
            "id": "c-tool",
            "name": "电话",
            "role_in_arc": "helper",
            "archetype": "tool_npc",
            "arc_type": "static",
            "agency_level": "low",
            "appearance_count": 10,
        },
        {
            "id": "c-action",
            "name": "保镖做出请的手势",
            "role_in_arc": "observer",
            "archetype": "tool_npc",
            "arc_type": "static",
            "agency_level": "low",
            "appearance_count": 8,
        },
        {
            "id": "c-villain",
            "name": "顾长卿",
            "role_in_arc": "blocker",
            "archetype": "absolute_villain",
            "arc_type": "static",
            "agency_level": "medium",
            "appearance_count": 18,
        },
    ]
    relationships = [
        {"a_id": "c-hero", "b_id": "c-villain", "type": "rival", "polarity": "negative"},
        {"a_id": "c-hero", "b_id": "c-tool", "type": "ally", "polarity": "positive"},  # 节点被滤掉应丢
        {"a_id": "c-hero", "b_id": "c-villain", "type": "rival", "polarity": "negative"},  # 重复对应丢
    ]

    nodes, edges = cgc._build_from_resolver(
        characters, relationships, max_nodes=12, max_edges=30
    )

    node_ids = [n.id for n in nodes]
    assert "c-hero" in node_ids
    assert "c-villain" in node_ids
    assert "c-tool" not in node_ids  # tool npc 被 is_real_character_name 过滤
    assert "c-action" not in node_ids  # 动作残片被过滤
    # 出场次数高的排前面（c-hero=30 > c-villain=18）
    assert node_ids[0] == "c-hero"
    # 边里只剩主角-反派，并且没有重复
    assert len(edges) == 1
    assert edges[0].source_id in {"c-hero", "c-villain"}
    assert edges[0].target_id in {"c-hero", "c-villain"}
    assert edges[0].type == "rival"
    assert edges[0].polarity == "negative"


def test_build_from_resolver_uses_uuid_id_space() -> None:
    """node.id 必须直接复用 character_entities.id（保证和 report.characters[] 同 id-space）。"""
    characters = [
        {
            "id": "11111111-2222-3333-4444-555555555555",
            "name": "主角",
            "role_in_arc": "actor",
            "archetype": "weak_start_hidden_strong",
            "arc_type": "power_growth",
            "agency_level": "high",
            "appearance_count": 12,
        },
    ]
    nodes, _ = cgc._build_from_resolver(characters, [], max_nodes=12, max_edges=30)
    assert nodes
    assert nodes[0].id == "11111111-2222-3333-4444-555555555555"


def test_build_from_resolver_empty_when_no_real_characters() -> None:
    characters = [
        {"id": "c1", "name": "电话", "appearance_count": 5},
        {"id": "c2", "name": "保镖", "appearance_count": 3},
    ]
    nodes, edges = cgc._build_from_resolver(characters, [], max_nodes=12, max_edges=30)
    assert nodes == []
    assert edges == []
