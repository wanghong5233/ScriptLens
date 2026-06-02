"""Configuration service."""

from __future__ import annotations

from importlib import import_module
import time
from typing import Any, Dict, Iterable

import httpx

from core.config import settings
from service.core.llm.runtime import (
    FORBIDDEN_LLM_MODELS as _RUNTIME_FORBIDDEN,
    LLMRuntime,
)
from service.core.rag.nlp.rag_tokenizer import RagTokenizer
from utils.billing_gateway import resolve_billing_gateway_base_url

FORBIDDEN_LLM_MODELS: set[str] = set(_RUNTIME_FORBIDDEN)

try:
    from nltk import word_tokenize as _wt
except Exception:  # pragma: no cover
    _wt = None

try:
    import fitz as _fitz  # PyMuPDF
except Exception:  # pragma: no cover
    _fitz = None


class ConfigService:
    """Provide configuration and health checks."""

    _model_probe_cache: Dict[str, Dict[str, Any]] = {}
    _model_probe_cache_ttl_secs = 600

    @staticmethod
    def _runtime() -> LLMRuntime:
        return LLMRuntime(settings_obj=settings)

    def get_feature_flags(self) -> Dict[str, Any]:
        """Return feature flag settings."""
        return {
            "retrievalStrategy": settings.SM_RETRIEVAL_STRATEGY,
            "rerankerStrategy": settings.SM_RERANKER_STRATEGY,
            "enableCitations": settings.SM_ENABLE_CITATIONS,
            "streamingEnabled": settings.SM_STREAMING_ENABLED,
            "defaultLanguage": settings.SM_DEFAULT_LANGUAGE,
            "multiQueryNum": settings.SM_MULTI_QUERY_NUM,
            "hydeEnabled": settings.SM_HYDE_ENABLED,
            # 索引增强（关键验证开关）
            "semanticChunkingEnabled": settings.SM_SEMANTIC_CHUNKING_ENABLED,
            "multimodalParseEnabled": settings.SM_MULTIMODAL_PARSE_ENABLED,
            "forcePymupdfFallback": settings.SM_FORCE_PYMUPDF_FALLBACK,
            "ragTopK": settings.SM_RAG_TOPK,
            "retrievePageSize": settings.SM_RETRIEVE_PAGE_SIZE,
            "maxTokens": settings.SM_MAX_TOKENS,
            "temperature": settings.SM_TEMPERATURE,
            # history controls
            "historyMaxTokens": settings.SM_HISTORY_MAX_TOKENS,
            "historyHeadroom": settings.SM_HISTORY_HEADROOM,
            "historyRecentTurns": settings.HISTORY_RECENT_TURNS,
            "enableRollingSummary": settings.ENABLE_ROLLING_SUMMARY,
        }

    def parsing_health(self) -> Dict[str, Any]:
        """Check parsing dependencies availability."""
        # ScriptLens MVP: deepdoc removed; flag preserved for compat but always false.
        deepdoc_import = False

        nltk_punkt_ok = True
        if _wt is None:
            nltk_punkt_ok = False
        else:
            try:
                _ = _wt("Hello world, this is a test.")
            except Exception:
                nltk_punkt_ok = False

        pymupdf_ok = _fitz is not None

        rag_tokenizer_ok = True
        try:
            tokenizer = RagTokenizer()
            _ = tokenizer.tokenize("Hello world, This is tokenizer smoke test.")
        except Exception:
            rag_tokenizer_ok = False

        return {
            "deepdoc_import": deepdoc_import,
            "nltk_punkt_ok": nltk_punkt_ok,
            "pymupdf_ok": pymupdf_ok,
            "rag_tokenizer_ok": rag_tokenizer_ok,
        }

    def llm_models(self, *, refresh: bool = False) -> Dict[str, Any]:
        """Return the runtime LLM model catalog for frontend selectors."""
        preferred_provider = self._preferred_provider()
        providers = ("dashscope", "openai")
        visibility = {
            provider: self._fetch_visible_model_ids(provider, refresh=refresh)
            for provider in providers
        }
        models = []
        for provider in providers:
            for model_name in self._candidate_models(provider):
                models.append(self._build_model_entry(provider, model_name, visibility[provider]))

        default_model = self._select_default_model(
            models=models,
            provider=preferred_provider,
            vision=False,
        )
        default_vision_model = self._select_default_model(
            models=models,
            provider=preferred_provider,
            vision=True,
        )
        return {
            "preferredProvider": preferred_provider,
            "defaultModel": default_model,
            "defaultVisionModel": default_vision_model,
            "models": models,
            "cacheTtlSeconds": self._model_probe_cache_ttl_secs,
        }

    def _fetch_visible_model_ids(self, provider: str, *, refresh: bool) -> Dict[str, Any]:
        """Fetch visible model ids from provider /models with a short TTL cache."""

        now = time.time()
        if self._should_probe_models_via_gateway():
            gateway_result = self._fetch_gateway_model_catalog(refresh=refresh, now=now)
            return self._provider_visibility_from_gateway(provider, gateway_result)

        cache_key = provider
        cached = self._model_probe_cache.get(cache_key)
        if (
            not refresh
            and cached
            and now - float(cached.get("checked_at", 0.0)) < self._model_probe_cache_ttl_secs
        ):
            return cached

        base_url, api_key = self._local_provider_transport(provider)
        if not api_key:
            result = {
                "status": "unavailable",
                "ids": set(),
                "reason": f"{provider} API key 未配置",
                "checked_at": now,
            }
            self._model_probe_cache[cache_key] = result
            return result
        if not base_url:
            result = {
                "status": "unknown",
                "ids": set(),
                "reason": f"{provider} base URL 未配置",
                "checked_at": now,
            }
            self._model_probe_cache[cache_key] = result
            return result

        result = self._probe_models_endpoint(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            checked_at=now,
        )
        self._model_probe_cache[cache_key] = result
        return result

    def _fetch_gateway_model_catalog(self, *, refresh: bool, now: float) -> Dict[str, Any]:
        cache_key = "__billing_gateway__"
        cached = self._model_probe_cache.get(cache_key)
        if (
            not refresh
            and cached
            and now - float(cached.get("checked_at", 0.0)) < self._model_probe_cache_ttl_secs
        ):
            return cached

        base_url, api_key = self._billing_transport_credentials()
        if not api_key:
            result = {
                "status": "unavailable",
                "ids": set(),
                "reason": "Billing gateway service secret 未配置",
                "checked_at": now,
                "source": "billing_gateway",
            }
            self._model_probe_cache[cache_key] = result
            return result
        if not base_url:
            result = {
                "status": "unknown",
                "ids": set(),
                "reason": "Billing gateway base URL 未配置",
                "checked_at": now,
                "source": "billing_gateway",
            }
            self._model_probe_cache[cache_key] = result
            return result

        result = self._probe_models_endpoint(
            provider="billing_gateway",
            base_url=base_url,
            api_key=api_key,
            checked_at=now,
            unavailable_reason="Billing gateway 鉴权失败或无权限",
        )
        result["source"] = "billing_gateway"
        self._model_probe_cache[cache_key] = result
        return result

    def _provider_visibility_from_gateway(
        self,
        provider: str,
        gateway_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        status = str(gateway_result.get("status") or "unknown")
        ids = gateway_result.get("ids") if isinstance(gateway_result.get("ids"), set) else set()
        provider_ids = self._filter_model_ids_for_provider(ids, provider)
        reason = gateway_result.get("reason")
        if status == "available" and not provider_ids:
            reason = f"{provider} 在 billing gateway 模型目录中无匹配项"
        return {
            "status": status,
            "ids": provider_ids,
            "reason": reason,
            "checked_at": gateway_result.get("checked_at"),
            "source": "billing_gateway",
        }

    def _probe_models_endpoint(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        checked_at: float,
        unavailable_reason: str | None = None,
    ) -> Dict[str, Any]:
        try:
            response = httpx.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5.0,
            )
            if response.status_code in {401, 403}:
                return {
                    "status": "unavailable",
                    "ids": set(),
                    "reason": unavailable_reason or f"{provider} API key 已失效或无权限",
                    "checked_at": checked_at,
                }
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            return {
                "status": "unknown",
                "ids": set(),
                "reason": f"{provider} 模型目录暂时不可用: {exc}",
                "checked_at": checked_at,
            }
        except ValueError as exc:
            return {
                "status": "unknown",
                "ids": set(),
                "reason": f"{provider} 模型目录响应无法解析: {exc}",
                "checked_at": checked_at,
            }

        model_ids = self._extract_model_ids(payload)
        return {
            "status": "available" if model_ids else "unknown",
            "ids": model_ids,
            "reason": None if model_ids else f"{provider} 模型目录为空",
            "checked_at": checked_at,
        }

    def _build_model_entry(
        self,
        provider: str,
        model_name: str,
        visibility: Dict[str, Any],
    ) -> Dict[str, Any]:
        is_vision = self._is_vision_model(model_name)
        status = str(visibility.get("status") or "unknown")
        ids = visibility.get("ids") if isinstance(visibility.get("ids"), set) else set()
        if status == "available" and ids:
            available = self._model_in_catalog(model_name, ids)
            reason = None if available else "模型不在当前账号可见列表中，可能已下线或无权限"
        elif status == "unavailable":
            available = False
            reason = str(visibility.get("reason") or "Provider 不可用")
        else:
            available = True
            reason = str(visibility.get("reason") or "未完成实时校验，运行时仍会兜底处理")
        return {
            "provider": provider,
            "model": model_name,
            "label": f"{self._provider_label(provider)} · {model_name}",
            "available": available,
            "status": "available" if available and status == "available" else status,
            "reason": reason,
            "isVision": is_vision,
            "capabilities": ["vision", "text"] if is_vision else ["text", "stream"],
            "contextWindow": self._context_window_hint(model_name),
        }

    def _select_default_model(
        self,
        *,
        models: list[Dict[str, Any]],
        provider: str,
        vision: bool,
    ) -> str:
        for strict_provider in (provider, "openai", "dashscope"):
            for item in models:
                if item["provider"] != strict_provider:
                    continue
                if bool(item["isVision"]) != vision:
                    continue
                if item["available"]:
                    return str(item["model"])
        for item in models:
            if bool(item["isVision"]) == vision:
                return str(item["model"])
        return ""

    @staticmethod
    def _split_csv(value: str | None) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in str(value or "").split(","):
            item = raw.strip().strip('"').strip("'")
            if not item or item in seen or item.lower() in FORBIDDEN_LLM_MODELS:
                continue
            seen.add(item)
            result.append(item)
        return result

    def _candidate_models(self, provider: str) -> list[str]:
        if provider not in {"openai", "dashscope"}:
            return []

        runtime = self._runtime()
        task_models = [
            getattr(settings, "SM_LLM_MODEL_ANSWER", None),
            getattr(settings, "SM_LLM_MODEL_AUX", None),
            getattr(settings, "SM_LLM_MODEL_GRAPH", None),
            getattr(settings, "SM_LLM_MODEL_SUMMARY", None),
        ]
        out: list[str] = []
        seen: set[str] = set()
        # 第一项用 runtime 默认解析；后续补 task 特定模型，全部复用 runtime 的统一候选规则。
        model_overrides: list[str | None] = [None]
        model_overrides.extend(
            str(item).strip()
            for item in task_models
            if item and self._infer_provider_from_model(str(item)) == provider
        )
        for override in model_overrides:
            llm_options = {"llm_model": override} if override else None
            for model_name in runtime.get_model_candidates_for_provider(provider, llm_options):
                if model_name in seen:
                    continue
                seen.add(model_name)
                out.append(model_name)
        return out

    @staticmethod
    def _dedupe(values: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = str(value or "").strip()
            if not item or item in seen or item.lower() in FORBIDDEN_LLM_MODELS:
                continue
            seen.add(item)
            result.append(item)
        return result

    @staticmethod
    def _extract_model_ids(payload: Any) -> set[str]:
        if not isinstance(payload, dict):
            return set()
        data = payload.get("data")
        if not isinstance(data, list):
            return set()
        result: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id") or item.get("model")
            if isinstance(model_id, str) and model_id.strip():
                result.add(model_id.strip())
        return result

    @staticmethod
    def _infer_provider_from_model(model_name: str) -> str | None:
        name = str(model_name or "").strip().lower()
        if name.startswith(("gpt-", "o1", "o3", "o4")):
            return "openai"
        if name.startswith(("qwen", "deepseek")):
            return "dashscope"
        return None

    @staticmethod
    def _is_vision_model(model_name: str) -> bool:
        name = str(model_name or "").strip().lower()
        return "vl" in name or name in {"gpt-4o"}

    @staticmethod
    def _context_window_hint(model_name: str) -> int | None:
        hints = {
            "qwen-max-latest": 200000,
            "qwen3-max-latest": 200000,
            "qwen3-max": 200000,
            "qwen-max": 200000,
            "qwen-vl-max": 32000,
            "qwen-vl-plus": 32000,
            "gpt-4o": 128000,
            "gpt-4o-mini": 128000,
            "gpt-4.1": 1048576,
            "gpt-5": 400000,
            "gpt-5-mini": 400000,
            "gpt-5.2": 400000,
        }
        return hints.get(str(model_name or "").strip().lower())

    @staticmethod
    def _provider_label(provider: str) -> str:
        return "OpenAI" if provider == "openai" else "通义"

    @staticmethod
    def _local_provider_transport(provider: str) -> tuple[str | None, str | None]:
        if provider == "openai":
            return settings.OPENAI_BASE_URL, settings.OPENAI_API_KEY
        if provider == "dashscope":
            return settings.DASHSCOPE_BASE_URL, settings.DASHSCOPE_API_KEY
        return None, None

    @staticmethod
    def _coerce_secret(value: Any) -> str | None:
        if value is None:
            return None
        if hasattr(value, "get_secret_value"):
            try:
                value = value.get_secret_value()
            except Exception:
                return None
        text = str(value or "").strip()
        return text or None

    def _billing_mode(self) -> str:
        mode = str(getattr(settings, "SCRIPTLENS_BILLING_MODE", "hybrid") or "hybrid").strip().lower()
        if mode not in {"gateway", "hybrid", "local"}:
            return "hybrid"
        return mode

    def _billing_transport_credentials(self) -> tuple[str | None, str | None]:
        base_url = resolve_billing_gateway_base_url(
            getattr(settings, "BILLING_GATEWAY_BASE_URL", None)
        )
        service_secret = self._coerce_secret(getattr(settings, "BILLING_SERVICE_SECRET", None))
        return base_url, service_secret

    def _billing_transport_available(self) -> bool:
        base_url, service_secret = self._billing_transport_credentials()
        return bool(service_secret and base_url)

    def _should_probe_models_via_gateway(self) -> bool:
        mode = self._billing_mode()
        if mode == "gateway":
            return True
        if mode == "hybrid":
            return self._billing_transport_available()
        return False

    @classmethod
    def _normalize_catalog_model_id(cls, model_id: str) -> str:
        return str(model_id or "").strip().split("/")[-1].strip()

    @classmethod
    def _model_in_catalog(cls, model_name: str, ids: set[str]) -> bool:
        target = str(model_name or "").strip()
        if not target or not ids:
            return False
        normalized_ids = {cls._normalize_catalog_model_id(item) for item in ids}
        return target in ids or target in normalized_ids

    @classmethod
    def _filter_model_ids_for_provider(cls, ids: set[str], provider: str) -> set[str]:
        provider_key = str(provider or "").strip().lower()
        result: set[str] = set()
        for raw_id in ids:
            normalized = cls._normalize_catalog_model_id(raw_id)
            if not normalized or normalized.lower() in FORBIDDEN_LLM_MODELS:
                continue
            inferred = cls._infer_provider_from_model(normalized)
            if inferred == provider_key:
                result.add(normalized)
        return result

    def _preferred_provider(self) -> str:
        runtime = self._runtime()
        candidates = runtime.get_provider_candidates()
        if candidates:
            return candidates[0]
        raw = str(getattr(settings, "SM_LLM_TYPE", "") or "").strip().lower()
        if raw in {"openai", "dashscope"}:
            return raw
        if settings.OPENAI_API_KEY:
            return "openai"
        return "dashscope"
