"""character_pipeline 单元测试。

聚焦三块纯本地能力（不调 LLM、不连 DB）：

1. ``resolve_entities`` 的别名归一与角色排序
2. ``cooccurrence_candidate_relationships`` 的 Jaccard 归一与字段契约
3. ``CharacterEntity.to_chain_dict`` 与 character_graph_chain 的对接契约

bio 写作链路（``write_bios_concurrent``）依赖 LlmCaller，留给集成测试。
"""

from __future__ import annotations

import asyncio
from typing import List

from service.script_tools import character_pipeline as cp
from service.script_tools.scene_repo import Scene


def _scene(
    *,
    sid: str,
    no: str,
    chars: List[str],
    text: str = "",
    episode: int = 1,
) -> Scene:
    return Scene(
        id=sid,
        script_id="s-1",
        episode_no=episode,
        scene_no=no,
        scene_label=f"E{episode:02d}-S{no}",
        characters=chars,
        start_line=None,
        end_line=None,
        text=text,
    )


def test_normalize_name_strips_brackets_and_punct() -> None:
    assert cp._normalize_name("鹿鸣于（OS）") == "鹿鸣于"
    assert cp._normalize_name("：鹿鸣于") == "鹿鸣于"
    assert cp._normalize_name("  ") == ""


def test_name_similarity_substring_treated_as_alias() -> None:
    # 包含关系是短剧 alias 的最强证据：「鹿鸣于」/「鹿鸣于OS」
    assert cp._name_similarity("鹿鸣于", "鹿鸣于OS") >= 0.9
    # 单字不该被吞：避免「于」匹配「鹿鸣于」
    assert cp._name_similarity("于", "鹿鸣于") < 0.9


def test_resolve_entities_merges_aliases_and_orders_by_scene_count() -> None:
    scenes = [
        _scene(sid="sc-1", no="1-1", chars=["鹿鸣于", "段休冥"]),
        _scene(sid="sc-2", no="1-2", chars=["鹿鸣于OS", "段休冥"]),
        _scene(sid="sc-3", no="1-3", chars=["鹿鸣于", "鹿秋良"]),
        _scene(sid="sc-4", no="1-4", chars=["段休冥", "鹿秋良"]),
        _scene(sid="sc-5", no="1-5", chars=["保镖若干", "鹿鸣于"]),
    ]
    entities = asyncio.run(
        cp.resolve_entities(script_id="s-1", scenes=scenes)
    )
    by_canonical = {e.canonical_name: e for e in entities}

    # 别名聚合：「鹿鸣于」/「鹿鸣于OS」必须合并到同一 canonical
    assert "鹿鸣于" in by_canonical
    luminyu = by_canonical["鹿鸣于"]
    assert "鹿鸣于OS" in luminyu.aliases

    # 通用群体不能成为 entity（被 is_real_character_name 过滤）
    assert "保镖若干" not in by_canonical

    # 出场最多的是主角（rank 0）
    assert entities[0].canonical_name == "鹿鸣于"
    assert entities[0].role == "protagonist"
    # 鹿鸣于场次：sc-1, sc-2, sc-3, sc-5 = 4 场（含 alias 命中的 sc-2）
    assert luminyu.appearance_count == 4

    # id 是 UUID（character_graph / character_bios 共用 id-space 的锚点）
    assert all(len(e.id) == 36 and e.id.count("-") == 4 for e in entities)


def test_resolve_entities_rejects_action_residue_and_generic_groups() -> None:
    scenes = [
        _scene(sid="sc-1", no="1-1", chars=["林初", "管家上前", "宾客若干"]),
        _scene(sid="sc-2", no="1-2", chars=["林初", "保镖", "电话"]),
    ]
    entities = asyncio.run(cp.resolve_entities(script_id="s-1", scenes=scenes))
    names = {e.canonical_name for e in entities}
    assert names == {"林初"}


def test_to_chain_dict_matches_character_graph_chain_contract() -> None:
    """to_chain_dict 字段必须 100% 对齐 chain._build_from_resolver 期望。

    陷在这里就是图谱节点丢空——直接锁死 schema。
    """
    entity = cp.CharacterEntity(
        id="00000000-0000-0000-0000-00000000abcd",
        script_id="s-1",
        canonical_name="鹿鸣于",
        aliases=["鹿鸣于OS"],
        role="protagonist",
        appearance_count=4,
        first_scene_id="sc-1",
        mention_count=5,
    )
    payload = entity.to_chain_dict()
    assert set(payload.keys()) == {
        "id",
        "name",
        "aliases",
        "archetype",
        "role_in_arc",
        "arc_type",
        "agency_level",
        "appearance_count",
    }
    assert payload["id"] == entity.id
    assert payload["name"] == entity.canonical_name
    assert payload["aliases"] == ["鹿鸣于OS"]
    assert payload["appearance_count"] == 4


