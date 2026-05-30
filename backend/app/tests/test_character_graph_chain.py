"""character_graph_chain：resolver baseline 路径的纯单元测试。

LLM enrichment / scene 共现 fallback 走集成测试和 e2e；这里聚焦
``_build_from_resolver`` 与 ``is_real_character_name`` 的合并语义，
确保从 character_graph_builder 收编进来的过滤规则没有丢失。
"""

from typing import List

from service.script_tools import character_graph_chain as cgc
from service.script_tools.scene_repo import Scene


def _scene(*, sid: str, no: str, chars: List[str], episode: int = 1) -> Scene:
    return Scene(
        id=sid,
        script_id="s-1",
        episode_no=episode,
        scene_no=no,
        scene_label=f"E{episode:02d}-S{no}",
        characters=chars,
        start_line=None,
        end_line=None,
        text="",
    )


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
        characters, relationships, scenes=[], max_nodes=12, max_edges=30
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
    nodes, _ = cgc._build_from_resolver(
        characters, [], scenes=[], max_nodes=12, max_edges=30
    )
    assert nodes
    assert nodes[0].id == "11111111-2222-3333-4444-555555555555"


def test_build_from_resolver_empty_when_no_real_characters() -> None:
    characters = [
        {"id": "c1", "name": "电话", "appearance_count": 5},
        {"id": "c2", "name": "保镖", "appearance_count": 3},
    ]
    nodes, edges = cgc._build_from_resolver(
        characters, [], scenes=[], max_nodes=12, max_edges=30
    )
    assert nodes == []
    assert edges == []


def test_build_from_resolver_bridges_top_n_subgraph_orphans() -> None:
    """v4 subgraph 连通性兜底回归：top-N 截断后仍必须连通。

    根因复现：
    - entities 总 6 个，max_nodes=4 → C / D 被截掉
    - relationships 只覆盖了 A-B 一条强边，E / F 之间没有显式 relationship
    - 但 scenes 共现里：E 和 A 同框、F 和 E 同框 → 子集上仍有连通信号
    - 修复前：F 完全孤立；修复后：bridge edge 把 F 连到 E（或同等强度邻居）
    """
    characters = [
        {"id": "ent-a", "name": "顾晓月", "aliases": ["晓月"], "appearance_count": 30},
        {"id": "ent-b", "name": "顾长卿", "aliases": [], "appearance_count": 25},
        {"id": "ent-c", "name": "C 小角", "aliases": [], "appearance_count": 4},
        {"id": "ent-d", "name": "D 小角", "aliases": [], "appearance_count": 3},
        {"id": "ent-e", "name": "宋芸", "aliases": [], "appearance_count": 22},
        {"id": "ent-f", "name": "顾老爷子", "aliases": ["老爷子"], "appearance_count": 18},
    ]
    relationships = [
        {"a_id": "ent-a", "b_id": "ent-b", "type": "rival", "polarity": "negative"},
    ]
    scenes = [
        _scene(sid="s1", no="1-1", chars=["顾晓月", "宋芸"]),  # A-E 共现
        _scene(sid="s2", no="1-2", chars=["顾晓月", "宋芸"]),  # A-E 再共现
        _scene(sid="s3", no="2-1", chars=["宋芸", "顾老爷子"]),  # E-F 共现
        _scene(sid="s4", no="2-2", chars=["宋芸", "老爷子"]),  # E-F 通过 alias 命中
        _scene(sid="s5", no="3-1", chars=["顾长卿"]),
    ]

    nodes, edges = cgc._build_from_resolver(
        characters, relationships, scenes=scenes, max_nodes=4, max_edges=30
    )
    node_ids = {n.id for n in nodes}
    assert node_ids == {"ent-a", "ent-b", "ent-e", "ent-f"}

    adj: dict = {nid: set() for nid in node_ids}
    for e in edges:
        adj[e.source_id].add(e.target_id)
        adj[e.target_id].add(e.source_id)

    # 子图必须全连通：从 ent-a 出发能 BFS 到所有 4 个节点
    seen = {"ent-a"}
    stack = ["ent-a"]
    while stack:
        cur = stack.pop()
        for nxt in adj[cur]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    assert seen == node_ids, f"top-N 截断后未重做连通性，bridge 失效：{seen} ≠ {node_ids}"


def test_build_from_resolver_hard_bridges_when_no_cross_component_cooccurrence() -> None:
    """线上稳态兜底：即使完全无跨分量共现，也要强制连通。

    构造：
    - 主分量：A-B（来自 relationships）
    - 孤立分量：E-F（只在自己组件内部共现）
    - A/B 与 E/F 在 scenes 中完全没有同框

    期望：
    - 仍输出强连通图（通过 hard bridge）
    - hard bridge 的 weight 使用固定兜底值，避免被误判为高置信关系
    """
    characters = [
        {"id": "ent-a", "name": "顾晓月", "aliases": ["晓月"], "appearance_count": 30},
        {"id": "ent-b", "name": "顾长卿", "aliases": [], "appearance_count": 25},
        {"id": "ent-e", "name": "宋芸", "aliases": [], "appearance_count": 22},
        {"id": "ent-f", "name": "顾老爷子", "aliases": ["老爷子"], "appearance_count": 18},
    ]
    relationships = [
        {"a_id": "ent-a", "b_id": "ent-b", "type": "rival", "polarity": "negative"},
    ]
    scenes = [
        _scene(sid="s1", no="1-1", chars=["顾晓月"]),
        _scene(sid="s2", no="1-2", chars=["顾长卿"]),
        _scene(sid="s3", no="2-1", chars=["宋芸", "顾老爷子"]),
        _scene(sid="s4", no="2-2", chars=["宋芸", "老爷子"]),
    ]

    nodes, edges = cgc._build_from_resolver(
        characters, relationships, scenes=scenes, max_nodes=4, max_edges=30
    )
    node_ids = {n.id for n in nodes}
    assert node_ids == {"ent-a", "ent-b", "ent-e", "ent-f"}

    adj: dict = {nid: set() for nid in node_ids}
    for e in edges:
        adj[e.source_id].add(e.target_id)
        adj[e.target_id].add(e.source_id)

    seen = {"ent-a"}
    stack = ["ent-a"]
    while stack:
        cur = stack.pop()
        for nxt in adj[cur]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    assert seen == node_ids, f"无跨分量共现时未触发硬兜底桥接：{seen} ≠ {node_ids}"

    left = {"ent-a", "ent-b"}
    right = {"ent-e", "ent-f"}
    cross_edges = [
        e
        for e in edges
        if (e.source_id in left and e.target_id in right)
        or (e.source_id in right and e.target_id in left)
    ]
    assert cross_edges, "硬兜底后应至少存在一条跨分量 bridge edge"
    assert any(
        abs(e.weight - cgc._HARD_BRIDGE_WEIGHT) < 1e-9 for e in cross_edges
    ), "hard bridge 应使用固定低权重，避免伪装高置信"
