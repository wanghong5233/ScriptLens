"""Unit tests for episode-level plot_unit segmenter (post C1 rewrite).

Covers:
- _validate_and_repair_units edge cases (gaps, overlaps, bounds)
- _fallback_one_unit_per_scene shape
- segment_plot_units happy path with mocked LLM
- segment_plot_units fallback when LLM returns invalid output
"""

from __future__ import annotations

import asyncio

import pytest

from service.script_tools import plot_unit_segmenter as seg
from service.script_tools.script_ir import IREpisode, IRLine, IRScene, ScriptIR


def _scene(scene_id: int, label: str, chars: list[str], start_line: int, lines: list[tuple[str, str]]) -> IRScene:
    ir_lines: list[IRLine] = []
    for i, (kind, text) in enumerate(lines):
        ir_lines.append(
            IRLine(
                idx=i + 1,
                kind=kind,  # type: ignore[arg-type]
                character=(chars[0] if (chars and kind == "dialogue") else None),
                text=text,
                abs_line=start_line + i,
            )
        )
    end_line = start_line + len(lines) - 1
    return IRScene(
        scene_id=f"scene-{scene_id}",
        episode_no=1,
        scene_no=str(scene_id),
        scene_label=label,
        characters=chars,
        lines=ir_lines,
        start_line=start_line,
        end_line=end_line,
    )


def _episode_three_scenes() -> IREpisode:
    scenes = [
        _scene(1, "1-1 客厅 日内", ["阿明"], 1, [("dialogue", "我要复仇"), ("action", "他握紧拳头")]),
        _scene(2, "1-2 客厅 日内", ["阿明", "敌人"], 3, [("dialogue", "你别想"), ("dialogue", "走着瞧")]),
        _scene(3, "1-3 办公室 夜内", ["老板"], 5, [("dialogue", "身份揭露"), ("action", "灯光暗下")]),
    ]
    return IREpisode(episode_no=1, scenes=scenes)


def test_validate_and_repair_units_drops_invalid_ranges() -> None:
    bounds = (10, 30)
    raw = [
        {"summary": "a", "line_range": [10, 15]},
        {"summary": "b", "line_range": ["not_int", 20]},  # invalid type
        {"summary": "c", "line_range": [50, 80]},  # out of bounds; clamped & dropped
        {"summary": "d", "line_range": [16, 30]},
    ]
    out = seg._validate_and_repair_units(raw, bounds=bounds)
    ranges = [u["line_range"] for u in out]
    # first range snapped to bounds[0]; last range snapped to bounds[1]
    assert ranges[0][0] == 10
    assert ranges[-1][1] == 30
    # contiguous: each next.start == prev.end + 1
    for prev, nxt in zip(out[:-1], out[1:]):
        assert nxt["line_range"][0] == prev["line_range"][1] + 1


def test_validate_and_repair_units_trims_overlap() -> None:
    bounds = (1, 20)
    raw = [
        {"summary": "a", "line_range": [1, 10]},
        {"summary": "b", "line_range": [5, 15]},  # overlaps prior; should be trimmed
        {"summary": "c", "line_range": [16, 20]},
    ]
    out = seg._validate_and_repair_units(raw, bounds=bounds)
    ranges = [u["line_range"] for u in out]
    assert ranges == [[1, 10], [11, 15], [16, 20]]


def test_validate_and_repair_units_empty_returns_empty() -> None:
    assert seg._validate_and_repair_units([], bounds=(1, 10)) == []
    assert seg._validate_and_repair_units([{"line_range": [5, 3]}], bounds=(1, 10)) == []


def test_fallback_one_unit_per_scene_emits_unit_per_scene() -> None:
    episode = _episode_three_scenes()
    units = seg._fallback_one_unit_per_scene(episode.scenes)
    assert len(units) == 3
    assert units[0]["line_range"] == [1, 2]
    assert units[1]["line_range"] == [3, 4]
    assert units[2]["line_range"] == [5, 6]


def test_segment_plot_units_happy_path(monkeypatch) -> None:
    episode = _episode_three_scenes()
    ir = ScriptIR(script_id="sid-1", title="demo", episodes=[episode])
    monkeypatch.setattr(seg, "build_script_ir", lambda *args, **kwargs: ir)

    async def fake_llm(**kwargs):  # noqa: ANN003
        # LLM returns 2 plot_units spanning scenes 1+2 then scene 3
        return [
            {
                "summary": "阿明立誓复仇，对峙敌人",
                "line_range": [1, 4],
                "location_hint": "客厅",
                "time_of_day_hint": "日",
                "in_out_hint": "内",
                "characters_hint": ["阿明", "敌人"],
                "evidence_lines": [1, 4],
            },
            {
                "summary": "老板身份揭露",
                "line_range": [5, 6],
                "location_hint": "办公室",
                "time_of_day_hint": "夜",
                "in_out_hint": "内",
                "characters_hint": ["老板"],
                "evidence_lines": [5],
            },
        ]

    monkeypatch.setattr(seg, "_llm_segment_episode", fake_llm)

    async def _run() -> None:
        units = await seg.segment_plot_units("sid-1", persist=False)
        assert len(units) == 2
        # first plot_unit spans 1..4 → scene-1 to scene-2
        assert units[0].start_scene_id == "scene-1"
        assert units[0].end_scene_id == "scene-2"
        assert units[0].start_line == 1 and units[0].end_line == 4
        # second plot_unit covers scene-3 alone
        assert units[1].start_scene_id == "scene-3"
        assert units[1].end_scene_id == "scene-3"
        assert units[1].start_line == 5 and units[1].end_line == 6
        # idx is global and 1-based
        assert [u.idx for u in units] == [1, 2]
        # summaries persisted
        assert "复仇" in units[0].summary
        assert "身份" in units[1].summary

    asyncio.run(_run())


def test_segment_plot_units_falls_back_when_llm_returns_empty(monkeypatch) -> None:
    episode = _episode_three_scenes()
    ir = ScriptIR(script_id="sid-2", title="demo", episodes=[episode])
    monkeypatch.setattr(seg, "build_script_ir", lambda *args, **kwargs: ir)

    async def fake_llm(**kwargs):  # noqa: ANN003
        # Simulating the fallback that _llm_segment_episode itself produces on bad LLM output
        return seg._fallback_one_unit_per_scene(episode.scenes)

    monkeypatch.setattr(seg, "_llm_segment_episode", fake_llm)

    async def _run() -> None:
        units = await seg.segment_plot_units("sid-2", persist=False)
        # fallback = one plot_unit per scene
        assert len(units) == 3
        assert [u.start_scene_id for u in units] == ["scene-1", "scene-2", "scene-3"]

    asyncio.run(_run())