def test_cooccurrence_candidate_relationships_uses_entity_ids_and_jaccard() -> None:
    e_a = cp.CharacterEntity(id="ent-a", script_id="s-1", canonical_name="A", aliases=["A1"])
    e_b = cp.CharacterEntity(id="ent-b", script_id="s-1", canonical_name="B", aliases=[])
    e_c = cp.CharacterEntity(id="ent-c", script_id="s-1", canonical_name="C", aliases=[])
    entities = [e_a, e_b, e_c]
    scenes = [
        _scene(sid="sc-1", no="1-1", chars=["A", "B"]),
        _scene(sid="sc-2", no="1-2", chars=["A1", "B"]),  # alias 命中算 A
        _scene(sid="sc-3", no="1-3", chars=["A", "C"]),
        _scene(sid="sc-4", no="1-4", chars=["B"]),
    ]
    rels = cp.cooccurrence_candidate_relationships(entities, scenes, max_edges=10)
    # AB 共 2 场，AC 共 1 场 —— Jaccard 不低于阈值的边都进
    pairs = {tuple(sorted((r["a_id"], r["b_id"]))) for r in rels}
    assert ("ent-a", "ent-b") in pairs
    # 字段契约：必须含 type / polarity 占位（让 chain LLM enrichment 能接住）
    for rel in rels:
        assert rel["type"] == "ally"
        assert rel["polarity"] == "mixed"
        assert rel["a_id"] in {e.id for e in entities}
        assert rel["b_id"] in {e.id for e in entities}


def test_normalize_appearance_fills_missing_fields_with_empty() -> None:
    out = cp._normalize_appearance({"age": "二十", "outfit": {"palette": "玄黑"}})
    assert out["age"] == "二十"
    assert out["height"] == ""
    assert out["outfit"]["palette"] == "玄黑"
    assert out["outfit"]["material"] == ""
    assert out["signature_props"] == []
    # 非 dict 入参也要兜底
    out2 = cp._normalize_appearance(None)
    assert out2["outfit"] == {"material": "", "palette": "", "form": ""}


def test_clamp_text_returns_original_when_within_soft_limit() -> None:
    """soft 内不动；hard 内放过；超 hard 走句末标点回退。

    替代 v1-mvp 的 `s[:max-1] + "…"` 中文中间硬截做法。
    """
    short = "二十出头"
    assert cp._clamp_text(short, cp.BioFieldLimits.AGE) == short


def test_clamp_text_falls_back_to_sentence_break_when_exceeds_hard() -> None:
    soft, hard = cp.BioFieldLimits.PERSONA_SURFACE  # (120, 240)
    # 重复一段含句末标点的短语，确保总长度超过 hard
    sentence = "她在豪宅里扮演温顺的女佣，每一句话都精准得体；"  # 含 23 字符，含 1 个句末标点
    head = sentence * 12  # 12 × 23 = 276 字符
    tail = "在独处的夜里，她把母亲的旧账本翻到掉皮的那一页。"
    long = head + tail
    assert len(long) > hard
    out = cp._clamp_text(long, cp.BioFieldLimits.PERSONA_SURFACE)
    # 必须切到一个句末标点后，不会留半个未完成中文短语
    assert out.endswith(("。", "；", "！", "？"))
    assert soft <= len(out) <= hard


def test_clamp_text_handles_empty_and_non_string() -> None:
    assert cp._clamp_text(None, cp.BioFieldLimits.AGE) == ""
    assert cp._clamp_text("", cp.BioFieldLimits.AGE) == ""
    # 非字符串也要兜底（不抛异常）
    assert cp._clamp_text(123, cp.BioFieldLimits.AGE) == "123"


