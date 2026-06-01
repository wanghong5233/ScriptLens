"""LLM-first 全剧改写计划链路。

入口 ``propose_plan``：根据投资决策评分的改进建议或整体诊断，让 LLM 自主
从全部场次里选 1~12 场出 plan tree（每条 plan 含 scene_id、target_dimensions、
rationale、expected_changes）。

输入路径（任一非空即可触发）：
  * ``improvement_brief``：单条改进建议上下文（前端「按此条改稿」按钮）
  * ``diagnostic_brief``：整剧诊断上下文（前端「按本次诊断改稿」按钮）
  * ``dimension_keys``：目标维度键列表

LLM 工作流：剧本概要 + 评分快照 + 场次清单 + 改进/诊断上下文 → 严格 JSON 输出
（``_LlmPlanResponse``）→ scene_id 存在性二次校验 → 返回 ``RewritePlan``。
模型幻觉的 scene_id 直接拒收，全空 plan 抛 ``ScoreLLMError``。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)

# 投资决策评分的目标维度键，保持与 service/scoring/framework.py 同步。
_INVESTMENT_DIM_KEYS = ("hook", "archetype", "payoff", "monetization", "producibility")
_INVESTMENT_DIM_LABELS_ZH = {
    "hook": "抓人力",
    "archetype": "模板力",
    "payoff": "兑现力",
    "monetization": "变现力",
    "producibility": "可生成力",
}

_MAX_PLAN_STEPS = 12
_CONTEXT_WINDOW = 2
_SCENE_DIGEST_CHARS = 180
_CHARACTERS_TOP_N = 12

# LLM-first propose_plan 上下文摘要参数。
_PLAN_SCENE_DIGEST_CHARS = 110  # 每场摘要长度（单字），太长会撑爆 prompt
_PLAN_MAX_SCENES_IN_PROMPT = 40  # 场次清单最多塞 N 场进 prompt，多了 LLM 注意力分散
_PLAN_OVERVIEW_MAX_CHARS = 600  # 整剧概要最大长度
_PLAN_TEMPERATURE = 0.3


# ============================================================
# LLM plan schema —— 由 LlmCaller.call_json(validate_with=...) 强校验
# ============================================================


class _LlmPlanStep(BaseModel):
    """LLM 返回的单条 plan step 期望结构。

    设计原则：让 LLM 自由表达「针对哪场、为什么、改什么」，但 scene_id 必须
    出现在 prompt 给出的候选清单里；本层会再做存在性校验。
    """

    scene_id: str = Field(..., min_length=1)
    target_dimensions: List[str] = Field(default_factory=list)
    rationale: str = Field(..., min_length=1, max_length=240)
    expected_changes: str = Field(..., min_length=1, max_length=300)

    @field_validator("target_dimensions", mode="before")
    @classmethod
    def _coerce_target_dimensions(cls, v: Any) -> List[str]:
        # LLM 在只选 1 个维度时常把 target_dimensions 退化成 scalar 字符串，
        # 这里把 "producibility" / "hook, payoff" 都归一成 list，再由
        # _normalize_target_dimensions 做枚举裁剪。
        if v is None:
            return []
        if isinstance(v, str):
            parts = [p.strip() for p in v.replace("、", ",").replace("/", ",").split(",")]
            return [p for p in parts if p]
        if isinstance(v, list):
            return v
        return [str(v)]


class _LlmPlanResponse(BaseModel):
    """LLM plan 总体结构。

    overall_summary：一句话概括本计划意图，会原样回传给前端 reply。
    steps：1-12 条 plan step，按 LLM 自主排序的优先级。
    """

    overall_summary: str = Field(..., min_length=1, max_length=400)
    steps: List[_LlmPlanStep] = Field(default_factory=list, max_length=_MAX_PLAN_STEPS)


@dataclass
class PlanStep:
    scene_id: str
    scene_label: str
    episode_no: Optional[int]
    scene_no: Optional[str]
    target_dimensions: List[str]
    rationale: str
    expected_changes: str
    current_excerpt: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "scene_label": self.scene_label,
            "episode_no": self.episode_no,
            "scene_no": self.scene_no,
            "target_dimensions": list(self.target_dimensions),
            "rationale": self.rationale,
            "expected_changes": self.expected_changes,
            "current_excerpt": self.current_excerpt,
        }


@dataclass
class RewritePlan:
    dimensions: List[str]
    overall_summary: str
    steps: List[PlanStep] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimensions": list(self.dimensions),
            "overall_summary": self.overall_summary,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class RewriteResult:
    scene_id: str
    scene_label: str
    target_dimensions: List[str]
    original_text: str
    rewritten_text: str
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "scene_label": self.scene_label,
            "target_dimensions": list(self.target_dimensions),
            "original_text": self.original_text,
            "rewritten_text": self.rewritten_text,
            "rationale": self.rationale,
        }


async def propose_plan(
    *,
    script_id: str,
    dimension_keys: Optional[Sequence[str]] = None,
    improvement_brief: Optional[Mapping[str, Any]] = None,
    diagnostic_brief: Optional[Mapping[str, Any]] = None,
    max_steps: int = _MAX_PLAN_STEPS,
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> RewritePlan:
    """生成全剧改写 plan（不写库）。

    参数（任一非空即可触发 LLM 选场）：

    - ``improvement_brief``：单条改进建议上下文。
    - ``diagnostic_brief``：整剧诊断上下文。
    - ``dimension_keys``：目标维度键列表。

    LLM 调用失败 / 返回零有效 step 时抛 ``ScoreLLMError``，由 agent 外层重试。
    """

    dim_keys = _normalize_investment_dim_keys(dimension_keys)

    if not (improvement_brief or diagnostic_brief or dim_keys):
        raise ValueError(
            "propose_plan 需要 dimension_keys / improvement_brief / "
            f"diagnostic_brief 之一非空：dimension_keys={dimension_keys!r} "
            f"improvement_brief={'set' if improvement_brief else 'none'} "
            f"diagnostic_brief={'set' if diagnostic_brief else 'none'}"
        )

    if caller is None:
        caller = LlmCaller()

    max_steps = max(1, min(int(max_steps or _MAX_PLAN_STEPS), _MAX_PLAN_STEPS))

    script_overview = _load_script_overview(script_id, engine=engine)
    verdict_snapshot = _load_latest_verdict_snapshot(script_id=script_id, engine=engine)
    scene_catalog = _load_scene_catalog(script_id=script_id, engine=engine)
    if not scene_catalog:
        # 剧本无场次：直接给空 plan，避免 LLM 凭空想 scene_id。
        return RewritePlan(
            dimensions=dim_keys,
            overall_summary="剧本暂无场次，无法生成改写计划。",
            steps=[],
        )

    valid_scene_ids = {row["scene_id"] for row in scene_catalog}

    prompt = _build_plan_prompt(
        script_overview=script_overview,
        verdict_snapshot=verdict_snapshot,
        scene_catalog=scene_catalog,
        improvement_brief=improvement_brief,
        diagnostic_brief=diagnostic_brief,
        dimension_keys=dim_keys,
        max_steps=max_steps,
    )

    try:
        resp = await caller.call_json(
            prompt,
            tier=ModelTier.PRIMARY,
            temperature=_PLAN_TEMPERATURE,
            max_tokens=TokenBudget.REWRITE_EXCERPT,
            validate_with=_LlmPlanResponse,
            chain_name="rewrite_plan",
        )
    except ScoreLLMError:
        logger.warning(
            "propose_plan LLM failed script_id=%s dim_keys=%s",
            script_id,
            dim_keys,
        )
        raise

    parsed: Any = resp.parsed
    if not isinstance(parsed, dict):
        raise ScoreLLMError(
            f"propose_plan: LLM returned non-dict parsed payload (type={type(parsed).__name__})"
        )

    overall_summary = str(parsed.get("overall_summary") or "").strip()
    raw_steps = parsed.get("steps") or []

    plan_steps: list[PlanStep] = []
    rejected = 0
    scene_index = {row["scene_id"]: row for row in scene_catalog}
    for item in raw_steps:
        if not isinstance(item, dict):
            rejected += 1
            continue
        scene_id = str(item.get("scene_id") or "").strip()
        if scene_id not in valid_scene_ids:
            # 模型幻觉的 scene_id：丢弃但记日志，不让一颗坏 step 拖垮整 plan
            rejected += 1
            logger.warning(
                "propose_plan dropped step with unknown scene_id=%s (script=%s)",
                scene_id or "<empty>",
                script_id,
            )
            continue
        target_dims = _normalize_target_dimensions(
            item.get("target_dimensions") or [],
            fallback_dim_keys=dim_keys,
        )
        scene_row = scene_index[scene_id]
        plan_steps.append(
            PlanStep(
                scene_id=scene_id,
                scene_label=str(scene_row.get("scene_label") or ""),
                episode_no=scene_row.get("episode_no"),
                scene_no=(
                    str(scene_row.get("scene_no"))
                    if scene_row.get("scene_no") is not None
                    else None
                ),
                target_dimensions=target_dims,
                rationale=_truncate(str(item.get("rationale") or "").strip(), 200),
                expected_changes=_truncate(str(item.get("expected_changes") or "").strip(), 240),
                current_excerpt=str(scene_row.get("digest") or ""),
            )
        )
        if len(plan_steps) >= max_steps:
            break

    if not plan_steps:
        # LLM 完整空 plan 视为失败，让 agent 重试或上报；不要给前端伪“成功”。
        raise ScoreLLMError(
            "propose_plan: LLM returned no valid steps "
            f"(raw_steps={len(raw_steps)} rejected={rejected})"
        )

    if not overall_summary:
        overall_summary = (
            f"基于本次改进建议生成 {len(plan_steps)} 个改写步骤；"
            "优先解决最影响投资决策评分的场次。"
        )

    return RewritePlan(
        dimensions=dim_keys,
        overall_summary=overall_summary,
        steps=plan_steps,
    )


# ============================================================
# helpers: input normalization
# ============================================================


def _normalize_investment_dim_keys(raw: Optional[Sequence[str]]) -> List[str]:
    if not raw:
        return []
    out: list[str] = []
    for item in raw:
        key = str(item or "").strip().lower()
        if key in _INVESTMENT_DIM_KEYS and key not in out:
            out.append(key)
    return out


def _normalize_target_dimensions(
    raw: Any,
    *,
    fallback_dim_keys: List[str],
) -> List[str]:
    """归一 LLM 返回的 target_dimensions：

    - 仅保留投资决策评分的维度键（hook/archetype/payoff/monetization/producibility）
    - 容忍 LLM 把单维度退化成 scalar 字符串：``"producibility"`` 与
      ``"hook, payoff"`` 都会被拆成数组。
    - 不识别的字符串丢弃；为空时回填本次请求的 dim_keys
    """

    items: list[str]
    if raw is None:
        items = []
    elif isinstance(raw, list):
        items = [str(x or "") for x in raw]
    elif isinstance(raw, str):
        # 容忍 LLM 退化输出（含中英文常见分隔符）
        items = [raw.replace("、", ",").replace("/", ",")]
        items = [seg for piece in items for seg in piece.split(",")]
    else:
        items = [str(raw)]

    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key in _INVESTMENT_DIM_KEYS and key not in out:
            out.append(key)
    if out:
        return out
    return list(fallback_dim_keys)


# ============================================================
# helpers: context loaders
# ============================================================


def _load_scene_catalog(*, script_id: str, engine: Engine) -> List[Dict[str, Any]]:
    """加载剧本所有场次的轻量摘要，给 plan LLM 当候选清单。

    摘要规则：
    - 按 episode_no / scene_no / start_line 排序，保留原有故事顺序；
    - text 截断到 ``_PLAN_SCENE_DIGEST_CHARS``，去掉换行；
    - characters 数组（可能为 PG ARRAY）展开为顿号分隔；
    - 超过 ``_PLAN_MAX_SCENES_IN_PROMPT`` 时只取前 N 场（保护 prompt 长度）。
    """

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS scene_id, episode_no, scene_no, scene_label,
                       characters, LEFT(text, :digest_chars) AS digest
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                """
            ),
            {"sid": script_id, "digest_chars": _PLAN_SCENE_DIGEST_CHARS},
        ).mappings().all()

    catalog: list[dict[str, Any]] = []
    for row in rows:
        chars_raw = row.get("characters")
        if isinstance(chars_raw, (list, tuple)):
            chars_text = "、".join(str(c).strip() for c in chars_raw if str(c).strip())[:60]
        else:
            chars_text = ""
        digest = str(row.get("digest") or "").replace("\r\n", " ").replace("\n", " ").strip()
        catalog.append(
            {
                "scene_id": str(row.get("scene_id")),
                "episode_no": row.get("episode_no"),
                "scene_no": row.get("scene_no"),
                "scene_label": str(row.get("scene_label") or ""),
                "characters": chars_text,
                "digest": digest,
            }
        )
    if len(catalog) > _PLAN_MAX_SCENES_IN_PROMPT:
        # 采样策略：均匀采样保留首尾场次，避免只看片头丢失全局上下文
        catalog = _evenly_sample(catalog, _PLAN_MAX_SCENES_IN_PROMPT)
    return catalog


