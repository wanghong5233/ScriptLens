import asyncio

from service.script_tools import character_entity_resolver as resolver
from service.script_tools.script_ir import IREpisode, IRLine, IRScene, ScriptIR


def _scene(i: int, chars: list[str], dialogue_pairs: list[tuple[str, str]]) -> IRScene:
    lines = []
    for idx, (speaker, text) in enumerate(dialogue_pairs, start=1):
        lines.append(IRLine(idx=idx, kind="dialogue", character=speaker, text=text, abs_line=i * 10 + idx))
    return IRScene(
        scene_id=f"s-{i}",
        episode_no=1,
        scene_no=str(i),
        scene_label=f"场景{i}",
        characters=chars,
        lines=lines,
        start_line=i * 10,
        end_line=i * 10 + len(lines),
    )


def test_name_similarity_basic() -> None:
    assert resolver._name_similarity("慕梦汐", "梦汐") >= 0.85
    assert resolver._name_similarity("阿明", "阿明") == 1.0
    assert resolver._name_similarity("慕梦汐", "云逸楚") < 0.85


def test_resolve_character_entities(monkeypatch) -> None:
    scenes = [
        _scene(1, ["慕梦汐", "云逸楚"], [("慕梦汐", "我要复仇"), ("云逸楚", "不许胡来")]),
        _scene(2, ["梦汐", "阿楚"], [("梦汐", "你帮我"), ("阿楚", "我会护你")]),
        _scene(3, ["慕梦汐"], [("慕梦汐", "我自己来")]),
    ]
    ir = ScriptIR(script_id="sid-3", title="x", episodes=[IREpisode(episode_no=1, scenes=scenes)])
    monkeypatch.setattr(resolver, "build_script_ir", lambda *args, **kwargs: ir)

    async def fake_should_merge(**kwargs):  # noqa: ANN003
        return True

    monkeypatch.setattr(resolver, "_llm_should_merge", fake_should_merge)

    async def _run():
        entities = await resolver.resolve_character_entities("sid-3", persist=False)
        names = [e.canonical_name for e in entities]
        assert "慕梦汐" in names
        assert "云逸楚" in names or "阿楚" in names
        assert entities[0].role in {"protagonist", "antagonist", "supporting", "minor"}
        assert all(e.source == "llm" for e in entities)

    asyncio.run(_run())

