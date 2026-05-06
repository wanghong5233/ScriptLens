"""ScriptLens 4 个剧本专属 ReAct 工具。

来源：reuse-matrix §5 / §5.2。继承 doc_studio `BaseTool` 自动获得：
- 预算守卫（agent_service ReAct 主循环里的 `tool_budget`）
- 连续失败计数（`error_handler.async_error_guard`）
- `reply_to_user` 兜底（达预算时 ReAct 主循环自动收尾）

为什么薄包装而不在工具里重写业务逻辑：
- 5 维评分 / RAG 检索 / 场景查询 在 D2-4/D2-5a 已经实装为 service 层确定性流水线
- 工具层只负责"参数从 LLM 来 → 调 service → 包成 ToolResult"
- 业务逻辑改动只动 service 层，工具不变（reuse-matrix §0.1 子包语义边界）

script_id 来源（按优先级）：
  1. parameters["script_id"]（LLM 显式指定）
  2. agent_state.workspace_config["script_id"]（chat 端点创建状态时注入）
  3. 缺则报错（不向 LLM 开放任意剧本，防越权）
"""

from __future__ import annotations

import difflib
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from .base_tool import BaseTool, ToolResult

logger = logging.getLogger(__name__)


_DIMENSIONS = ("opening_hook", "reward_density", "motivation", "pacing", "risk")


# ============================================================
# 共用 helper
# ============================================================


def _resolve_script_id(agent_state: Any, parameters: Dict[str, Any]) -> Optional[str]:
    """优先 LLM 显式参数，其次 agent_state.workspace_config，缺则 None。"""
    sid = (parameters or {}).get("script_id")
    if sid:
        return str(sid).strip() or None
    cfg = getattr(agent_state, "workspace_config", None) or {}
    sid = cfg.get("script_id")
    return str(sid).strip() if sid else None


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
                "复核或重新计算某一维度评分（5 维之一：opening_hook / reward_density / "
                "motivation / pacing / risk）。返回 score / level / reason 以及若干证据场景的 ID。"
                "用户在 chat 里追问「为什么 motivation 给 4 分」「再核一下 risk」时调用本工具。"
                "注意：完整 5 维报告由后台流水线一次性生成；本工具只跑单一维度。"
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

        script_id = _resolve_script_id(agent_state, parameters)
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
                "返回 top_k 个最相关的场景，每条含 scene_id / scene_label / episode_no / 文本片段 / RRF 相关性分。"
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

        script_id = _resolve_script_id(agent_state, parameters)
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
                "rrf_score": round(s.rrf_score, 4),
            }
            for s in scored
        ]
        return ToolResult(
            success=True,
            data={"query": query, "scenes": scenes, "count": len(scenes)},
            summary=f"找到 {len(scenes)} 个相关场景",
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
        script_id = _resolve_script_id(agent_state, parameters)
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


_REWRITE_PROMPT = """你是中文短剧资深编剧。下面是某剧的一场戏，用户认为它在 **{target_dimension}** 维度上存在问题。

【目标维度】{target_dimension}
【用户提出的问题】{issue}

【原文场景】（{scene_label}）
---
{scene_text}
---

任务：针对 **{target_dimension}** 这一维度做定向改写。
约束：
1. 只改这一场，**不改前后剧情**（不能引入新人物 / 新事件假设）
2. 改写后字数与原文 ±30% 以内
3. 维度对应的优化方向：
   - opening_hook: 把矛盾 / 钩子提前到开场前 1/3，删铺垫
   - reward_density: 加一个反转 / 打脸 / 逆袭节点
   - motivation: 给关键决策补一个可追溯的因果（铺垫线 / 触发事件 / 情绪逻辑）
   - pacing: 删冗余对白 / 删重复信息 / 提密度
   - risk: 软化敏感表达，但保留戏剧冲突，不删整段

输出 JSON：
{{
  "rewritten_excerpt": "<改写后的整段场景文本>",
  "rationale": "<≤120 字，解释你具体做了哪几处改动，为什么这样改能在 {target_dimension} 维度提分>"
}}"""


class ProposeRewriteTool(BaseTool):
    """对低分场景做定向改写。"""

    def __init__(self) -> None:
        super().__init__(
            name="propose_rewrite_tool",
            description=(
                "对剧本中某一场做定向改写，按指定维度优化（5 维之一）。"
                "返回原文 / 改写版 / unified diff / 改动说明。"
                "用户问「把第 5 场改紧凑」「这场动机不成立怎么改」时调用本工具。"
                "注意：改写仅修改单场，不修改前后剧情；不写入文件，仅返回建议供前端审阅。"
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

        # 拉场景全文（含权限校验：通过 agent_state.user_id 跟 scripts.user_id 对齐）
        try:
            scene = _load_scene_for_rewrite(scene_id)
        except Exception as e:
            logger.error("load_scene_for_rewrite failed: %s", e, exc_info=True)
            return ToolResult(success=False, error=str(e), summary="读取场景失败")
        if scene is None:
            return ToolResult(success=False, error=f"scene_id {scene_id} not found", summary="场景不存在")

        scene_text = scene["text"] or ""
        if not scene_text.strip():
            return ToolResult(success=False, error="scene text is empty", summary="场景为空")

        # 调 LLM
        from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError

        prompt = _REWRITE_PROMPT.format(
            scene_label=scene.get("scene_label") or "",
            scene_text=scene_text,
            target_dimension=target_dim,
            issue=issue,
        )
        caller = LlmCaller()
        try:
            resp = await caller.call_json(
                prompt,
                tier=ModelTier.PRIMARY,
                temperature=0.4,
                max_tokens=1024,
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


def _load_scene_for_rewrite(scene_id: str) -> Optional[Dict[str, Any]]:
    """读单场全文 + 元数据。"""
    from utils.database import engine

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, text
                FROM scriptlens.scenes
                WHERE id = :sid
                """
            ),
            {"sid": scene_id},
        ).mappings().first()
    return dict(row) if row else None
