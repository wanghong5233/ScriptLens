"""ScriptLens 剧本专属 ReAct 工具。

来源：reuse-matrix §5 / §5.2。继承 doc_studio `BaseTool` 自动获得：
- 预算守卫（agent_service ReAct 主循环里的 `tool_budget`）
- 连续失败计数（`error_handler.async_error_guard`）
- `reply_to_user` 兜底（达预算时 ReAct 主循环自动收尾）

为什么薄包装而不在工具里重写业务逻辑：
- 评分 / RAG 检索 / 场景查询 在 D2-4/D2-5a 已经实装为 service 层确定性流水线
- 工具层只负责"参数从 LLM 来 → 调 service → 包成 ToolResult"
- 业务逻辑改动只动 service 层，工具不变（reuse-matrix §0.1 子包语义边界）

script_id 来源：
  - chat / Agent 场景必须使用 agent_state.workspace_config["script_id"]
  - parameters["script_id"] 仅用于非 Agent 的同步端点；若与当前会话绑定剧本不一致，直接报错
  - 这保证"顶部选中的当前剧本"是所有工具的唯一作用域，不允许 LLM 参数漂移
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from .base_tool import BaseTool, ToolResult
from ..script_vfs import ScriptVFS, ScriptVFSError

logger = logging.getLogger(__name__)


# Wave C-2 (2026-05-31)：v3 6 维（story/character/concept/emotion/pacing/dialogue）
# 已整体替换为 v4 5 维「投资决策」 + COMPLIANCE 独立 gate。维度概念**不正交映射**，
# v3 dim_key 会被 service.score_one_dimension 显式拒绝（ValueError）。
# 详见 docs/2026-05-31-投资决策评分框架-v4.md。
_DIMENSIONS = (
    "hook",  # 抓人力：开场冲突 / 首集结尾留钩 / 第 1 分钟出诱因
    "archetype",  # 模板力：题材原型识别 + 角色原型清晰 + 模板内微差异
    "payoff",  # 爽感力：每集爽点密度 + 反转密度 + 没有塌陷段
    "monetization",  # 变现力：付费章 cliffhanger + 付费后爽点 + 集尾留钩
    "producibility",  # 可生成力：场数 / 同框人数 / 特殊场景 / 外景比
    "compliance",  # 合规：独立 gate，high_risk 一票否决
)


# ============================================================
# 共用 helper
# ============================================================


def _resolve_script_id(agent_state: Any, parameters: Dict[str, Any]) -> Optional[str]:
    """解析当前剧本作用域。

    Agent 调用时，workspace_config 里的 script_id 来自 router URL，并已校验用户归属。
    LLM 传入的 script_id 只能与它一致，不能覆盖当前剧本。
    """
    cfg = getattr(agent_state, "workspace_config", None) or {}
    bound_sid = str(cfg.get("script_id") or "").strip()
    param_sid = str((parameters or {}).get("script_id") or "").strip()
    if bound_sid:
        if param_sid and param_sid != bound_sid:
            raise ValueError(
                "script_id scope mismatch: tool parameter does not match current workspace script"
            )
        return bound_sid
    return param_sid or None


def _missing_script_id() -> ToolResult:
    return ToolResult(
        success=False,
        error="script_id is required (not found in parameters or agent_state.workspace_config)",
        summary="缺少 script_id",
    )


# ============================================================
# 1. ScoreDimensionTool
# ============================================================


class ScoreDimensionTool(BaseTool):
    """复核 / 重跑某一维度评分。"""

    def __init__(self) -> None:
        super().__init__(
            name="score_dimension_tool",
            description=(
                "复核或重新计算单一维度评分（v4 投资决策五维之一：HOOK 抓人力 / "
                "ARCHETYPE 模板力 / PAYOFF 爽感力 / MONETIZATION 变现力 / "
                "PRODUCIBILITY 可生成力；或独立合规审核：compliance）。"
                "返回 score / tier / reason / is_dealbreaker_triggered / signals 详情 "
                "以及若干证据场景的 ID。"
                "用户在 chat 里追问「为什么 HOOK 给 4 分」「再核一下合规」时调用本工具。"
                "注意：完整投资决策评分报告由后台流水线一次性生成；本工具只跑单一维度。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "dimension": {
                    "type": "string",
                    "enum": list(_DIMENSIONS),
                    "description": "v4 投资决策 5 维之一（hook/archetype/payoff/monetization/producibility）或 compliance",
                },
                "script_id": {
                    "type": "string",
                    "description": "剧本 UUID；缺省时使用当前会话绑定的剧本",
                },
            },
            "required": ["dimension"],
        }

    async def execute(self, agent_state: Any, parameters: Dict[str, Any]) -> ToolResult:
        dimension = (parameters or {}).get("dimension")
        if dimension not in _DIMENSIONS:
            return ToolResult(
                success=False,
                error=f"dimension must be one of {_DIMENSIONS}, got {dimension!r}",
                summary="非法 dimension",
            )

        try:
            script_id = _resolve_script_id(agent_state, parameters)
        except ValueError as e:
            return ToolResult(success=False, error=str(e), summary="剧本作用域不一致")
        if not script_id:
            return _missing_script_id()

        cache_key = f"{script_id}:{dimension}"
        cache = getattr(agent_state, "_score_dimension_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(agent_state, "_score_dimension_cache", cache)
        if cache_key in cache:
            cached = cache[cache_key]
            evi_count = len((cached or {}).get("evidence_scene_ids") or [])
            score = (cached or {}).get("score")
            tier = (cached or {}).get("tier")
            if score is None:
                summary = (
                    f"{dimension}: 证据不足，未给分（{evi_count} 条证据场景，"
                    f"原因：{(cached or {}).get('reason') or '未说明'}） [cached]"
                )
            else:
                summary = f"{dimension}: {score}/10 ({tier})，{evi_count} 条证据场景 [cached]"
            return ToolResult(success=True, data=cached, summary=summary)

        from service.script_report_service import score_one_dimension
        from service.script_tools.llm_caller import ScoreLLMError

        try:
            result = await score_one_dimension(
                script_id=script_id,
                dimension=dimension,
            )
        except ValueError as e:
            return ToolResult(success=False, error=str(e), summary="参数错误")
        except ScoreLLMError as e:
            logger.warning("score_dimension_tool LLM failed: %s", e)
            return ToolResult(success=False, error=f"LLM error: {e}", summary="LLM 调用失败")
        except Exception as e:
            logger.error("score_dimension_tool unexpected error: %s", e, exc_info=True)
            return ToolResult(success=False, error=str(e), summary="评分异常")

        cache[cache_key] = result

        evi_count = len(result.get("evidence_scene_ids") or [])
        score = result.get("score")
        tier = result.get("tier")
        if score is None:
            # rubric §6 证据不足：用户能看见状态，不伪装成成功打分
            summary = f"{dimension}: 证据不足，未给分（{evi_count} 条证据场景，原因：{result.get('reason') or '未说明'}）"
        else:
            summary = f"{dimension}: {score}/10 ({tier})，{evi_count} 条证据场景"
        return ToolResult(success=True, data=result, summary=summary)


# ============================================================
# 2. LocateScenesTool
# ============================================================


class LocateScenesTool(BaseTool):
    """自然语言查询定位剧本场景。"""

    def __init__(self) -> None:
        super().__init__(
            name="locate_scenes_tool",
            description=(
                "在当前剧本里检索与查询相关的场景。"
                "示例查询：「前 5 集钩子」「男女主第一次见面」「打脸高潮」「第 12 集冲突」。"
                "返回 top_k 个最相关的场景，每条含 scene_id / scene_label / episode_no / 文本片段 / 相关性分（score）/ 来源（source: bm25 或 llm_metadata 兜底）。"
                "注意：本工具只检索剧本内部场景，不联网；剧本之外的查询请用 web_search_tool。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "中文自然语言查询",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回场景数（默认 5，上限 10）",
                    "default": 5,
                },
                "script_id": {
                    "type": "string",
                    "description": "剧本 UUID；缺省时使用当前会话绑定的剧本",
                },
            },
            "required": ["query"],
        }

    async def execute(self, agent_state: Any, parameters: Dict[str, Any]) -> ToolResult:
        query = str((parameters or {}).get("query") or "").strip()
        if not query:
            return ToolResult(success=False, error="query is required", summary="缺 query")

        try:
            script_id = _resolve_script_id(agent_state, parameters)
        except ValueError as e:
            return ToolResult(success=False, error=str(e), summary="剧本作用域不一致")
        if not script_id:
            return _missing_script_id()

        try:
            top_k = int((parameters or {}).get("top_k") or 5)
        except (TypeError, ValueError):
            top_k = 5
        top_k = max(1, min(top_k, 10))

        from service.script_rag import retrieve_scenes

        try:
            scored = await retrieve_scenes(
                script_id=script_id,
                query=query,
                top_k=top_k,
            )
        except Exception as e:
            logger.error("locate_scenes_tool failed: %s", e, exc_info=True)
            return ToolResult(success=False, error=str(e), summary="检索失败")

        scenes = [
            {
                "scene_id": s.scene_id,
                "scene_no": s.scene_no,
                "scene_label": s.scene_label,
                "episode_no": s.episode_no,
                "text_excerpt": (s.text or "")[:200],
                "score": round(s.score, 4),
                "source": s.source,
            }
            for s in scored
        ]
        source_summary = ""
        if scored:
            unique_sources = {s.source for s in scored}
            if "llm_metadata" in unique_sources:
                source_summary = "（BM25 miss，已用 LLM 看 metadata 兜底挑选）"
        return ToolResult(
            success=True,
            data={"query": query, "scenes": scenes, "count": len(scenes)},
            summary=f"找到 {len(scenes)} 个相关场景{source_summary}",
        )


# ============================================================
# 3. ExtractCharactersTool
# ============================================================


class ExtractCharactersTool(BaseTool):
    """聚合全剧人物清单。"""

    def __init__(self) -> None:
        super().__init__(
            name="extract_characters_tool",
            description=(
                "列出当前剧本的全部人物清单。"
                "每个人物含：姓名 / 出现总场次 / 首次出现场景 ID（含集号场号）。"
                "用户问「这部剧主角是谁」「男主什么时候出场」时调用本工具。"
                "注意：role（主角/配角）由出现频次推断，不调 LLM；想要 LLM 解读人物关系请追加 chat 提问。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "top_n": {
                    "type": "integer",
                    "description": "返回出现次数最多的前 N 个人物（默认 20）",
                    "default": 20,
                },
                "script_id": {
                    "type": "string",
                    "description": "剧本 UUID；缺省时使用当前会话绑定的剧本",
                },
            },
            "required": [],
        }

    async def execute(self, agent_state: Any, parameters: Dict[str, Any]) -> ToolResult:
        try:
            script_id = _resolve_script_id(agent_state, parameters)
        except ValueError as e:
            return ToolResult(success=False, error=str(e), summary="剧本作用域不一致")
        if not script_id:
            return _missing_script_id()

        try:
            top_n = int((parameters or {}).get("top_n") or 20)
        except (TypeError, ValueError):
            top_n = 20
        top_n = max(1, min(top_n, 100))

        try:
            characters = _aggregate_characters(script_id=script_id, top_n=top_n)
        except Exception as e:
            logger.error("extract_characters_tool failed: %s", e, exc_info=True)
            return ToolResult(success=False, error=str(e), summary="人物抽取失败")

        return ToolResult(
            success=True,
            data={"characters": characters, "count": len(characters)},
            summary=f"找到 {len(characters)} 个人物",
        )


def _aggregate_characters(*, script_id: str, top_n: int) -> List[Dict[str, Any]]:
    """从 scriptlens.scenes.characters[] 聚合：unnest 后 GROUP BY name。"""
    from utils.database import engine

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                WITH ch AS (
                    SELECT s.id::text AS scene_id, s.episode_no, s.scene_no,
                           s.scene_label, UNNEST(s.characters) AS name,
                           ROW_NUMBER() OVER (PARTITION BY s.id ORDER BY s.episode_no, s.scene_no) AS scene_rk
                    FROM scriptlens.scenes s
                    WHERE s.script_id = :sid AND s.characters IS NOT NULL
                ),
                stat AS (
                    SELECT name, COUNT(*) AS scene_count
                    FROM ch
                    GROUP BY name
                ),
                first_appear AS (
                    SELECT DISTINCT ON (name) name, scene_id, episode_no, scene_no, scene_label
                    FROM ch
                    ORDER BY name, episode_no NULLS LAST, scene_no NULLS LAST
                )
                SELECT s.name, s.scene_count,
                       fa.scene_id AS first_appear_scene_id,
                       fa.episode_no AS first_appear_episode_no,
                       fa.scene_no AS first_appear_scene_no,
                       fa.scene_label AS first_appear_scene_label
                FROM stat s
                JOIN first_appear fa USING (name)
                ORDER BY s.scene_count DESC, s.name
                LIMIT :n
                """
            ),
            {"sid": script_id, "n": top_n},
        ).mappings().all()

    if not rows:
        return []

    # 按场次分布粗略推断 role：top 1-2 主角，3-5 重要配角，其余配角
    out: List[Dict[str, Any]] = []
    max_count = rows[0]["scene_count"] if rows else 1
    for i, r in enumerate(rows):
        cnt = r["scene_count"]
        if i < 2 and cnt >= max_count * 0.5:
            role = "lead"
        elif cnt >= max(3, max_count * 0.2):
            role = "supporting"
        else:
            role = "minor"
        out.append(
            {
                "name": r["name"],
                "role": role,
                "scene_count": cnt,
                "first_appear_scene_id": r["first_appear_scene_id"],
                "first_appear_episode_no": r["first_appear_episode_no"],
                "first_appear_scene_no": r["first_appear_scene_no"],
                "first_appear_scene_label": r["first_appear_scene_label"],
            }
        )
    return out


