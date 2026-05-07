from __future__ import annotations
from typing import Iterable, Generator, Optional, Dict, Any, List
from core.config import settings
import httpx
import logging


logger = logging.getLogger(__name__)


class LLMClient:
    """
    Unified LLM client facade:
    - providers: dashscope/openai/local
    - streaming & non-streaming
    - simple retry with backoff
    """

    def __init__(
        self,
        *,
        task: str = "default",
        provider: Optional[str] = None,
        model: Optional[str] = None,
        timeout_secs: Optional[float] = None,
    ) -> None:
        # 动态选择：若显式配置为 dashscope/openai 则使用；
        # 若为 local 但存在云端 API Key，则自动切换到对应云端；否则使用本地占位实现。
        prov = self._normalize_provider(provider or settings.SM_LLM_TYPE)
        if prov == "local" and settings.DASHSCOPE_API_KEY:
            prov = "dashscope"
        if prov == "local" and settings.OPENAI_API_KEY:
            prov = "openai"
        if prov not in {"dashscope", "openai", "local"}:
            raise ValueError(f"Unsupported LLM provider: {prov}")
        self.provider = prov
        self.task = (task or "default").strip().lower()
        self.model = model or self._resolve_task_model(self.task, self.provider)
        self.base_url, self.api_key = self._resolve_transport(self.provider)
        self.timeout_secs = float(timeout_secs or getattr(settings, "SM_LLM_REQUEST_TIMEOUT_SECS", 60) or 60)
        self._last_usage: Dict[str, int] | None = None
        self._last_runtime_model: Dict[str, Any] | None = None

    @staticmethod
    def _normalize_provider(provider: Optional[str]) -> str:
        raw = str(provider or "").strip().lower()
        if raw in {"dashscope", "openai", "local"}:
            return raw
        if raw in {"", "auto"}:
            return "local"
        return raw

    def _resolve_provider(self, provider: Optional[str]) -> str:
        prov = self._normalize_provider(provider) if provider is not None else self.provider
        if prov == "local":
            if settings.DASHSCOPE_API_KEY:
                return "dashscope"
            if settings.OPENAI_API_KEY:
                return "openai"
        return prov

    def _resolve_transport(self, provider: str) -> tuple[Optional[str], Optional[str]]:
        normalized = self._normalize_provider(provider)
        if normalized == "openai":
            return settings.OPENAI_BASE_URL, settings.OPENAI_API_KEY
        if normalized == "dashscope":
            return settings.DASHSCOPE_BASE_URL, settings.DASHSCOPE_API_KEY
        return None, None

    def _infer_provider_from_model(self, model: Optional[str]) -> Optional[str]:
        name = str(model or "").strip().lower()
        if not name:
            return None
        openai_prefixes = ("gpt-", "o1", "o3", "o4")
        dashscope_prefixes = ("qwen", "deepseek")
        if name.startswith(openai_prefixes):
            return "openai"
        if name.startswith(dashscope_prefixes):
            return "dashscope"
        return None

    @staticmethod
    def _uses_max_completion_tokens(model: str) -> bool:
        name = str(model or "").strip().lower()
        return name.startswith("gpt-5")

    @staticmethod
    def _supports_custom_temperature(model: str) -> bool:
        name = str(model or "").strip().lower()
        return not name.startswith("gpt-5")

    def _model_matches_provider(self, model: Optional[str], provider: str) -> bool:
        inferred = self._infer_provider_from_model(model)
        return inferred is None or inferred == provider

    def _resolve_runtime_config(
        self,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> tuple[str, Optional[str], Optional[str], str]:
        resolved_provider = self._resolve_provider(provider)
        inferred_provider = self._infer_provider_from_model(model)
        if provider is None and inferred_provider in {"dashscope", "openai"}:
            resolved_provider = inferred_provider
        if resolved_provider not in {"dashscope", "openai", "local"}:
            raise ValueError(f"Unsupported LLM provider: {resolved_provider}")
        fallback_model = (
            self.model
            if resolved_provider == self.provider
            else self._resolve_task_model(self.task, resolved_provider)
        )
        model_name = str(model or fallback_model)
        base_url, api_key = self._resolve_transport(resolved_provider)
        return resolved_provider, base_url, api_key, model_name

    def _resolve_task_model(self, task: str, provider: str) -> str:
        default_model = (
            getattr(settings, "OPENAI_MODEL_NAME", "gpt-5.2")
            if provider == "openai"
            else getattr(settings, "DASHSCOPE_MODEL_NAME", "qwen-max-latest")
        )
        answer = getattr(settings, "SM_LLM_MODEL_ANSWER", None)
        aux = getattr(settings, "SM_LLM_MODEL_AUX", None)
        graph = getattr(settings, "SM_LLM_MODEL_GRAPH", None)
        summary = getattr(settings, "SM_LLM_MODEL_SUMMARY", None)
        routing = {
            "answer": answer,
            "summary": summary or aux,
            "graph": graph or aux,
            "aux": aux,
            "rewrite": aux,
            "translate": aux,
            "hyde": aux,
        }
        selected = routing.get(task) or aux or answer or default_model
        candidates = [selected, aux, answer, default_model]
        for candidate in candidates:
            if candidate and self._model_matches_provider(str(candidate), provider):
                return str(candidate)
        return str(default_model)

    @staticmethod
    def _split_csv(value: Optional[str]) -> List[str]:
        items: List[str] = []
        seen: set[str] = set()
        for raw in str(value or "").split(","):
            item = raw.strip().strip('"').strip("'")
            if not item or item in seen:
                continue
            seen.add(item)
            items.append(item)
        return items

    def _configured_models_for_provider(self, provider: str) -> List[str]:
        if provider == "openai":
            raw_candidates = self._split_csv(getattr(settings, "OPENAI_MODEL_CANDIDATES", ""))
            default_model = str(getattr(settings, "OPENAI_MODEL_NAME", "gpt-5.2") or "")
        elif provider == "dashscope":
            raw_candidates = self._split_csv(getattr(settings, "DASHSCOPE_MODEL_CANDIDATES", ""))
            default_model = str(getattr(settings, "DASHSCOPE_MODEL_NAME", "qwen-max-latest") or "")
        else:
            return []

        task_models = [
            getattr(settings, "SM_LLM_MODEL_ANSWER", None),
            getattr(settings, "SM_LLM_MODEL_AUX", None),
            getattr(settings, "SM_LLM_MODEL_GRAPH", None),
            getattr(settings, "SM_LLM_MODEL_SUMMARY", None),
        ]
        candidates = [
            default_model,
            self._resolve_task_model(self.task, provider),
            *raw_candidates,
            *[
                str(item).strip()
                for item in task_models
                if item and self._model_matches_provider(str(item), provider)
            ],
        ]
        result: List[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            model_name = str(candidate or "").strip()
            if not model_name or model_name in seen:
                continue
            seen.add(model_name)
            result.append(model_name)
        return result

    def _fallback_candidates(
        self,
        *,
        provider: Optional[str],
        model: Optional[str],
    ) -> List[tuple[str, Optional[str], Optional[str], str]]:
        primary_provider, primary_base_url, primary_api_key, primary_model = self._resolve_runtime_config(
            provider=provider,
            model=model,
        )
        ordered_providers = [primary_provider]
        inferred = self._infer_provider_from_model(model)
        if inferred and inferred not in ordered_providers:
            ordered_providers.append(inferred)
        for candidate_provider in ("openai", "dashscope"):
            if candidate_provider not in ordered_providers:
                ordered_providers.append(candidate_provider)

        candidates: List[tuple[str, Optional[str], Optional[str], str]] = []
        seen: set[tuple[str, str]] = set()
        for candidate_provider in ordered_providers:
            if candidate_provider == "local":
                key = ("local", primary_model)
                if key not in seen:
                    seen.add(key)
                    candidates.append(("local", None, None, primary_model))
                continue

            base_url, api_key = self._resolve_transport(candidate_provider)
            if not base_url or not api_key:
                continue
            model_names = []
            if candidate_provider == primary_provider:
                model_names.append(primary_model)
            model_names.extend(self._configured_models_for_provider(candidate_provider))
            for model_name in model_names:
                normalized_model = str(model_name or "").strip()
                if not normalized_model or not self._model_matches_provider(normalized_model, candidate_provider):
                    continue
                key = (candidate_provider, normalized_model)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((candidate_provider, base_url, api_key, normalized_model))

        return candidates or [self._resolve_runtime_config(provider=provider, model=model)]

    @staticmethod
    def _error_text(exc: BaseException) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                body = exc.response.text
            except Exception:
                body = ""
            return f"{exc.response.status_code} {body or exc}"
        return str(exc)

    @classmethod
    def _is_fallbackable_error(cls, exc: BaseException) -> bool:
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            text = cls._error_text(exc).lower()
            if status_code in {401, 403, 404, 429, 500, 502, 503, 504}:
                return True
            if status_code == 400:
                markers = (
                    "model",
                    "temperature",
                    "unsupported value",
                    "quota",
                    "rate limit",
                    "insufficient",
                    "billing",
                    "credit",
                    "balance",
                    "temporarily unavailable",
                )
                return any(marker in text for marker in markers)
        return False

    @staticmethod
    def _summarize_candidate_error(
        provider: str,
        model_name: str,
        exc: BaseException,
    ) -> str:
        text = LLMClient._error_text(exc)
        if len(text) > 240:
            text = text[:240] + "..."
        return f"{provider}/{model_name}: {text}"

    def generate(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 512,
        stream: bool = True,
        retries: int = 2,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Iterable[str] | str:
        self._last_usage = None
        if stream:
            return self._generate_stream(
                messages,
                temperature,
                max_tokens,
                retries,
                model=model,
                provider=provider,
            )
        return self._generate_once(
            messages,
            temperature,
            max_tokens,
            retries,
            model=model,
            provider=provider,
        )

    def get_last_usage(self) -> Dict[str, int] | None:
        """Return usage of the last generation request."""
        return dict(self._last_usage) if isinstance(self._last_usage, dict) else None

    def get_last_runtime_model(self) -> Dict[str, Any] | None:
        """Return the requested and actual model used by the last generation."""
        return dict(self._last_runtime_model) if isinstance(self._last_runtime_model, dict) else None

    # --- internals ---
    def _generate_stream(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: int,
        retries: int,
        *,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Generator[str, None, None]:
        errors: List[str] = []
        candidates = self._fallback_candidates(
            provider=provider,
            model=model,
        )
        requested_provider, _, _, requested_model = self._resolve_runtime_config(
            provider=provider,
            model=model,
        )
        for resolved_provider, base_url, api_key, model_name in candidates:
            answer_parts: List[str] = []
            try:
                if resolved_provider in ("dashscope", "openai"):
                    if not base_url or not api_key:
                        raise RuntimeError(f"{resolved_provider} API key not configured")
                    self._last_runtime_model = {
                        "requested_provider": requested_provider,
                        "requested_model": requested_model,
                        "actual_provider": resolved_provider,
                        "actual_model": model_name,
                        "fallback_applied": (
                            resolved_provider != requested_provider
                            or model_name != requested_model
                        ),
                    }
                    url = f"{base_url.rstrip('/')}/chat/completions"
                    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                    payload = {
                        "model": model_name,
                        "messages": messages,
                        "stream": True,
                    }
                    if self._supports_custom_temperature(model_name) or float(temperature) == 1.0:
                        payload["temperature"] = temperature
                    if self._uses_max_completion_tokens(model_name):
                        payload["max_completion_tokens"] = max_tokens
                    else:
                        payload["max_tokens"] = max_tokens
                    # 直连 SSE，逐行解析 data: {...}
                    with httpx.stream("POST", url, headers=headers, json=payload, timeout=self.timeout_secs) as resp:
                        resp.raise_for_status()
                        for line in resp.iter_lines():
                            if not line:
                                continue
                            if isinstance(line, bytes):
                                line = line.decode("utf-8", errors="ignore")
                            if line.startswith("data: "):
                                data = line[len("data: "):].strip()
                                if data == "[DONE]":
                                    break
                                try:
                                    obj = httpx.Response(200, text=data).json()
                                except Exception:
                                    answer_parts.append(data)
                                    yield data
                                    continue
                                # OpenAI/DashScope 兼容：delta.content 或 choices[0].message.content
                                choices = obj.get("choices") or []
                                if choices:
                                    c0 = choices[0]
                                    delta = c0.get("delta") or {}
                                    content = delta.get("content") or c0.get("message", {}).get("content")
                                    if content:
                                        answer_parts.append(str(content))
                                        yield content
                                usage = self._coerce_usage(obj.get("usage"))
                                if usage:
                                    self._last_usage = usage
                    if self._last_usage is None:
                        self._last_usage = self._estimate_usage(messages=messages, answer="".join(answer_parts))
                    if errors:
                        logger.warning("LLM stream fallback succeeded with %s/%s", resolved_provider, model_name)
                    return
                # 本地占位：仅用于无 Key 的开发场景
                self._last_runtime_model = {
                    "requested_provider": requested_provider,
                    "requested_model": requested_model,
                    "actual_provider": resolved_provider,
                    "actual_model": model_name,
                    "fallback_applied": (
                        resolved_provider != requested_provider
                        or model_name != requested_model
                    ),
                }
                content = self._fake_completion(messages, temperature, max_tokens, model=model_name)
                self._last_usage = self._estimate_usage(messages=messages, answer=content)
                for part in content.split():
                    yield part + " "
                return
            except Exception as exc:
                if answer_parts or not self._is_fallbackable_error(exc):
                    raise
                errors.append(self._summarize_candidate_error(resolved_provider, model_name, exc))
                logger.warning(
                    "LLM stream candidate failed, trying fallback: %s/%s",
                    resolved_provider,
                    model_name,
                    exc_info=True,
                )
                continue
        raise RuntimeError("All LLM fallback candidates failed: " + " | ".join(errors))

    def _generate_once(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: int,
        retries: int,
        *,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> str:
        errors: List[str] = []
        candidates = self._fallback_candidates(
            provider=provider,
            model=model,
        )
        requested_provider, _, _, requested_model = self._resolve_runtime_config(
            provider=provider,
            model=model,
        )
        for resolved_provider, base_url, api_key, model_name in candidates:
            try:
                if resolved_provider in ("dashscope", "openai"):
                    if not base_url or not api_key:
                        raise RuntimeError(f"{resolved_provider} API key not configured")
                    self._last_runtime_model = {
                        "requested_provider": requested_provider,
                        "requested_model": requested_model,
                        "actual_provider": resolved_provider,
                        "actual_model": model_name,
                        "fallback_applied": (
                            resolved_provider != requested_provider
                            or model_name != requested_model
                        ),
                    }
                    url = f"{base_url.rstrip('/')}/chat/completions"
                    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                    payload = {
                        "model": model_name,
                        "messages": messages,
                        "stream": False,
                    }
                    if self._supports_custom_temperature(model_name) or float(temperature) == 1.0:
                        payload["temperature"] = temperature
                    if self._uses_max_completion_tokens(model_name):
                        payload["max_completion_tokens"] = max_tokens
                    else:
                        payload["max_tokens"] = max_tokens
                    r = httpx.post(url, headers=headers, json=payload, timeout=self.timeout_secs)
                    r.raise_for_status()
                    obj = r.json()
                    usage = self._coerce_usage(obj.get("usage"))
                    if usage:
                        self._last_usage = usage
                    choices = obj.get("choices") or []
                    if choices:
                        c0 = choices[0]
                        text = c0.get("message", {}).get("content") or c0.get("text") or ""
                        if self._last_usage is None:
                            self._last_usage = self._estimate_usage(messages=messages, answer=str(text))
                        if errors:
                            logger.warning("LLM fallback succeeded with %s/%s", resolved_provider, model_name)
                        return text
                    self._last_usage = self._estimate_usage(messages=messages, answer="")
                    return ""
                # 占位本地实现
                self._last_runtime_model = {
                    "requested_provider": requested_provider,
                    "requested_model": requested_model,
                    "actual_provider": resolved_provider,
                    "actual_model": model_name,
                    "fallback_applied": (
                        resolved_provider != requested_provider
                        or model_name != requested_model
                    ),
                }
                text = self._fake_completion(messages, temperature, max_tokens, model=model_name)
                self._last_usage = self._estimate_usage(messages=messages, answer=text)
                return text
            except Exception as exc:
                if not self._is_fallbackable_error(exc):
                    raise
                errors.append(self._summarize_candidate_error(resolved_provider, model_name, exc))
                logger.warning(
                    "LLM candidate failed, trying fallback: %s/%s",
                    resolved_provider,
                    model_name,
                    exc_info=True,
                )
                continue
        raise RuntimeError("All LLM fallback candidates failed: " + " | ".join(errors))

    def _fake_completion(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: int,
        model: Optional[str] = None,
    ) -> str:
        # Deterministic placeholder for now
        # Concatenate last user message with a fixed acknowledgement
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        raw_q = (last_user or {}).get("content", "")
        if isinstance(raw_q, list):
            q = " ".join(
                str(item.get("text") or "")
                for item in raw_q
                if isinstance(item, dict) and str(item.get("type") or "").strip().lower() == "text"
            )
        else:
            q = str(raw_q or "")
        model_name = str(model or self.model)
        return f"[model={model_name}] Answer based on context. Q: {q[:128]}"

    def _coerce_usage(self, usage: Any) -> Dict[str, int] | None:
        if not isinstance(usage, dict):
            return None
        try:
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(
                usage.get("total_tokens")
                or (prompt_tokens + completion_tokens)
            )
            if prompt_tokens < 0 or completion_tokens < 0 or total_tokens < 0:
                return None
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }
        except Exception:
            return None

    def _estimate_usage(self, *, messages: List[Dict[str, Any]], answer: str) -> Dict[str, int]:
        prompt_chars = sum(self._content_chars(item.get("content")) for item in (messages or []))
        completion_chars = len(answer or "")
        # 统一沿用「中文≈1 token/字，英文≈4 chars/token」的保守估算
        prompt_tokens = max(prompt_chars // 2, 1) if prompt_chars else 0
        completion_tokens = max(completion_chars // 2, 1) if completion_chars else 0
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def _content_chars(self, content: Any) -> int:
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            total = 0
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type") or "").strip().lower()
                if part_type == "text":
                    total += len(str(part.get("text") or ""))
                elif part_type == "image_url":
                    # 图片不计入文本 token 估算，避免 base64 夸大
                    continue
            return total
        return len(str(content or ""))
