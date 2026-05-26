import asyncio

from service.script_tools import v0_asr_tag_extractor as asr_ext
from service.script_tools import v0_drama_tag_extractor as drama_ext
from service.script_tools import v0_plot_tag_extractor as plot_ext
from service.script_tools.v0_extractor_common import PlotUnitContext


class _FakeResp:
    def __init__(self, parsed: dict, model: str = "qwen-max-latest") -> None:
        self.parsed = parsed
        self.model = model


class _FakeCaller:
    async def call_json_deterministic(self, prompt: str, **kwargs):  # noqa: ANN003
        dim = kwargs.get("dim")
        if dim == "drama_tags":
            return _FakeResp({"drama_tags": ["重生", "总裁"]})
        if dim == "plot_bundle":
            return _FakeResp(
                {
                    "plot_hook": "identity_reveal",
                    "conflict_type": "status_gap",
                    "story_stage": "trigger",
                    "relationship_arc": "chase_and_reject",
                    "payoff_type": "none",
                    "emotional_driver": "humiliation",
                    "business_content_archetype": "relationship_payoff",
                    "business_conflict_bucket": "relationship_power",
                    "business_payoff_bucket": "none",
                    "business_emotion_bucket": "anger_humiliation",
                }
            )
        if dim == "asr_bundle":
            return _FakeResp(
                {
                    "speech_style": "dramatic",
                    "cta_type": "none",
                    "emotional_keywords": "high",
                    "keyword_theme": "romance",
                }
            )
        return _FakeResp({})


def test_drama_extractor(monkeypatch) -> None:
    monkeypatch.setattr(drama_ext, "load_script_text", lambda *args, **kwargs: ("sid-1", "剧情文本"))
    saved = {"values": None}

    def fake_persist(**kwargs):  # noqa: ANN003
        saved["values"] = kwargs["values"]

    monkeypatch.setattr(drama_ext, "persist_script_tags", fake_persist)

    async def _run():
        payload = await drama_ext.extract_drama_tags("sid-1", caller=_FakeCaller(), persist=True)
        assert payload["drama_tags"] == "总裁|重生"
        assert saved["values"] == ["重生", "总裁"]

    asyncio.run(_run())


def test_plot_and_asr_extractors(monkeypatch) -> None:
    context = PlotUnitContext(
        plot_unit_id="pu-1",
        script_id="sid-1",
        idx=1,
        episode_no=1,
        summary="慕梦汐求助云逸楚",
        prev_summary="前情",
        next_summary="后续",
        full_text="旁白：夜色沉沉\n慕梦汐：求你帮我",
        dialogue_text="慕梦汐：求你帮我",
        action_text="夜色沉沉",
        start_scene_id="s-1",
        end_scene_id="s-2",
    )
    monkeypatch.setattr(plot_ext, "load_plot_unit_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(asr_ext, "load_plot_unit_context", lambda *args, **kwargs: context)

    persisted_plot = {"dims": None}
    persisted_asr = {"dims": None}

    def fake_persist_plot(**kwargs):  # noqa: ANN003
        persisted_plot["dims"] = kwargs["values_by_dim"]

    def fake_persist_asr(**kwargs):  # noqa: ANN003
        persisted_asr["dims"] = kwargs["values_by_dim"]

    monkeypatch.setattr(plot_ext, "persist_plot_unit_tags", fake_persist_plot)
    monkeypatch.setattr(asr_ext, "persist_plot_unit_tags", fake_persist_asr)

    async def _run():
        plot_payload = await plot_ext.extract_plot_tags("sid-1::plot::1", caller=_FakeCaller(), persist=True)
        asr_payload = await asr_ext.extract_asr_tags("sid-1::plot::1", caller=_FakeCaller(), persist=True)
        assert plot_payload["plot_hook"] == "identity_reveal"
        assert "business_content_archetype" in plot_payload
        assert asr_payload["speech_style"] == "dramatic"
        assert asr_payload["dialogue_density"] in {"dense", "moderate", "sparse", "none"}
        assert persisted_plot["dims"] is not None
        assert persisted_asr["dims"] is not None

    asyncio.run(_run())

