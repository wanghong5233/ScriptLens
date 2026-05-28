import asyncio

from service.script_tools.llm_cache import CachedLLMResponse
from service.script_tools.llm_caller import LLMResponse, LlmCaller


def test_call_json_deterministic_hit_cache(monkeypatch) -> None:
    caller = LlmCaller()

    async def fake_get(_input_hash: str):
        return CachedLLMResponse(
            raw='{"plot_hook":"identity_reveal"}',
            parsed={"plot_hook": "identity_reveal"},
            provider="dashscope",
            model="qwen-max-latest",
            elapsed_ms=12,
        )

    async def fake_put(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("cache hit path should not call put")

    async def fake_call_json_internal(**kwargs):  # noqa: ANN003
        raise AssertionError("cache hit path should not call LLM")

    monkeypatch.setattr("service.script_tools.llm_cache.LlmCache.get", fake_get)
    monkeypatch.setattr("service.script_tools.llm_cache.LlmCache.put", fake_put)
    monkeypatch.setattr(caller, "_call_json_internal", fake_call_json_internal)

    async def _run():
        resp = await caller.call_json_deterministic(
            "dummy",
            tag_set_ver="script",
            prompt_ver="script:plot_hook:a",
            dim="plot_hook",
            seed=42,
        )
        assert resp.parsed["plot_hook"] == "identity_reveal"
        assert resp.provider == "dashscope"

    asyncio.run(_run())


def test_call_json_deterministic_cache_miss(monkeypatch) -> None:
    caller = LlmCaller()
    called = {"put": 0, "llm": 0}

    async def fake_get(_input_hash: str):
        return None

    async def fake_put(*args, **kwargs):  # noqa: ANN002, ANN003
        called["put"] += 1

    async def fake_call_json_internal(**kwargs):  # noqa: ANN003
        called["llm"] += 1
        return LLMResponse(
            raw='{"plot_hook":"reversal"}',
            parsed={"plot_hook": "reversal"},
            provider="openai",
            model="gpt-4.1",
            elapsed_ms=33,
        )

    monkeypatch.setattr("service.script_tools.llm_cache.LlmCache.get", fake_get)
    monkeypatch.setattr("service.script_tools.llm_cache.LlmCache.put", fake_put)
    monkeypatch.setattr(caller, "_call_json_internal", fake_call_json_internal)

    async def _run():
        resp = await caller.call_json_deterministic(
            "dummy",
            tag_set_ver="script",
            prompt_ver="script:plot_hook:a",
            dim="plot_hook",
            seed=123,
        )
        assert resp.parsed["plot_hook"] == "reversal"
        assert called["llm"] == 1
        assert called["put"] == 1

    asyncio.run(_run())
