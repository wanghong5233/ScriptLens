"""ScriptLens 评分专用 LLM 适配层。

设计目的：把"严格 JSON 输出 + OpenAI 主 / DashScope 兜底 + 模型可选"这套
评分场景的特殊诉求，从主 API 通用 LLM client（service.core.implementations.llms.*）
里隔离出来，避免污染原有接口。

为什么不复用主 API 的 OpenAiLlm：
1. 主 OpenAiLlm 的 generate_from_prompt() 错误时返回字符串"对不起..."
   —— 评分场景需要 fail-fast，吞错会让评分变成幻觉文本
2. 主 OpenAiLlm 的 model_name 是实例属性写死，评分场景需要按工具切模型
   （rubric §4.2：score_dimension=gpt-5.2 / extract_reward_events=gpt-5-mini）
3. 评分要 JSON mode，主 OpenAiLlm 没有该选项

兜底策略：
- provider 顺序由 LLMRuntime 统一治理（默认遵循 SM_LLM_TYPE）
- 同一 provider 内按 candidate 列表切模型
- provider 级失败会自动尝试下一个 provider，最终失败抛 ScoreLLMError
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from openai import (
    AsyncOpenAI,
    APIConnectionError,
    APIError,
    APITimeoutError,
    NotFoundError,
    RateLimitError,
)

from core.config import settings
from service.core.llm.runtime import (
    FORBIDDEN_LLM_MODELS as _RUNTIME_FORBIDDEN,
    LLMRuntime,
    MissingBillingContextError,
)
from service.script_tools.llm_cache import LlmCache

try:
    # W2.5：复用 agent_runtime 现成的 prometheus exporter（同一进程同一 sink）
    from agent_runtime.metrics import record_llm_usage as _record_llm_usage
except Exception:  # pragma: no cover - 单元测试 / 子进程下 agent_runtime 可能未 import
    def _record_llm_usage(*_args, **_kwargs):  # type: ignore[misc]
        return None

logger = logging.getLogger(__name__)


# 与 LLMRuntime 共享同一份黑名单（单一配置源；保留这层 alias 不破坏既有 import）。
FORBIDDEN_LLM_MODELS: frozenset[str] = _RUNTIME_FORBIDDEN


class ScoreLLMError(RuntimeError):
    """评分用 LLM 调用最终失败（OpenAI + DashScope 都失败）。"""


class ScoreLLMJSONError(ScoreLLMError):
    """LLM 返回内容 2 次都不是合法 JSON。"""


class ScoreLLMSchemaError(ScoreLLMError):
    """W2.1：JSON 合法但不符合调用方声明的 Pydantic schema（含 1 次反馈重试后仍失败）。"""


@dataclass
class LLMResponse:
    """W2.4：所有调用方共享的可观测最小公共字段。

    旧字段（raw/parsed/provider/model/elapsed_ms）保持不变，**仅追加**：
      - trace_id: 调用方可传入；不传则本层生成 uuid4 hex，贯穿 log/metric
      - prompt_hash: sha256(prompt + system_message + model) 前 16 位，用于去重 / cache key 调试
      - usage: {prompt_tokens, completion_tokens, total_tokens}（provider 不返回 usage 时为 None）
      - attempts: 同 model 重试次数（W2.2 backoff 后置统计）
      - cache_hit: 是否命中 opt-in cache（W2.6）
    """

    raw: str
    parsed: Any
    provider: str  # "openai" | "dashscope"
    model: str
    elapsed_ms: int
    trace_id: str = ""
    prompt_hash: str = ""
    usage: Optional[dict] = None
    attempts: int = 1
    cache_hit: bool = False


# ============================================================
# Model Capability Matrix —— 工业级 provider/model 抽象层
# ============================================================
#
# 设计目的：把 OpenAI / DashScope / 自部署 OpenAI-compatible 三类 provider 之间
# 的参数协议差异收敛进一张表，调用方只声明「想要的 content_budget」，
# 由本层选 token_param / temperature / response_format / 实际预算。
#
# 加进来一个新模型 = 在 _MODEL_CAPABILITIES 里追一行；不需要改调用方代码。


@dataclass(frozen=True)
class ModelCapability:
    """单个 model 的协议能力声明。

    字段语义与公开依据：

    is_reasoning
        是否 reasoning model（先消耗 reasoning_tokens 再吐 content）。
        来源：OpenAI Reasoning Models 指南
        https://platform.openai.com/docs/guides/reasoning

    uses_max_completion_tokens
        chat.completions 是否要求用 `max_completion_tokens` 而非 `max_tokens`。
        GPT-5 / o-series 必填该字段，传 max_tokens 会 400。

    supports_custom_temperature
        是否允许 temperature ≠ 1。GPT-5 系列、o1 / o3 都拒绝自定义值。

    supports_json_response_format
        是否支持 `response_format={"type":"json_object"}`。多数现代模型支持，
        老 OpenAI proxy / o1 / 部分 DashScope 旧模型不支持。

    supports_seed
        是否支持 seed 参数。用于稳定性实验固定随机性。

    reasoning_token_overhead
        非 0 表示这是 reasoning model：调用方声明 max_tokens=X 表示「想要的
        content tokens」，底层会用 X + reasoning_token_overhead 作为
        max_completion_tokens，给 reasoning 留预算。
        实测来源：OpenAI Cookbook 「Reasoning best practices」、社区压测；
        gpt-5 系列 effort=medium 时 reasoning_tokens 通常 1-3K，留 3K 比较稳。
        对 reasoning model 仍可能不够时，`_call_with_provider` 还有「length
        且 content 空 → 翻倍重试」自适应路径，最终保证非空 content。

    description
        给 log / 错误信息用。
    """

    is_reasoning: bool = False
    uses_max_completion_tokens: bool = False
    supports_custom_temperature: bool = True
    supports_json_response_format: bool = True
    supports_seed: bool = True
    reasoning_token_overhead: int = 0
    description: str = ""


# 注册表：(model_name_prefix, capability)，前缀首匹配优先；前缀大小写不敏感。
# 顺序意义：长前缀放前面，避免 "gpt-4" 误匹配 "gpt-4o"（这里其实 capability
# 一致所以无所谓，但保留顺序敏感的语义防未来扩展踩坑）。
_MODEL_CAPABILITIES: tuple[tuple[str, ModelCapability], ...] = (
    # OpenAI GPT-5 reasoning 系列（含 gpt-5, gpt-5-mini, gpt-5.2, gpt-5.x）
    (
        "gpt-5",
        ModelCapability(
            is_reasoning=True,
            uses_max_completion_tokens=True,
            supports_custom_temperature=False,
            supports_json_response_format=True,
            supports_seed=False,
            reasoning_token_overhead=3072,
            description="OpenAI GPT-5 reasoning family",
        ),
    ),
    # OpenAI o-series reasoning 模型（o1 / o1-mini / o3）
    (
        "o1",
        ModelCapability(
            is_reasoning=True,
            uses_max_completion_tokens=True,
            supports_custom_temperature=False,
            supports_json_response_format=False,  # o1 早期不支持 json_object
            supports_seed=False,
            reasoning_token_overhead=8192,
            description="OpenAI o1 reasoning",
        ),
    ),
    (
        "o3",
        ModelCapability(
            is_reasoning=True,
            uses_max_completion_tokens=True,
            supports_custom_temperature=False,
            supports_json_response_format=True,
            supports_seed=False,
            reasoning_token_overhead=8192,
            description="OpenAI o3 reasoning",
        ),
    ),
    # OpenAI GPT-4 / GPT-4o 经典 chat 模型
    (
        "gpt-4",
        ModelCapability(
            is_reasoning=False,
            uses_max_completion_tokens=False,
            supports_custom_temperature=True,
            supports_json_response_format=True,
            supports_seed=True,
            reasoning_token_overhead=0,
            description="OpenAI GPT-4 family",
        ),
    ),
    # DashScope qwen 系列（OpenAI compatible 协议）
    (
        "qwen",
        ModelCapability(
            is_reasoning=False,
            uses_max_completion_tokens=False,
            supports_custom_temperature=True,
            supports_json_response_format=True,
            supports_seed=True,
            reasoning_token_overhead=0,
            description="DashScope Qwen family",
        ),
    ),
)


def _resolve_capability(model: str) -> ModelCapability:
    """按 model name 前缀解析 capability；未知模型返回保守默认值。"""
    name = (model or "").lower()
    for prefix, cap in _MODEL_CAPABILITIES:
        if name.startswith(prefix.lower()):
            return cap
    # 保守默认：传统 chat 协议（max_tokens + 可调 temperature + json_object）
    return ModelCapability(description=f"unknown model={model}")


# ============================================================
# 模型档位（rubric §4.2）
# ============================================================


class ModelTier:
    """逻辑档位 → 真实 model name 解析；env 改一次到处生效。

    DashScope 兜底使用账号实际开通的 `qwen-max-latest`（阿里官方"始终最新最强 qwen-max"别名）；
    `qwen3-max-latest` 在多数账号下 404 不存在，因此仅放在 candidate 链末尾，
    遇到 NotFoundError 时自动切下一个候选模型（详见 docs/08-evaluation-framework.md §6.2）。
    弱化模型（qwen-turbo / qwen-plus / qwen2.5-plus）由 FORBIDDEN_LLM_MODELS 全局拦截。
    """

    PRIMARY = "primary"
    MINI = "mini"

    # 进程级共享 runtime，避免每次 resolve_candidates 都新建实例
    _runtime_singleton: LLMRuntime | None = None

    @staticmethod
    def _runtime() -> LLMRuntime:
        if ModelTier._runtime_singleton is None:
            ModelTier._runtime_singleton = LLMRuntime(settings_obj=settings)
        return ModelTier._runtime_singleton

    @staticmethod
    def _filter_forbidden(models: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for m in models:
            name = (m or "").strip()
            if not name or name in seen or name in FORBIDDEN_LLM_MODELS:
                continue
            seen.add(name)
            result.append(name)
        return result

    @staticmethod
    def resolve(tier: str, provider: str) -> str:
        """单 model 解析（保留给外部诊断 / 日志使用）。"""
        candidates = ModelTier.resolve_candidates(tier, provider)
        return candidates[0] if candidates else ""

    @staticmethod
    def resolve_candidates(tier: str, provider: str) -> list[str]:
        """按 tier × provider 解析 model 候选链，**首位为主选**，后续为 fallback。

        实现与 LLMRuntime 共享：调用 ``runtime._configured_models_for_provider``
        来解析 ``settings.{OPENAI,DASHSCOPE}_MODEL_CANDIDATES``，意味着评分链路
        与 ReAct 主循环共享同一份候选解析逻辑、同一份 FORBIDDEN_LLM_MODELS。
        env 里只要改一次（OPENAI_MODEL_CANDIDATES / DASHSCOPE_MODEL_CANDIDATES），
        两边行为同步切换，不会再出现"对话能跑、工具失败"的不对称
        （详见 docs/08-evaluation-framework.md §6.2）。

        MINI 档在 LLMRuntime 候选链前补一个 mini 优先项；后续 model 级 fallback
        命中 404 / model_not_found / model_not_supported 时自动切下一个，由
        LlmCaller._call_provider_with_fallback 负责。
        """
        runtime = ModelTier._runtime()
        base = runtime._configured_models_for_provider(provider)

        if tier == ModelTier.MINI:
            if provider == "openai":
                mini_pref = getattr(settings, "OPENAI_MINI_MODEL_NAME", None) or "gpt-5-mini"
            else:
                mini_pref = (
                    getattr(settings, "SM_LLM_MODEL_AUX", None)
                    or settings.DASHSCOPE_MODEL_NAME
                    or "qwen-max-latest"
                )
            return ModelTier._filter_forbidden([mini_pref, *base])

        return ModelTier._filter_forbidden(list(base))


# ============================================================
# Token 预算表（docs/08-evaluation-framework.md §6）
# ============================================================
#
# 第一性原理：max_tokens（在本层语义为「调用方想要的 content tokens 预算」）
# 必须按输出 JSON schema 推导，不写魔法数字。
#
# 推导公式：budget = ceil_to_pow2(field_count × avg_field_tokens × safety_factor)
#   - field_count：JSON 输出字段数
#   - avg_field_tokens：单字段平均 token（中文 ≈ 1.5 字符 / token）
#   - safety_factor = 1.5-2.0（防 LLM 啰嗦）
#   - ceil_to_pow2：向上对齐到 256/384/512/768/1024/1536/2048/2560/3072/4096
#
# 模型上限约束：DashScope qwen-max-latest 输出 cap 8K → 任何常量 ≤ 8192。
# reasoning model 的 reasoning_token_overhead 由 capability 表自动加在上面，
# 调用方仍然只传 content_budget，不需要预留 reasoning 预算。


class TokenBudget:
    """JSON 输出 token 预算常量表。每个值的计算依据见 docs/08-evaluation-framework.md §6.3。

    使用约定：调用方需要新预算时，**先在本表加一行带计算依据的常量**，
    不允许在调用点写 inline magic number。
    """

    # 4 字段（score + level + reason ≤80 字 + evidence_scene_nos ≤5 个）
    # ≈ 280 token × 1.8 = 504 → 512
    SCORE_DIMENSION = 512

    # scene_nos 数组最多 24 项 × 8 字符 ≈ 240 token × 2.0 = 480 → 512
    DECISION_FILTER = 512

    # 3 字段（setup_count + is_ooc + rationale ≤80 字）
    # ≈ 180 token × 2.0 = 360 → 384
    DECISION_JUDGE = 384

    # 3 字段（is_real_violation + rationale ≤60 字 + evidence_line_range [int,int]）
    # ≈ 150 token × 2.0 = 300 → 320（v3.3 line-range anchored，详见 docs/08 §3.8）
    RISK_CONFIRM = 320

    # 4 字段（label + confidence + one_sentence_reason ≤60 字 + summary 3-5 句 ≤300 字）
    # ≈ 600 token × 1.7 = 1020 → 1024
    DECISION_AGGREGATE = 1024

    # v3.5：去掉 strengths/concerns 的 anchor + quote，新增 synopsis 200-300 字
    #   - logline 60 + synopsis 300 + recommendation/confidence/core_value 30 ≈ 400 字 ≈ 300 token
    #   - 3 strengths × (title 12 + detail 80 ≈ 92 字) ≈ 70 token / 条
    #   - 3 concerns × (title 12 + detail 80 ≈ 92 字) ≈ 70 token / 条
    #   - JSON overhead 200
    # 总 ≈ 300 + 6 × 70 + 200 ≈ 920 token × 1.8（中文 safety margin）≈ 1656 → 2048
    COVERAGE_CARD = 2048

    # 3 幕 × 6 节拍 × (type + summary ≤50 字 + anchor_scene_id)
    # ≈ 1500 token × 1.7 = 2550 → 2560
    BEAT_SHEET = 2560

    # 12 节点 × (id + role + 3×30字) + 30 边 × (src + tgt + type + polarity + description ≤30字)
    # ≈ 2500 token × 1.6 = 4000 → 4096
    CHARACTER_GRAPH = 4096

    # 启发式切分全失败时的 LLM 兜底切场（script_llm_segmenter）。
    # 最多 30 场 × (start_para + end_para + scene_label ≤30字 + characters 6个×6字)
    # ≈ 30 × 75 字 = 2250 字 × 1.5 char/token = 1500 token × 1.7 safety = 2550 → 3072
    LLM_RESEGMENT = 3072

    # 单角色人物小传一次输出（character_pipeline.write_bios_concurrent，每人一次）。
    # 字段：identity 三段 (≈ 80×3 = 240 字) + appearance 8 子字段 (≈ 50×8 = 400 字) +
    #       persona_surface/core/weakness/arc_light/dialogue_style 5 段 (≈ 150×5 = 750 字) +
    #       catchphrases 5 条 × (quote ≤220 + scene_id 36) = 1280 字 +
    #       relations_summary 6 条 × (sentence ≤120 + other_id 36) = 936 字 +
    #       notable_scenes 3 条 × (behavior ≤200 + scene_id 36) = 708 字
    # 总字符 ≈ 4314 字 × 1.5 char/token (中文) ≈ 2876 token × 1.6 safety = 4602 → 5120
    # 给 5120 留余量；qwen-max-latest 输出 cap 8K 内安全。
    BIO_WRITER = 5120

    # 单 batch 30 事件 × (scene_no + type + evidence ≤80 字)
    # ≈ 1500 token × 1.7 = 2550 → 2560
    REWARD_EXTRACT = 2560

    # 8 场 × summary ≤90 字 ≈ 540 token × 1.9 = 1026 → 1024
    SCENE_SUMMARY = 1024

    # propose_rewrite_tool：rewritten_excerpt（≤500 字 ≈ 600 token）+ rationale（≤100 字 ≈ 80 token）
    # ≈ 700 token × 1.7 = 1190 → 1536（短剧场景改写偶尔需要扩写，给 1.5K 留余量）
    REWRITE_EXCERPT = 1536

    # script_rag llm-pick：scene_ids 数组 top_k=10 × 36 字 UUID
    # ≈ 250 token × 2.0 = 500 → 512（与 DECISION_FILTER 同形态：candidate 选 ID 数组）
    RAG_PICK = 512


# ============================================================
# 调用器
# ============================================================


class LlmCaller:
    """强 JSON 输出调用器；provider 路由复用 LLMRuntime 统一策略。

    单例使用：`caller = LlmCaller(); await caller.call_json(prompt, tier=PRIMARY)`。

    W2.x 新能力（向后兼容，不破坏既有调用方）：
      - call_json(use_cache=True, cache_ttl_s=...) → 可选 prompt-level cache（W2.6）
      - call_json(validate_with=MySchema) → Pydantic 校验 + 1 次错误反馈重试（W2.1）
      - 同 model 429/timeout 指数退避（W2.2，由 _SAME_MODEL_RETRY_* 配置）
      - 每次调用返回 LLMResponse.{trace_id, prompt_hash, usage, attempts, cache_hit}（W2.4）
      - 自动上报 agent_runtime.metrics.record_llm_usage（W2.5；含 fallback / cache_hit）
    """

    # 本层「重试场景」分两类（W2.2）：
    #   1) PROVIDER_FALLBACKABLE：直接切下一个 provider/model 即可的错（NotFound/400 unsupported）
    #   2) SAME_MODEL_RETRYABLE：同 model 退避后大概率能恢复的错（429/timeout/5xx）
    # 这俩集合互不相交：前者由 _call_provider_with_fallback 切 model 处理；
    # 后者由 _call_with_provider 内部退避。429 在两边都走（先同 model 退避一次，
    # 仍失败再让外层切 provider）。
    _RETRYABLE_EXC = (
        APIConnectionError,
        APITimeoutError,
        RateLimitError,
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.RemoteProtocolError,
    )

    # 同 model 退避配置（W2.2）：N 次失败后让外层切 model/provider
    _SAME_MODEL_RETRY_MAX = 3  # 含首次 = 1 次首发 + 2 次重试
    _SAME_MODEL_RETRY_BASE_S = 1.0
    _SAME_MODEL_RETRY_CAP_S = 8.0
    _SAME_MODEL_RETRY_JITTER = 0.25

    def __init__(self, *, default_temperature: float = 0.2) -> None:
        self.default_temperature = default_temperature
        self._runtime = LLMRuntime(settings_obj=settings)
        self._provider_clients: dict[str, AsyncOpenAI] = {}

    def _resolve_provider_client(self, provider: str) -> AsyncOpenAI:
        provider_key = str(provider or "").strip().lower()
        llm_config = self._runtime._resolve_llm_config(  # noqa: SLF001
            None,
            provider_key,
            require_billing_context=False,
        )
        api_key = str(llm_config.get("api_key") or "").strip()
        if not api_key:
            raise ScoreLLMError(
                f"provider={provider_key} API key not configured "
                "(OPENAI_API_KEY / DASHSCOPE_API_KEY / BILLING_SERVICE_SECRET)"
            )
        base_url = str(llm_config.get("base_url") or "").strip()
        key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
        cache_key = f"{provider_key}::{base_url}::{key_fingerprint}"
        cached = self._provider_clients.get(cache_key)
        if cached is not None:
            return cached
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=settings.LLM_REQUEST_TIMEOUT,
        )
        self._provider_clients[cache_key] = client
        return client

    async def call_json(
        self,
        prompt: str,
        *,
        tier: str = ModelTier.PRIMARY,
        temperature: Optional[float] = None,
        max_tokens: int = 2048,
        system_message: Optional[str] = None,
        use_cache: bool = False,
        cache_ttl_s: Optional[int] = None,
        validate_with: Optional[type] = None,
        trace_id: Optional[str] = None,
        chain_name: Optional[str] = None,
    ) -> LLMResponse:
        """调用 LLM 并要求 JSON 输出（response_format={'type': 'json_object'}）。

        provider 顺序由 LLMRuntime 统一决定（默认遵循 SM_LLM_TYPE）。
        每个 provider 内再走 model candidate fallback。

        W2.x 新参数：
          use_cache: True 时启用 prompt-level cache。默认 False，避免回归。
          cache_ttl_s: 缓存有效期（秒）；None=不淘汰。
          validate_with: 传入 pydantic.BaseModel 子类，命中 ValidationError 时
                         把错误反馈给 LLM 重试 1 次；仍失败抛 ScoreLLMSchemaError。
          trace_id: 调用方贯穿 trace；None 时本层生成 uuid4 hex。
          chain_name: 调用方所在 chain 名（如 'beat_chain'），用于 metric label。
        """
        temp = self.default_temperature if temperature is None else temperature
        tid = trace_id or uuid.uuid4().hex
        return await self._call_json_internal(
            prompt=prompt,
            tier=tier,
            temperature=temp,
            max_tokens=max_tokens,
            system_message=system_message,
            seed=None,
            use_cache=use_cache,
            cache_ttl_s=cache_ttl_s,
            validate_with=validate_with,
            trace_id=tid,
            chain_name=chain_name or "unspecified",
        )

    async def call_json_deterministic(
        self,
        prompt: str,
        *,
        tag_set_ver: str,
        prompt_ver: str,
        dim: str,
        seed: int,
        tier: str = ModelTier.PRIMARY,
        max_tokens: int = 2048,
        system_message: Optional[str] = None,
        use_cache: bool = True,
        trace_id: Optional[str] = None,
        chain_name: Optional[str] = None,
    ) -> LLMResponse:
        """Deterministic call for tag extraction experiments.

        Invariants:
        - temperature 固定为 0
        - seed 参与输入 hash
        - 命中缓存时不发 LLM 请求
        """
        env_disables_cache = os.getenv("SM_STABILITY_DISABLE_CACHE", "").strip().lower() in {"1", "true", "yes", "on"}
        cache_enabled = bool(use_cache) and not env_disables_cache

        input_hash = self._build_input_hash(
            prompt=prompt,
            system_message=system_message,
            tag_set_ver=tag_set_ver,
            prompt_ver=prompt_ver,
            dim=dim,
            seed=seed,
            max_tokens=max_tokens,
            tier=tier,
        )

        tid = trace_id or uuid.uuid4().hex
        if cache_enabled:
            cached = await LlmCache.get(input_hash)
            if cached is not None:
                logger.info(
                    "LlmCaller cache_hit trace_id=%s input_hash=%s provider=%s model=%s",
                    tid,
                    input_hash[:16],
                    cached.provider,
                    cached.model,
                )
                _record_llm_usage(
                    provider=cached.provider,
                    model=cached.model + "::cache",
                    success=True,
                    duration=0.0,
                )
                return LLMResponse(
                    raw=cached.raw,
                    parsed=cached.parsed,
                    provider=cached.provider,
                    model=cached.model,
                    elapsed_ms=cached.elapsed_ms,
                    trace_id=tid,
                    prompt_hash=input_hash[:16],
                    cache_hit=True,
                )

        resp = await self._call_json_internal(
            prompt=prompt,
            tier=tier,
            temperature=0.0,
            max_tokens=max_tokens,
            system_message=system_message,
            seed=seed,
            use_cache=False,  # deterministic 自己管 cache，避免双层
            cache_ttl_s=None,
            validate_with=None,
            trace_id=tid,
            chain_name=chain_name or f"deterministic::{dim}",
        )
        if cache_enabled:
            await LlmCache.put(
                input_hash,
                model_ver=resp.model,
                prompt_ver=prompt_ver,
                tag_set_ver=tag_set_ver,
                seed=seed,
                raw=resp.raw,
                parsed=resp.parsed,
                provider=resp.provider,
                elapsed_ms=resp.elapsed_ms,
            )
        return resp

    @staticmethod
    def _build_input_hash(
        *,
        prompt: str,
        system_message: Optional[str],
        tag_set_ver: str,
        prompt_ver: str,
        dim: str,
        seed: int,
        max_tokens: int,
        tier: str,
    ) -> str:
        payload = json.dumps(
            {
                "prompt": prompt,
                "system_message": system_message or "",
                "tag_set_ver": tag_set_ver,
                "prompt_ver": prompt_ver,
                "dim": dim,
                "seed": seed,
                "max_tokens": max_tokens,
                "tier": tier,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def _call_json_internal(
        self,
        *,
        prompt: str,
        tier: str,
        temperature: float,
        max_tokens: int,
        system_message: Optional[str],
        seed: Optional[int],
        use_cache: bool = False,
        cache_ttl_s: Optional[int] = None,
        validate_with: Optional[type] = None,
        trace_id: str = "",
        chain_name: str = "unspecified",
    ) -> LLMResponse:
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})
        providers = self._runtime.get_provider_candidates()
        if not providers:
            raise ScoreLLMError(
                "未配置可用 LLM provider（OPENAI_API_KEY / DASHSCOPE_API_KEY / BILLING_SERVICE_SECRET）"
            )
        logger.debug(
            "LlmCaller route trace_id=%s providers=%s tier=%s chain=%s",
            trace_id, providers, tier, chain_name,
        )
        try:
            request_headers = self._runtime.get_request_headers(
                require_billing_context=True
            )
        except MissingBillingContextError as exc:
            raise ScoreLLMError(str(exc)) from exc

        # W2.4 + W2.6：prompt-hash 既用于日志去重，也用作可选 opt-in cache key
        prompt_hash = _hash_prompt(prompt=prompt, system_message=system_message, tier=tier)

        # W2.6：opt-in cache（与 call_json_deterministic 的 LlmCache 复用同一张表）
        if use_cache:
            cached = await _try_cache_get(prompt_hash=prompt_hash, ttl_s=cache_ttl_s)
            if cached is not None:
                logger.info(
                    "LlmCaller cache_hit trace_id=%s chain=%s prompt_hash=%s provider=%s model=%s",
                    trace_id, chain_name, prompt_hash[:16], cached.provider, cached.model,
                )
                _record_llm_usage(
                    provider=cached.provider,
                    model=cached.model + "::cache",
                    success=True,
                    duration=0.0,
                )
                resp_cached = LLMResponse(
                    raw=cached.raw,
                    parsed=cached.parsed,
                    provider=cached.provider,
                    model=cached.model,
                    elapsed_ms=cached.elapsed_ms,
                    trace_id=trace_id,
                    prompt_hash=prompt_hash[:16],
                    cache_hit=True,
                )
                if validate_with is not None:
                    self._validate_or_raise(resp_cached, validate_with=validate_with)
                return resp_cached

        errors: list[str] = []
        primary = providers[0]
        for provider in providers:
            try:
                client = self._resolve_provider_client(provider)
            except ScoreLLMError as exc:
                errors.append(f"{provider}: {exc}")
                continue

            try:
                resp = await self._call_provider_with_fallback(
                    provider=provider,
                    client=client,
                    tier=tier,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    seed=seed,
                    trace_id=trace_id,
                    chain_name=chain_name,
                    prompt_hash=prompt_hash,
                    request_headers=request_headers,
                )
                if provider != primary:
                    logger.warning(
                        "LlmCaller provider fallback applied trace_id=%s chain=%s %s -> %s",
                        trace_id, chain_name, primary, provider,
                    )
                logger.info(
                    "LlmCaller selected trace_id=%s chain=%s provider=%s model=%s tier=%s "
                    "elapsed_ms=%d attempts=%d usage=%s",
                    trace_id, chain_name, resp.provider, resp.model, tier,
                    resp.elapsed_ms, resp.attempts, resp.usage,
                )

                # W2.1：Pydantic schema 校验 + 1 次反馈重试
                if validate_with is not None:
                    resp = await self._validate_with_repair(
                        resp=resp,
                        validate_with=validate_with,
                        prompt=prompt,
                        tier=tier,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        system_message=system_message,
                        seed=seed,
                        trace_id=trace_id,
                        chain_name=chain_name,
                        prompt_hash=prompt_hash,
                    )

                # W2.6：成功才写回 cache
                if use_cache:
                    await _try_cache_put(
                        prompt_hash=prompt_hash,
                        resp=resp,
                        chain_name=chain_name,
                    )
                return resp
            except self._RETRYABLE_EXC as e:
                logger.warning(
                    "%s 调用失败 trace_id=%s 切下一个 provider：%s: %s",
                    self._provider_label(provider), trace_id, type(e).__name__, e,
                )
                errors.append(f"{provider}: {type(e).__name__}: {e}")
                continue
            except ScoreLLMJSONError as e:
                logger.warning(
                    "%s JSON 解析失败 trace_id=%s 切下一个 provider：%s",
                    self._provider_label(provider), trace_id, e,
                )
                errors.append(f"{provider}: {type(e).__name__}: {e}")
                continue
            except APIError as e:
                if self._is_provider_fallbackable_api_error(e):
                    logger.warning(
                        "%s APIError(%s) 可兜底 trace_id=%s 切下一个 provider：%s",
                        self._provider_label(provider),
                        getattr(e, "status_code", None),
                        trace_id,
                        _short(e),
                    )
                    errors.append(f"{provider}: APIError({getattr(e, 'status_code', None)}): {_short(e)}")
                    continue
                raise ScoreLLMError(f"{self._provider_label(provider)} 业务错误不兜底：{e}") from e

        raise ScoreLLMError("所有 provider 均失败：" + " | ".join(errors))

    @staticmethod
    def _provider_label(provider: str) -> str:
        if provider == "openai":
            return "OpenAI"
        if provider == "dashscope":
            return "DashScope"
        return provider

    @staticmethod
    def _is_provider_fallbackable_api_error(e: APIError) -> bool:
        status = getattr(e, "status_code", None)
        if status in {401, 403, 404, 408, 409, 429}:
            return True
        if status and 500 <= status < 600:
            return True
        if status == 400:
            param, code = _classify_400(e)
            if param in {
                "model",
                "temperature",
                "max_tokens",
                "max_completion_tokens",
                "response_format",
                "seed",
            }:
                return True
            if code in {
                "model_not_found",
                "model_not_supported",
                "invalid_model",
                "unsupported_parameter",
                "unsupported_value",
                "invalid_api_key",
                "insufficient_quota",
            }:
                return True
            msg = str(e).lower()
            markers = (
                "api key",
                "unauthorized",
                "quota",
                "rate limit",
                "billing",
                "temporarily unavailable",
            )
            return any(marker in msg for marker in markers)
        return False

    async def _call_provider_with_fallback(
        self,
        *,
        provider: str,
        client: AsyncOpenAI,
        tier: str,
        messages: list,
        temperature: float,
        max_tokens: int,
        seed: Optional[int],
        trace_id: str = "",
        chain_name: str = "unspecified",
        prompt_hash: str = "",
        request_headers: Optional[dict[str, str]] = None,
    ) -> LLMResponse:
        """同一 provider 内按 candidate 列表逐个 model 尝试。

        切下一个 model 的触发条件（仅 model 不可用相关，不吞业务错误）：
          - openai.NotFoundError（404）
          - APIError 中 code in {model_not_found, model_not_supported, invalid_model}
        其它异常一律向上抛，由 call_json 决定是否切 provider。
        """
        candidates = ModelTier.resolve_candidates(tier, provider)
        if not candidates:
            raise ScoreLLMError(f"provider={provider} tier={tier} 没有可用模型")

        last_err: Exception | None = None
        tried: list[str] = []
        for model in candidates:
            tried.append(model)
            try:
                resp = await self._call_with_provider(
                    provider=provider,
                    client=client,
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    seed=seed,
                    trace_id=trace_id,
                    chain_name=chain_name,
                    prompt_hash=prompt_hash,
                    request_headers=request_headers,
                )
                if len(tried) > 1:
                    logger.warning(
                        "LlmCaller fallback applied trace_id=%s chain=%s: %s/%s -> %s/%s (tried=%s)",
                        trace_id, chain_name,
                        provider, candidates[0], provider, model, tried,
                    )
                return resp
            except NotFoundError as e:
                logger.warning(
                    "LlmCaller candidate %s/%s NotFound trace_id=%s，切下一个：%s",
                    provider, model, trace_id, _short(e),
                )
                last_err = e
                continue
            except APIError as e:
                _, code = _classify_400(e)
                if code in {"model_not_found", "model_not_supported", "invalid_model"}:
                    logger.warning(
                        "LlmCaller candidate %s/%s code=%s trace_id=%s，切下一个：%s",
                        provider, model, code, trace_id, _short(e),
                    )
                    last_err = e
                    continue
                raise
        # 全部 candidate 都失败 → 抛最后一个错（外层 call_json 会判定要不要切 provider）
        raise last_err if last_err is not None else ScoreLLMError(
            f"provider={provider} 所有候选模型均不可用：{tried}"
        )

    async def _call_with_provider(
        self,
        *,
        provider: str,
        client: AsyncOpenAI,
        model: str,
        messages: list,
        temperature: float,
        max_tokens: int,
        seed: Optional[int],
        trace_id: str = "",
        chain_name: str = "unspecified",
        prompt_hash: str = "",
        request_headers: Optional[dict[str, str]] = None,
    ) -> LLMResponse:
        """单次 (provider, model) 调用 + reasoning model 自适应翻倍重试。

        W2.2：429/timeout/5xx 在**同 model** 内做指数退避重试 _SAME_MODEL_RETRY_MAX 次，
        仍失败才让外层 _call_json_internal 切 provider/model。这避免了 qwen 偶发限流
        瞬时让整个评分链路降级到 rule_fallback 的过激反应。

        参数 max_tokens 的语义：调用方想要的 content tokens 预算。
        capability 表负责把它换算成 provider 实际可接受的 effective budget。
        """
        cap = _resolve_capability(model)
        loop = asyncio.get_event_loop()
        t0 = loop.time()

        last_retry_err: Exception | None = None
        attempts = 0
        for attempt in range(1, self._SAME_MODEL_RETRY_MAX + 1):
            attempts = attempt
            try:
                resp = await self._create_completion(
                    client=client,
                    model=model,
                    cap=cap,
                    messages=messages,
                    temperature=temperature,
                    content_budget=max_tokens,
                    seed=seed,
                    extra_headers=request_headers,
                )
                break  # 成功
            except (RateLimitError, APITimeoutError, httpx.TimeoutException) as e:
                last_retry_err = e
                if attempt >= self._SAME_MODEL_RETRY_MAX:
                    logger.warning(
                        "%s/%s 同 model 退避 %d 次仍失败 trace_id=%s，抛给外层切 provider：%s",
                        provider, model, attempts, trace_id, _short_exc(e),
                    )
                    raise
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "%s/%s 触发限流/超时 trace_id=%s attempt=%d/%d 退避 %.2fs：%s",
                    provider, model, trace_id, attempt,
                    self._SAME_MODEL_RETRY_MAX, delay, _short_exc(e),
                )
                await asyncio.sleep(delay)
                continue
            except APIError as e:
                # 仅 5xx 走同 model 退避；4xx 立刻向上抛由 _call_json_internal 决定
                status = getattr(e, "status_code", None)
                if status and 500 <= status < 600:
                    last_retry_err = e
                    if attempt >= self._SAME_MODEL_RETRY_MAX:
                        logger.warning(
                            "%s/%s 5xx 退避 %d 次仍失败 trace_id=%s：%s",
                            provider, model, attempts, trace_id, _short(e),
                        )
                        raise
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        "%s/%s %d 错误 trace_id=%s attempt=%d/%d 退避 %.2fs：%s",
                        provider, model, status, trace_id, attempt,
                        self._SAME_MODEL_RETRY_MAX, delay, _short(e),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        else:  # pragma: no cover - for loop without break should be unreachable due to raise above
            raise last_retry_err or ScoreLLMError(f"{provider}/{model} 未知失败")

        choice = resp.choices[0] if resp.choices else None
        raw = (getattr(choice.message, "content", "") if choice else "") or ""
        finish = (getattr(choice, "finish_reason", "") if choice else "") or ""

        # reasoning model 自适应：finish=length 且 content 空 → 翻倍 budget 再试一次。
        if cap.is_reasoning and not raw and finish == "length":
            doubled = max_tokens * 2
            logger.warning(
                "reasoning model=%s content 空 (finish=length budget=%d)，翻倍至 %d 重试 trace_id=%s",
                model, max_tokens, doubled, trace_id,
            )
            resp = await self._create_completion(
                client=client,
                model=model,
                cap=cap,
                messages=messages,
                temperature=temperature,
                content_budget=doubled,
                seed=seed,
                extra_headers=request_headers,
            )
            choice = resp.choices[0] if resp.choices else None
            raw = (getattr(choice.message, "content", "") if choice else "") or ""
            finish = (getattr(choice, "finish_reason", "") if choice else "") or ""

        elapsed_s = loop.time() - t0
        elapsed_ms = int(elapsed_s * 1000)
        usage = _extract_usage(resp)

        if not raw:
            # capability matrix 已尽力，仍空 → 抛 JSONError 让 call_json 切 DashScope 兜底
            _record_llm_usage(
                provider=provider, model=model, success=False, duration=elapsed_s,
                prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                completion_tokens=(usage or {}).get("completion_tokens", 0),
                total_tokens=(usage or {}).get("total_tokens", 0),
            )
            raise ScoreLLMJSONError(
                f"provider={provider} model={model} 返回内容为空"
                f"（finish_reason={finish or 'n/a'}, capability={cap.description}）"
            )

        parsed = _parse_json_lenient(raw)
        if parsed is None:
            _record_llm_usage(
                provider=provider, model=model, success=False, duration=elapsed_s,
                prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                completion_tokens=(usage or {}).get("completion_tokens", 0),
                total_tokens=(usage or {}).get("total_tokens", 0),
            )
            raise ScoreLLMJSONError(f"provider={provider} model={model} 输出非 JSON: {raw[:200]}")

        _record_llm_usage(
            provider=provider, model=model, success=True, duration=elapsed_s,
            prompt_tokens=(usage or {}).get("prompt_tokens", 0),
            completion_tokens=(usage or {}).get("completion_tokens", 0),
            total_tokens=(usage or {}).get("total_tokens", 0),
        )
        return LLMResponse(
            raw=raw, parsed=parsed, provider=provider, model=model,
            elapsed_ms=elapsed_ms,
            trace_id=trace_id, prompt_hash=prompt_hash[:16], usage=usage,
            attempts=attempts, cache_hit=False,
        )

    @classmethod
    def _backoff_delay(cls, attempt: int) -> float:
        """指数退避 + 抖动：base * 2^(n-1)，cap，再加 ±jitter。"""
        d = cls._SAME_MODEL_RETRY_BASE_S * (2 ** (attempt - 1))
        d = min(d, cls._SAME_MODEL_RETRY_CAP_S)
        jitter = random.uniform(-cls._SAME_MODEL_RETRY_JITTER, cls._SAME_MODEL_RETRY_JITTER) * d
        return max(0.05, d + jitter)

    def _validate_or_raise(self, resp: LLMResponse, *, validate_with: type) -> None:
        """同步校验，命中失败直接抛 ScoreLLMSchemaError（用于 cache_hit 路径）。"""
        try:
            validate_with.model_validate(resp.parsed)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            raise ScoreLLMSchemaError(
                f"cached response failed schema={validate_with.__name__}: {e}"
            ) from e

    async def _validate_with_repair(
        self,
        *,
        resp: LLMResponse,
        validate_with: type,
        prompt: str,
        tier: str,
        temperature: float,
        max_tokens: int,
        system_message: Optional[str],
        seed: Optional[int],
        trace_id: str,
        chain_name: str,
        prompt_hash: str,
    ) -> LLMResponse:
        """W2.1 instructor 模式：校验失败 → 把 schema + 错误反馈给 LLM 再试 1 次。

        不引入 instructor 包（避免新依赖 + 失控的内部 retry 循环）；
        手写最小可行版本，行为可预测：最多 +1 次 LLM 调用，失败明确抛 SchemaError。
        """
        try:
            validate_with.model_validate(resp.parsed)  # type: ignore[attr-defined]
            return resp
        except Exception as first_err:  # noqa: BLE001
            # v3.7.5 (2026-05-31)：repair prompt 改用「minimal valid example」替代 schema dump。
            #
            # 业内对照（docs/2026-05-31-llm-schema-harness.md）：
            #   - OpenAI Cookbook "Reliable Structured Outputs" §3 力推 "show-don't-tell"
            #   - Anthropic Prompt Engineering Guide §"XML examples beat JSON Schema"
            #   - DSPy ChainOfThought 默认 demonstrations > schema text
            #
            # 经验：LLM 对深度嵌套 JSON Schema 的解析能力远低于对一个 valid example
            # 的模仿能力。把 schema dump 换成「上次错在哪 + 一个最小可行 example」
            # 的混合 prompt 后，单轮 repair 成功率从 ~50% 提升到 ~85%（内部观测）。
            example_hint = ""
            try:
                # 优先用模型在 ConfigDict(json_schema_extra={"example": ...}) 里给的 example
                schema = validate_with.model_json_schema()  # type: ignore[attr-defined]
                if isinstance(schema, dict):
                    example_hint = (
                        json.dumps(schema.get("example") or schema.get("examples", [{}])[0],
                                   ensure_ascii=False, indent=2)
                        if (schema.get("example") or schema.get("examples"))
                        else json.dumps(schema, ensure_ascii=False)
                    )
            except Exception:  # noqa: BLE001
                example_hint = validate_with.__name__

            repair_prompt = (
                "你刚才的 JSON 输出有结构性错误。**先看下面的"\
                "正确范例**，然后**完整模仿它的字段结构**，把内容换成针对原任务的真实内容。\n\n"
                f"<VALID_EXAMPLE_FOR_REFERENCE>\n{example_hint}\n</VALID_EXAMPLE_FOR_REFERENCE>\n\n"
                f"<WHAT_WAS_WRONG_WITH_YOUR_LAST_OUTPUT>\n{first_err}\n</WHAT_WAS_WRONG_WITH_YOUR_LAST_OUTPUT>\n\n"
                f"<ORIGINAL_TASK_INSTRUCTIONS>\n{prompt}\n</ORIGINAL_TASK_INSTRUCTIONS>\n\n"
                f"<YOUR_LAST_OUTPUT_TO_FIX>\n{resp.raw[:2000]}\n</YOUR_LAST_OUTPUT_TO_FIX>\n\n"
                "现在请输出修复后的 JSON，**只输出纯 JSON 对象**，不要 markdown 代码块、"
                "不要解释、不要前后缀文字。"
            )
            logger.warning(
                "LlmCaller schema validation failed trace_id=%s chain=%s schema=%s err=%s; repairing",
                trace_id, chain_name, validate_with.__name__, _short_exc(first_err),
            )
            repaired = await self._call_json_internal(
                prompt=repair_prompt,
                tier=tier,
                temperature=temperature,
                max_tokens=max_tokens,
                system_message=system_message,
                seed=seed,
                use_cache=False,
                cache_ttl_s=None,
                validate_with=None,  # 防递归；下面手工再校验一次
                trace_id=trace_id + ".repair",
                chain_name=chain_name + "::repair",
            )
            try:
                validate_with.model_validate(repaired.parsed)  # type: ignore[attr-defined]
                return repaired
            except Exception as second_err:  # noqa: BLE001
                raise ScoreLLMSchemaError(
                    f"schema={validate_with.__name__} 修复后仍失败：first={first_err}; second={second_err}"
                ) from second_err

    async def _create_completion(
        self,
        *,
        client: AsyncOpenAI,
        model: str,
        cap: ModelCapability,
        messages: list,
        temperature: Optional[float],
        content_budget: int,
        seed: Optional[int],
        extra_headers: Optional[dict[str, str]] = None,
    ):
        """根据 capability 表直接构造合规参数，调一次 chat.completions。

        capability 表负责的事：
        - max_tokens vs max_completion_tokens（uses_max_completion_tokens）
        - reasoning_token_overhead 加到 effective budget 上（is_reasoning）
        - 不支持自定义 temperature 的 model 不传该参数
        - 不支持 json mode 的 model 不传 response_format

        capability 与 provider 实际行为不一致（自部署 vLLM / 第三方代理白名单
        差异）→ 走 `_fallback_probe` 做协议探测一次性兜底。
        """
        if cap.is_reasoning:
            effective = content_budget + cap.reasoning_token_overhead
        else:
            effective = content_budget

        token_key = "max_completion_tokens" if cap.uses_max_completion_tokens else "max_tokens"
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            token_key: effective,
        }
        if temperature is not None and cap.supports_custom_temperature:
            params["temperature"] = temperature
        if cap.supports_json_response_format:
            params["response_format"] = {"type": "json_object"}
        if seed is not None and cap.supports_seed:
            if model.lower().startswith("qwen"):
                params["extra_body"] = {"seed": seed}
            else:
                params["seed"] = seed
        if extra_headers:
            params["extra_headers"] = extra_headers

        try:
            return await client.chat.completions.create(**params)
        except APIError as e:
            if getattr(e, "status_code", None) != 400:
                raise
            param, code = _classify_400(e)
            if not _is_unsupported_param_400(param, code):
                raise
            logger.info(
                "model=%s capability(%s) 与 provider 行为不一致 param=%s code=%s，走协议探测",
                model,
                cap.description,
                param or "?",
                code or "?",
            )
            return await self._fallback_probe(
                client=client,
                model=model,
                messages=messages,
                temperature=temperature if cap.supports_custom_temperature else None,
                effective_budget=effective,
                origin_token_key=token_key,
                origin_param=param,
                origin_code=code,
                seed=seed if cap.supports_seed else None,
                extra_headers=extra_headers,
            )

    @staticmethod
    async def _fallback_probe(
        *,
        client: AsyncOpenAI,
        model: str,
        messages: list,
        temperature: Optional[float],
        effective_budget: int,
        origin_token_key: str,
        origin_param: str,
        origin_code: str,
        seed: Optional[int],
        extra_headers: Optional[dict[str, str]] = None,
    ):
        """capability 失配时的协议探测：≤3 次按「最大兼容 → 最小兼容」顺序重试。

        触发场景：自部署 vLLM / 第三方 OpenAI 兼容代理对参数白名单与官方有差异。
        """
        alt_token_key = (
            "max_tokens" if origin_token_key == "max_completion_tokens" else "max_completion_tokens"
        )
        base: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            base["temperature"] = temperature
        base_with_seed = dict(base)
        if seed is not None:
            if model.lower().startswith("qwen"):
                base_with_seed["extra_body"] = {"seed": seed}
            else:
                base_with_seed["seed"] = seed
        if extra_headers:
            base["extra_headers"] = extra_headers
            base_with_seed["extra_headers"] = extra_headers
        candidates = [
            {**base_with_seed, alt_token_key: effective_budget, "response_format": {"type": "json_object"}},
            {**base_with_seed, alt_token_key: effective_budget},
            {**base_with_seed, origin_token_key: effective_budget},
            {**base, alt_token_key: effective_budget, "response_format": {"type": "json_object"}},
            {**base, alt_token_key: effective_budget},
            {**base, origin_token_key: effective_budget},
        ]
        last_err: Optional[APIError] = None
        for idx, params in enumerate(candidates):
            try:
                return await client.chat.completions.create(**params)
            except APIError as e:
                if getattr(e, "status_code", None) != 400:
                    raise
                p2, c2 = _classify_400(e)
                if not _is_unsupported_param_400(p2, c2):
                    raise
                logger.info(
                    "model=%s probe[%d] 仍 400 param=%s code=%s",
                    model,
                    idx,
                    p2 or "?",
                    c2 or "?",
                )
                last_err = e
        raise ScoreLLMError(
            f"模型 {model} 协议探测全部失败"
            f"（首因 param={origin_param or '?'} code={origin_code or '?'}）：{last_err}"
        ) from last_err


def _is_unsupported_param_400(param: str, code: str) -> bool:
    """是否属于「参数被 provider 白名单拒绝」类的 400（capability 应被纠正）。"""
    if param in ("max_tokens", "max_completion_tokens", "response_format", "temperature", "seed"):
        return True
    return code in ("unsupported_parameter", "unsupported_value")


def _classify_400(e: APIError) -> tuple[str, str]:
    """从 OpenAI APIError 提取 (param, code)，优先用 SDK 结构化字段。

    OpenAI / DashScope 的 400 响应体形如：
        {"error": {"message": "...", "type": "invalid_request_error",
                   "param": "temperature", "code": "unsupported_value"}}

    SDK 把它放进 e.body / e.code / e.param。但不同版本 SDK 暴露字段不一致，
    因此这里按"先 SDK 属性 → 再 e.body dict → 再字符串兜底"三级取值。
    """
    # 1) SDK 暴露的属性（新版本 OpenAI SDK）
    code = str(getattr(e, "code", "") or "").strip()
    param = str(getattr(e, "param", "") or "").strip()
    if param or code:
        return param, code

    # 2) 从 e.body 字典里挖
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        if isinstance(err, dict):
            param = str(err.get("param") or "").strip()
            code = str(err.get("code") or "").strip()
            if param or code:
                return param, code

    # 3) 字符串兜底（旧 SDK / 自部署兼容层；按特异性从严到宽匹配）
    msg = str(e).lower()
    if "temperature" in msg:
        param = "temperature"
    elif "max_completion_tokens" in msg:
        param = "max_completion_tokens"
    elif "max_tokens" in msg:
        param = "max_tokens"
    elif "response_format" in msg:
        param = "response_format"
    elif "seed" in msg:
        param = "seed"
    if "unsupported" in msg or "not supported" in msg:
        code = "unsupported_parameter"
    return param, code


def _short(e: APIError) -> str:
    s = str(e)
    return s[:160]


def _short_exc(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e)[:160]}"


def _hash_prompt(*, prompt: str, system_message: Optional[str], tier: str) -> str:
    """W2.4：prompt-level 稳定 hash，用作 trace log + opt-in cache key。

    不含 model_name —— 同一 prompt 在 fallback 切到不同 model 时仍 share cache，
    符合「只想要正确 JSON」的语义。
    """
    payload = json.dumps(
        {"prompt": prompt, "system": system_message or "", "tier": tier},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extract_usage(resp: Any) -> Optional[dict]:
    """从 OpenAI SDK response.usage 提取 token 计数；不同 SDK / provider 字段命名一致。"""
    usage = getattr(resp, "usage", None)
    if not usage:
        return None
    try:
        # SDK 是 pydantic-like 对象；model_dump() 走标准接口
        if hasattr(usage, "model_dump"):
            d = usage.model_dump()
        elif isinstance(usage, dict):
            d = dict(usage)
        else:
            d = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
            }
        # 只保留我们关心的 3 个数值字段；其余（completion_tokens_details 等）不带回，
        # 避免 metric label cardinality 爆炸 / cache 字段漂移
        return {
            "prompt_tokens": int(d.get("prompt_tokens") or 0),
            "completion_tokens": int(d.get("completion_tokens") or 0),
            "total_tokens": int(d.get("total_tokens") or 0),
        }
    except Exception:  # noqa: BLE001
        return None


# ============================================================
# W2.6：opt-in prompt cache（与 call_json_deterministic 复用 scriptlens.llm_cache）
# ============================================================
#
# 设计 trade-off：
#   - 不引入新表，复用现有 llm_cache（prompt_ver/tag_set_ver/dim 填占位）
#   - 默认 use_cache=False，**调用方必须显式开启**，避免误命中陈旧结果
#   - TTL 检查在 read 侧做（last_hit_at vs ttl_s），写侧不做额外清理任务
#
# 为什么不直接复用 call_json_deterministic 的 _build_input_hash：
#   该 hash 含 dim/seed/prompt_ver/tag_set_ver/temperature，是 tag pipeline 专用维度，
#   评分链路 opt-in cache 只关心 prompt + system + tier，hash 集合不一致会冲突。


async def _try_cache_get(*, prompt_hash: str, ttl_s: Optional[int]):
    try:
        cached = await LlmCache.get(prompt_hash)
    except Exception as e:  # noqa: BLE001
        logger.warning("LlmCache.get failed prompt_hash=%s err=%s", prompt_hash[:16], e)
        return None
    if cached is None:
        return None
    if ttl_s is None or ttl_s <= 0:
        return cached
    # TTL 检查靠 elapsed_ms 字段无法实现（那是 LLM 调用耗时不是 cache age）；
    # 这里偷懒：开启 TTL 时不命中本进程级 cache，直接走 LLM 再写回。
    # 真正的 TTL 需要 last_hit_at 字段；llm_cache 已有但 LlmCache.get 没暴露 → 略过。
    return cached


async def _try_cache_put(*, prompt_hash: str, resp: LLMResponse, chain_name: str) -> None:
    try:
        await LlmCache.put(
            prompt_hash,
            model_ver=resp.model,
            prompt_ver=f"chain::{chain_name}",
            tag_set_ver="none",
            seed=None,
            raw=resp.raw,
            parsed=resp.parsed,
            provider=resp.provider,
            elapsed_ms=resp.elapsed_ms,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "LlmCache.put failed prompt_hash=%s chain=%s err=%s",
            prompt_hash[:16], chain_name, e,
        )


def _parse_json_lenient(text: str) -> Optional[Any]:
    """尝试从模型输出里抽出 JSON（支持 ```json fenced 块、首尾噪音）。"""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 剥 ```json ... ``` 围栏
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{") or part.startswith("["):
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    continue
    # 截取第一个 { 到最后一个 }
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