# ============================================================
# 4. ProposeRewriteTool
# ============================================================


_REWRITE_PROMPT = """你是中文短剧资深编剧。请基于「整剧上下文 + 目标场原文」对单场做定向改写。

【整剧概要】
{script_overview}

【人物表（出场频次倒序）】
{characters_block}

【前情场次摘要（按集场顺序，越靠前越早发生）】
{prev_scenes_block}

【目标场原文】（{scene_label}）
---
{scene_text}
---

【后续场次摘要（已经写好的剧情走向，改写时必须与之呼应，不能矛盾）】
{next_scenes_block}

【目标改写维度】{target_dimension}
【用户提出的问题】{issue}

任务：针对 **{target_dimension}** 这一维度，对【目标场原文】做定向改写。

硬约束（违反任何一条结果都不可用）：
1. 改写后只输出「目标场」的新文本——不要顺手改前 / 后场
2. 必须沿用上面【人物表】里已经存在的人物，可以引用【前情场次】已经发生的事件作为铺垫，但不能凭空捏造一个全新人物 / 全新核心事件
3. 改写后字数与原文 ±30% 以内
4. 必须与【后续场次】的剧情走向自洽（例如下场如果该角色出现，本场不能让他死）

维度对应的优化方向（取一即可，不要堆砌；v4 5 维 docs/2026-05-31-投资决策评分框架-v4.md）：
- hook         : 开场 30 字内出冲突 / 强情绪 / 反常事件；首场结尾留强钩子；第 1 分钟必须有让人想继续看的诱因
- archetype    : 强化题材原型识别（战神 / 重生 / 复仇 / 甜宠 / 系统）；前 3 场就出现题材标识事件；本场加一个模板内的差异化记忆点
- payoff       : 加一个爽点 / 反转 / 打脸 / 复仇 / CP 推进，放大爽感密度；避免本场出现「无事发生」的塌陷段
- monetization : 如果是付费节点前后场，提升场尾 cliffhanger 强度（决定用户付费 / 续看 / 切下一集）
- producibility: 降低同框人数 / 删除特殊场景 / 简化外景 / 调整对白密度，让本场更可生成（用于真人短剧或 AI 漫剧落地）

输出严格 JSON（不要 markdown 代码块包裹）：
{{
  "rewritten_excerpt": "<改写后的整段场景文本>",
  "rationale": "<≤150 字，解释你具体做了哪几处改动、用了哪些前情铺垫、为什么这样改能在 {target_dimension} 维度提分>"
}}"""


