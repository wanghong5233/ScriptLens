"""LLMRuntime —— ScriptLens 全局 LLM 调用底座。

物理位置 `service/core/llm/runtime.py` 表示分层定位：基础设施级依赖，能被
agent_runtime（接入层）、service/script_tools（领域层）、service/core/rag
（核心服务）三方共享，**不反向依赖任何上层模块**。

职责：
  1. provider × model 双层 fallback：openai → dashscope；同 provider 内按
     candidate 列表逐个 model 尝试，命中 NotFound / model_not_found 切下一个
  2. 黑名单（FORBIDDEN_LLM_MODELS）单点拦截弱化模型
  3. provider 健康冷却：连续失败 N 次进入 90s 冷却
  4. 启动期可用性检查 boot_check：对每个 provider 首位 candidate 发 1-token
     探测，全败抛错挡启动
  5. 两种调用形态：
       - generate_text(prompt, tools=None, ...)  ReAct 主决策与一般生成共用
       - generate_json(prompt, ...)              强 JSON 输出（评分/改写工具用）

设计约束：
  - 不依赖任何 agent_runtime 内部模块；通过 DI 接收 settings 与
    metrics_callback。子类（如 agent_runtime.service.llm_client.LLMClient）
    可以提供本模块需要的字段子集。
  - settings 必须包含的字段见 _REQUIRED_SETTINGS_FIELDS（启动时校验）。
"""
from typing import Awaitable, Callable, Dict, Any, Optional, List, Sequence
import asyncio
import logging
import os
import json
import time
from openai import AsyncOpenAI, NotFoundError, APIError

logger = logging.getLogger(__name__)

# 弱化模型绝不进入 fallback 链；docs/08-evaluation-framework.md §6.2
FORBIDDEN_LLM_MODELS = {"qwen-plus", "qwen-turbo", "qwen2.5-plus"}

# DI 注入的 settings 必须含的字段（启动时缺哪个会抛清晰错）
_REQUIRED_SETTINGS_FIELDS = (
    "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "DASHSCOPE_MODEL_NAME", "DASHSCOPE_MODEL_CANDIDATES",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL_NAME", "OPENAI_MODEL_CANDIDATES",
    "LLM_REQUEST_TIMEOUT", "LLM_FALLBACK_ENABLED", "LLM_FALLBACK_ALLOW_EXPLICIT_PROVIDER",
    "LLM_HEALTH_FAILURE_THRESHOLD", "LLM_HEALTH_COOLDOWN_SECONDS",
    "LLM_TEMPERATURE", "LLM_MAX_TOKENS",
    "LLM_COST_CONFIG", "LLM_COST_PER_1K_INPUT_TOKENS", "LLM_COST_PER_1K_OUTPUT_TOKENS",
)


class LLMRuntimeError(RuntimeError):
    """LLMRuntime 抛出的统一基类异常。"""


class LLMUnavailableError(LLMRuntimeError):
    """所有候选 provider × model 都不可用（boot_check 与 generate_text 共用）。"""


class LLMJsonParseError(LLMRuntimeError):
    """generate_json 返回内容 2 次都不是合法 JSON。"""


def _noop_metrics(**_kwargs: Any) -> None:
    return None


def _default_settings():
    """默认从 app.core.config 加载主 settings；调用方可显式注入子集 settings。"""
    from core.config import settings as _settings
    return _settings