def _evenly_sample(items: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """长场次清单的均匀采样：保留首尾 + 等距取中间，保证全局视角。"""

    if n <= 0 or len(items) <= n:
        return list(items)
    if n == 1:
        return [items[0]]
    step = (len(items) - 1) / (n - 1)
    picked: list[Dict[str, Any]] = []
    seen_idx: set[int] = set()
    for i in range(n):
        idx = int(round(i * step))
        if idx in seen_idx:
            continue
        seen_idx.add(idx)
        picked.append(items[idx])
    return picked


def _load_latest_verdict_snapshot(
    *, script_id: str, engine: Engine
) -> Optional[Dict[str, Any]]:
    """从 reports.report_json 加载最近一次评估快照（label/score/reason/top_improvements）。

    没有报告或字段缺失时返回 None，由 prompt 拼装层自行兜底。
    """

    with engine.connect() as conn:
        row = conn.execute(
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
    if row is None:
        return None
    payload = row.get("report_json")
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else None
    snapshot: dict[str, Any] = {
        "investment_score": payload.get("investment_score"),
        "verdict_label": (verdict or {}).get("label"),
        "verdict_reason": (verdict or {}).get("one_sentence_reason")
        or (verdict or {}).get("reason"),
        "top_improvements": payload.get("top_improvements") or [],
    }
    if all(v in (None, "", []) for v in snapshot.values()):
        return None
    return snapshot


# ============================================================
# helpers: prompt building
# ============================================================


_PLAN_SYSTEM_MESSAGE = (
    "你是中文短剧投资决策助理。短剧主要面向抖音/快手等竖屏短视频平台投放。"
    "你的任务是基于剧本的投资决策评分（hook/抓人力、archetype/模板力、"
    "payoff/兑现力、monetization/变现力、producibility/可生成力）与具体的"
    "改进建议，从剧本场次清单里选出最该改写的若干场，输出严格 JSON 的 plan。"
)


def _build_plan_prompt(
    *,
    script_overview: str,
    verdict_snapshot: Optional[Dict[str, Any]],
    scene_catalog: List[Dict[str, Any]],
    improvement_brief: Optional[Mapping[str, Any]],
    diagnostic_brief: Optional[Mapping[str, Any]],
    dimension_keys: List[str],
    max_steps: int,
) -> str:
    """把所有上下文拼成一个长 prompt 喂 LLM。

    prompt 块顺序遵循「先全局、后局部、最后任务约束」的常见 LLM 引导规律，
    让 LLM 优先理解剧本结构再下决策。
    """

    sections: list[str] = []
    sections.append(_PLAN_SYSTEM_MESSAGE)
    sections.append("【剧本概要】")
    sections.append(_truncate(script_overview, _PLAN_OVERVIEW_MAX_CHARS))

    if verdict_snapshot:
        sections.append("")
        sections.append("【最近一次投资决策评分快照】")
        sections.append(_format_verdict_snapshot(verdict_snapshot))

    if improvement_brief:
        sections.append("")
        sections.append("【本次改进建议（用户点击「按此条改稿」）】")
        sections.append(_format_improvement_brief(improvement_brief))
    if diagnostic_brief:
        sections.append("")
        sections.append("【本次诊断改写上下文（用户点击「按本次诊断改稿」）】")
        sections.append(_format_diagnostic_brief(diagnostic_brief))

    sections.append("")
    sections.append("【场次清单】（按集/场次顺序，scene_id 是唯一标识，必须原样引用）")
    sections.append(_format_scene_catalog(scene_catalog))

    sections.append("")
    sections.append(_format_dimension_requirement(dimension_keys))

    sections.append("")
    sections.append(_format_output_contract(max_steps=max_steps))
    return "\n".join(sections)


def _format_verdict_snapshot(snapshot: Mapping[str, Any]) -> str:
    score = snapshot.get("investment_score")
    score_txt = "—" if score is None else f"{float(score):.1f}/10"
    label = snapshot.get("verdict_label") or "—"
    reason = (snapshot.get("verdict_reason") or "").strip() or "—"
    lines = [
        f"- investment_score: {score_txt}",
        f"- verdict: {label}",
        f"- 一句话理由: {reason}",
    ]
    tops = snapshot.get("top_improvements") or []
    if isinstance(tops, list) and tops:
        lines.append("- top_improvements（按权重排序）:")
        for idx, it in enumerate(tops[:5], 1):
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip() or "（无标题）"
            dim_label = it.get("dimension_label") or it.get("dimension_key") or ""
            tag = f"（{dim_label}）" if dim_label else ""
            lines.append(f"  {idx}. {title}{tag}")
    return "\n".join(lines)


def _format_improvement_brief(brief: Mapping[str, Any]) -> str:
    dim_key = str(brief.get("dimension_key") or "").strip()
    dim_label = (
        str(brief.get("dimension_label") or "").strip()
        or _INVESTMENT_DIM_LABELS_ZH.get(dim_key, dim_key or "—")
    )
    sig_key = str(brief.get("signal_key") or "").strip()
    sig_label = str(brief.get("signal_label") or "").strip() or sig_key or "—"
    title = str(brief.get("title") or "").strip() or "（无标题）"
    rationale = str(brief.get("rationale") or "").strip() or "（无 rationale）"
    lines = [
        f"- 目标维度: {dim_label}（dimension_key={dim_key or 'unknown'}）",
        f"- 目标信号: {sig_label}（signal_key={sig_key or 'unknown'}）",
        f"- 建议: {title}",
        f"- 理由: {rationale}",
    ]
    return "\n".join(lines)


def _format_diagnostic_brief(brief: Mapping[str, Any]) -> str:
    score = brief.get("investment_score")
    score_txt = "—" if score is None else f"{float(score):.1f}/10"
    verdict = str(brief.get("verdict_label") or "").strip() or "—"
    reason = str(brief.get("verdict_reason") or "").strip() or "—"
    lines = [
        f"- investment_score: {score_txt}",
        f"- verdict: {verdict}",
        f"- 一句话理由: {reason}",
        "- 优先解决（top_improvements）:",
    ]
    tops = brief.get("top_improvements") or []
    if isinstance(tops, list):
        for idx, it in enumerate(tops[:5], 1):
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip() or "（无标题）"
            dim_label = it.get("dimension_label") or ""
            tag = f"（{dim_label}）" if dim_label else ""
            lines.append(f"  {idx}. {title}{tag}")
    return "\n".join(lines)


def _format_scene_catalog(catalog: List[Dict[str, Any]]) -> str:
    if not catalog:
        return "（暂无场次）"
    rows: list[str] = []
    for sc in catalog:
        ep = sc.get("episode_no")
        sn = sc.get("scene_no")
        label = str(sc.get("scene_label") or "").strip()
        head_bits: list[str] = [f"scene_id={sc.get('scene_id')}"]
        if ep is not None:
            head_bits.append(f"第{ep}集")
        if sn is not None and str(sn).strip():
            head_bits.append(f"第{sn}场")
        if label:
            head_bits.append(f"《{label}》")
        chars = str(sc.get("characters") or "").strip()
        chars_tag = f" [角色: {chars}]" if chars else ""
        digest = str(sc.get("digest") or "").strip()
        rows.append(f"- [{' | '.join(head_bits)}]{chars_tag} {digest}")
    return "\n".join(rows)


def _format_dimension_requirement(dim_keys: List[str]) -> str:
    if not dim_keys:
        return (
            "未显式指定目标维度，请根据本次建议/诊断与剧本短板自行选择 target_dimensions。"
        )
    text = "、".join(
        f"{key}({_INVESTMENT_DIM_LABELS_ZH.get(key, key)})" for key in dim_keys
    )
    return f"目标维度：{text}"


def _format_output_contract(*, max_steps: int) -> str:
    return (
        "【输出契约】严格 JSON，schema：\n"
        "{\n"
        '  "overall_summary": "≤ 100 字，本计划要解决什么、预期把哪类信号补齐",\n'
        '  "steps": [\n'
        "    {\n"
        '      "scene_id": "<必须来自上面场次清单的 scene_id，不允许编造>",\n'
        '      "target_dimensions": ["<维度键，1-3 个，例如 producibility / hook>"],\n'
        '      "rationale": "≤ 100 字：为什么这场是短板，对应建议/信号的哪一点",\n'
        '      "expected_changes": "≤ 120 字：具体怎么改，给编剧可执行指令"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "约束：\n"
        f"1. steps 数量 1~{max_steps} 条，按优先级降序排列；若全剧确无可改场次，allow steps=[]。\n"
        "2. scene_id 必须原样取自场次清单；不要拼接、不要截断、不要发明。\n"
        "3. 优先解决最影响投资决策评分的场次（最贴近本次建议/诊断信号的几场）。\n"
        "4. rationale 与 expected_changes 都用中文，不要复述场次清单原文，提炼后给指令。\n"
        "5. 当本次建议是「压缩同框人数」「降低制作复杂度」这种 producibility 信号时，"
        "优先挑同框人数多/角色复杂的场。\n"
        "6. 输出必须是合法 JSON 对象，不要包裹 ```json 代码块，不要附加任何解释文本。"
    )


_EXECUTE_PROMPT = """你是中文短剧资深编剧。请基于整剧上下文对目标场进行改写。

【整剧概要】
{script_overview}

【人物表】
{characters_block}

【前情场次摘要】
{prev_scenes_block}

【目标场原文】（{scene_label}）
---
{scene_text}
---

【后续场次摘要】
{next_scenes_block}

【目标改写维度】{target_dimensions_text}

【改写动作指令】
{expected_changes}

约束：
1. 只输出目标场的新文本，不改前后场。
2. 保持角色、世界观、核心事件连续性，不得引入新主线。
3. 字数与原文尽量同量级（允许上下浮动约 30%）。
4. 多维同时优化时优先保证主线推进与角色动机清晰。

输出严格 JSON：
{{
  "rewritten_text": "<改写后的整段场景文本>",
  "rationale": "<≤150字，说明主要改动及提分原因>"
}}"""


async def execute_plan_step(
    *,
    script_id: str,
    scene_id: str,
    target_dimensions: List[str],
    expected_changes: str = "",
    caller: Optional[LlmCaller] = None,
    engine: Engine = default_engine,
) -> RewriteResult:
    dims = _normalize_investment_dim_keys(target_dimensions)
    if not dims:
        raise ValueError(
            "target_dimensions must be a non-empty subset of "
            f"{list(_INVESTMENT_DIM_KEYS)}; got {target_dimensions!r}"
        )

    ctx = _load_rewrite_context(scene_id, expected_script_id=script_id, engine=engine)
    if ctx is None:
        raise ValueError(f"scene_id {scene_id} not found in script {script_id}")
    scene = ctx["scene"]
    scene_text = str(scene.get("text") or "")
    if not scene_text.strip():
        raise ValueError(f"scene {scene_id} has empty text")

    if not expected_changes.strip():
        expected_changes = "按目标维度修复弱项并提升可读性。"

    target_dims_text = " + ".join(
        f"{dim}（{_INVESTMENT_DIM_LABELS_ZH.get(dim, dim)}）" for dim in dims
    )
    prompt = _EXECUTE_PROMPT.format(
        script_overview=ctx["script_overview"],
        characters_block=ctx["characters_block"],
        prev_scenes_block=ctx["prev_scenes_block"],
        scene_label=scene.get("scene_label") or "",
        scene_text=scene_text,
        next_scenes_block=ctx["next_scenes_block"],
        target_dimensions_text=target_dims_text,
        expected_changes=expected_changes,
    )

    caller = caller or LlmCaller()
    try:
        resp = await caller.call_json(
            prompt,
            tier=ModelTier.PRIMARY,
            temperature=0.2,
            max_tokens=TokenBudget.REWRITE_EXCERPT,
        )
    except ScoreLLMError as exc:
        logger.warning("execute_plan_step LLM failed for scene %s: %s", scene_id, exc)
        raise

    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    rewritten_text = str(parsed.get("rewritten_text") or "").strip()
    rationale = str(parsed.get("rationale") or "").strip()
    if not rewritten_text:
        raise ScoreLLMError(f"execute_plan_step: LLM returned empty rewritten_text for scene {scene_id}")

    return RewriteResult(
        scene_id=scene_id,
        scene_label=str(scene.get("scene_label") or ""),
        target_dimensions=dims,
        original_text=scene_text,
        rewritten_text=rewritten_text,
        rationale=rationale or "已按目标维度完成改写。",
    )


def _load_script_overview(script_id: str, *, engine: Engine = default_engine) -> str:
    with engine.connect() as conn:
        row = conn.execute(
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
    if row is None:
        return "（暂无诊断报告，仅基于上下文改写）"
    payload = row.get("report_json")
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    if not isinstance(payload, dict):
        return "（报告解析失败，仅基于上下文改写）"
    summary = str(payload.get("summary") or "").strip()
    decision = payload.get("decision") or {}
    reason = str(decision.get("one_sentence_reason") or "").strip() if isinstance(decision, dict) else ""
    parts = [part for part in (summary, reason) if part]
    return "\n".join(parts) if parts else "（暂无整剧概要）"


def _load_rewrite_context(
    scene_id: str,
    *,
    expected_script_id: str,
    engine: Engine = default_engine,
) -> Optional[Dict[str, Any]]:
    with engine.connect() as conn:
        target = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, text
                FROM scriptlens.scenes
                WHERE id = :scene_id
                """
            ),
            {"scene_id": scene_id},
        ).mappings().first()
        if target is None:
            return None
        target_dict = dict(target)
        if target_dict.get("script_id") != expected_script_id:
            raise ValueError("scene_id does not belong to current script")

        all_scenes = conn.execute(
            text(
                """
                SELECT id::text AS id, episode_no, scene_no, scene_label,
                       LEFT(text, :digest_chars) AS digest
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                """
            ),
            {"sid": expected_script_id, "digest_chars": _SCENE_DIGEST_CHARS},
        ).mappings().all()

        character_rows = conn.execute(
            text(
                """
                SELECT character_name, COUNT(*) AS appearances
                FROM (
                    SELECT unnest(characters) AS character_name
                    FROM scriptlens.scenes
                    WHERE script_id = :sid
                ) t
                WHERE character_name IS NOT NULL AND character_name <> ''
                GROUP BY character_name
                ORDER BY appearances DESC, character_name ASC
                LIMIT :top_n
                """
            ),
            {"sid": expected_script_id, "top_n": _CHARACTERS_TOP_N},
        ).mappings().all()

    scenes_list = [dict(row) for row in all_scenes]
    target_idx = next((idx for idx, row in enumerate(scenes_list) if row.get("id") == scene_id), None)
    if target_idx is None:
        return None
    prev_window = scenes_list[max(0, target_idx - _CONTEXT_WINDOW) : target_idx]
    next_window = scenes_list[target_idx + 1 : target_idx + 1 + _CONTEXT_WINDOW]
    return {
        "scene": target_dict,
        "script_overview": _load_script_overview(expected_script_id, engine=engine),
        "characters_block": _format_characters([dict(row) for row in character_rows]),
        "prev_scenes_block": _format_window(prev_window) or "（无前情场次）",
        "next_scenes_block": _format_window(next_window) or "（无后续场次）",
    }


def _format_window(window: list[dict[str, Any]]) -> str:
    if not window:
        return ""
    # Python 3.11 不允许 f-string 表达式里出现反斜杠（3.12 才放开），所以这里
    # 先把 digest 清洗成单行变量再拼 f-string，避免 SyntaxError。
    lines: list[str] = []
    for scene in window:
        digest_raw = str(scene.get("digest") or "").strip()
        digest = digest_raw.replace("\n", " ").replace("\r", " ")
        lines.append(f"- {_scene_title(scene)}：{digest}")
    return "\n".join(lines)


def _format_characters(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "（暂无人物统计）"
    return "\n".join(
        f"- {row.get('character_name')}（出场 {row.get('appearances')} 场）" for row in rows
    )


def _scene_title(scene: dict[str, Any]) -> str:
    parts: list[str] = []
    ep = scene.get("episode_no")
    if ep is not None:
        parts.append(f"第{ep}集")
    sn = scene.get("scene_no")
    if sn is not None and str(sn).strip():
        parts.append(f"第{sn}场")
    label = scene.get("scene_label")
    if label:
        parts.append(f"《{label}》")
    return " ".join(parts) if parts else "未命名场"


def _truncate(text: str, max_len: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"
