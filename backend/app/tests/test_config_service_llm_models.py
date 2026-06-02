import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

for _missing in ("datrie", "hanziconv"):
    if _missing not in sys.modules:
        sys.modules[_missing] = MagicMock()

_rag_tokenizer = MagicMock()
_rag_tokenizer.RagTokenizer = MagicMock
sys.modules["service.core.rag.nlp.rag_tokenizer"] = _rag_tokenizer

from service.core.system.config_service import ConfigService


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://billing.example.com/v1/gateway/models")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self) -> dict:
        return self._payload


def _stub_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        get_provider_candidates=lambda: ["openai", "dashscope"],
        get_model_candidates_for_provider=lambda provider, llm_options=None: (
            ["gpt-5.2"] if provider == "openai" else ["qwen-max"]
        ),
    )


def test_llm_models_hybrid_with_gateway_uses_billing_catalog(monkeypatch) -> None:
    service = ConfigService()
    service._model_probe_cache.clear()
    monkeypatch.setattr(service, "_runtime", _stub_runtime)
    monkeypatch.setattr(service, "_should_probe_models_via_gateway", lambda: True)
    monkeypatch.setattr(
        service,
        "_billing_transport_credentials",
        lambda: ("https://billing.example.com/v1/gateway", "billing-secret"),
    )

    def fake_get(url: str, headers: dict | None = None, timeout: float = 5.0):
        assert url == "https://billing.example.com/v1/gateway/models"
        assert headers == {"Authorization": "Bearer billing-secret"}
        return _FakeResponse(
            {
                "data": [
                    {"id": "gpt-5.2"},
                    {"id": "dashscope/qwen-max"},
                ]
            }
        )

    monkeypatch.setattr("service.core.system.config_service.httpx.get", fake_get)

    catalog = service.llm_models(refresh=True)
    by_model = {(item["provider"], item["model"]): item for item in catalog["models"]}

    assert by_model[("openai", "gpt-5.2")]["available"] is True
    assert by_model[("dashscope", "qwen-max")]["available"] is True


def test_llm_models_gateway_mode_without_billing_secret_marks_unavailable(monkeypatch) -> None:
    service = ConfigService()
    service._model_probe_cache.clear()
    monkeypatch.setattr(service, "_runtime", _stub_runtime)
    monkeypatch.setattr(service, "_should_probe_models_via_gateway", lambda: True)
    monkeypatch.setattr(service, "_billing_transport_credentials", lambda: (None, None))

    catalog = service.llm_models(refresh=True)
    assert all(item["available"] is False for item in catalog["models"])


def test_llm_models_local_mode_ignores_gateway_and_uses_provider_key(monkeypatch) -> None:
    service = ConfigService()
    service._model_probe_cache.clear()
    monkeypatch.setattr(service, "_runtime", _stub_runtime)
    monkeypatch.setattr(service, "_should_probe_models_via_gateway", lambda: False)
    monkeypatch.setattr(
        service,
        "_local_provider_transport",
        lambda provider: (
            ("https://api.openai.com/v1", "openai-local-key")
            if provider == "openai"
            else ("https://dashscope.aliyuncs.com/compatible-mode/v1", "dashscope-local-key")
        ),
    )

    calls: list[str] = []

    def fake_get(url: str, headers: dict | None = None, timeout: float = 5.0):
        calls.append(url)
        return _FakeResponse({"data": [{"id": "gpt-5.2" if "openai" in url else "qwen-max"}]})

    monkeypatch.setattr("service.core.system.config_service.httpx.get", fake_get)

    catalog = service.llm_models(refresh=True)
    assert set(calls) == {
        "https://api.openai.com/v1/models",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
    }
    assert catalog["models"][0]["available"] is True
