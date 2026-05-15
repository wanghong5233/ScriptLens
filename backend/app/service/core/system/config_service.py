"""Configuration service."""

from __future__ import annotations

from importlib import import_module
import time
from typing import Any, Dict, Iterable

import httpx

from core.config import settings
from service.core.rag.nlp.rag_tokenizer import RagTokenizer

FORBIDDEN_LLM_MODELS = {"qwen-plus", "qwen-turbo", "qwen2.5-plus"}

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
            provider=self._preferred_provider(),
            vision=False,
        )
        default_vision_model = self._select_default_model(
            models=models,
            provider=self._preferred_provider(),
            vision=True,
        )
        return {
            "preferredProvider": self._preferred_provider(),
            "defaultModel": default_model,
            "defaultVisionModel": default_vision_model,
            "models": models,
            "cacheTtlSeconds": self._model_probe_cache_ttl_secs,
        }

    def _fetch_visible_model_ids(self, provider: str, *, refresh: bool) -> Dict[str, Any]:
        """Fetch visible model ids from provider /models with a short TTL cache."""

        cache_key = provider
        now = time.time()
        cached = self._model_probe_cache.get(cache_key)
        if (
            not refresh
            and cached
            and now - float(cached.get("checked_at", 0.0)) < self._model_probe_cache_ttl_secs
        ):
            return cached

        base_url, api_key = self._provider_transport(provider)
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

        try:
            response = httpx.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5.0,
            )
            if response.status_code in {401, 403}:
                result = {
                    "status": "unavailable",
                    "ids": set(),
                    "reason": f"{provider} API key 已失效或无权限",
                    "checked_at": now,
                }
                self._model_probe_cache[cache_key] = result
                return result
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            result = {
                "status": "unknown",
                "ids": set(),
                "reason": f"{provider} 模型目录暂时不可用: {exc}",
                "checked_at": now,
            }
            self._model_probe_cache[cache_key] = result
            return result
        except ValueError as exc:
            result = {
                "status": "unknown",
                "ids": set(),
                "reason": f"{provider} 模型目录响应无法解析: {exc}",
                "checked_at": now,
            }
            self._model_probe_cache[cache_key] = result
            return result

        model_ids = self._extract_model_ids(payload)
        result = {
            "status": "available" if model_ids else "unknown",
            "ids": model_ids,
            "reason": None if model_ids else f"{provider} 模型目录为空",
            "checked_at": now,
        }
        self._model_probe_cache[cache_key] = result
        return result

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
            available = model_name in ids
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
        configured = (
            getattr(settings, "OPENAI_MODEL_CANDIDATES", "")
            if provider == "openai"
            else getattr(settings, "DASHSCOPE_MODEL_CANDIDATES", "")
        )
        default_model = (
            getattr(settings, "OPENAI_MODEL_NAME", "")
            if provider == "openai"
            else getattr(settings, "DASHSCOPE_MODEL_NAME", "")
        )
        task_models = [
            getattr(settings, "SM_LLM_MODEL_ANSWER", None),
            getattr(settings, "SM_LLM_MODEL_AUX", None),
            getattr(settings, "SM_LLM_MODEL_GRAPH", None),
            getattr(settings, "SM_LLM_MODEL_SUMMARY", None),
        ]
        candidates = [default_model, *self._split_csv(configured)]
        candidates.extend(
            str(item).strip()
            for item in task_models
            if item and self._infer_provider_from_model(str(item)) == provider
        )
        return self._dedupe(candidates)

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
    def _provider_transport(provider: str) -> tuple[str | None, str | None]:
        if provider == "openai":
            return settings.OPENAI_BASE_URL, settings.OPENAI_API_KEY
        if provider == "dashscope":
            return settings.DASHSCOPE_BASE_URL, settings.DASHSCOPE_API_KEY
        return None, None

    @staticmethod
    def _preferred_provider() -> str:
        raw = str(getattr(settings, "SM_LLM_TYPE", "") or "").strip().lower()
        if raw in {"openai", "dashscope"}:
            return raw
        if settings.OPENAI_API_KEY:
            return "openai"
        return "dashscope"