class LLMRuntime:
    """LLM 调用底座（线程安全实例：可单例使用）。

    Args:
        settings_obj: settings 实例；至少要有 _REQUIRED_SETTINGS_FIELDS 字段。
                      省略时从 ``core.config.settings`` 取主 API 配置。
        metrics_callback: 可选的 usage 回调；签名同 record_llm_usage。
                          省略时使用 noop（不上报指标）。
    """

    def __init__(
        self,
        *,
        settings_obj: Any = None,
        metrics_callback: Optional[Callable[..., None]] = None,
    ) -> None:
        self.settings = settings_obj if settings_obj is not None else _default_settings()
        self._metrics = metrics_callback or _noop_metrics
        missing = [f for f in _REQUIRED_SETTINGS_FIELDS if not hasattr(self.settings, f)]
        if missing:
            raise LLMRuntimeError(
                f"settings_obj 缺字段：{missing}；请确认传入的是含完整 LLM_* 配置的 Settings 实例"
            )

        self.mode = "api"
        self.provider: Optional[str] = None
        self.temperature = self.settings.LLM_TEMPERATURE
        self.max_tokens = self.settings.LLM_MAX_TOKENS
        self.client: Optional[AsyncOpenAI] = None
        self._client_cache: Dict[str, AsyncOpenAI] = {}
        self._provider_health: Dict[str, Dict[str, Any]] = {}
        self._last_runtime_model: Dict[str, Any] | None = None
        self._configure()

    def refresh_settings(self, settings_obj: Any = None) -> Dict[str, Any]:
        """Hot-reload settings 后重新构建客户端缓存。

        调用方负责持有 settings 的更新（例如 refresh_settings 由 main 主进程触发），
        本方法只负责重新读取并重建 client。
        """
        if settings_obj is not None:
            self.settings = settings_obj
        self._configure()
        return {
            "mode": self.mode,
            "provider": self.provider,
            "model": getattr(self, "model", None),
        }

    def _configure(self) -> None:
        """Apply current settings to the client."""

        self.mode = "api"
        self.temperature = self.settings.LLM_TEMPERATURE
        self.max_tokens = self.settings.LLM_MAX_TOKENS
        self.client = None
        self.provider = None

        if self.mode == "api":
            self.api_key = self.settings.OPENAI_API_KEY or self.settings.DASHSCOPE_API_KEY
            self.base_url = (
                self.settings.OPENAI_BASE_URL if self.settings.OPENAI_API_KEY else self.settings.DASHSCOPE_BASE_URL
            )
            self.model = (
                self.settings.OPENAI_MODEL_NAME if self.settings.OPENAI_API_KEY else self.settings.DASHSCOPE_MODEL_NAME
            )
            self.provider = "OpenAI" if self.settings.OPENAI_API_KEY else "DashScope"

            if not self.api_key:
                logger.warning("LLM API key not configured. LLM calls will fail.")
                self.client = None
            else:
                self.client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=self.settings.LLM_REQUEST_TIMEOUT,
                )
                cache_key = self._build_client_cache_key(self.provider, self.base_url)
                self._client_cache[cache_key] = self.client
                logger.info(
                    "LLMRuntime initialized: mode=api, provider=%s, model=%s",
                    self.provider,
                    self.model,
                )

    def _build_client_cache_key(self, provider: str, base_url: str) -> str:
        """Build a cache key for provider clients."""

        return f"{provider.lower()}::{base_url}"

    @staticmethod
    def _normalize_provider_key(provider: str) -> str:
        return str(provider or "").strip().lower()

    def _get_provider_config(self, provider: str) -> Dict[str, Any]:
        """Return provider settings for the given provider."""

        normalized = provider.lower()
        if normalized == "dashscope":
            return {
                "provider": "DashScope",
                "api_key": self.settings.DASHSCOPE_API_KEY,
                "base_url": self.settings.DASHSCOPE_BASE_URL,
                "model": self.settings.DASHSCOPE_MODEL_NAME,
            }
        if normalized == "openai":
            return {
                "provider": "OpenAI",
                "api_key": self.settings.OPENAI_API_KEY,
                "base_url": self.settings.OPENAI_BASE_URL,
                "model": self.settings.OPENAI_MODEL_NAME,
            }
        raise ValueError(f"Unsupported LLM provider: {provider}")

    def _coerce_float(self, value: Any, fallback: float) -> float:
        """Coerce a value to float if possible."""

        try:
            if value is None:
                return fallback
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def _coerce_int(self, value: Any, fallback: int) -> int:
        """Coerce a value to int if possible."""

        try:
            if value is None:
                return fallback
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _uses_max_completion_tokens(model_name: Optional[str]) -> bool:
        name = str(model_name or "").strip().lower()
        return name.startswith("gpt-5")

    @staticmethod
    def _supports_custom_temperature(model_name: Optional[str]) -> bool:
        name = str(model_name or "").strip().lower()
        return not name.startswith("gpt-5")

    @staticmethod
    def _infer_provider_from_model(model_name: Optional[str]) -> Optional[str]:
        name = str(model_name or "").strip().lower()
        if not name:
            return None
        if name.startswith(("gpt-", "o1", "o3", "o4")):
            return "openai"
        if name.startswith(("qwen", "deepseek")):
            return "dashscope"
        return None

    def _model_matches_provider(self, model_name: Optional[str], provider_key: str) -> bool:
        inferred = self._infer_provider_from_model(model_name)
        return inferred is None or inferred == self._normalize_provider_key(provider_key)

    @staticmethod
    def _split_csv(value: Optional[str]) -> List[str]:
        items: List[str] = []
        seen: set[str] = set()
        for raw_item in str(value or "").split(","):
            item = raw_item.strip().strip('"').strip("'")
            if not item or item in seen or item.lower() in FORBIDDEN_LLM_MODELS:
                continue
            seen.add(item)
            items.append(item)
        return items

    def _configured_models_for_provider(self, provider_key: str) -> List[str]:
        normalized = self._normalize_provider_key(provider_key)
        config = self._get_provider_config(normalized)
        if normalized == "openai":
            configured = self._split_csv(getattr(self.settings, "OPENAI_MODEL_CANDIDATES", ""))
        elif normalized == "dashscope":
            configured = self._split_csv(getattr(self.settings, "DASHSCOPE_MODEL_CANDIDATES", ""))
        else:
            configured = []

        result: List[str] = []
        seen: set[str] = set()
        for candidate in [config.get("model"), *configured]:
            model_name = str(candidate or "").strip()
            if (
                not model_name
                or model_name in seen
                or model_name.lower() in FORBIDDEN_LLM_MODELS
                or not self._model_matches_provider(model_name, normalized)
            ):
                continue
            seen.add(model_name)
            result.append(model_name)
        return result

    def _model_candidates_for_provider(
        self,
        llm_options: Optional[Dict[str, Any]],
        provider_key: str,
    ) -> List[str]:
        normalized = self._normalize_provider_key(provider_key)
        candidates: List[str] = []
        model_override = (llm_options or {}).get("llm_model")
        if (
            model_override
            and str(model_override).strip().lower() not in FORBIDDEN_LLM_MODELS
            and self._model_matches_provider(str(model_override), normalized)
        ):
            candidates.append(str(model_override).strip())
        candidates.extend(self._configured_models_for_provider(normalized))

        result: List[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            model_name = str(candidate or "").strip()
            if not model_name or model_name in seen:
                continue
            seen.add(model_name)
            result.append(model_name)
        return result

    def _resolve_llm_config(
        self,
        llm_options: Optional[Dict[str, Any]],
        provider_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve LLM config based on optional overrides."""

        resolved_provider = provider_key
        if not resolved_provider:
            provider_override = (llm_options or {}).get("llm_provider")
            if provider_override and str(provider_override).lower() != "auto":
                resolved_provider = str(provider_override)
            else:
                resolved_provider = "openai" if self.settings.OPENAI_API_KEY else "dashscope"

        normalized_provider = self._normalize_provider_key(resolved_provider)
        config = self._get_provider_config(normalized_provider)
        config["provider_key"] = normalized_provider
        model_override = (llm_options or {}).get("llm_model")
        if (
            model_override
            and str(model_override).strip().lower() not in FORBIDDEN_LLM_MODELS
            and self._model_matches_provider(str(model_override), normalized_provider)
        ):
            config["model"] = str(model_override)

        config["temperature"] = self._coerce_float(
            (llm_options or {}).get("llm_temperature"),
            self.temperature,
        )
        config["max_tokens"] = self._coerce_int(
            (llm_options or {}).get("llm_max_tokens"),
            self.max_tokens,
        )
        return config

    def _get_available_providers(self) -> List[str]:
        providers: List[str] = []
        if self.settings.DASHSCOPE_API_KEY:
            providers.append("dashscope")
        if self.settings.OPENAI_API_KEY:
            providers.append("openai")
        return providers

    def _get_provider_candidates(self, llm_options: Optional[Dict[str, Any]]) -> List[str]:
        provider_override = (llm_options or {}).get("llm_provider")
        normalized_override = self._normalize_provider_key(provider_override or "")
        preferred = ""
        if normalized_override and normalized_override != "auto":
            preferred = normalized_override
        else:
            preferred = "openai" if self.settings.OPENAI_API_KEY else "dashscope"

        available = self._get_available_providers()
        if preferred and preferred not in available and available:
            preferred = available[0]

        if not self.settings.LLM_FALLBACK_ENABLED:
            return [preferred] if preferred else available

        if provider_override and normalized_override != "auto" and not self.settings.LLM_FALLBACK_ALLOW_EXPLICIT_PROVIDER:
            return [preferred] if preferred else available

        candidates = [preferred] if preferred else []
        for provider in available:
            if provider not in candidates:
                candidates.append(provider)
        return candidates

    def get_health_snapshot(self) -> Dict[str, Any]:
        """Return provider health information for UI/monitoring."""

        now = time.time()
        available = self._get_available_providers()
        candidates = self._get_provider_candidates(None)
        providers: List[Dict[str, Any]] = []
        for provider in available:
            state = self._get_provider_state(provider)
            cooldown_until = float(state.get("cooldown_until") or 0.0)
            in_cooldown = now < cooldown_until
            providers.append(
                {
                    "provider": provider,
                    "available": True,
                    "in_cooldown": in_cooldown,
                    "cooldown_remaining_seconds": max(0, int(cooldown_until - now)),
                    "failures": int(state.get("failures") or 0),
                    "last_error": state.get("last_error"),
                    "last_success_at": state.get("last_success_at"),
                    "last_failure_at": state.get("last_failure_at"),
                }
            )

        return {
            "preferred_provider": candidates[0] if candidates else None,
            "available_providers": available,
            "providers": providers,
            "fallback_enabled": self.settings.LLM_FALLBACK_ENABLED,
            "fallback_allow_explicit_provider": self.settings.LLM_FALLBACK_ALLOW_EXPLICIT_PROVIDER,
            "failure_threshold": self.settings.LLM_HEALTH_FAILURE_THRESHOLD,
            "cooldown_seconds": self.settings.LLM_HEALTH_COOLDOWN_SECONDS,
            "request_timeout": self.settings.LLM_REQUEST_TIMEOUT,
        }

    def get_last_runtime_model(self) -> Dict[str, Any] | None:
        """Return requested/actual model metadata for the last generation."""

        return dict(self._last_runtime_model) if isinstance(self._last_runtime_model, dict) else None

    def _get_provider_state(self, provider_key: str) -> Dict[str, Any]:
        key = self._normalize_provider_key(provider_key)
        state = self._provider_health.get(key)
        if not state:
            state = {
                "failures": 0,
                "cooldown_until": 0.0,
                "last_error": None,
                "last_failure_at": None,
                "last_success_at": None,
            }
            self._provider_health[key] = state
        return state

    def _is_provider_in_cooldown(self, provider_key: str) -> bool:
        state = self._get_provider_state(provider_key)
        return time.time() < float(state.get("cooldown_until") or 0.0)

    def _mark_provider_failure(self, provider_key: str, error: Exception) -> None:
        state = self._get_provider_state(provider_key)
        state["failures"] = int(state.get("failures") or 0) + 1
        state["last_failure_at"] = time.time()
        state["last_error"] = str(error)
        if state["failures"] >= self.settings.LLM_HEALTH_FAILURE_THRESHOLD:
            state["cooldown_until"] = time.time() + self.settings.LLM_HEALTH_COOLDOWN_SECONDS
            logger.warning(
                "LLM provider %s进入冷却期 %ss (failures=%s): %s",
                provider_key,
                self.settings.LLM_HEALTH_COOLDOWN_SECONDS,
                state["failures"],
                error,
            )

    def _mark_provider_success(self, provider_key: str) -> None:
        state = self._get_provider_state(provider_key)
        state["failures"] = 0
        state["cooldown_until"] = 0.0
        state["last_success_at"] = time.time()
        state["last_error"] = None

    def _estimate_cost(
        self,
        provider_key: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        provider_key = self._normalize_provider_key(provider_key)
        config = self.settings.LLM_COST_CONFIG or {}
        provider_config = config.get(provider_key) or config.get(provider_key.lower()) or {}
        model_config = provider_config.get(model) or provider_config.get("default") or {}
        input_rate = float(
            model_config.get("input", self.settings.LLM_COST_PER_1K_INPUT_TOKENS)
        )
        output_rate = float(
            model_config.get("output", self.settings.LLM_COST_PER_1K_OUTPUT_TOKENS)
        )
        return (prompt_tokens / 1000.0) * input_rate + (completion_tokens / 1000.0) * output_rate

    @staticmethod
    def _extract_usage(usage: Any) -> Dict[str, int]:
        if not usage:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def _get_client(self, provider: str, api_key: str, base_url: str) -> AsyncOpenAI:
        """Get or create a client for a provider."""

        cache_key = self._build_client_cache_key(provider, base_url)
        if self.provider == provider and self.client:
            return self.client
        cached = self._client_cache.get(cache_key)
        if cached:
            return cached
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=self.settings.LLM_REQUEST_TIMEOUT,
        )
        self._client_cache[cache_key] = client
        return client
    
    async def generate(
        self,
        prompt: str,
        tools: Optional[list] = None,
        temperature: float = 0.3,
        llm_options: Optional[Dict[str, Any]] = None,
        image_attachments: Optional[List[Dict[str, Any]]] = None,
        stream_text_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """
        生成回复（支持 API 和本地模型两种模式）
        
        Args:
            prompt: 提示词
            tools: 可用工具列表（Tool Calling 格式）
            temperature: 温度参数
            
        Returns:
            LLM 响应，包含 content 和 tool_calls
        """
        if self.mode == "local":
            # 本地模型模式（未来实现）
            return await self._generate_local(prompt, tools, temperature, image_attachments=image_attachments)
        else:
            # API 模式（当前使用）
            candidates = self._get_provider_candidates(llm_options)
            if not candidates:
                raise ValueError("No available LLM providers configured")

            healthy_candidates = [p for p in candidates if not self._is_provider_in_cooldown(p)]
            if not healthy_candidates:
                healthy_candidates = candidates

            temp_value = temperature
            max_tokens_value = self.max_tokens
            if llm_options and llm_options.get("llm_temperature") is not None:
                temp_value = None  # 将在配置中读取
            if llm_options and llm_options.get("llm_max_tokens") is not None:
                max_tokens_value = None

            last_error: Optional[Exception] = None
            requested_provider = self._normalize_provider_key(
                (llm_options or {}).get("llm_provider") or healthy_candidates[0]
            )
            if requested_provider in {"", "auto"}:
                requested_provider = healthy_candidates[0]
            requested_model = str((llm_options or {}).get("llm_model") or "").strip()
            primary_provider = healthy_candidates[0]
            primary_model: Optional[str] = None

            for provider_key in healthy_candidates:
                for model_name in self._model_candidates_for_provider(llm_options, provider_key):
                    try:
                        llm_config = self._resolve_llm_config(llm_options, provider_key)
                        llm_config["model"] = model_name
                        if primary_model is None:
                            primary_model = model_name
                        resolved_provider = self._normalize_provider_key(
                            llm_config.get("provider_key") or provider_key
                        )
                        runtime_model = {
                            "requested_provider": requested_provider,
                            "requested_model": requested_model or primary_model or model_name,
                            "actual_provider": resolved_provider,
                            "actual_model": model_name,
                            "fallback_applied": (
                                resolved_provider != requested_provider
                                or (
                                    bool(requested_model)
                                    and model_name != requested_model
                                )
                            ),
                        }
                        self._last_runtime_model = runtime_model
                        resolved_temp = temp_value if temp_value is not None else llm_config["temperature"]
                        resolved_max_tokens = (
                            max_tokens_value if max_tokens_value is not None else llm_config["max_tokens"]
                        )
                        response = await self._generate_api(
                            prompt,
                            tools,
                            resolved_temp,
                            llm_config,
                            resolved_max_tokens,
                            image_attachments=image_attachments,
                            stream_text_callback=stream_text_callback,
                        )
                        response["runtime_model"] = runtime_model
                        response["requested_provider"] = runtime_model["requested_provider"]
                        response["requested_model"] = runtime_model["requested_model"]
                        response["actual_provider"] = runtime_model["actual_provider"]
                        response["actual_model"] = runtime_model["actual_model"]
                        response["fallback_applied"] = runtime_model["fallback_applied"]
                        self._mark_provider_success(resolved_provider)
                        if resolved_provider != primary_provider or model_name != primary_model:
                            logger.warning(
                                "LLM fallback applied: %s/%s -> %s/%s",
                                primary_provider,
                                primary_model,
                                resolved_provider,
                                model_name,
                            )
                        return response
                    except Exception as exc:
                        if isinstance(exc, asyncio.CancelledError) or exc.__class__.__name__ == "AgentCancelledError":
                            raise
                        last_error = exc
                        self._mark_provider_failure(provider_key, exc)
                        logger.warning(
                            "LLM candidate %s/%s failed: %s",
                            provider_key,
                            model_name,
                            exc,
                        )
                        continue

            if last_error:
                raise last_error
            raise ValueError("No available LLM providers configured")
    
    async def _generate_api(
        self,
        prompt: str,
        tools: Optional[list] = None,
        temperature: float = 0.3,
        llm_config: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
        image_attachments: Optional[List[Dict[str, Any]]] = None,
        stream_text_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """
        使用 API 生成回复（DashScope/OpenAI）
        
        使用 OpenAI SDK（和主 API 服务一样），自带重试和超时管理
        """
        config = llm_config or self._resolve_llm_config(None)
        api_key = config.get("api_key")
        base_url = config.get("base_url")
        model = config.get("model")
        provider = config.get("provider")
        if not api_key:
            raise ValueError("LLM API key not configured")
        client = self._get_client(provider, api_key, base_url)

        start_time = time.perf_counter()
        try:
            messages = self._build_messages_for_chat(prompt, image_attachments)
            should_stream_text = bool(stream_text_callback and not tools)
            token_key = (
                "max_completion_tokens"
                if self._uses_max_completion_tokens(str(model))
                else "max_tokens"
            )
            # 构建请求参数（和主 API 服务保持一致）
            kwargs = {
                "model": model,
                "messages": messages,
                "stream": should_stream_text,
            }
            if self._supports_custom_temperature(str(model)) or float(temperature) == 1.0:
                kwargs["temperature"] = temperature
            kwargs[token_key] = max_tokens or self.max_tokens
            
            # 如果提供了工具，添加到请求中
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            content = ""
            formatted_tool_calls = None
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

            if should_stream_text:
                # Ask 模式正文流式输出：按 token/delta 回调到上层（SSE 再转发给前端）
                kwargs["stream_options"] = {"include_usage": True}
                try:
                    stream_response = await client.chat.completions.create(**kwargs)
                except Exception:
                    # 兼容部分 OpenAI-Compatible 服务不支持 stream_options 的情况
                    kwargs.pop("stream_options", None)
                    stream_response = await client.chat.completions.create(**kwargs)
                chunks: List[str] = []
                received_any_chunk = False
                async for chunk in stream_response:
                    received_any_chunk = True
                    chunk_usage = self._extract_usage(getattr(chunk, "usage", None))
                    if chunk_usage["total_tokens"] > 0:
                        usage = chunk_usage

                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue

                    delta = getattr(choices[0], "delta", None)
                    if not delta:
                        continue

                    delta_content = getattr(delta, "content", None)
                    delta_text = ""
                    if isinstance(delta_content, str):
                        delta_text = delta_content
                    elif isinstance(delta_content, list):
                        delta_text = "".join(
                            str(part.get("text", ""))
                            for part in delta_content
                            if isinstance(part, dict)
                        )

                    if not delta_text:
                        continue
                    chunks.append(delta_text)
                    if stream_text_callback:
                        await stream_text_callback(delta_text)

                if not received_any_chunk:
                    raise ValueError("No stream chunks in LLM response")
                content = "".join(chunks)
                if not content.strip():
                    raise ValueError("Empty streamed content in LLM response")
            else:
                # 调用 OpenAI SDK（自带重试和超时管理）
                response = await client.chat.completions.create(**kwargs)

                # 解析响应
                if not response.choices:
                    raise ValueError("No choices in LLM response")

                message = response.choices[0].message
                content = message.content or ""
                tool_calls = message.tool_calls

                # 转换 tool_calls 为标准格式
                if tool_calls:
                    formatted_tool_calls = [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ]

                usage = self._extract_usage(getattr(response, "usage", None))

            duration = time.perf_counter() - start_time
            cost = self._estimate_cost(provider, model, usage["prompt_tokens"], usage["completion_tokens"])
            self._metrics(
                provider=self._normalize_provider_key(provider),
                model=str(model),
                success=True,
                duration=duration,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                cost=cost,
            )

            return {
                "content": content,
                "tool_calls": formatted_tool_calls,
                "usage": usage,
                "provider": provider,
                "model": model,
                "cost": cost,
            }
        
        except Exception as e:
            if isinstance(e, asyncio.CancelledError) or e.__class__.__name__ == "AgentCancelledError":
                raise
            duration = time.perf_counter() - start_time
            self._metrics(
                provider=self._normalize_provider_key(provider),
                model=str(model),
                success=False,
                duration=duration,
            )
            logger.error(f"Error calling LLM: {e}", exc_info=True)
            raise
    
    async def _generate_local(
        self,
        prompt: str,
        tools: Optional[list] = None,
        temperature: float = 0.3,
        image_attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        使用本地模型生成回复（未来实现）
        
        TODO: 实现本地模型推理逻辑
        - 使用 transformers 加载微调的 Qwen-7B
        - 或使用 vLLM 进行高效推理
        - 支持 Tool Calling 格式输出
        """
        raise NotImplementedError(
            "本地模型模式尚未实现。请设置 RL_MODEL_ENABLED=False 使用 API 模式，"
            "或等待本地模型推理功能开发完成。"
        )

    @staticmethod
    def _normalize_image_attachments(
        image_attachments: Optional[List[Dict[str, Any]]],
    ) -> List[str]:
        urls: List[str] = []
        if not image_attachments:
            return urls
        for item in image_attachments:
            if not isinstance(item, dict):
                continue
            url = str(item.get("data_url") or item.get("url") or "").strip()
            if not url:
                continue
            if not (
                url.startswith("data:image/")
                or url.startswith("http://")
                or url.startswith("https://")
            ):
                continue
            urls.append(url)
        return urls

    def _build_messages_for_chat(
        self,
        prompt: str,
        image_attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        image_urls = self._normalize_image_attachments(image_attachments)
        if not image_urls:
            return [{"role": "user", "content": prompt}]

        content_parts: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for url in image_urls:
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": url},
                }
            )
        return [{"role": "user", "content": content_parts}]
    
    # ============================================================
    # 公共调用接口
    # ============================================================

    async def generate_text(
        self,
        prompt: str,
        *,
        tools: Optional[list] = None,
        temperature: float = 0.3,
        llm_options: Optional[Dict[str, Any]] = None,
        image_attachments: Optional[List[Dict[str, Any]]] = None,
        stream_text_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """文本生成入口（含 tool calling）；与旧 ``generate`` 等价。

        参数命名与现有 LLMClient.generate 兼容；返回结构 dict 含
        content / tool_calls / usage / provider / model / runtime_model 等字段。
        """
        return await self.generate(
            prompt=prompt,
            tools=tools,
            temperature=temperature,
            llm_options=llm_options,
            image_attachments=image_attachments,
            stream_text_callback=stream_text_callback,
        )

    async def generate_json(
        self,
        prompt: str,
        *,
        system_message: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """强 JSON 输出（评分 / 改写 / 风险 / reward 等内部工具用）。

        相比 generate_text：
          - 默认 temperature=0（评分要稳定）
          - 解析阶段 lenient JSON：剥 ```json 围栏、首 `{` 末 `}` 抽取
          - 失败时抛 LLMJsonParseError 而不是返回字符串

        参数 max_tokens 直接传给底层 chat completions（OpenAI 推理模型自动加 reasoning overhead）。
        Provider × model fallback 由 generate_text 统一负责，本方法不做 fallback 重试。
        """
        composed_prompt = (
            f"{system_message}\n\n{prompt}" if system_message else prompt
        )
        composed_options = dict(llm_options or {})
        composed_options.setdefault("llm_temperature", float(temperature))
        composed_options.setdefault("llm_max_tokens", int(max_tokens))

        response = await self.generate(
            prompt=composed_prompt,
            tools=None,
            temperature=temperature,
            llm_options=composed_options,
        )
        raw = response.get("content") or ""
        parsed = _parse_json_lenient(raw)
        if parsed is None:
            raise LLMJsonParseError(
                f"LLM 输出非合法 JSON（前 200 字）: {raw[:200]}"
            )
        return {
            "raw": raw,
            "parsed": parsed,
            "provider": response.get("provider"),
            "model": response.get("model"),
            "runtime_model": response.get("runtime_model"),
            "usage": response.get("usage"),
        }

    # ============================================================
    # 启动期可用性检查
    # ============================================================

    async def boot_check(self, *, probe_max_tokens: int = 4) -> Dict[str, Any]:
        """启动期对每个 available provider 的首位 candidate 发 1-token 探测。

        返回 {provider: {ok: bool, model: str, error: str|None}}；
        所有 available provider 全失败 → 抛 LLMUnavailableError，让 FastAPI startup 失败。
        """
        results: Dict[str, Any] = {}
        any_ok = False
        for provider_key in self._get_available_providers():
            candidates = self._model_candidates_for_provider(None, provider_key)
            if not candidates:
                results[provider_key] = {"ok": False, "model": None, "error": "no candidates"}
                continue
            model = candidates[0]
            llm_config = self._get_provider_config(provider_key)
            llm_config["model"] = model
            try:
                client = self._get_client(
                    llm_config["provider"], llm_config["api_key"], llm_config["base_url"]
                )
                token_key = (
                    "max_completion_tokens"
                    if self._uses_max_completion_tokens(model)
                    else "max_tokens"
                )
                kwargs: Dict[str, Any] = {
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    token_key: probe_max_tokens,
                }
                if self._supports_custom_temperature(model):
                    kwargs["temperature"] = 0.0
                await client.chat.completions.create(**kwargs)
                results[provider_key] = {"ok": True, "model": model, "error": None}
                any_ok = True
                logger.info("LLMRuntime boot_check ✓ %s/%s", provider_key, model)
            except Exception as exc:
                results[provider_key] = {"ok": False, "model": model, "error": str(exc)[:200]}
                logger.warning(
                    "LLMRuntime boot_check ✗ %s/%s: %s", provider_key, model, str(exc)[:200]
                )

        if not any_ok:
            raise LLMUnavailableError(
                "boot_check: 所有 available provider 都不可用；"
                f"详情：{json.dumps(results, ensure_ascii=False)}"
            )
        return results


# ============================================================
# JSON 解析辅助
# ============================================================


def _parse_json_lenient(text: str) -> Any:
    """从模型输出里抽 JSON：支持 ```json fenced 块、首尾噪音；不合法返回 None。"""
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if "```" in text:
        for part in text.split("```"):
            stripped = part.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    continue
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    start = text.find("[")
    end = text.rfind("]")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return None

