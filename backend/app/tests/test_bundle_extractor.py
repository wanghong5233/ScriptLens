import asyncio

from service.script_tools import bundle_extractor as be
from service.script_tools.extractor_common import CharacterContext, EpisodeContext, PlotUnitContext, RelationshipContext


class _FakeResp:
    def __init__(self, parsed: dict, model: str = "qwen-max-latest") -> None:
        self.parsed = parsed
        self.model = model


class _FakeCaller:
    async def call_json_deterministic(self, prompt: str, **kwargs):  # noqa: ANN003
        dim = str(kwargs.get("dim") or "")
        if dim == "bundle:drama_tags":
            return _FakeResp({"drama_tags": ["重生", "总裁霸总"]})
        if dim == "bundle:plot_core":
            return _FakeResp(
                {
                    "plot_hook": "identity_reveal",
                    "conflict_type": "status_gap",
                    "story_stage": "escalation",
                    "relationship_arc": "chase_and_reject",
                    "payoff_type": "none",
                }
            )
        if dim == "bundle:episode_structure":
            return _FakeResp(
                {
                    "episode_opening_type": "hook_in_3s",
                    "episode_end_hook": "cliffhanger",
                    "intra_episode_peak_count": "2",
                    "paid_break_position": "ep_end",
                }
            )
        if dim == "bundle:character_attrs":
            return _FakeResp(
                {
                    "character_archetype": "mentor_elder",
                    "character_role_in_arc": "mentor",
                    "character_arc_type": "static",
                    "character_agency_level": "medium",
                }
            )
        if dim == "bundle:relationship_attrs":
            return _FakeResp(
                {
                    "relationship_type": "mentor",
                    "relationship_polarity": "positive",
                    "relationship_dynamic_arc": "persistent_support",
                    "relationship_triangle": "none",
                }
            )
        return _FakeResp({})


def test_extract_bundle_across_scopes(monkeypatch) -> None:
    be._PAYLOAD_CACHE.clear()
    monkeypatch.delenv("SM_TAGGING_DISABLE_LLM", raising=False)
    script_saved = {"dims": []}
    plot_saved = {"dims": None}
    episode_saved = {"dims": None}
    character_saved = {"dims": None}
    relationship_saved = {"dims": None}

    monkeypatch.setattr(be, "load_script_text", lambda *args, **kwargs: ("sid-1", "剧情文本"))
    monkeypatch.setattr(
        be,
        "load_plot_unit_context",
        lambda *args, **kwargs: PlotUnitContext(
            plot_unit_id="pu-1",
            script_id="sid-1",
            idx=1,
            episode_no=1,
            summary="慕梦汐求助",
            prev_summary="前情",
            next_summary="后续",
            full_text="旁白：夜色沉沉\n慕梦汐：求你帮我",
            dialogue_text="慕梦汐：求你帮我",
            action_text="夜色沉沉",
            start_scene_id="s-1",
            end_scene_id="s-2",
        ),
    )
    monkeypatch.setattr(
        be,
        "load_episode_context",
        lambda *args, **kwargs: EpisodeContext(script_id="sid-1", episode_no=1, episode_text="第一集文本"),
    )
    monkeypatch.setattr(
        be,
        "load_character_context",
        lambda *args, **kwargs: CharacterContext(
            character_id="char-1",
            script_id="sid-1",
            canonical_name="云逸楚",
            aliases=["阿楚"],
            role="protagonist",
            character_text="角色证据",
        ),
    )
    monkeypatch.setattr(
        be,
        "load_relationship_context",
        lambda *args, **kwargs: RelationshipContext(
            relationship_id="rel-1",
            script_id="sid-1",
            src_char_id="char-1",
            dst_char_id="char-2",
            src_name="云逸楚",
            dst_name="慕梦汐",
            relationship_text="关系证据",
        ),
    )

    def _persist_script_tags(**kwargs):  # noqa: ANN003
        script_saved["dims"].append((kwargs["dim"], kwargs["values"]))

    monkeypatch.setattr(be, "persist_script_tags", _persist_script_tags)
    monkeypatch.setattr(be, "persist_plot_unit_tags", lambda **kwargs: plot_saved.update({"dims": kwargs["values_by_dim"]}))
    monkeypatch.setattr(be, "persist_episode_tags", lambda **kwargs: episode_saved.update({"dims": kwargs["values_by_dim"]}))
    monkeypatch.setattr(be, "_persist_character_values", lambda **kwargs: character_saved.update({"dims": kwargs["values"]}))
    monkeypatch.setattr(be, "_persist_relationship_values", lambda **kwargs: relationship_saved.update({"dims": kwargs["values"]}))

    async def _run() -> None:
        drama = await be.extract_bundle("drama_tags", "sid-1", tag_set_ver="script", caller=_FakeCaller(), persist=True)
        plot = await be.extract_bundle("plot_core", "sid-1::plot::1", tag_set_ver="script", caller=_FakeCaller(), persist=True)
        episode = await be.extract_bundle(
            "episode_structure",
            "sid-1::ep::1",
            tag_set_ver="script",
            caller=_FakeCaller(),
            persist=True,
        )
        character = await be.extract_bundle("character_attrs", "char-1", tag_set_ver="script", caller=_FakeCaller(), persist=True)
        relationship = await be.extract_bundle("relationship_attrs", "rel-1", tag_set_ver="script", caller=_FakeCaller(), persist=True)

        assert drama["drama_tags"] == ["重生", "总裁霸总"]
        assert plot["plot_hook"] == "identity_reveal"
        assert episode["episode_opening_type"] == "hook_in_3s"
        assert character["character_role_in_arc"] == "mentor"
        assert relationship["relationship_dynamic_arc"] == "persistent_support"
        assert script_saved["dims"]
        assert plot_saved["dims"] is not None
        assert episode_saved["dims"] is not None
        assert character_saved["dims"] is not None
        assert relationship_saved["dims"] is not None

    asyncio.run(_run())

