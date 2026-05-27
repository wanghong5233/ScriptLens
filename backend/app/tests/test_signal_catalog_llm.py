import asyncio

from service.score_registry.loader import load_rubric
from service.script_tools.llm_caller import LLMResponse
from service.script_tools.signal_catalog import SignalContext
from service.script_tools.signal_catalog.llm_signals import compute_llm_signals


def _ctx() -> SignalContext:
    return SignalContext(
        script_id="script-llm",
        script_meta={"id": "script-llm", "title": "LLM测试", "total_episodes": 2, "total_scenes": 4},
        plot_units=[
            {"id": "u1", "episode_no": 1, "idx": 1, "summary": "开场钩子"},
            {"id": "u2", "episode_no": 2, "idx": 2, "summary": "高潮回收"},
        ],
        plot_unit_tags=[
            {"plot_unit_id": "u1", "dim": "plot_hook", "value": "identity_reveal"},
            {"plot_unit_id": "u2", "dim": "payoff_type", "value": "revenge"},
        ],
        script_tags=[{"dim": "drama_tags", "value": "复仇"}],
        episode_tags=[],
        character_entities=[],
        character_relationships=[],
        scenes=[],
        drama_tags=["复仇"],
        plot_tags_by_unit={
            "u1": {"plot_hook": ["identity_reveal"]},
            "u2": {"payoff_type": ["revenge"]},
        },
        script_tag_map={"drama_tags": ["复仇"]},
        episode_tag_map={},
    )


class _FakeCaller:
    def __init__(self, rubric_id: str) -> None:
        self.rubric = load_rubric(rubric_id)
        self.calls: list[str] = []

    async def call_json_deterministic(self, prompt: str, **kwargs):  # noqa: ANN003
        _ = prompt
        dim = str(kwargs.get("dim") or "")
        self.calls.append(dim)
        bundle_id = dim.split(":", 1)[1]
        bundle = self.rubric.get_bundle(bundle_id)
        payload = {
            signal: {
                "score": 7.8,
                "confidence": 0.82,
                "value": {"note": f"{signal}_ok"},
                "evidence": [{"scene_id": "scene-1"}],
            }
            for signal in bundle.signals
        }
        return LLMResponse(
            raw="{}",
            parsed={"signals": payload},
            provider="openai",
            model="gpt-test",
            elapsed_ms=18,
        )


def test_compute_llm_signals_with_caller() -> None:
    rubric = load_rubric("v3.0.0")
    fake = _FakeCaller("v3.0.0")
    ctx = _ctx()

    async def _run():
        out = await compute_llm_signals(rubric, ctx, caller=fake, seed=11)
        assert out
        assert "logline_clear" in out
        signal = out["logline_clear"]
        assert signal.source == "llm"
        assert signal.score is not None and signal.score > 0
        assert signal.meta.get("model") == "gpt-test"
        assert fake.calls

    asyncio.run(_run())


def test_compute_llm_signals_without_caller_fallback() -> None:
    rubric = load_rubric("v3.0.0")
    ctx = _ctx()

    async def _run():
        out = await compute_llm_signals(rubric, ctx, caller=None, seed=9)
        assert "logline_clear" in out
        assert out["logline_clear"].score is None
        assert out["logline_clear"].meta.get("fallback") is True

    asyncio.run(_run())