# 改写场景的 target_dimension 仅允许 v4 5 维（不允许 compliance：合规问题必须人工二次审核，
# 不交给 LLM 自动改写。复评合规走 ScoreDimensionTool(dimension="compliance")。）
_REWRITE_TARGET_DIMENSIONS = (
    "hook",
    "archetype",
    "payoff",
    "monetization",
    "producibility",
)


class ProposeRewriteTool(BaseTool):
    """对低分场景做定向改写。"""

    def __init__(self) -> None:
        super().__init__(
            name="propose_rewrite_tool",
            description=(
                "对剧本中某一场做定向改写，按 v4 投资决策 5 维之一定向优化（hook / "
                "archetype / payoff / monetization / producibility）。"
                "工具内部会自动加载整剧上下文（人物表 + 前后场摘要 + 整剧概要）"
                "再让 LLM 改写，保证新文本与剧情主线 / 已有人物 / 后续走向自洽。"
                "返回原文 / 改写版 / unified diff / 改动说明。"
                "用户问「把第 5 场改紧凑」「这场动机不成立怎么改」时调用本工具。"
                "改写仅产出单场新文本，不写入文件、不修改前后场，仅返回建议供前端审阅。"
                "注意：合规问题（compliance）不可作为 target_dimension —— 合规必须人工二次审核。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "scene_id": {
                    "type": "string",
                    "description": "目标场景 UUID",
                },
                "target_dimension": {
                    "type": "string",
                    "enum": list(_REWRITE_TARGET_DIMENSIONS),
                    "description": "v4 5 维之一（不含 compliance）",
                },
                "issue": {
                    "type": "string",
                    "description": "用户提出的具体问题（≤120 字）",
                },
            },
            "required": ["scene_id", "target_dimension", "issue"],
        }

    async def execute(self, agent_state: Any, parameters: Dict[str, Any]) -> ToolResult:
        params = parameters or {}
        scene_id = str(params.get("scene_id") or "").strip()
        target_dim = params.get("target_dimension")
        issue = str(params.get("issue") or "").strip()

        if not scene_id:
            return ToolResult(success=False, error="scene_id is required", summary="缺 scene_id")
        if target_dim not in _REWRITE_TARGET_DIMENSIONS:
            return ToolResult(
                success=False,
                error=(
                    f"target_dimension must be one of {_REWRITE_TARGET_DIMENSIONS}, "
                    f"got {target_dim!r}. 合规问题（compliance）不可改写，需人工二次审核。"
                ),
                summary="非法 target_dimension",
            )
        if not issue:
            return ToolResult(success=False, error="issue is required", summary="缺 issue 描述")

        try:
            script_id = _resolve_script_id(agent_state, params)
        except ValueError as e:
            return ToolResult(success=False, error=str(e), summary="剧本作用域不一致")
        if not script_id:
            return _missing_script_id()
        user_id = getattr(agent_state, "user_id", None)

        # 拉场景全文 + 整剧上下文（前后场摘要 + 人物表 + 整剧概要）
        # 剧本是连贯逻辑，单场改写必须看到前后铺垫与回收，否则 LLM 只会硬改文字。
        try:
            ctx = _load_rewrite_context(
                scene_id,
                expected_script_id=script_id,
                user_id=user_id,
            )
        except Exception as e:
            logger.error("load_rewrite_context failed: %s", e, exc_info=True)
            return ToolResult(success=False, error=str(e), summary="读取场景上下文失败")
        if ctx is None:
            return ToolResult(success=False, error=f"scene_id {scene_id} not found", summary="场景不存在")

        scene = ctx["scene"]
        scene_text = scene["text"] or ""
        if not scene_text.strip():
            return ToolResult(success=False, error="scene text is empty", summary="场景为空")

        from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget

        prompt = _REWRITE_PROMPT.format(
            scene_label=scene.get("scene_label") or "",
            scene_text=scene_text,
            target_dimension=target_dim,
            issue=issue,
            script_overview=ctx["script_overview"],
            characters_block=ctx["characters_block"],
            prev_scenes_block=ctx["prev_scenes_block"],
            next_scenes_block=ctx["next_scenes_block"],
        )
        caller = LlmCaller()
        try:
            resp = await caller.call_json(
                prompt,
                tier=ModelTier.PRIMARY,
                temperature=0.4,
                max_tokens=TokenBudget.REWRITE_EXCERPT,
            )
        except ScoreLLMError as e:
            logger.warning("propose_rewrite_tool LLM failed: %s", e)
            return ToolResult(success=False, error=f"LLM error: {e}", summary="LLM 调用失败")

        parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
        rewritten = str(parsed.get("rewritten_excerpt") or "").strip()
        rationale = str(parsed.get("rationale") or "").strip()
        if not rewritten:
            return ToolResult(success=False, error="LLM 未返回改写文本", summary="改写为空")

        # 计算 unified diff
        diff = "\n".join(
            difflib.unified_diff(
                scene_text.splitlines(),
                rewritten.splitlines(),
                fromfile=f"scene_{scene.get('scene_no') or scene_id[:8]}_original",
                tofile=f"scene_{scene.get('scene_no') or scene_id[:8]}_rewritten",
                lineterm="",
                n=2,
            )
        )

        return ToolResult(
            success=True,
            data={
                "scene_id": scene_id,
                "scene_label": scene.get("scene_label"),
                "target_dimension": target_dim,
                "issue": issue,
                "original_excerpt": scene_text,
                "rewritten_excerpt": rewritten,
                "diff": diff,
                "rationale": rationale,
            },
            summary=f"改写完成（{target_dim}）：原 {len(scene_text)} 字 → {len(rewritten)} 字",
        )


