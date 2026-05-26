import asyncio

from service.script_tools import plot_unit_segmenter as seg
from service.script_tools.script_ir import IREpisode, IRLine, IRScene, ScriptIR


def _scene(i: int, label: str, chars: list[str], text: str) -> IRScene:
    return IRScene(
        scene_id=f"scene-{i}",
        episode_no=1,
        scene_no=str(i),
        scene_label=label,
        characters=chars,
        lines=[
            IRLine(idx=1, kind="dialogue", character=chars[0] if chars else None, text=text, abs_line=i * 10),
            IRLine(idx=2, kind="action", character=None, text="动作描述", abs_line=i * 10 + 1),
        ],
        start_line=i * 10,
        end_line=i * 10 + 1,
    )


def test_segment_plot_units_basic(monkeypatch) -> None:
    scenes = [
        _scene(1, "客厅 日内", ["阿明"], "我要复仇"),
        _scene(2, "客厅 日内", ["阿明"], "继续冲突"),
        _scene(3, "办公室 夜内", ["老板"], "身份揭露"),
    ]
    ir = ScriptIR(script_id="sid-1", title="t", episodes=[IREpisode(episode_no=1, scenes=scenes)])

    monkeypatch.setattr(seg, "build_script_ir", lambda *args, **kwargs: ir)

    async def fake_keep_boundary(**kwargs):  # noqa: ANN003
        return seg._BoundaryDecision(keep=True, score=0.9, reason="ok")

    monkeypatch.setattr(seg, "_llm_keep_boundary", fake_keep_boundary)

    async def _run():
        units = await seg.segment_plot_units("sid-1", persist=False)
        assert len(units) >= 2
        assert units[0].idx == 1
        assert units[-1].script_id == "sid-1"
        assert units[0].start_scene_id is not None

    asyncio.run(_run())


def test_segment_plot_units_episode_cap(monkeypatch) -> None:
    scenes = [_scene(i, f"场景{i}", [f"角色{i}"], f"台词{i}") for i in range(1, 13)]
    ir = ScriptIR(script_id="sid-2", title="t2", episodes=[IREpisode(episode_no=1, scenes=scenes)])
    monkeypatch.setattr(seg, "build_script_ir", lambda *args, **kwargs: ir)

    async def fake_keep_boundary(**kwargs):  # noqa: ANN003
        return seg._BoundaryDecision(keep=True, score=0.95, reason="keep")

    monkeypatch.setattr(seg, "_llm_keep_boundary", fake_keep_boundary)

    async def _run():
        units = await seg.segment_plot_units("sid-2", persist=False, max_plot_units_per_episode=4)
        assert len(units) <= 4

    asyncio.run(_run())

