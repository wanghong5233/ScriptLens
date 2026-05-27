import asyncio

from service.script_tools import v0_asr_tag_extractor as asr_ext
from service.script_tools import v0_drama_tag_extractor as drama_ext
from service.script_tools import v0_plot_tag_extractor as plot_ext


def test_drama_extractor(monkeypatch) -> None:
    async def fake_extract_bundle(bundle_id: str, target_id: str, **kwargs):  # noqa: ANN003
        assert bundle_id == "v0_drama"
        assert target_id == "sid-1"
        return {"drama_tags": ["重生", "总裁"], "__model_ver": "mock"}

    monkeypatch.setattr(drama_ext, "extract_bundle", fake_extract_bundle)

    async def _run():
        payload = await drama_ext.extract_drama_tags("sid-1", persist=True)
        assert payload["drama_tags"] == "总裁|重生"
        assert payload["__drama_tags_list"] == ["重生", "总裁"]

    asyncio.run(_run())


def test_plot_and_asr_extractors(monkeypatch) -> None:
    async def fake_extract_bundle(bundle_id: str, target_id: str, **kwargs):  # noqa: ANN003
        if bundle_id == "v0_plot":
            return {
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
                "__model_ver": "mock",
            }
        if bundle_id == "v0_asr":
            return {
                "dialogue_density": "moderate",
                "speech_style": "dramatic",
                "cta_type": "none",
                "voiceover_type": "character",
                "emotional_keywords": "high",
                "keyword_theme": "romance",
                "__model_ver": "mock",
            }
        raise AssertionError(bundle_id)

    monkeypatch.setattr(plot_ext, "extract_bundle", fake_extract_bundle)
    monkeypatch.setattr(asr_ext, "extract_bundle", fake_extract_bundle)

    async def _run():
        plot_payload = await plot_ext.extract_plot_tags("sid-1::plot::1", persist=True)
        asr_payload = await asr_ext.extract_asr_tags("sid-1::plot::1", persist=True)
        assert plot_payload["plot_hook"] == "identity_reveal"
        assert "business_content_archetype" in plot_payload
        assert asr_payload["speech_style"] == "dramatic"
        assert asr_payload["dialogue_density"] == "moderate"

    asyncio.run(_run())

