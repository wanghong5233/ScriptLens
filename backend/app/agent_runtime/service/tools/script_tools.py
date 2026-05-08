"""ScriptLens 4 个剧本专属 ReAct 工具。

来源：reuse-matrix §5 / §5.2。继承 doc_studio `BaseTool` 自动获得：
- 预算守卫（agent_service ReAct 主循环里的 `tool_budget`）
- 连续失败计数（`error_handler.async_error_guard`）
- `reply_to_user` 兜底（达预算时 ReAct 主循环自动收尾）

为什么薄包装而不在工具里重写业务逻辑：
- 5 维评分 / RAG 检索 / 场景查询 在 D2-4/D2-5a 已经实装为 service 层确定性流水线
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
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from .base_tool import BaseTool, ToolResult

logger = logging.getLogger(__name__)


_DIMENSIONS = ("story", "character", "concept", "emotion", "pacing", "compliance")


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
                "复核或重新计算单一维度评分（阅文五力之一：story / character / concept / "
                "emotion / pacing；或独立合规审核：compliance）。"
                "返回 score / level / reason 以及若干证据场景的 ID。"
                "用户在 chat 里追问「为什么人物力给 4 分」「再核一下合规」时调用本工具。"
                "注意：完整五力报告由后台流水线一次性生成；本工具只跑单一维度。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "dimension": {
                    "type": "string",
                    "enum": list(_DIMENSIONS),
                    "description": "5 维之一",
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

        evi_count = len(result.get("evidence_scene_ids") or [])
        score = result.get("score")
        level = result.get("level")
        if score is None:
            # rubric §6 证据不足：用户能看见状态，不伪装成成功打分
            summary = f"{dimension}: 证据不足，未给分（{evi_count} 条证据场景，原因：{result.get('reason') or '未说明'}）"
        else:
            summary = f"{dimension}: {score}/10 ({level})，{evi_count} 条证据场景"
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

维度对应的优化方向（取一即可，不要堆砌；阅文五力 docs/08 §3）：
- story    : 强化主线推进 / 补一个反转或打脸节点，回应前情已埋的伏笔
- character: 给关键决策补一段可追溯的因果（用前情人物关系 / 已发生事件做铺垫）
- concept  : 把题材标识 / 核心卖点的钩子提前到本场前 1/3，删冗余铺垫
- emotion  : 加一个情感钩子或爽点（CP 进展 / 反派败落 / 逆袭）放大情绪密度
- pacing   : 删冗余对白 / 重复信息，节奏前推；首场 20 段内出冲突

输出严格 JSON（不要 markdown 代码块包裹）：
{{
  "rewritten_excerpt": "<改写后的整段场景文本>",
  "rationale": "<≤150 字，解释你具体做了哪几处改动、用了哪些前情铺垫、为什么这样改能在 {target_dimension} 维度提分>"
}}"""


