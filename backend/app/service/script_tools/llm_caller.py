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
- OpenAI 调用抛网络/限速/超时类异常 → 切 DashScope 重试一次
- 二次失败 → 抛 ScoreLLMError，由上层流水线决定该维标记 score=null
- JSON 解析失败 → 重试一次（temperature=0），仍失败抛错
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from openai import AsyncOpenAI, APIConnectionError, APIError, APITimeoutError, RateLimitError

from core.config import settings

logger = logging.getLogger(__name__)


class ScoreLLMError(RuntimeError):
    """评分用 LLM 调用最终失败（OpenAI + DashScope 都失败）。"""


class ScoreLLMJSONError(ScoreLLMError):
    """LLM 返回内容 2 次都不是合法 JSON。"""


@dataclass
class LLMResponse:
    raw: str
    parsed: Any
    provider: str  # "openai" | "dashscope"
    model: str
    elapsed_ms: int


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

    DashScope 兜底统一用 `qwen-max-latest` 别名（阿里官方"始终指向最新最强 qwen-max"），
    避免版本号在升级时迁移成本。短剧分析对 reasoning 强度敏感，
    弱化模型（qwen-turbo / qwen-plus）已从 fallback 中移除（详见 docs/08-evaluation-framework.md §6.2）。
    """

    PRIMARY = "primary"
    MINI = "mini"

    @staticmethod
    def resolve(tier: str, provider: str) -> str:
        if provider == "openai":
            if tier == ModelTier.MINI:
                return getattr(settings, "OPENAI_MINI_MODEL_NAME", None) or "gpt-5-mini"
            return settings.OPENAI_MODEL_NAME or "gpt-5.2"
        if tier == ModelTier.MINI:
            return getattr(settings, "SM_LLM_MODEL_AUX", None) or "qwen-max-latest"
        return settings.DASHSCOPE_MODEL_NAME or "qwen-max-latest"


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

    # 2 字段（is_real_violation + rationale ≤60 字）
    # ≈ 130 token × 2.0 = 260 → 256（已对齐 risk 二级判定的实测稳定区间）
    RISK_CONFIRM = 256

    # 4 字段（label + confidence + one_sentence_reason ≤60 字 + summary 3-5 句 ≤300 字）
    # ≈ 600 token × 1.7 = 1020 → 1024
    DECISION_AGGREGATE = 1024

    # logline + recommendation + confidence + genre + core_value + 3 优 + 3 劣（8 段 × ≤80 字）
    # ≈ 900 token × 1.7 = 1530 → 1536
    COVERAGE_CARD = 1536

    # 3 幕 × 6 节拍 × (type + summary ≤50 字 + anchor_scene_id)
    # ≈ 1500 token × 1.7 = 2550 → 2560
    BEAT_SHEET = 2560

    # 12 节点 × (id + role + 3×30字) + 30 边 × (src + tgt + type + polarity + description ≤30字)
    # ≈ 2500 token × 1.6 = 4000 → 4096
    CHARACTER_GRAPH = 4096

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
    """OpenAI 主、DashScope 兜底；强 JSON 输出；可指定模型档位。

    单例使用：`caller = LlmCaller(); await caller.call_json(prompt, tier=PRIMARY)`。
    """

    _RETRYABLE_EXC = (
        APIConnectionError,
        APITimeoutError,
        RateLimitError,
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.RemoteProtocolError,
    )

    def __init__(self, *, default_temperature: float = 0.2) -> None:
        self.default_temperature = default_temperature
        self._openai = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY or "",
            base_url=settings.OPENAI_BASE_URL,
        )
        # DashScope 也走 OpenAI compatible 协议
        self._dashscope = AsyncOpenAI(
            api_key=settings.DASHSCOPE_API_KEY or "",
            base_url=settings.DASHSCOPE_BASE_URL,
        )

    async def call_json(
        self,
        prompt: str,
        *,
        tier: str = ModelTier.PRIMARY,
        temperature: Optional[float] = None,
        max_tokens: int = 2048,
        system_message: Optional[str] = None,
    ) -> LLMResponse:
        """调用 LLM 并要求 JSON 输出（response_format={'type': 'json_object'}）。

        失败序列：
          OpenAI 一次 → JSON 解析 → 失败时切 DashScope 一次 → 仍失败 raise
        """
        temp = self.default_temperature if temperature is None else temperature
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})

        # 1. OpenAI 主路径
        try:
            return await self._call_with_provider(
                provider="openai",
                client=self._openai,
                tier=tier,
                messages=messages,
                temperature=temp,
                max_tokens=max_tokens,
            )
        except self._RETRYABLE_EXC as e:
            logger.warning("OpenAI 调用失败，切 DashScope 兜底：%s: %s", type(e).__name__, e)
        except APIError as e:
            # 5xx / quota 类错误也兜底；4xx 业务错误（如 invalid prompt）直接抛
            if e.status_code and 500 <= e.status_code < 600:
                logger.warning("OpenAI 5xx 错误兜底：%s", e)
            else:
                raise ScoreLLMError(f"OpenAI 业务错误不兜底：{e}") from e
        except ScoreLLMJSONError as e:
            logger.warning("OpenAI JSON 解析失败，切 DashScope 重试：%s", e)

        # 2. DashScope 兜底
        if not settings.DASHSCOPE_API_KEY:
            raise ScoreLLMError("OpenAI 失败且未配置 DASHSCOPE_API_KEY 兜底")
        try:
            return await self._call_with_provider(
                provider="dashscope",
                client=self._dashscope,
                tier=tier,
                messages=messages,
                temperature=temp,
                max_tokens=max_tokens,
            )
        except (APIError, ScoreLLMJSONError) + self._RETRYABLE_EXC as e:
            logger.error("DashScope 兜底也失败：%s: %s", type(e).__name__, e)
            raise ScoreLLMError(f"OpenAI 与 DashScope 都失败：{e}") from e

    async def _call_with_provider(
        self,
        *,
        provider: str,
        client: AsyncOpenAI,
        tier: str,
        messages: list,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        """单次 provider 调用 + reasoning model 自适应翻倍重试。

        参数 max_tokens 的语义：调用方想要的 content tokens 预算。
        capability 表负责把它换算成 provider 实际可接受的 effective budget。
        """
        model = ModelTier.resolve(tier, provider)
        cap = _resolve_capability(model)
        loop = asyncio.get_event_loop()
        t0 = loop.time()

        resp = await self._create_completion(
            client=client,
            model=model,
            cap=cap,
            messages=messages,
            temperature=temperature,
            content_budget=max_tokens,
        )
        choice = resp.choices[0] if resp.choices else None
        raw = (getattr(choice.message, "content", "") if choice else "") or ""
        finish = (getattr(choice, "finish_reason", "") if choice else "") or ""

        # reasoning model 自适应：finish=length 且 content 空 → 翻倍 budget 再试一次。
        # 真自适应（不是再多一个魔法数字），覆盖 capability 静态估算不够的边界。
        if cap.is_reasoning and not raw and finish == "length":
            doubled = max_tokens * 2
            logger.warning(
                "reasoning model=%s content 空 (finish=length budget=%d)，翻倍至 %d 重试",
                model,
                max_tokens,
                doubled,
            )
            resp = await self._create_completion(
                client=client,
                model=model,
                cap=cap,
                messages=messages,
                temperature=temperature,
                content_budget=doubled,
            )
            choice = resp.choices[0] if resp.choices else None
            raw = (getattr(choice.message, "content", "") if choice else "") or ""
            finish = (getattr(choice, "finish_reason", "") if choice else "") or ""

        if not raw:
            # capability matrix 已尽力，仍空 → 抛 JSONError 让 call_json 切 DashScope 兜底
            raise ScoreLLMJSONError(
                f"provider={provider} model={model} 返回内容为空"
                f"（finish_reason={finish or 'n/a'}, capability={cap.description}）"
            )

        parsed = _parse_json_lenient(raw)
        if parsed is None:
            raise ScoreLLMJSONError(f"provider={provider} model={model} 输出非 JSON: {raw[:200]}")

        elapsed_ms = int((loop.time() - t0) * 1000)
        return LLMResponse(raw=raw, parsed=parsed, provider=provider, model=model, elapsed_ms=elapsed_ms)

    async def _create_completion(
        self,
        *,
        client: AsyncOpenAI,
        model: str,
        cap: ModelCapability,
        messages: list,
        temperature: Optional[float],
        content_budget: int,
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
        candidates = [
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
    if param in ("max_tokens", "max_completion_tokens", "response_format", "temperature"):
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
    if "unsupported" in msg or "not supported" in msg:
        code = "unsupported_parameter"
    return param, code


def _short(e: APIError) -> str:
    s = str(e)
    return s[:160]


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
