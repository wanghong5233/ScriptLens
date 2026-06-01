"""LLMClient —— agent_runtime ReAct 主循环用的 LLM 客户端。

继承自 ``service.core.llm.runtime.LLMRuntime``，保留 ReAct 专用方法
``reason_and_act`` / ``_build_react_prompt``；通用 generate / fallback /
candidate / blacklist / boot_check 全部由 LLMRuntime 提供。

DI 注入：
  - settings 来自 ``..core.config``（agent_runtime 自己的 Settings 实例，
    包含 LOG_FULL_PROMPT 等 ReAct 专用字段）
  - metrics 来自 ``..metrics.record_llm_usage`` 上报到 doc_studio_* 指标命名空间

历史保留 ``FORBIDDEN_LLM_MODELS`` 顶层常量与 ``LLMClient`` 类名以兼容现有
import；旧调用点不需要改。
"""
from typing import Any, Dict, List, Optional
import json
import logging

from service.core.llm.runtime import (
    LLMRuntime,
    FORBIDDEN_LLM_MODELS as _FORBIDDEN_LLM_MODELS,
)

from ..core.config import settings
from ..utils.language import guess_language
from ..utils.prompt_loader import load_prompt_bundle
from ..metrics import record_llm_usage

logger = logging.getLogger(__name__)

FORBIDDEN_LLM_MODELS = _FORBIDDEN_LLM_MODELS