class ProposeRewriteTool(BaseTool):
    """对低分场景做定向改写。"""

    def __init__(self) -> None:
        super().__init__(
            name="propose_rewrite_tool",
            description=(
                "对剧本中某一场做定向改写，按指定维度优化（5 维之一）。"
                "工具内部会自动加载整剧上下文（人物表 + 前后场摘要 + 整剧概要）"
                "再让 LLM 改写，保证新文本与剧情主线 / 已有人物 / 后续走向自洽。"
                "返回原文 / 改写版 / unified diff / 改动说明。"
                "用户问「把第 5 场改紧凑」「这场动机不成立怎么改」时调用本工具。"
                "改写仅产出单场新文本，不写入文件、不修改前后场，仅返回建议供前端审阅。"
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
                    "enum": list(_DIMENSIONS),
                    "description": "目标优化维度",
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
        if target_dim not in _DIMENSIONS:
            return ToolResult(
                success=False,
                error=f"target_dimension must be one of {_DIMENSIONS}",
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
# 5. PropDimensionRewriteTool —— 全剧维度改写（plan-then-execute）
# ============================================================
#
# docs/10-rewrite-agent.md §5 实装。两阶段工具：
#
#   mode='plan'    → 调 rewrite_chain.propose_plan 输出全剧 plan tree（不写 DB），
#                    交给前端 RewritePlanCard 渲染让用户审 / 勾选；
#   mode='execute' → 拿 plan_steps（用户勾选过的子集）调 rewrite_chain.execute_plan_step
#                    逐场改写，UPDATE scriptlens.scenes.text + mutate
#                    state.modified_files / state.original_file_contents，
#                    Agent 收尾时 _generate_file_diffs 自动产 file_diffs 给
#                    AgentDiffReview（剧本场景透明走 LaTeX 同款 diff 链路）。
#
# 「LaTeX 工具直接写磁盘 → accept all 关闭 modal」语义在剧本下统一为
# 「改写工具直接 UPDATE DB → accept all 关闭 modal」。reject 路径走前端
# updateFileContent → PUT scenes/{id}/content 写 DB（与 LaTeX 写磁盘对称）。


class PropDimensionRewriteTool(BaseTool):
    """全剧维度级改写：plan / execute 双模式。"""

    def __init__(self) -> None:
        super().__init__(
            name="propose_dimension_rewrite_tool",
            description=(
                "对当前剧本做全剧改写。两阶段调用：\n"
                "  1. mode='plan'：基于五力评分，输出全剧改写计划（plan tree，每条含 "
                "scene_id / target_dimensions / rationale / expected_changes）。**不修改任何场**，"
                "供用户审阅与勾选。返回 data.rewrite_plan。\n"
                "  2. mode='execute'：拿前一步 plan_steps（用户勾选过的子集），逐场调 LLM "
                "改写 → UPDATE scriptlens.scenes.text → 触发 file_diffs 给 AgentDiffReview。"
                "返回 data.executed_scenes / data.failed_scenes。\n"
                "用户消息含 <TASK_META>{kind:'fulltext_rewrite', dimensions, mode}</TASK_META> 时直调。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["plan", "execute"],
                    "description": "plan = 出 plan tree 不改文；execute = 按 plan_steps 改写写库",
                },
                "dimensions": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["story", "character", "concept", "emotion", "pacing"],
                    },
                    "description": "目标维度（1-5 个，阅文五力子集；compliance 不参与改写）",
                },
                "plan_steps": {
                    "type": "array",
                    "description": (
                        "execute 模式必传：前端从 plan tree 勾选后传回。"
                        "每条形如 {scene_id, target_dimensions, expected_changes}。"
                        "缺省时 execute 模式会重跑 propose_plan 取全部 step。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "scene_id": {"type": "string"},
                            "target_dimensions": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "expected_changes": {"type": "string"},
                        },
                        "required": ["scene_id", "target_dimensions"],
                    },
                },
                "script_id": {
                    "type": "string",
                    "description": "剧本 UUID；缺省时使用当前会话绑定的剧本",
                },
            },
            "required": ["mode", "dimensions"],
        }

    async def execute(self, agent_state: Any, parameters: Dict[str, Any]) -> ToolResult:
        params = parameters or {}
        mode = str(params.get("mode") or "").strip()
        if mode not in ("plan", "execute"):
            return ToolResult(
                success=False,
                error=f"mode must be 'plan' or 'execute', got {mode!r}",
                summary="非法 mode",
            )

        dims_raw = params.get("dimensions") or []
        dims = [d for d in dims_raw if isinstance(d, str) and d in _DIMENSIONS and d != "compliance"]
        if not dims:
            return ToolResult(
                success=False,
                error="dimensions must be a non-empty subset of story/character/concept/emotion/pacing",
                summary="缺 dimensions",
            )

        try:
            script_id = _resolve_script_id(agent_state, params)
        except ValueError as e:
            return ToolResult(success=False, error=str(e), summary="剧本作用域不一致")
        if not script_id:
            return _missing_script_id()

        from service.script_tools.llm_caller import LlmCaller, ScoreLLMError
        from service.script_tools.rewrite_chain import (
            execute_plan_step,
            propose_plan,
            select_target_scenes,
        )

        caller = LlmCaller()

        if mode == "plan":
            try:
                scenes = await asyncio.to_thread(
                    select_target_scenes,
                    script_id=script_id,
                    dimensions=dims,
                )
                plan = await propose_plan(
                    script_id=script_id,
                    dimensions=dims,
                    scenes=scenes,
                    caller=caller,
                )
            except ValueError as e:
                return ToolResult(success=False, error=str(e), summary="plan 参数错误")
            except ScoreLLMError as e:
                logger.warning("propose_dimension_rewrite_tool plan failed: %s", e)
                return ToolResult(success=False, error=f"LLM error: {e}", summary="plan LLM 失败")
            except Exception as e:
                logger.error("propose_dimension_rewrite_tool plan unexpected: %s", e, exc_info=True)
                return ToolResult(success=False, error=str(e), summary="plan 异常")

            plan_dict = plan.to_dict()
            step_count = len(plan_dict["steps"])
            if step_count == 0:
                summary = "全剧 plan：所选维度无 score<7 的明显短板场，无须改写"
            else:
                summary = f"全剧 plan 完成：共 {step_count} 场建议改写（dims={'/'.join(dims)}）"
            return ToolResult(
                success=True,
                data={
                    "mode": "plan",
                    "rewrite_plan": plan_dict,
                    "script_id": script_id,
                },
                summary=summary,
            )

        # mode == 'execute'
        plan_steps_raw = params.get("plan_steps") or []
        plan_steps: List[Dict[str, Any]] = []
        if isinstance(plan_steps_raw, list) and plan_steps_raw:
            for raw in plan_steps_raw:
                if not isinstance(raw, dict):
                    continue
                sid = str(raw.get("scene_id") or "").strip()
                tdims = [
                    d for d in (raw.get("target_dimensions") or [])
                    if isinstance(d, str) and d in dims
                ]
                if not sid or not tdims:
                    continue
                plan_steps.append(
                    {
                        "scene_id": sid,
                        "target_dimensions": tdims,
                        "expected_changes": str(raw.get("expected_changes") or ""),
                    }
                )

        if not plan_steps:
            # 兜底：用户没传 plan_steps（例如 chat 直接说"全剧改写吧"），重跑 plan 取全部
            try:
                plan = await propose_plan(
                    script_id=script_id,
                    dimensions=dims,
                    caller=caller,
                )
            except ScoreLLMError as e:
                return ToolResult(success=False, error=f"LLM error: {e}", summary="plan 兜底失败")
            for step in plan.steps:
                plan_steps.append(
                    {
                        "scene_id": step.scene_id,
                        "target_dimensions": list(step.target_dimensions),
                        "expected_changes": step.expected_changes,
                    }
                )

        if not plan_steps:
            return ToolResult(
                success=False,
                error="execute 阶段没有可改写的场（plan_steps 空且兜底 plan 也无短板场）",
                summary="无可改写场",
            )

        executed: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []

        for step in plan_steps:
            sid = step["scene_id"]
            tdims = step["target_dimensions"]
            expected = step.get("expected_changes") or ""
            try:
                result = await execute_plan_step(
                    script_id=script_id,
                    scene_id=sid,
                    target_dimensions=tdims,
                    expected_changes=expected,
                    caller=caller,
                )
            except (ValueError, ScoreLLMError) as e:
                logger.warning("execute_plan_step failed for scene %s: %s", sid, e)
                failed.append({"scene_id": sid, "error": str(e)})
                continue
            except Exception as e:
                logger.error("execute_plan_step unexpected for scene %s: %s", sid, e, exc_info=True)
                failed.append({"scene_id": sid, "error": str(e)})
                continue

            try:
                _persist_scene_text(
                    scene_id=sid,
                    new_text=result.rewritten_text,
                    expected_script_id=script_id,
                )
            except Exception as e:
                logger.error("persist scene %s failed: %s", sid, e, exc_info=True)
                failed.append({"scene_id": sid, "error": f"persist failed: {e}"})
                continue

            # mutate agent_state：让 _generate_file_diffs 走透明 diff 链路
            # state.modified_files 装 scene_id（UUID 形态），state.original_file_contents
            # 装改写前文本——后端 _generate_file_diffs 会做剧本工作区分支识别
            try:
                modified_files = getattr(agent_state, "modified_files", None)
                if modified_files is None:
                    modified_files = set()
                    setattr(agent_state, "modified_files", modified_files)
                modified_files.add(sid)

                originals = getattr(agent_state, "original_file_contents", None)
                if originals is None:
                    originals = {}
                    setattr(agent_state, "original_file_contents", originals)
                # 只在第一次见到该 scene 时记 original，后续累积改写不能覆盖
                if sid not in originals:
                    originals[sid] = result.original_text
            except Exception as e:
                logger.warning("mutate agent_state failed for %s: %s (continuing)", sid, e)

            executed.append(
                {
                    "scene_id": sid,
                    "scene_label": result.scene_label,
                    "target_dimensions": list(result.target_dimensions),
                    "rationale": result.rationale,
                    "original_chars": len(result.original_text),
                    "rewritten_chars": len(result.rewritten_text),
                }
            )

        ok = len(executed)
        bad = len(failed)
        if ok == 0:
            return ToolResult(
                success=False,
                error=f"全部 {bad} 场改写均失败",
                data={"mode": "execute", "executed_scenes": [], "failed_scenes": failed},
                summary="全剧改写失败",
            )
        summary = (
            f"改写完成：成功 {ok} 场"
            + (f" / 失败 {bad} 场" if bad else "")
            + f"（dims={'/'.join(dims)}）"
        )
        return ToolResult(
            success=True,
            data={
                "mode": "execute",
                "dimensions": dims,
                "executed_scenes": executed,
                "failed_scenes": failed,
                "script_id": script_id,
            },
            summary=summary,
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
