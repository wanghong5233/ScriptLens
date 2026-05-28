import asyncio
from types import SimpleNamespace

import pytest

from service.script_tools import tag_pipeline as tp
from service.tag_registry.loader import BundleConfig


def _bundle(bundle_id: str, scope: str) -> BundleConfig:
    return BundleConfig(
        id=bundle_id,
        scope=scope,
        dims=("dummy_dim",),
        prompt="prompts/mock.jinja",
    )


_BUNDLES_BY_VERSION = {
    "script": [
        _bundle("drama_tags", "script"),
        _bundle("script_structure", "script"),
        _bundle("plot_core", "plot_unit"),
        _bundle("episode_structure", "episode"),
        _bundle("character_attrs", "character"),
        _bundle("relationship_attrs", "relationship"),
    ],
}


@pytest.mark.parametrize(
    ("tag_set_ver", "expected_bundle_runs", "expected_relationship_count"),
    [
        (
            "script",
            {
                "drama_tags": 1,
                "script_structure": 1,
                "plot_core": 2,
                "episode_structure": 2,
                "character_attrs": 2,
                "relationship_attrs": 1,
                "rule_paid_break_position": 2,
            },
            1,
        ),
    ],
)
def test_run_tag_pipeline_dispatches_by_bundle_scope(
    monkeypatch,
    tag_set_ver: str,
    expected_bundle_runs: dict[str, int],
    expected_relationship_count: int,
) -> None:
    dispatch_calls: list[tuple[str, str, str]] = []
    prereq_calls: dict[str, int] = {"segment": 0, "resolve": 0, "relationship_seed": 0}
    rule_calls: list[tuple[str, list[int], str, str]] = []

    monkeypatch.setattr(tp, "resolve_script_id", lambda script_ref, engine=None: "sid-1")

    async def _fake_segment(script_id: str, **kwargs):  # noqa: ANN003
        prereq_calls["segment"] += 1
        assert script_id == "sid-1"
        assert kwargs["tag_set_ver"] == tag_set_ver
        return [SimpleNamespace(id="pu-1"), SimpleNamespace(id="pu-2")]

    async def _fake_resolve(script_id: str, **kwargs):  # noqa: ANN003
        prereq_calls["resolve"] += 1
        assert script_id == "sid-1"
        assert kwargs["tag_set_ver"] == tag_set_ver
        return [SimpleNamespace(id="char-1"), SimpleNamespace(id="char-2")]

    def _fake_seed_relationships(script_ref: str, **kwargs):  # noqa: ANN003
        prereq_calls["relationship_seed"] += 1
        assert script_ref == "sid-1"
        assert kwargs["tag_set_ver"] == tag_set_ver
        return []

    def _fake_character_ids(script_id: str, **kwargs):  # noqa: ANN003
        assert script_id == "sid-1"
        return ["char-1", "char-2"]

    def _fake_relationship_ids(script_id: str, *, tag_set_ver: str, **kwargs):  # noqa: ANN003
        assert script_id == "sid-1"
        assert tag_set_ver == "script"
        return ["rel-1"]

    def _fake_episode_targets(script_id: str, **kwargs):  # noqa: ANN003
        assert script_id == "sid-1"
        return ["sid-1::ep::1", "sid-1::ep::2"]

    async def _fake_extract_bundle(bundle_id: str, target_id: str, **kwargs):  # noqa: ANN003
        dispatch_calls.append((bundle_id, target_id, kwargs["tag_set_ver"]))
        return {"__bundle_id": bundle_id}

    def _fake_persist_paid_break_positions_for_episodes(
        script_id: str,
        episode_nos: list[int],
        **kwargs,
    ) -> None:  # noqa: ANN003
        rule_calls.append((script_id, episode_nos, kwargs["tag_set_ver"], kwargs["prompt_ver"]))

    monkeypatch.setattr(tp, "segment_plot_units", _fake_segment)
    monkeypatch.setattr(tp, "resolve_character_entities", _fake_resolve)
    monkeypatch.setattr(tp, "ensure_relationship_candidates", _fake_seed_relationships)
    monkeypatch.setattr(tp, "_character_ids", _fake_character_ids)
    monkeypatch.setattr(tp, "_relationship_ids", _fake_relationship_ids)
    monkeypatch.setattr(tp, "_episode_targets", _fake_episode_targets)
    monkeypatch.setattr(tp, "list_bundles", lambda ver: list(_BUNDLES_BY_VERSION[ver]))
    monkeypatch.setattr(tp, "extract_bundle", _fake_extract_bundle)
    monkeypatch.setattr(tp, "persist_paid_break_positions_for_episodes", _fake_persist_paid_break_positions_for_episodes)

    async def _run() -> tp.PipelineRunSummary:
        return await tp.run_tag_pipeline(
            "sid-1",
            tag_set_ver=tag_set_ver,
            seed=7,
            variant="b",
            caller=object(),  # caller internals are irrelevant in this dispatch test.
        )

    summary = asyncio.run(_run())

    assert prereq_calls == {"segment": 1, "resolve": 1, "relationship_seed": 1}
    assert summary.script_id == "sid-1"
    assert summary.tag_set_ver == tag_set_ver
    assert summary.seed == 7
    assert summary.variant == "b"
    assert summary.plot_unit_count == 2
    assert summary.character_entity_count == 2
    assert summary.relationship_count == expected_relationship_count
    assert summary.bundle_runs == expected_bundle_runs
    assert rule_calls == [("sid-1", [1, 2], "script", "rule:paid_break_position:b")]

    expected_scope_targets = {
        "script": ["sid-1"],
        "plot_unit": ["pu-1", "pu-2"],
        "episode": ["sid-1::ep::1", "sid-1::ep::2"],
        "character": ["char-1", "char-2"],
        "relationship": ["rel-1"],
    }
    expected_pairs = {
        (bundle.id, target)
        for bundle in _BUNDLES_BY_VERSION[tag_set_ver]
        for target in expected_scope_targets[bundle.scope]
    }
    actual_pairs = {(bundle_id, target_id) for bundle_id, target_id, _ in dispatch_calls}
    assert actual_pairs == expected_pairs
    assert all(ver == tag_set_ver for _, _, ver in dispatch_calls)