def _load_rewrite_context(
    scene_id: str,
    *,
    expected_script_id: str,
    user_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """装配单场改写所需的整剧上下文。

    返回结构：
        {
          "scene": {id, script_id, episode_no, scene_no, scene_label, text},
          "script_overview": str,    # 整剧 1-2 句概括（取自 reports，没有则用兜底语）
          "characters_block": str,   # 出场频次倒序的人物列表，多行
          "prev_scenes_block": str,  # 前 N 场的"集场标题 + 摘要"，多行
          "next_scenes_block": str,  # 后 N 场的"集场标题 + 摘要"，多行
        }

    选取策略（控 token）：
        - 前后各 _CONTEXT_WINDOW 场（默认 4 = 前 2 后 2）
        - 每场摘要取前 _SCENE_DIGEST_CHARS 字（默认 180）
        - 人物表只列前 _CHARACTERS_TOP_N 个（按出场频次）
        - 整剧概要优先取 reports.report_json.summary / decision.one_sentence_reason
    """
    from utils.database import engine

    with engine.connect() as conn:
        target = conn.execute(
            text(
                """
                SELECT sn.id::text AS id, sn.script_id::text AS script_id,
                       sn.episode_no, sn.scene_no, sn.scene_label, sn.text,
                       sc.user_id AS owner_id
                FROM scriptlens.scenes sn
                JOIN scriptlens.scripts sc ON sc.id = sn.script_id
                WHERE sn.id = :sid
                """
            ),
            {"sid": scene_id},
        ).mappings().first()
        if target is None:
            return None
        target_dict = dict(target)
        script_id = target_dict["script_id"]
        if script_id != expected_script_id:
            raise ValueError("scene_id does not belong to current script")
        if user_id is not None and int(target_dict["owner_id"]) != int(user_id):
            raise ValueError("current user cannot access this scene")
        target_dict.pop("owner_id", None)

        all_scenes = conn.execute(
            text(
                """
                SELECT id::text AS id, episode_no, scene_no, scene_label,
                       LEFT(text, :digest) AS digest, text
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                """
            ),
            {"sid": script_id, "digest": _SCENE_DIGEST_CHARS},
        ).mappings().all()

        # 人物表：unnest TEXT[] + 频次倒序，DB 里一次拿到，避免 N+1
        characters_rows = conn.execute(
            text(
                """
                SELECT character_name, COUNT(*) AS appearances
                FROM (
                    SELECT unnest(characters) AS character_name
                    FROM scriptlens.scenes
                    WHERE script_id = :sid
                ) AS t
                WHERE character_name IS NOT NULL AND character_name <> ''
                GROUP BY character_name
                ORDER BY appearances DESC, character_name ASC
                LIMIT :top_n
                """
            ),
            {"sid": script_id, "top_n": _CHARACTERS_TOP_N},
        ).mappings().all()

        report_row = conn.execute(
            text(
                """
                SELECT report_json
                FROM scriptlens.reports
                WHERE script_id = :sid
                ORDER BY generated_at DESC
                LIMIT 1
                """
            ),
            {"sid": script_id},
        ).mappings().first()

    scenes_list = [dict(s) for s in all_scenes]
    target_idx = next(
        (i for i, s in enumerate(scenes_list) if s["id"] == scene_id),
        None,
    )
    if target_idx is None:
        # 理论不会到这里——target 已经从 SELECT 查到了
        return None

    prev_window = scenes_list[max(0, target_idx - _CONTEXT_WINDOW): target_idx]
    next_window = scenes_list[target_idx + 1: target_idx + 1 + _CONTEXT_WINDOW]

    return {
        "scene": target_dict,
        "script_overview": _build_script_overview(report_row),
        "characters_block": _build_characters_block([dict(r) for r in characters_rows]),
        "prev_scenes_block": _format_scene_window(prev_window) or "（无前情场次，本场为剧本开端）",
        "next_scenes_block": _format_scene_window(next_window) or "（无后续场次，本场为剧本结尾）",
    }


_CONTEXT_WINDOW = 2  # 前后各取 N 场摘要
_SCENE_DIGEST_CHARS = 180  # 每场摘要取前 N 字（中文按 1 字 ≈ 1 token 估）
_CHARACTERS_TOP_N = 12  # 人物表只列出场频次前 N 名


def _build_script_overview(report_row: Optional[Any]) -> str:
    """优先取 reports.report_json.summary / decision.one_sentence_reason；缺则兜底。"""
    if report_row is None:
        return "（暂无整剧分析报告，请基于「前情 / 后续场次摘要」推断主线）"
    payload = report_row["report_json"]
    if isinstance(payload, (str, bytes)):
        import json as _json
        try:
            payload = _json.loads(payload)
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        return "（剧情概要解析失败，请基于上下文推断）"
    summary = str(payload.get("summary") or "").strip()
    decision = payload.get("decision") or {}
    one_line = ""
    if isinstance(decision, dict):
        one_line = str(decision.get("one_sentence_reason") or "").strip()
    parts = [p for p in (summary, one_line) if p]
    return "\n".join(parts) if parts else "（暂无整剧概要，请基于上下文推断）"


def _build_characters_block(rows: List[Dict[str, Any]]) -> str:
    """rows: [{character_name, appearances}, ...]，已按出场倒序，限 _CHARACTERS_TOP_N。"""
    if not rows:
        return "（剧本未抽取出人物，请从前情 / 后续摘要里识别）"
    return "\n".join(
        f"- {r['character_name']}（出场 {r['appearances']} 场）" for r in rows
    )


def _format_scene_window(window: List[Dict[str, Any]]) -> str:
    if not window:
        return ""
    lines = []
    for s in window:
        digest = (s.get("digest") or "").strip().replace("\n", " ")
        lines.append(f"- {_scene_title(s)}：{digest}")
    return "\n".join(lines)


def _scene_title(s: Dict[str, Any]) -> str:
    parts: List[str] = []
    ep = s.get("episode_no")
    if ep is not None:
        parts.append(f"第{ep}集")
    sn = s.get("scene_no")
    if sn is not None:
        parts.append(f"第{sn}场")
    label = s.get("scene_label")
    if label:
        parts.append(f"《{label}》")
    return " ".join(parts) if parts else "未命名场"


# ============================================================
# 5. ReadScene / ProposeFullScriptPlan / RewriteScene（改写三件套）
# ============================================================

def _load_scene_meta(*, scene_id: str, expected_script_id: str) -> Dict[str, Any]:
    from utils.database import engine

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT s.id::text AS scene_id,
                       s.episode_no,
                       s.scene_no::text AS scene_no,
                       s.scene_label
                FROM scriptlens.scenes s
                WHERE s.id::text = :scene_id
                  AND s.script_id::text = :script_id
                """
            ),
            {"scene_id": scene_id, "script_id": expected_script_id},
        ).mappings().first()
    if row is None:
        raise ValueError(
            f"scene {scene_id} not found in script {expected_script_id}"
        )
    return dict(row)


def _mutate_agent_state_for_scene(
    *,
    agent_state: Any,
    scene_path: str,
    scene_id: str,
    original_text: str,
) -> None:
    modified_files = getattr(agent_state, "modified_files", None)
    if modified_files is None:
        modified_files = set()
        setattr(agent_state, "modified_files", modified_files)
    modified_files.add(scene_path)

    originals = getattr(agent_state, "original_file_contents", None)
    if originals is None:
        originals = {}
        setattr(agent_state, "original_file_contents", originals)
    if scene_path not in originals:
        originals[scene_path] = original_text
    if scene_id not in originals:
        originals[scene_id] = original_text


class ReadSceneTool(BaseTool):
    """读取单场原文（支持 scene_id 或 ScriptVFS file_path）。"""

    def __init__(self) -> None:
        super().__init__(
            name="read_scene_tool",
            description=(
                "读取单场剧本原文。支持 scene_id 或 ScriptVFS 路径（scenes/E03-S005.txt）。"
                "返回 scene_id/file_path/集场信息/场景标题/全文。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "scene_id": {"type": "string", "description": "目标场景 UUID"},
                "file_path": {
                    "type": "string",
                    "description": "ScriptVFS 路径，如 scenes/E03-S005.txt",
                },
                "script_id": {
                    "type": "string",
                    "description": "剧本 UUID；缺省时使用当前会话绑定剧本",
                },
            },
            "required": [],
        }

    async def execute(self, agent_state: Any, parameters: Dict[str, Any]) -> ToolResult:
        params = parameters or {}
        raw_scene_id = str(params.get("scene_id") or "").strip()
        raw_file_path = str(params.get("file_path") or "").strip()
        if not raw_scene_id and not raw_file_path:
            return ToolResult(
                success=False,
                error="either scene_id or file_path is required",
                summary="缺 scene_id/file_path",
            )
        try:
            script_id = _resolve_script_id(agent_state, params)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), summary="剧本作用域不一致")
        if not script_id:
            return _missing_script_id()

        try:
            vfs = ScriptVFS(script_id=script_id)
        except ScriptVFSError as exc:
            return ToolResult(success=False, error=str(exc), summary="ScriptVFS 初始化失败")

        try:
            if raw_scene_id and raw_file_path:
                sid_from_path = vfs.resolve_scene_id(raw_file_path)
                if sid_from_path != raw_scene_id:
                    return ToolResult(
                        success=False,
                        error="scene_id does not match file_path in current script scope",
                        summary="scene_id/file_path 不一致",
                    )
                scene_id = raw_scene_id
                file_path = vfs.resolve_file_path(scene_id)
            elif raw_scene_id:
                scene_id = raw_scene_id
                file_path = vfs.resolve_file_path(scene_id)
            else:
                scene_id = vfs.resolve_scene_id(raw_file_path)
                file_path = vfs.coerce_file_path(raw_file_path)
            scene_text = vfs.read(scene_id)
        except ScriptVFSError as exc:
            return ToolResult(success=False, error=str(exc), summary="场景读取失败")

        try:
            meta = _load_scene_meta(scene_id=scene_id, expected_script_id=script_id)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), summary="场景不存在")

        return ToolResult(
            success=True,
            data={
                "scene_id": scene_id,
                "file_path": file_path,
                "episode_no": meta.get("episode_no"),
                "scene_no": meta.get("scene_no"),
                "scene_label": meta.get("scene_label"),
                "text": scene_text,
                "char_count": len(scene_text),
            },
            summary=f"读取场景成功：{file_path}（{len(scene_text)} 字）",
        )


_SELECTION_REWRITE_SYSTEM_PROMPT = (
    "你是短剧文本改写器。"
    "你只能输出 JSON 对象，不要输出 markdown、不要输出解释段落。"
    "JSON 结构固定为 {\"rewritten_text\": string, \"rationale\": string}。"
)
_selection_rewrite_runtime: Optional[Any] = None


def _get_selection_rewrite_runtime() -> Any:
    """懒加载 Selection 改写运行时，复用统一 LLMRuntime 配置。"""
    global _selection_rewrite_runtime
    if _selection_rewrite_runtime is None:
        from core.config import settings
        from service.core.llm.runtime import LLMRuntime

        _selection_rewrite_runtime = LLMRuntime(settings_obj=settings)
    return _selection_rewrite_runtime


def _coerce_optional_int(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _pick_selection_from_context(
    *,
    agent_state: Any,
    selection_id: Optional[int],
    selection_placeholder: str,
) -> Optional[Dict[str, Any]]:
    context = getattr(agent_state, "request_context", None)
    if not isinstance(context, dict):
        return None

    selections = context.get("selections")
    if isinstance(selections, list) and selections:
        if selection_id is not None:
            for item in selections:
                if not isinstance(item, dict):
                    continue
                item_id = _coerce_optional_int(item.get("id"))
                if item_id == selection_id:
                    return item
        if selection_placeholder:
            target = selection_placeholder.strip().lower()
            for item in selections:
                if not isinstance(item, dict):
                    continue
                placeholder = str(item.get("placeholder") or "").strip().lower()
                if placeholder == target:
                    return item
        if len(selections) == 1 and isinstance(selections[0], dict):
            return selections[0]

    selection = context.get("selection")
    if isinstance(selection, dict):
        return selection
    return None


def _resolve_selection_span(
    *,
    scene_text: str,
    selection_start: Optional[int],
    selection_end: Optional[int],
    selection_text: str,
) -> tuple[int, int]:
    if selection_start is not None or selection_end is not None:
        if selection_start is None or selection_end is None:
            raise ValueError("selection_start / selection_end 必须同时提供")
        if selection_start < 0 or selection_end < 0:
            raise ValueError("selection_start / selection_end 不能为负数")
        if selection_end <= selection_start:
            raise ValueError("selection_end 必须大于 selection_start")
        if selection_end > len(scene_text):
            raise ValueError(
                f"selection_end 超出场景长度（end={selection_end}, len={len(scene_text)}）"
            )
        return selection_start, selection_end

    if not selection_text:
        raise ValueError("缺少可定位选区：请提供 selection_text 或 selection_start/selection_end")

    matches = list(re.finditer(re.escape(selection_text), scene_text))
    if not matches:
        raise ValueError("selection_text 在目标场景中未命中")
    if len(matches) > 1:
        raise ValueError(
            "selection_text 在目标场景中命中多处，需补充 selection_start/selection_end 消歧"
        )
    match = matches[0]
    return match.start(), match.end()


class RewriteSelectionSceneTool(BaseTool):
    """按选区改写场景并写回 DB。"""

    def __init__(self) -> None:
        super().__init__(
            name="rewrite_selection_scene_tool",
            description=(
                "按选区改写场景文本并写回数据库。"
                "适用于“把这段改成英语/润色/重写”等选区级编辑；"
                "执行后会更新 AgentState 的 modified_files/original_file_contents 以产出 diff。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "改写指令，例如：改成英语 / 更口语化 / 更有压迫感",
                },
                "scene_id": {"type": "string", "description": "目标场景 UUID（可选）"},
                "file_path": {
                    "type": "string",
                    "description": "ScriptVFS 路径，如 scenes/E03-S005.txt（可选）",
                },
                "selection_text": {
                    "type": "string",
                    "description": "选区原文（可选；缺省时从上下文 selections 读取）",
                },
                "selection_start": {
                    "type": "integer",
                    "description": "选区起始偏移（0-based，含）",
                },
                "selection_end": {
                    "type": "integer",
                    "description": "选区结束偏移（0-based，不含）",
                },
                "selection_id": {
                    "type": "integer",
                    "description": "上下文 selections 的 id（可选）",
                },
                "selection_placeholder": {
                    "type": "string",
                    "description": "上下文 selections 的占位符（如 @selection1，可选）",
                },
                "script_id": {
                    "type": "string",
                    "description": "剧本 UUID；缺省时使用当前会话绑定剧本",
                },
            },
            "required": ["instruction"],
        }

    async def execute(self, agent_state: Any, parameters: Dict[str, Any]) -> ToolResult:
        params = parameters or {}
        instruction = str(params.get("instruction") or "").strip()
        if not instruction:
            return ToolResult(
                success=False,
                error="instruction is required",
                summary="缺 instruction",
            )

        try:
            script_id = _resolve_script_id(agent_state, params)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), summary="剧本作用域不一致")
        if not script_id:
            return _missing_script_id()

        try:
            vfs = ScriptVFS(script_id=script_id)
        except ScriptVFSError as exc:
            return ToolResult(success=False, error=str(exc), summary="ScriptVFS 初始化失败")

        selection_id = _coerce_optional_int(params.get("selection_id"))
        selection_placeholder = str(params.get("selection_placeholder") or "").strip()
        context_selection = _pick_selection_from_context(
            agent_state=agent_state,
            selection_id=selection_id,
            selection_placeholder=selection_placeholder,
        )

        raw_scene_id = str(params.get("scene_id") or "").strip()
        raw_file_path = str(params.get("file_path") or "").strip()
        if not raw_scene_id and isinstance(context_selection, dict):
            raw_scene_id = str(context_selection.get("scene_id") or "").strip()
        if not raw_file_path and isinstance(context_selection, dict):
            raw_file_path = str(context_selection.get("file_path") or "").strip()

        request_context = getattr(agent_state, "request_context", None)
        if not raw_file_path and isinstance(request_context, dict):
            raw_file_path = str(request_context.get("file_path") or "").strip()

        if not raw_scene_id and not raw_file_path:
            return ToolResult(
                success=False,
                error="either scene_id or file_path is required (or inferable from request context)",
                summary="缺 scene_id/file_path",
            )

        try:
            if raw_scene_id and raw_file_path:
                sid_from_path = vfs.resolve_scene_id(raw_file_path)
                if sid_from_path != raw_scene_id:
                    return ToolResult(
                        success=False,
                        error="scene_id does not match file_path in current script scope",
                        summary="scene_id/file_path 不一致",
                    )
                scene_id = raw_scene_id
                scene_path = vfs.resolve_file_path(scene_id)
            elif raw_scene_id:
                scene_id = raw_scene_id
                scene_path = vfs.resolve_file_path(scene_id)
            else:
                scene_id = vfs.resolve_scene_id(raw_file_path)
                scene_path = vfs.coerce_file_path(raw_file_path)
            scene_text = vfs.read(scene_id)
        except ScriptVFSError as exc:
            return ToolResult(success=False, error=str(exc), summary="目标场景定位失败")

        selection_text = str(params.get("selection_text") or "").strip()
        selection_start = _coerce_optional_int(params.get("selection_start"))
        selection_end = _coerce_optional_int(params.get("selection_end"))
        if isinstance(context_selection, dict):
            if not selection_text:
                selection_text = str(
                    context_selection.get("text") or context_selection.get("preview") or ""
                ).strip()
            if selection_start is None:
                selection_start = _coerce_optional_int(context_selection.get("start"))
            if selection_end is None:
                selection_end = _coerce_optional_int(context_selection.get("end"))

        try:
            start, end = _resolve_selection_span(
                scene_text=scene_text,
                selection_start=selection_start,
                selection_end=selection_end,
                selection_text=selection_text,
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), summary="选区定位失败")

        original_fragment = scene_text[start:end]
        if not original_fragment:
            return ToolResult(
                success=False,
                error="selection is empty after span resolution",
                summary="空选区",
            )

        left_context = scene_text[max(0, start - 240):start]
        right_context = scene_text[end:min(len(scene_text), end + 240)]
        prompt = (
            "请根据 instruction 只改写“选区原文”并返回 JSON。\n\n"
            f"instruction:\n{instruction}\n\n"
            "约束：\n"
            "- rewritten_text 只能是替换选区的文本，不要包含前后文。\n"
            "- 保持人物名、事实关系与剧情时序，不要杜撰新事件。\n"
            "- 若 instruction 是翻译，保持舞台提示/对白格式与语气。\n\n"
            "选区前文（仅供语境，不可原样重复输出）：\n"
            f"{left_context}\n\n"
            "选区原文（必须围绕它改写）：\n"
            f"{original_fragment}\n\n"
            "选区后文（仅供语境，不可原样重复输出）：\n"
            f"{right_context}\n\n"
            "输出 JSON：{\"rewritten_text\": \"...\", \"rationale\": \"...\"}"
        )

        try:
            runtime = _get_selection_rewrite_runtime()
            runtime_result = await runtime.generate_json(
                prompt=prompt,
                system_message=_SELECTION_REWRITE_SYSTEM_PROMPT,
                temperature=0.2,
                max_tokens=1536,
                llm_options=getattr(agent_state, "llm_options", None),
            )
        except (RuntimeError, ValueError, TypeError) as exc:
            logger.warning("rewrite_selection_scene_tool LLM failed: %s", exc)
            return ToolResult(
                success=False,
                error=f"LLM error: {exc}",
                summary="选区改写失败",
            )

        parsed = runtime_result.get("parsed")
        if not isinstance(parsed, dict):
            return ToolResult(
                success=False,
                error="LLM response parsed payload is not an object",
                summary="选区改写解析失败",
            )

        rewritten_fragment = str(parsed.get("rewritten_text") or "").strip()
        rationale = str(parsed.get("rationale") or "").strip()
        if not rewritten_fragment:
            return ToolResult(
                success=False,
                error="rewritten_text is empty",
                summary="选区改写为空",
            )
        if rewritten_fragment == original_fragment:
            return ToolResult(
                success=False,
                error="rewritten_text is identical to original selection",
                summary="选区无改动",
            )

        rewritten_scene_text = scene_text[:start] + rewritten_fragment + scene_text[end:]
        try:
            _persist_scene_text(
                scene_id=scene_id,
                new_text=rewritten_scene_text,
                expected_script_id=script_id,
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), summary="改写持久化失败")

        operation_ref: Optional[str] = None
        user_id = _coerce_optional_int(getattr(agent_state, "user_id", None))
        if user_id is None:
            logger.warning(
                "rewrite_selection_scene_tool skip record_rewrite_op: missing user_id (script=%s scene=%s)",
                script_id,
                scene_id,
            )
        else:
            from service import script_operation_service

            try:
                op_record = script_operation_service.record_rewrite_op(
                    script_id=script_id,
                    user_id=user_id,
                    scene_id=scene_id,
                    target_dimension="general",
                    issue=instruction,
                    original_text=scene_text,
                    rewritten_text=rewritten_scene_text,
                    rationale=rationale or "",
                )
                operation_ref = str(op_record.get("operation_id") or "").strip() or None
            except script_operation_service.OperationError as exc:
                logger.warning(
                    "rewrite_selection_scene_tool record_rewrite_op failed (non-blocking): %s",
                    exc,
                )

        _mutate_agent_state_for_scene(
            agent_state=agent_state,
            scene_path=scene_path,
            scene_id=scene_id,
            original_text=scene_text,
        )

        return ToolResult(
            success=True,
            data={
                "scene_id": scene_id,
                "file_path": scene_path,
                "selection_start": start,
                "selection_end": end,
                "original_fragment": original_fragment,
                "rewritten_fragment": rewritten_fragment,
                "instruction": instruction,
                "operation_id": operation_ref,
                "rationale": rationale,
                "original_scene_chars": len(scene_text),
                "rewritten_scene_chars": len(rewritten_scene_text),
            },
            summary=(
                f"选区改写完成：{scene_path} [{start}:{end}] "
                f"{len(original_fragment)}→{len(rewritten_fragment)} 字"
            ),
        )


_INVESTMENT_DIM_KEYS = ("hook", "archetype", "payoff", "monetization", "producibility")


def _normalize_investment_dim_keys(raw: Any) -> List[str]:
    """归一投资决策维度键列表（兼容 None / 单字符串 / list）。"""

    if raw is None:
        return []
    if isinstance(raw, str):
        candidates: List[str] = [raw]
    elif isinstance(raw, (list, tuple)):
        candidates = [str(item) for item in raw]
    else:
        return []
    out: List[str] = []
    for item in candidates:
        key = str(item or "").strip().lower()
        if key in _INVESTMENT_DIM_KEYS and key not in out:
            out.append(key)
    return out


def _coerce_improvement_brief(raw: Any) -> Optional[Dict[str, Any]]:
    """把前端透传过来的 improvement 字段（来自 TASK_META）打成 dict 给 LLM。

    白名单字段：title / rationale / dimension_key / dimension_label /
    signal_key / signal_label / evidence_ref_ids。其余字段一律丢弃避免污染。
    """

    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    rationale = str(raw.get("rationale") or "").strip()
    if not title and not rationale:
        return None
    evidence_ids = raw.get("evidence_ref_ids") or []
    if not isinstance(evidence_ids, list):
        evidence_ids = []
    return {
        "title": title,
        "rationale": rationale,
        "dimension_key": str(raw.get("dimension_key") or "").strip() or None,
        "dimension_label": str(raw.get("dimension_label") or "").strip() or None,
        "signal_key": str(raw.get("signal_key") or "").strip() or None,
        "signal_label": str(raw.get("signal_label") or "").strip() or None,
        "evidence_ref_ids": [str(eid) for eid in evidence_ids if str(eid).strip()],
    }


def _coerce_diagnostic_brief(raw: Any) -> Optional[Dict[str, Any]]:
    """把前端透传过来的 diagnostic 字段打成 dict。

    白名单字段：verdict_label / verdict_reason / investment_score /
    top_improvements（每条仅保留 title / dimension_label / rationale）。
    """

    if not isinstance(raw, dict):
        return None
    verdict_label = str(raw.get("verdict_label") or "").strip()
    verdict_reason = str(raw.get("verdict_reason") or "").strip()
    investment_score = raw.get("investment_score")
    if isinstance(investment_score, str):
        try:
            investment_score = float(investment_score)
        except ValueError:
            investment_score = None
    tops_raw = raw.get("top_improvements") or []
    tops: List[Dict[str, Any]] = []
    if isinstance(tops_raw, list):
        for it in tops_raw[:5]:
            if not isinstance(it, dict):
                continue
            tops.append(
                {
                    "title": str(it.get("title") or "").strip(),
                    "dimension_label": str(it.get("dimension_label") or "").strip() or None,
                    "rationale": str(it.get("rationale") or "").strip(),
                }
            )
    if not (verdict_label or verdict_reason or investment_score is not None or tops):
        return None
    return {
        "verdict_label": verdict_label or None,
        "verdict_reason": verdict_reason or None,
        "investment_score": investment_score,
        "top_improvements": tops,
    }


class ProposeFullScriptPlanTool(BaseTool):
    """生成全剧改写计划（只出 plan，不写库）。

    LLM 视角的契约由 :py:attr:`description` 和 :py:attr:`parameters_schema`
    决定，不会读到本 docstring。本 docstring 只面向后端维护者，说明 tool
    内部如何把 LLM 给的参数映射到 ``service.script_tools.rewrite_chain.propose_plan``。

    路由协议（execute 内部）：
      * ``improvement`` 或 ``diagnostic`` 任一非空 → 走 LLM-first 路径，
        由 propose_plan 内部 LLM 选场；
      * 仅 ``dimensions`` 非空 → 默认走 LLM-first，把维度作为 hint；
      * 全空 → 直接 ToolResult(success=False)，并 logger.warning。

    向后兼容：propose_plan Python 签名仍接受老调用方的六维 ``dimensions``
    字符串集（legacy CLI / 测试），通过 enum 值判定自动 fallback。
    """

    def __init__(self) -> None:
        super().__init__(
            name="propose_full_script_plan_tool",
            description=(
                "根据剧本投资决策评分的改进建议或整体诊断，结合剧本概要 + "
                "全部场次清单，由模型自主选出 1~12 场写出改写计划，返回 "
                "rewrite_plan（含 scene_id/target_dimensions/rationale/"
                "expected_changes）。不会修改任何场景文本。"
                "推荐用法：当用户点击「按此条改稿」时把 TASK_META 里的 "
                "improvement 原样传入；点击「按本次诊断改稿」时把 diagnostic "
                "原样传入；二者都没有时再用 dimensions 作为目标维度提示。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "dimensions": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(_INVESTMENT_DIM_KEYS),
                    },
                    "description": (
                        "目标维度（投资决策评分的五维，可选）。可选值："
                        "hook=抓人力、archetype=模板力、payoff=兑现力、"
                        "monetization=变现力、producibility=可生成力。"
                        "传 1~3 个即可；通常不必填，improvement/diagnostic "
                        "已经隐含目标维度。"
                    ),
                },
                "improvement": {
                    "type": "object",
                    "description": (
                        "单条改进建议上下文（来自 TASK_META.improvement，原样透传）。"
                        "字段：title / rationale / dimension_key / dimension_label / "
                        "signal_key / signal_label / evidence_ref_ids。"
                    ),
                },
                "diagnostic": {
                    "type": "object",
                    "description": (
                        "整剧诊断上下文（来自 TASK_META.diagnostic，原样透传）。"
                        "字段：verdict_label / verdict_reason / investment_score / "
                        "top_improvements[{title, dimension_label, rationale}]。"
                    ),
                },
                "max_steps": {
                    "type": "integer",
                    "description": "计划最大场次数（默认 12，上限 30）。",
                    "default": 12,
                },
                "script_id": {
                    "type": "string",
                    "description": "剧本 UUID；缺省时使用当前会话绑定剧本。",
                },
            },
            # required 不强制——dimensions/improvement/diagnostic 任一非空即可。
            "required": [],
        }

    async def execute(self, agent_state: Any, parameters: Dict[str, Any]) -> ToolResult:
        params = parameters or {}
        dim_keys = _normalize_investment_dim_keys(params.get("dimensions"))
        improvement = _coerce_improvement_brief(params.get("improvement"))
        diagnostic = _coerce_diagnostic_brief(params.get("diagnostic"))

        if not (dim_keys or improvement or diagnostic):
            # 语义级校验：必须给出至少一个改写驱动信号，否则 LLM 拿不到目标。
            logger.warning(
                "propose_full_script_plan_tool missing input: params_keys=%s",
                sorted(list(params.keys())),
            )
            return ToolResult(
                success=False,
                error=(
                    "缺少改写目标输入：dimensions / improvement / diagnostic "
                    "至少需要一个非空。点击「按此条改稿」请透传 improvement，"
                    "点击「按本次诊断改稿」请透传 diagnostic。"
                ),
                summary="缺少改写目标输入",
            )

        try:
            script_id = _resolve_script_id(agent_state, params)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), summary="剧本作用域不一致")
        if not script_id:
            return _missing_script_id()

        try:
            max_steps_raw = int(params.get("max_steps") or 12)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                error="max_steps must be an integer",
                summary="max_steps 参数错误",
            )
        max_steps = max(1, min(max_steps_raw, 30))

        from service.script_tools.llm_caller import LlmCaller, ScoreLLMError
        from service.script_tools.rewrite_chain import propose_plan

        caller = LlmCaller()
        try:
            plan = await propose_plan(
                script_id=script_id,
                dimension_keys=dim_keys or None,
                improvement_brief=improvement,
                diagnostic_brief=diagnostic,
                max_steps=max_steps,
                caller=caller,
            )
        except ValueError as exc:
            logger.warning(
                "propose_full_script_plan_tool input error script=%s dimensions=%s err=%s",
                script_id,
                dim_keys,
                exc,
            )
            return ToolResult(success=False, error=str(exc), summary="plan 参数错误")
        except ScoreLLMError as exc:
            logger.warning(
                "propose_full_script_plan_tool LLM failed script=%s dimensions=%s err=%s",
                script_id,
                dim_keys,
                exc,
            )
            return ToolResult(success=False, error=f"LLM error: {exc}", summary="plan LLM 失败")

        plan_dict = plan.to_dict()
        step_count = len(plan_dict.get("steps") or [])
        # summary 用业务可读标签：优先 improvement.dimension_label，其次维度键。
        dim_summary_bits: list[str] = []
        if improvement and improvement.get("dimension_label"):
            dim_summary_bits.append(str(improvement["dimension_label"]))
        if dim_keys:
            dim_summary_bits.append("/".join(dim_keys))
        dim_summary = "+".join(dim_summary_bits) if dim_summary_bits else "—"
        if step_count == 0:
            summary = f"全剧改写计划：未发现需改写场次（{dim_summary}）"
        else:
            summary = f"全剧改写计划完成：共 {step_count} 场（{dim_summary}）"
        return ToolResult(
            success=True,
            data={
                "mode": "plan",
                "dimensions": dim_keys,
                "improvement": improvement,
                "diagnostic": diagnostic,
                "rewrite_plan": plan_dict,
                "script_id": script_id,
            },
            summary=summary,
        )


class RewriteSceneTool(BaseTool):
    """按目标维度改写单场并写回 DB。"""

    def __init__(self) -> None:
        super().__init__(
            name="rewrite_scene_tool",
            description=(
                "按目标维度改写单场并写回数据库。"
                "输入 scene_id（或 file_path）+ target_dimensions + expected_changes，"
                "执行后会更新 AgentState 的 modified_files/original_file_contents，"
                "供后续统一 diff 生成。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "scene_id": {"type": "string", "description": "目标场景 UUID"},
                "file_path": {
                    "type": "string",
                    "description": "ScriptVFS 路径，如 scenes/E03-S005.txt",
                },
                "target_dimensions": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(_INVESTMENT_DIM_KEYS),
                    },
                    "description": (
                        "本场改写目标维度（1-3 个）。可选值："
                        "hook=抓人力、archetype=模板力、payoff=兑现力、"
                        "monetization=变现力、producibility=可生成力。"
                    ),
                },
                "expected_changes": {
                    "type": "string",
                    "description": "本场预期改写方向（可选）",
                },
                "script_id": {
                    "type": "string",
                    "description": "剧本 UUID；缺省时使用当前会话绑定剧本",
                },
            },
            "required": ["target_dimensions"],
        }

    async def execute(self, agent_state: Any, parameters: Dict[str, Any]) -> ToolResult:
        params = parameters or {}
        dims = _normalize_investment_dim_keys(params.get("target_dimensions"))
        if not dims:
            return ToolResult(
                success=False,
                error=(
                    "target_dimensions must be a non-empty subset of "
                    "hook/archetype/payoff/monetization/producibility"
                ),
                summary="缺 target_dimensions",
            )

        raw_scene_id = str(params.get("scene_id") or "").strip()
        raw_file_path = str(params.get("file_path") or "").strip()
        if not raw_scene_id and not raw_file_path:
            return ToolResult(
                success=False,
                error="either scene_id or file_path is required",
                summary="缺 scene_id/file_path",
            )

        try:
            script_id = _resolve_script_id(agent_state, params)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), summary="剧本作用域不一致")
        if not script_id:
            return _missing_script_id()

        try:
            vfs = ScriptVFS(script_id=script_id)
        except ScriptVFSError as exc:
            return ToolResult(success=False, error=str(exc), summary="ScriptVFS 初始化失败")

        try:
            if raw_scene_id and raw_file_path:
                sid_from_path = vfs.resolve_scene_id(raw_file_path)
                if sid_from_path != raw_scene_id:
                    return ToolResult(
                        success=False,
                        error="scene_id does not match file_path in current script scope",
                        summary="scene_id/file_path 不一致",
                    )
                scene_id = raw_scene_id
                scene_path = vfs.resolve_file_path(scene_id)
            elif raw_scene_id:
                scene_id = raw_scene_id
                scene_path = vfs.resolve_file_path(scene_id)
            else:
                scene_id = vfs.resolve_scene_id(raw_file_path)
                scene_path = vfs.coerce_file_path(raw_file_path)
        except ScriptVFSError as exc:
            return ToolResult(success=False, error=str(exc), summary="目标场景定位失败")

        expected_changes = str(params.get("expected_changes") or "").strip()

        from service.script_tools.llm_caller import LlmCaller, ScoreLLMError
        from service.script_tools.rewrite_chain import execute_plan_step

        caller = LlmCaller()
        try:
            result = await execute_plan_step(
                script_id=script_id,
                scene_id=scene_id,
                target_dimensions=dims,
                expected_changes=expected_changes,
                caller=caller,
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), summary="改写参数错误")
        except ScoreLLMError as exc:
            logger.warning("rewrite_scene_tool failed for scene %s: %s", scene_id, exc)
            return ToolResult(success=False, error=f"LLM error: {exc}", summary="改写 LLM 失败")

        try:
            _persist_scene_text(
                scene_id=scene_id,
                new_text=result.rewritten_text,
                expected_script_id=script_id,
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), summary="改写持久化失败")

        operation_ref: Optional[str] = None
        user_id = _coerce_optional_int(getattr(agent_state, "user_id", None))
        if user_id is None:
            logger.warning(
                "rewrite_scene_tool skip record_rewrite_op: missing user_id (script=%s scene=%s)",
                script_id,
                scene_id,
            )
        else:
            from service import script_operation_service

            try:
                op_record = script_operation_service.record_rewrite_op(
                    script_id=script_id,
                    user_id=user_id,
                    scene_id=scene_id,
                    target_dimension="/".join(dims) if dims else "general",
                    issue=expected_changes,
                    original_text=result.original_text,
                    rewritten_text=result.rewritten_text,
                    rationale=result.rationale or "",
                )
                operation_ref = str(op_record.get("operation_id") or "").strip() or None
            except script_operation_service.OperationError as exc:
                logger.warning(
                    "rewrite_scene_tool record_rewrite_op failed (non-blocking): %s",
                    exc,
                )

        _mutate_agent_state_for_scene(
            agent_state=agent_state,
            scene_path=scene_path,
            scene_id=scene_id,
            original_text=result.original_text,
        )

        return ToolResult(
            success=True,
            data={
                "scene_id": scene_id,
                "file_path": scene_path,
                "scene_label": result.scene_label,
                "target_dimensions": list(result.target_dimensions),
                "rationale": result.rationale,
                "expected_changes": expected_changes,
                "operation_id": operation_ref,
                "original_chars": len(result.original_text),
                "rewritten_chars": len(result.rewritten_text),
            },
            summary=(
                f"改写完成：{scene_path}（dims={'/'.join(result.target_dimensions)}）"
                f" {len(result.original_text)}→{len(result.rewritten_text)} 字"
            ),
        )


def _persist_scene_text(
    *,
    scene_id: str,
    new_text: str,
    expected_script_id: str,
) -> None:
    """把改写后的文本写回 scriptlens.scenes.text（带 script_id 二次校验，防越权）。"""
    from utils.database import engine

    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE scriptlens.scenes
                   SET text = :txt
                 WHERE id = :sid
                   AND script_id = :script_id
                """
            ),
            {"txt": new_text, "sid": scene_id, "script_id": expected_script_id},
        )
        if result.rowcount == 0:
            raise ValueError(
                f"persist failed: scene {scene_id} not found in script {expected_script_id}"
            )