class LLMClient(LLMRuntime):
    """ReAct 主循环 LLM 客户端：LLMRuntime + reason_and_act / _build_react_prompt。"""

    def __init__(self) -> None:
        super().__init__(settings_obj=settings, metrics_callback=record_llm_usage)

    def refresh_config(self) -> Dict[str, Any]:
        """向后兼容旧 API：reload .env 后重建 client。"""
        try:
            from config import refresh_settings  # type: ignore[import-not-found]

            refresh_settings()
        except ImportError:
            pass
        return self.refresh_settings(settings)

    async def reason_and_act(
        self,
        observation: str,
        available_tools: list,
        history: Optional[list] = None,
        llm_options: Optional[Dict[str, Any]] = None,
        image_attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """推理并决定下一步行动（Tool Calling）。

        使用 ReAct 模式构造 prompt，调用 Tool Calling API。
        返回 {tool_name, parameters, thought}。
        """
        llm_config = self._resolve_llm_config(llm_options)
        if not llm_config.get("api_key"):
            raise ValueError("LLM API key not configured")

        prompt = self._build_react_prompt(observation, available_tools, history)

        if settings.LOG_FULL_PROMPT:
            divider = "=" * 80
            logger.info(
                "\n%s\n📤 完整的 LLM Prompt (发送给大模型)\n%s\n%s\n%s",
                divider,
                divider,
                prompt,
                divider,
            )
            tool_params_note = ""
            if not settings.LOG_PROMPT_INCLUDE_TOOL_PARAMS:
                tool_params_note = " (工具参数详情已省略，如需查看请设置 LOG_PROMPT_INCLUDE_TOOL_PARAMS=True)"
            logger.info(
                "🔧 Prompt 元信息: tools=%s, temperature=%s, model=%s (%s)%s",
                len(available_tools),
                llm_config["temperature"],
                llm_config["model"],
                llm_config["provider"],
                tool_params_note,
            )

        try:
            response = await self.generate(
                prompt=prompt,
                tools=available_tools,
                temperature=self.temperature,
                llm_options=llm_options,
                image_attachments=image_attachments,
            )

            if settings.LOG_FULL_PROMPT:
                divider = "=" * 80
                try:
                    response_dump = json.dumps(response, ensure_ascii=False, indent=2)
                except TypeError:
                    response_dump = str(response)
                logger.info(
                    "\n%s\n📥 LLM 响应结果\n%s\n%s\n%s",
                    divider,
                    divider,
                    response_dump,
                    divider,
                )

            tool_calls = response.get("tool_calls")
            content = response.get("content", "")

            if tool_calls and len(tool_calls) > 0:
                tool_call = tool_calls[0]
                function = tool_call.get("function", {})
                tool_name = function.get("name", "")
                arguments_str = function.get("arguments", "{}")

                # Qwen (and other Chinese LLMs) routinely emit Tool-Calling
                # arguments where the JSON string value contains raw LF / CR /
                # TAB characters (e.g. multi-line markdown stuffed into a
                # "reply" field) instead of escaping them as \n. Strict
                # json.loads then fails, the caller silently falls back to
                # parameters={}, and reply_to_user_tool reports "Reply content
                # cannot be empty" — a completely misleading error that wastes
                # a guardrail slot (consecutive_failures=1/2 → 2/2) and shows
                # the user a fake "回复内容不能为空" banner.
                #
                # Fix: parse with strict=False (RFC-compliant relaxation that
                # accepts unescaped control chars inside strings) and only fall
                # back to empty when both attempts fail. This keeps behaviour
                # for well-formed payloads while letting borderline-malformed
                # ones through.
                parameters = None
                try:
                    parameters = json.loads(arguments_str)
                except json.JSONDecodeError as strict_err:
                    try:
                        parameters = json.loads(arguments_str, strict=False)
                        logger.warning(
                            "Tool arguments recovered via strict=False "
                            "(model emitted unescaped control chars). tool=%s "
                            "strict_err=%s",
                            tool_name,
                            strict_err,
                        )
                    except json.JSONDecodeError as lax_err:
                        logger.warning(
                            "Failed to parse tool arguments (both strict and "
                            "strict=False). tool=%s strict_err=%s lax_err=%s "
                            "arguments_str=%r",
                            tool_name,
                            strict_err,
                            lax_err,
                            arguments_str[:500],
                        )
                        parameters = {}

                return {
                    "tool_name": tool_name,
                    "parameters": parameters,
                    "thought": content or f"Calling tool: {tool_name}",
                }

            if content and ("完成" in content or "finish" in content.lower()):
                return {
                    "tool_name": "finish",
                    "parameters": {},
                    "thought": content,
                }

            return {
                "tool_name": "analyze_context_tool",
                "parameters": {"text": observation},
                "thought": content or "Analyzing the situation...",
            }

        except Exception as e:
            logger.error("Error in reason_and_act: %s", e, exc_info=True)
            return {
                "tool_name": "analyze_context_tool",
                "parameters": {"text": observation},
                "thought": f"Error occurred: {e}. Trying to analyze context...",
            }

    def _build_react_prompt(
        self,
        observation: str,
        available_tools: list,
        history: Optional[list] = None,
    ) -> str:
        """构造 ReAct 模式的 prompt。"""
        prompt_parts: List[str] = []

        lang = guess_language(observation)
        prompts = load_prompt_bundle("script_studio", lang)
        system_prompt = (prompts.get("react_system") or "").strip()
        history_header = (prompts.get("react_history_header") or "## 执行历史").strip()
        tools_header = (prompts.get("react_tools_header") or "## 可用工具").strip()
        observation_header = (prompts.get("react_observation_header") or "## 当前观察").strip()
        task_prompt = (prompts.get("react_task") or "").strip()

        if not system_prompt:
            system_prompt = "You are an intelligent script analysis agent."
        prompt_parts.append(system_prompt)

        if history:
            prompt_parts.append(f"\n{history_header}")
            for i, step in enumerate(history[-5:], 1):
                step_type = step.get("type", "unknown")
                step_content = step.get("content", "")
                step_tool = step.get("tool", "")
                step_result = step.get("result", {})

                prompt_parts.append(f"\n步骤 {i} ({step_type}):")
                if step_tool:
                    prompt_parts.append(f"  工具: {step_tool}")
                prompt_parts.append(f"  内容: {step_content}")
                if step_result:
                    result_str = json.dumps(step_result, ensure_ascii=False, indent=2)
                    prompt_parts.append(f"  结果: {result_str}")

        prompt_parts.append(f"\n{tools_header}")
        for tool in available_tools:
            tool_name = tool.get("function", {}).get("name", "")
            tool_desc = tool.get("function", {}).get("description", "")
            tool_params = tool.get("function", {}).get("parameters", {})

            prompt_parts.append(f"\n- **{tool_name}**: {tool_desc}")

            if settings.LOG_PROMPT_INCLUDE_TOOL_PARAMS and tool_params:
                params_desc = json.dumps(tool_params, ensure_ascii=False, indent=2)
                prompt_parts.append(f"  参数: {params_desc}")

        prompt_parts.append(f"\n{observation_header}")
        prompt_parts.append(observation)

        if task_prompt:
            prompt_parts.append(task_prompt)

        return "\n".join(prompt_parts)