def test_normalize_notable_scenes_rejects_unknown_scene_ids_entirely() -> None:
    """notable_scenes 不同于 catchphrases：scene_id 缺失时整条丢弃。

    catchphrases 即使 scene_id 缺失，原文 quote 仍有展示价值；
    notable_scenes 缺了 scene_id 就丧失"跳转原文"的核心价值，保留下来反而干扰。
    """
    raw = [
        {"scene_id": "sc-1", "behavior": "她踏入豪宅完成首次试探。"},
        {"scene_id": "sc-fake", "behavior": "应被丢弃：scene_id 不在合法集合。"},
        {"scene_id": "sc-1", "behavior": "重复 scene_id 应去重。"},
        {"scene_id": "sc-2", "behavior": ""},  # behavior 空 → 丢弃
        {"scene_id": "sc-3", "behavior": "找到关键证据，决心动手。"},
        {"scene_id": "sc-4", "behavior": "第 4 条仍合法。"},
        {"scene_id": "sc-5", "behavior": "第 5 条会被 max=3 截掉。"},
    ]
    valid = {"sc-1", "sc-2", "sc-3", "sc-4", "sc-5"}
    out = cp._normalize_notable_scenes(raw, valid_scene_ids=valid)
    assert len(out) == cp._MAX_NOTABLE_SCENES  # 3
    assert [r["scene_id"] for r in out] == ["sc-1", "sc-3", "sc-4"]


def test_normalize_catchphrases_drops_unknown_scene_ids_and_caps_to_5() -> None:
    raw = [
        {"quote": "台词1", "scene_id": "sc-1"},
        {"quote": "台词2", "scene_id": "sc-fake"},  # 不在 valid 集合 → 落空
        {"quote": "", "scene_id": "sc-1"},  # 空台词丢弃
        {"quote": "台词3", "scene_id": "sc-2"},
        {"quote": "台词4", "scene_id": "sc-1"},
        {"quote": "台词5", "scene_id": "sc-1"},
        {"quote": "台词6", "scene_id": "sc-1"},  # 第 6 条被截掉
    ]
    out = cp._normalize_catchphrases(raw, valid_scene_ids={"sc-1", "sc-2"})
    assert len(out) == 5
    # 第二条 scene_id 被洗为空（保留台词，丢错误锚点）
    assert out[1]["scene_id"] == ""


def test_empty_bio_keeps_character_id_for_join() -> None:
    """单点失败时，空占位 bio 仍要保 character_id 联表，否则前端 join 不到。"""
    entity = cp.CharacterEntity(
        id="ent-x", script_id="s-1", canonical_name="X"
    )
    bio = cp._empty_bio(entity, reason="llm_error:Timeout")
    assert bio.character_id == entity.id
    assert bio.script_id == entity.script_id
    assert bio.evidence["status"] == "failed"
    # 字段保持空，而不是缺失
    assert bio.identity_present == ""
    assert bio.appearance == cp._normalize_appearance(None)


def test_write_bios_concurrent_isolates_individual_failures() -> None:
    """单个 entity 的 LLM 异常不应阻塞其他人的 bio 产出。"""
    entities = [
        cp.CharacterEntity(id=f"ent-{i}", script_id="s-1", canonical_name=f"角色{i}")
        for i in range(3)
    ]
    scenes = [_scene(sid=f"sc-{i}", no=f"1-{i}", chars=[f"角色{i}"]) for i in range(3)]

    class _StubCaller:
        """第 0/2 个 entity 正常返回 dict；第 1 个（character_id=ent-1）抛 ScoreLLMError。

        prompt 模板里『其他主要角色 id 表』会列出全部 entity，因此 stub 只能按
        『目标角色 character_id: ent-X』这行（_build_bio_prompt 里 character_id 字段
        紧跟 canonical_name 块）唯一定位当前调用的 entity。
        """

        async def call_json(self, *args, **kwargs):
            from service.script_tools import llm_caller as lc

            prompt = kwargs.get("prompt") or args[0]
            if "角色 id: ent-1" in prompt:
                raise lc.ScoreLLMError("simulated")
            return lc.LLMResponse(
                raw="{}",
                parsed={
                    "identity_present": "测试身份",
                    "persona_core": "测试性格",
                    "catchphrases": [],
                    "relations_summary": [],
                },
                provider="stub",
                model="stub",
                elapsed_ms=1,
            )

    async def _run() -> List[cp.CharacterBio]:
        return await cp.write_bios_concurrent(
            entities, scenes=scenes, caller=_StubCaller(), semaphore_size=2
        )

    bios = asyncio.run(_run())
    assert len(bios) == 3
    by_id = {b.character_id: b for b in bios}
    assert by_id["ent-0"].persona_core == "测试性格"
    assert by_id["ent-1"].evidence.get("status") == "failed"
    assert by_id["ent-2"].persona_core == "测试性格"
