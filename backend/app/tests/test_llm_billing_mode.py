import asyncio
from types import SimpleNamespace

import pytest

from agent_runtime.billing_context import (
    BillingContext,
    BillingIdentity,
    BillingMetadata,
    use_billing,
)
from service.core.llm.runtime import LLMRuntime, MissingBillingContextError
from service.script_tools.llm_caller import LLMResponse, LlmCaller, ScoreLLMError


def _make_settings(**overrides):
    base = {
        "DASHSCOPE_API_KEY": "dashscope-local-key",
        "DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_MODEL_NAME": "qwen-max-latest",
        "DASHSCOPE_MODEL_CANDIDATES": "qwen-max-latest,qwen-max",
        "OPENAI_API_KEY": "openai-local-key",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_MODEL_NAME": "gpt-5.2",
        "OPENAI_MODEL_CANDIDATES": "gpt-5.2,gpt-5-mini",
        "LLM_REQUEST_TIMEOUT": 30,
        "LLM_FALLBACK_ENABLED": True,
        "LLM_FALLBACK_ALLOW_EXPLICIT_PROVIDER": True,
        "LLM_HEALTH_FAILURE_THRESHOLD": 3,
        "LLM_HEALTH_COOLDOWN_SECONDS": 90,
        "LLM_TEMPERATURE": 0.2,
        "LLM_MAX_TOKENS": 2048,
        "LLM_COST_CONFIG": {},
        "LLM_COST_PER_1K_INPUT_TOKENS": 0.0,
        "LLM_COST_PER_1K_OUTPUT_TOKENS": 0.0,
        "SM_LLM_TYPE": "openai",
        "BILLING_GATEWAY_BASE_URL": "https://billing.example.com/v1/gateway",
        "BILLING_SERVICE_SECRET": "billing-secret",
        "SCRIPTLENS_BILLING_MODE": "hybrid",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_runtime_gateway_mode_requires_billing_context() -> None:
    runtime = LLMRuntime(
        settings_obj=_make_settings(
            OPENAI_API_KEY=None,
            DASHSCOPE_API_KEY=None,
            SCRIPTLENS_BILLING_MODE="gateway",
        )
    )

    with pytest.raises(MissingBillingContextError):
        runtime.get_request_headers(require_billing_context=True)


def test_runtime_hybrid_without_context_falls_back_to_local_transport() -> None:
    runtime = LLMRuntime(
        settings_obj=_make_settings(
            SCRIPTLENS_BILLING_MODE="hybrid",
            OPENAI_API_KEY="openai-local-key",
        )
    )

    api_key, base_url = runtime._resolve_transport("openai", require_billing_context=True)  # noqa: SLF001
    assert api_key == "openai-local-key"
    assert base_url == "https://api.openai.com/v1"
    assert runtime.get_request_headers(require_billing_context=True) == {}


def test_runtime_headers_from_billing_context() -> None:
    runtime = LLMRuntime(settings_obj=_make_settings(SCRIPTLENS_BILLING_MODE="gateway"))
    billing = BillingContext(
        identity=BillingIdentity(user_id="user-123"),
        metadata=BillingMetadata(
            script_id="script-1",
            intent="chat",
            tool_name="rewrite_scene_tool",
            trace_id="trace-abc",
        ),
    )

    with use_billing(billing):
        headers = runtime.get_request_headers(require_billing_context=True)

    assert headers["X-User-Id"] == "user-123"
    assert headers["X-LiteLLM-User-Id"] == "litellm-user-123"
    assert headers["X-LiteLLM-Metadata-ScriptId"] == "script-1"
    assert headers["X-LiteLLM-Metadata-Intent"] == "chat"
    assert headers["X-LiteLLM-Metadata-Tool"] == "rewrite_scene_tool"
    assert headers["X-LiteLLM-Metadata-TraceId"] == "trace-abc"


def test_runtime_headers_do_not_leak_between_users() -> None:
    runtime = LLMRuntime(settings_obj=_make_settings(SCRIPTLENS_BILLING_MODE="gateway"))
    first = BillingContext(identity=BillingIdentity(user_id="u1"))
    second = BillingContext(identity=BillingIdentity(user_id="u2"))

    with use_billing(first):
        h1 = runtime.get_request_headers(require_billing_context=True)
    with use_billing(second):
        h2 = runtime.get_request_headers(require_billing_context=True)

    assert h1["X-User-Id"] == "u1"
    assert h2["X-User-Id"] == "u2"


def test_llm_caller_passes_per_request_headers(monkeypatch) -> None:
    caller = LlmCaller()
    captured_headers: list[dict[str, str]] = []
    next_headers = iter(
        [
            {"X-User-Id": "u1", "X-LiteLLM-User-Id": "litellm-u1"},
            {"X-User-Id": "u2", "X-LiteLLM-User-Id": "litellm-u2"},
        ]
    )

    monkeypatch.setattr(caller._runtime, "get_provider_candidates", lambda *_args, **_kwargs: ["openai"])  # noqa: SLF001
    monkeypatch.setattr(caller, "_resolve_provider_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        caller._runtime,  # noqa: SLF001
        "get_request_headers",
        lambda **_kwargs: next(next_headers),
    )

    async def fake_call_provider_with_fallback(**kwargs):  # noqa: ANN003
        captured_headers.append(dict(kwargs.get("request_headers") or {}))
        return LLMResponse(
            raw='{"ok": true}',
            parsed={"ok": True},
            provider="openai",
            model="gpt-5.2",
            elapsed_ms=1,
        )

    monkeypatch.setattr(caller, "_call_provider_with_fallback", fake_call_provider_with_fallback)

    async def _run():
        await caller.call_json("hello")
        await caller.call_json("world")

    asyncio.run(_run())
    assert captured_headers[0]["X-User-Id"] == "u1"
    assert captured_headers[1]["X-User-Id"] == "u2"


def test_llm_caller_gateway_mode_missing_context_raises(monkeypatch) -> None:
    caller = LlmCaller()
    monkeypatch.setattr(caller._runtime, "get_provider_candidates", lambda *_args, **_kwargs: ["openai"])  # noqa: SLF001

    def _raise_missing(**_kwargs):
        raise MissingBillingContextError("missing billing context")

    monkeypatch.setattr(caller._runtime, "get_request_headers", _raise_missing)  # noqa: SLF001

    async def _run():
        with pytest.raises(ScoreLLMError):
            await caller.call_json("hello")

    asyncio.run(_run())

