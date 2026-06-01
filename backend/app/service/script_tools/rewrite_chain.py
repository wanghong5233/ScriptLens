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

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_tools.llm_caller import LlmCaller, ModelTier, ScoreLLMError, TokenBudget
from service.script_tools.prompt_loader import (
    PromptNotFoundError,
    known_dimension_keys,
    load_execute_dimension_guidance,
    load_execute_system,
    load_plan_critic,
    load_plan_dimension_guidance,
    load_plan_output_contract,
    load_plan_system,
    load_scene_brief_prompt,
)
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
#
# 容量推导（第一性原理，不再拍脑袋）：
#   - qwen-max-latest 上下文窗 32K token；输出预留 8K + prompt 框架 ≈ 2K
#     → 场次清单可用上限 ≈ 22K token ≈ 33K 中文字符
#   - 每场摘要 ≈ 110 字 + 头部元信息（scene_id/集场/角色） ≈ 60 字 ≈ 170 字
#     → 33K / 170 ≈ 190 场理论上限
#   - 取 120 场作为生产上限，覆盖 100-120 场常见短剧 + 留 60% 缓冲
#
# 排序策略（应对 LLM "Lost in the Middle" 现象，Liu et al. 2023）：
#   - LLM 对长清单开头和结尾的注意力 > 中部
#   - 因此 plan 阶段把「评分短板对应的 evidence_scene_ids」排到清单最前面，
#     LLM 看到时注意力天然集中在真正需要改的场上；剩余场按原集场顺序填充。
#   - 不做"过滤"——所有场都喂给 LLM，只调整顺序。
_PLAN_SCENE_DIGEST_CHARS = 110  # 每场摘要长度（单字），太长会撑爆 prompt
_PLAN_MAX_SCENES_IN_PROMPT = 120  # 场次清单上限；100 场剧本全塞、120+ 场剧本按优先级裁剪
_PLAN_OVERVIEW_MAX_CHARS = 600  # 整剧概要最大长度
_PLAN_TEMPERATURE = 0.3

# scene_brief 生成参数（C1c）
#
# brief 是给 plan/execute LLM 看的预消化简介，落库到 scriptlens.scenes.brief_json。
# 生成时单场调用 1 次 LLM，写库后下次直接复用，直到原文被改写（execute_plan_step
# 改写后置 NULL）才重新生成。
#
# - _BRIEF_SCENE_TEXT_CHARS: 喂给 LLM 看的原文截断（单场）。1500 字够覆盖 90% 短剧
#   单场长度；超过的尾部信息在 brief 里通常体现不出来。
# - _BRIEF_PARALLELISM: ensure 时的最大并发；qwen-max 提供商一般支持 5-8 并发不限速。
#   选 4 保守，单次 plan 触发 ≤ 4 个 brief 也能在 ~6s 内完工。
# - _BRIEF_TIMEOUT_S: 单 brief 调用超时。生成失败留 NULL，下次 plan 再尝试。
# - _BRIEF_MAX_PER_PLAN: 单次 propose_plan 触发的 brief 上限 —— 只补 priority 场。
#   非 priority 的场让用户慢慢访问 / 评分阶段批跑时补齐。
_BRIEF_SCENE_TEXT_CHARS = 1500
_BRIEF_PARALLELISM = 4
_BRIEF_TIMEOUT_S = 25.0
_BRIEF_MAX_PER_PLAN = 6


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


class SceneBrief(BaseModel):
    """单场结构化简介 — 落库到 ``scriptlens.scenes.brief_json``。

    生成逻辑见 ``_ensure_scene_briefs``；展示逻辑见 ``_format_scene_brief``。
    plan / execute / critic 都读这同一份 brief，避免每条链路自己再解析原文。

    LLM 生成时强约束 schema（``LlmCaller.call_json(validate_with=SceneBrief)``），
    格式错的输出会自动被 caller 重试。
    """

    conflict: str = Field(..., min_length=1, max_length=80)
    scene_function: str = Field(..., min_length=1, max_length=20)
    protagonist_actions: List[str] = Field(default_factory=list, max_length=5)
    supporting_actions: List[str] = Field(default_factory=list, max_length=5)
    removable_characters: List[str] = Field(default_factory=list, max_length=10)
    group_density: str = Field(default="mid", max_length=10)

    @field_validator("scene_function", mode="before")
    @classmethod
    def _coerce_scene_function(cls, v: Any) -> str:
        if v is None:
            return "过渡"
        return str(v).strip() or "过渡"

    @field_validator("group_density", mode="before")
    @classmethod
    def _coerce_group_density(cls, v: Any) -> str:
        if v is None:
            return "mid"
        # LLM 偶尔写"中"或"mid_high"，做一次容错收窄到 high/mid/low
        s = str(v).strip().lower()
        if s in {"high", "高"}:
            return "high"
        if s in {"low", "低"}:
            return "low"
        return "mid"

    @field_validator(
        "protagonist_actions",
        "supporting_actions",
        "removable_characters",
        mode="before",
    )
    @classmethod
    def _coerce_str_list(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            # LLM 偶尔吐 scalar 而非 list — 转单元素列表
            cleaned = v.strip()
            return [cleaned] if cleaned else []
        if isinstance(v, list):
            return [str(item).strip() for item in v if str(item).strip()]
        return []


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
    # 评分驱动选场：从 improvement / diagnostic / verdict_snapshot 抽取
    # evidence_scene_ids，作为 priority 排序键传给场次清单加载器；命中的场
    # 会被排到清单最前面（应对 LLM long-context "Lost in the Middle"）。
    priority_scene_ids = _collect_priority_scene_ids(
        improvement_brief=improvement_brief,
        diagnostic_brief=diagnostic_brief,
        verdict_snapshot=verdict_snapshot,
        dim_keys=dim_keys,
    )
    scene_catalog = _load_scene_catalog(
        script_id=script_id,
        engine=engine,
        priority_scene_ids=priority_scene_ids,
    )
    if not scene_catalog:
        # 剧本无场次：直接给空 plan，避免 LLM 凭空想 scene_id。
        return RewritePlan(
            dimensions=dim_keys,
            overall_summary="剧本暂无场次，无法生成改写计划。",
            steps=[],
        )

    valid_scene_ids = {row["scene_id"] for row in scene_catalog}
    logger.info(
        "propose_plan scene_catalog script_id=%s total=%d priority=%d dim_keys=%s",
        script_id,
        len(scene_catalog),
        len(priority_scene_ids),
        dim_keys,
    )

    # 对 priority 场（评分判定的短板证据场）做 brief 预热：让 plan LLM 看到的
    # 是「【冲突】xxx 【可压缩】yyy」这种结构化预消化，而不是 110 字 digest 末截。
    # 一次最多生成 _BRIEF_MAX_PER_PLAN 个（≈ top_improvements top-5 量级），
    # 控制 plan 主路径延迟在 LLM 单次响应 ~5-8s 内。
    # ensure 失败完全降级到 digest 路径，不影响主流程。
    if priority_scene_ids:
        try:
            written = await _ensure_scene_briefs(
                priority_scene_ids,
                script_id=script_id,
                caller=caller,
                engine=engine,
            )
            if written > 0:
                # 重新加载 catalog 才能让 _format_scene_catalog 看到新写入的 brief_json
                scene_catalog = _load_scene_catalog(
                    script_id=script_id,
                    engine=engine,
                    priority_scene_ids=priority_scene_ids,
                )
        except Exception as exc:  # noqa: BLE001 — ensure 是 best-effort
            logger.warning(
                "propose_plan _ensure_scene_briefs failed script=%s err=%s — "
                "falling back to digest-only catalog",
                script_id,
                exc,
            )

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

    # critic 二阶段：用同模型不同 prompt 过滤模板话术 + 重写 rationale。
    # 即使 critic 失败也不影响主流程 — 退回 planner 原始 plan。
    critic_summary, plan_steps = await _critique_plan_steps(
        plan_steps=plan_steps,
        overall_summary=overall_summary,
        script_overview=script_overview,
        improvement_brief=improvement_brief,
        diagnostic_brief=diagnostic_brief,
        script_id=script_id,
        caller=caller,
        engine=engine,
    )
    if critic_summary:
        overall_summary = critic_summary

    if not plan_steps:
        # critic 把所有 step 都判不合格 — 视为"planner 出的是垃圾"，让前端
        # 看到失败而不是空 plan，agent 会重试 propose_plan。
        raise ScoreLLMError(
            "propose_plan: critic dropped all steps as template-only / "
            "protagonist-violating; LLM should retry with stronger constraints"
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


def _load_character_role_map(
    *,
    script_id: str,
    engine: Engine,
) -> Dict[str, str]:
    """加载剧本的角色身份映射 ``{canonical_name | alias: role}``。

    数据来源：``scriptlens.character_entities``（由
    ``character_pipeline._role_of`` 按出场场次自动判定，并允许 LLM enrichment
    在后续覆盖）。role 枚举：``protagonist / antagonist / support / minor``。

    为什么把 alias 也铺平进 map：``scenes.characters`` 这一列在不同剧里可能
    存的是 canonical_name 也可能是 alias（比如 LLM 抽取阶段把"姜栀枝"和"枝枝"
    都吐进了不同场的角色名数组）。如果只按 canonical_name 匹配，那些 alias
    场会 fallback 到 "support" 默认值，主角识别会丢一半。把 alias 一起索引，
    JOIN 命中率从 ~60% 拉到接近 100%（实测 19de2370 剧本）。

    map 命中失败的 character 由调用方降级为 "support"（保守、可改写但不破坏
    主线）。返回空 map 表示这剧还没跑 extract_characters_tool/character 主链，
    plan 侧应当退回到无身份信息的旧路径，不应抛错。
    """
    role_map: Dict[str, str] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT canonical_name, aliases, role
                FROM scriptlens.character_entities
                WHERE script_id = :sid
                """
            ),
            {"sid": script_id},
        ).mappings().all()

    for row in rows:
        role = str(row.get("role") or "").strip().lower()
        if role not in {"protagonist", "antagonist", "support", "minor"}:
            # 未知值不索引，避免污染主角识别
            continue
        name = str(row.get("canonical_name") or "").strip()
        if name:
            role_map[name] = role
        aliases_raw = row.get("aliases")
        # aliases 列是 jsonb，psycopg 已经反序列化成 list；防御性处理 str。
        if isinstance(aliases_raw, str):
            try:
                aliases_raw = json.loads(aliases_raw)
            except (TypeError, ValueError):
                aliases_raw = []
        if isinstance(aliases_raw, (list, tuple)):
            for alias in aliases_raw:
                alias_name = str(alias or "").strip()
                if alias_name and alias_name not in role_map:
                    role_map[alias_name] = role
    return role_map


def _bucket_characters_by_role(
    characters: Sequence[str],
    role_map: Mapping[str, str],
) -> Dict[str, List[str]]:
    """把单场 ``characters`` 列表按 role 分桶。

    返回顺序保留原 characters 数组里的相对顺序（编剧通常按戏份重要性写人物
    表，这个顺序对 LLM 有提示价值）。未在 role_map 命中的角色降级到 "support"
    桶——保守地视为"可改写"，但 prompt 端会让 LLM 二次斟酌。
    """
    buckets: Dict[str, List[str]] = {
        "protagonist": [],
        "antagonist": [],
        "support": [],
        "minor": [],
    }
    for char in characters:
        name = str(char or "").strip()
        if not name:
            continue
        role = role_map.get(name, "support")
        buckets[role].append(name)
    return buckets


def _load_scene_catalog(
    *,
    script_id: str,
    engine: Engine,
    priority_scene_ids: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """加载剧本所有场次的轻量摘要，给 plan LLM 当候选清单。

    每场返回的字段：

    - ``scene_id`` / ``episode_no`` / ``scene_no`` / ``scene_label``
    - ``characters_raw``: 原 PG text[]，保留以便后续按 role 重分桶
    - ``characters_by_role``: ``{protagonist/antagonist/support/minor: [...]}``，
      由 ``character_entities`` JOIN 得来；plan/execute prompt 用这个来强化"主角
      必复用、可压缩的是配角"约束
    - ``digest``: 截断到 ``_PLAN_SCENE_DIGEST_CHARS`` 的去 newline 原文
    - ``brief_json``: 评分阶段落库的结构化简介（可能为 None；后续 commit 会补
      on-demand 生成）

    排序与裁剪策略（应对 LLM long-context "Lost in the Middle"）：

    - ``priority_scene_ids`` 命中的场（来自 evaluation_v4 evidence）排清单最前
    - 总场数 ≤ ``_PLAN_MAX_SCENES_IN_PROMPT`` 全量保留；超出时 priority 全保，
      剩余均匀采样补齐
    """

    role_map = _load_character_role_map(script_id=script_id, engine=engine)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS scene_id, episode_no, scene_no, scene_label,
                       characters, brief_json,
                       LEFT(text, :digest_chars) AS digest
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
        chars_list: List[str] = []
        if isinstance(chars_raw, (list, tuple)):
            chars_list = [str(c).strip() for c in chars_raw if str(c).strip()]
        chars_by_role = _bucket_characters_by_role(chars_list, role_map)
        digest = str(row.get("digest") or "").replace("\r\n", " ").replace("\n", " ").strip()
        catalog.append(
            {
                "scene_id": str(row.get("scene_id")),
                "episode_no": row.get("episode_no"),
                "scene_no": row.get("scene_no"),
                "scene_label": str(row.get("scene_label") or ""),
                "characters_raw": chars_list,
                "characters_by_role": chars_by_role,
                "brief_json": row.get("brief_json"),
                "digest": digest,
            }
        )

    return _arrange_for_plan(
        catalog,
        priority_scene_ids=priority_scene_ids or [],
        max_items=_PLAN_MAX_SCENES_IN_PROMPT,
    )


def _arrange_for_plan(
    catalog: List[Dict[str, Any]],
    *,
    priority_scene_ids: Sequence[str],
    max_items: int,
) -> List[Dict[str, Any]]:
    """对场次清单按 priority 重排序 + 上限裁剪。

    步骤：
      1) 把 priority_scene_ids（保持调用方给的顺序）的场抽到列表前面，
         并打上 ``is_priority=True`` 标记，prompt 渲染时挂"短板"角标提示 LLM。
      2) 剩余场保持原集场顺序。
      3) 总数 ≤ max_items：直接返回拼好的清单。
      4) 总数 > max_items：priority 段**全部保留**；剩余段做均匀采样填到 max_items。

    这样既不丢评分短板场，又控制 prompt 长度。
    """

    if not catalog:
        return []

    priority_set = {sid for sid in priority_scene_ids if sid}
    priority_order = [sid for sid in priority_scene_ids if sid in priority_set]
    seen_priority: set[str] = set()
    priority_block: list[dict[str, Any]] = []
    remainder_block: list[dict[str, Any]] = []
    by_id = {row["scene_id"]: row for row in catalog}
    # 按调用方提供的 priority 顺序抽场（dedup）。
    for sid in priority_order:
        if sid in seen_priority:
            continue
        row = by_id.get(sid)
        if row is None:
            continue
        marked = dict(row)
        marked["is_priority"] = True
        priority_block.append(marked)
        seen_priority.add(sid)
    for row in catalog:
        if row["scene_id"] in seen_priority:
            continue
        remainder_block.append(row)

    total = len(priority_block) + len(remainder_block)
    if total <= max_items:
        return priority_block + remainder_block

    # 超量：priority 全保留，remainder 均匀采样填到上限。
    remainder_quota = max(0, max_items - len(priority_block))
    if remainder_quota <= 0:
        return priority_block[:max_items]
    return priority_block + _evenly_sample(remainder_block, remainder_quota)


def _evenly_sample(items: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """长场次清单的均匀采样：保留首尾 + 等距取中间，保证全局视角。

    仅作为「priority 之外、超过上限」场景的兜底；正常 100 场剧本走不到这里。
    """

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


def _collect_priority_scene_ids(
    *,
    improvement_brief: Optional[Mapping[str, Any]],
    diagnostic_brief: Optional[Mapping[str, Any]],
    verdict_snapshot: Optional[Dict[str, Any]],
    dim_keys: Sequence[str],
) -> List[str]:
    """评分驱动选场：从 improvement / diagnostic / verdict_snapshot 抽出
    evidence_scene_ids，决定哪些场应该排到清单前面。

    优先级（高 → 低）：
      1) improvement_brief.evidence_ref_ids：来自「按此条改稿」CTA，最强信号。
      2) verdict_snapshot.dimensions[k].evidence_scene_ids，
         其中 k 命中 improvement.dimension_key 或 dim_keys。
      3) diagnostic_brief.top_improvements[*].evidence_scene_ids：
         整剧诊断的 top 短板场。
      4) verdict_snapshot.top_improvements[*].evidence_scene_ids（兜底）。

    去重后保持原顺序返回。所有 scene_id 都是字符串；不存在的场会在
    ``_arrange_for_plan`` 里被静默丢弃（不影响主流程）。
    """

    out: list[str] = []
    seen: set[str] = set()

    def _push(items: Any) -> None:
        if not isinstance(items, (list, tuple)):
            return
        for raw in items:
            sid = str(raw or "").strip()
            if sid and sid not in seen:
                out.append(sid)
                seen.add(sid)

    if isinstance(improvement_brief, Mapping):
        _push(improvement_brief.get("evidence_ref_ids"))
        _push(improvement_brief.get("evidence_scene_ids"))

    # 收集 verdict_snapshot.dimensions[k].evidence_scene_ids
    dims_to_pick: list[str] = []
    if isinstance(improvement_brief, Mapping):
        ik = str(improvement_brief.get("dimension_key") or "").strip().lower()
        if ik:
            dims_to_pick.append(ik)
    for k in dim_keys:
        if k and k not in dims_to_pick:
            dims_to_pick.append(k)
    if isinstance(verdict_snapshot, Mapping):
        dims = verdict_snapshot.get("dimensions")
        if isinstance(dims, list):
            for dim in dims:
                if not isinstance(dim, dict):
                    continue
                key = str(dim.get("key") or dim.get("dimension") or "").strip().lower()
                if dims_to_pick and key not in dims_to_pick:
                    continue
                _push(dim.get("evidence_scene_ids"))

    if isinstance(diagnostic_brief, Mapping):
        for it in diagnostic_brief.get("top_improvements") or []:
            if isinstance(it, dict):
                _push(it.get("evidence_scene_ids"))
                _push(it.get("evidence_ref_ids"))

    if isinstance(verdict_snapshot, Mapping):
        for it in verdict_snapshot.get("top_improvements") or []:
            if isinstance(it, dict):
                _push(it.get("evidence_scene_ids"))

    return out


def _load_latest_verdict_snapshot(
    *, script_id: str, engine: Engine
) -> Optional[Dict[str, Any]]:
    """从 reports.report_json 加载最近一次评估快照。

    返回字段：
    - ``investment_score`` / ``verdict_label`` / ``verdict_reason``
    - ``top_improvements``：list[{title, dimension_label, evidence_scene_ids, ...}]
    - ``dimensions``：list[{key, score, tier, evidence_scene_ids, ...}]
      —— **评分驱动选场**直接读这个字段，把短板维度的 evidence 场排到 plan 清单最前。

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
    evaluation_v4 = (
        payload.get("evaluation_v4") if isinstance(payload.get("evaluation_v4"), dict) else None
    )
    dimensions_block: list[dict[str, Any]] = []
    if evaluation_v4 and isinstance(evaluation_v4.get("dimensions"), list):
        for dim in evaluation_v4["dimensions"]:
            if not isinstance(dim, dict):
                continue
            dimensions_block.append(
                {
                    "key": dim.get("key") or dim.get("dimension"),
                    "score": dim.get("score"),
                    "tier": dim.get("tier"),
                    "evidence_scene_ids": list(dim.get("evidence_scene_ids") or []),
                }
            )
    snapshot: dict[str, Any] = {
        "investment_score": payload.get("investment_score"),
        "verdict_label": (verdict or {}).get("label"),
        "verdict_reason": (verdict or {}).get("one_sentence_reason")
        or (verdict or {}).get("reason"),
        "top_improvements": payload.get("top_improvements") or [],
        "dimensions": dimensions_block,
    }
    if all(v in (None, "", []) for v in snapshot.values()):
        return None
    return snapshot


# ============================================================
# helpers: prompt building
# ============================================================


# _PLAN_SYSTEM_MESSAGE 已经迁出到 prompts/script_studio/plan/_system.zh.md，
# 通过 prompt_loader.load_plan_system() 按需加载。模块顶层不再保留字符串字面量，
# 避免"改 prompt 必须改 Python"的反模式。


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
    sections.append(load_plan_system())
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

    # 维度专属方法论：根据 improvement_brief 的主维度（用户点哪条建议就 lock 哪维）
    # 注入对应 by_dimension/*.zh.md。dimension_brief 路径有则用，否则回退到
    # dimension_keys[0]。这样 LLM 拿到 hook 维度建议时，会同时看到 hook 的判定
    # 准则 + 修复方法论 + few-shot，不再裸喂"目标维度=hook"。
    primary_dim = _pick_primary_dimension(
        improvement_brief=improvement_brief,
        diagnostic_brief=diagnostic_brief,
        dimension_keys=dimension_keys,
    )
    if primary_dim:
        try:
            dim_md = load_plan_dimension_guidance(primary_dim)
        except PromptNotFoundError:
            # 不在已知 5 维内（极少数旧 evaluation 数据可能漏值）— skip 而非崩。
            logger.warning(
                "plan dimension guidance missing dim_key=%s; falling back to generic prompt",
                primary_dim,
            )
        else:
            sections.append("")
            sections.append(dim_md)

    sections.append("")
    sections.append(_format_dimension_requirement(dimension_keys))

    sections.append("")
    sections.append(_format_output_contract(max_steps=max_steps))
    return "\n".join(sections)


def _pick_primary_dimension(
    *,
    improvement_brief: Optional[Mapping[str, Any]],
    diagnostic_brief: Optional[Mapping[str, Any]],
    dimension_keys: List[str],
) -> Optional[str]:
    """挑出本次 plan 的主维度 — 仅用于加载维度专属方法论。

    优先级：
    1. improvement_brief.dimension_key（用户点击的具体建议 — 最强信号）
    2. diagnostic_brief.dimension_key 或 diagnostic_brief.primary_dimension
    3. dimension_keys[0]（API 调用方手动指定）

    返回值必须是 ``known_dimension_keys()`` 之一才有意义；不在白名单内的返回
    None，让上游 fallback 到无维度专属方法论的旧路径。
    """
    candidates: list[Any] = []
    if improvement_brief:
        candidates.append(improvement_brief.get("dimension_key"))
    if diagnostic_brief:
        candidates.append(diagnostic_brief.get("dimension_key"))
        candidates.append(diagnostic_brief.get("primary_dimension"))
    candidates.extend(dimension_keys)

    valid = set(known_dimension_keys())
    for raw in candidates:
        if not raw:
            continue
        key = str(raw).strip().lower()
        if key in valid:
            return key
    return None


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


async def _generate_single_brief(
    *,
    scene_row: Mapping[str, Any],
    role_map: Mapping[str, str],
    caller: LlmCaller,
) -> Optional[SceneBrief]:
    """对单场调 LLM 生成 SceneBrief。失败返回 None（不抛）。

    内部 try/except 包住所有异常 — best-effort 设计：scene_brief 是 plan/execute
    的预消化缓存，缺失了下游会 fallback 到 110 字 digest，不能让 brief 故障吃掉
    主流程。
    """
    scene_id = str(scene_row.get("id") or "").strip()
    if not scene_id:
        return None
    raw_text = str(scene_row.get("text") or "")
    scene_text = raw_text[:_BRIEF_SCENE_TEXT_CHARS].strip()
    if not scene_text:
        return None

    raw_chars = scene_row.get("characters") or []
    if not isinstance(raw_chars, (list, tuple)):
        raw_chars = []
    chars_list = [str(c).strip() for c in raw_chars if str(c).strip()]
    by_role = _bucket_characters_by_role(chars_list, role_map)
    characters_block = _format_character_buckets(by_role) or "（本场无明确角色标注）"

    prompt = load_scene_brief_prompt(
        episode_no=str(scene_row.get("episode_no") or "—"),
        scene_no=str(scene_row.get("scene_no") or "—"),
        scene_label=str(scene_row.get("scene_label") or "—"),
        characters_block=characters_block,
        scene_text=scene_text,
    )

    try:
        resp = await asyncio.wait_for(
            caller.call_json(
                prompt,
                tier=ModelTier.PRIMARY,
                temperature=0.2,
                max_tokens=TokenBudget.REWRITE_EXCERPT,
                validate_with=SceneBrief,
                chain_name="scene_brief",
            ),
            timeout=_BRIEF_TIMEOUT_S,
        )
    except (ScoreLLMError, asyncio.TimeoutError) as exc:
        logger.warning(
            "scene_brief LLM failed scene_id=%s err=%s — leaving brief_json NULL",
            scene_id,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scene_brief unexpected error scene_id=%s err=%s — leaving NULL",
            scene_id,
            exc,
        )
        return None

    parsed = resp.parsed
    if isinstance(parsed, SceneBrief):
        return parsed
    if isinstance(parsed, dict):
        try:
            return SceneBrief.model_validate(parsed)
        except Exception as exc:  # noqa: BLE001 — pydantic ValidationError 太宽容
            logger.warning(
                "scene_brief pydantic re-validate failed scene_id=%s err=%s",
                scene_id,
                exc,
            )
            return None
    logger.warning(
        "scene_brief unexpected parsed type=%s scene_id=%s",
        type(parsed).__name__,
        scene_id,
    )
    return None


async def _ensure_scene_briefs(
    scene_ids: Sequence[str],
    *,
    script_id: str,
    caller: LlmCaller,
    engine: Engine,
    role_map: Optional[Mapping[str, str]] = None,
    max_briefs: int = _BRIEF_MAX_PER_PLAN,
    parallelism: int = _BRIEF_PARALLELISM,
) -> int:
    """对 ``scene_ids`` 中 ``brief_json`` 仍为 NULL 的场，并发调 LLM 生成 brief
    并 UPDATE 写库。

    设计：
    - 只补 NULL；已有 brief 的场跳过（execute_plan_step 改写后会主动置 NULL）
    - max_briefs 上限保护 plan 主路径延迟：一次最多生成 N 个 brief
      （priority 场通常 ≤ 6，跟评分时的 top_improvements top-5 evidence 同量级）
    - 并发：asyncio.Semaphore(parallelism) 限流，避免触发上游 provider 限速
    - 失败：单场失败留 NULL，不影响其它场；整批失败也只是降级回 digest，
      不抛错给 propose_plan 主路径

    Returns
    -------
    int
        本次成功写入 brief 的场数（用于日志统计 / 测试断言）。
    """
    if not scene_ids:
        return 0

    if role_map is None:
        try:
            role_map = _load_character_role_map(script_id=script_id, engine=engine)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_ensure_scene_briefs role_map load failed script=%s err=%s — "
                "using empty role map",
                script_id,
                exc,
            )
            role_map = {}

    # 取 NULL brief 的场原文 + 元数据
    distinct_ids = list({sid for sid in scene_ids if sid})
    if not distinct_ids:
        return 0
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id, episode_no, scene_no, scene_label,
                       characters, text, brief_json
                FROM scriptlens.scenes
                WHERE script_id = :sid AND id::text = ANY(:ids)
                """
            ),
            {"sid": script_id, "ids": distinct_ids},
        ).mappings().all()

    pending = [dict(r) for r in rows if r.get("brief_json") is None]
    if not pending:
        return 0

    # 截到 max_briefs，priority 排序由调用方负责（scene_ids 进来时已经按
    # priority 排过了，distinct_ids 不保证顺序，但实际 priority 场数量本身就 ≤ 6）
    pending = pending[:max_briefs]

    sem = asyncio.Semaphore(parallelism)

    async def _gen_with_sem(scene_row: dict) -> tuple[str, Optional[SceneBrief]]:
        async with sem:
            brief = await _generate_single_brief(
                scene_row=scene_row,
                role_map=role_map,
                caller=caller,
            )
        return str(scene_row.get("id") or ""), brief

    results = await asyncio.gather(
        *[_gen_with_sem(row) for row in pending],
        return_exceptions=False,
    )

    written = 0
    with engine.begin() as conn:
        for sid, brief in results:
            if not sid or brief is None:
                continue
            payload = brief.model_dump()
            try:
                conn.execute(
                    text(
                        """
                        UPDATE scriptlens.scenes
                        SET brief_json = CAST(:payload AS JSONB)
                        WHERE id::text = :sid AND script_id = :script_id
                        """
                    ),
                    {
                        "payload": json.dumps(payload, ensure_ascii=False),
                        "sid": sid,
                        "script_id": script_id,
                    },
                )
                written += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_ensure_scene_briefs UPDATE failed scene=%s err=%s",
                    sid,
                    exc,
                )

    logger.info(
        "_ensure_scene_briefs script=%s requested=%d pending=%d written=%d",
        script_id,
        len(distinct_ids),
        len(pending),
        written,
    )
    return written


def _invalidate_scene_brief(
    *,
    script_id: str,
    scene_id: str,
    engine: Engine,
) -> None:
    """把单场 brief_json 置 NULL — execute_plan_step 写完新原文后立刻调用，
    让下一次 plan 拉清单时触发 _ensure_scene_briefs 重新生成。

    幂等 + 不抛 — DB 层失败只是 brief 仍是旧值，下次 plan 也只是看到陈旧 brief，
    不会破坏改写主流程。
    """
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE scriptlens.scenes
                    SET brief_json = NULL
                    WHERE id::text = :sid AND script_id = :script_id
                    """
                ),
                {"sid": scene_id, "script_id": script_id},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_invalidate_scene_brief failed scene=%s err=%s — brief may be stale",
            scene_id,
            exc,
        )


async def _critique_plan_steps(
    *,
    plan_steps: List["PlanStep"],
    overall_summary: str,
    script_overview: str,
    improvement_brief: Optional[Mapping[str, Any]],
    diagnostic_brief: Optional[Mapping[str, Any]],
    script_id: str,
    caller: LlmCaller,
    engine: Engine,
) -> tuple[str, List["PlanStep"]]:
    """plan-side 二阶段 critic：同模型不同 prompt 做自审。

    任务（详见 prompts/script_studio/critic/plan_critic.zh.md）：
    1. 模板话术零容忍 — 没有具体角色名 / 具体冲突的 rationale 直接打不合格
    2. expected_changes 涉及删除 protagonist/antagonist → 不合格
    3. 仅有"简化/减少/提升"之类笼统动词 → 不合格
    4. 可救活的 step 由 critic 改写 rationale/expected_changes 后保留
    5. 无法救活的 step 进 steps_dropped + 原因

    Best-effort：critic LLM 调用失败 / JSON 不合法 / 返回空 → 退回原 plan_steps，
    记 warning 但不抛 — 不让 critic 故障吃掉 planner 的合理输出。

    Returns
    -------
    (new_overall_summary, new_plan_steps)
        如果 critic 没动 summary，new_overall_summary 返回 ""。
        new_plan_steps 是审核后的最终 step 列表（可能为空，由调用方决定是否
        当作 ScoreLLMError 抛）。
    """
    if not plan_steps:
        return "", plan_steps

    # 主角 / 反派名单：critic 拿来做硬校验。
    # 任何 DB 异常（fake script_id 测试 / schema drift / 表缺失）都不应该
    # 让 critic 整条链路崩 — 降级为空名单，critic 仍能审 rationale 模板话术。
    try:
        role_map = _load_character_role_map(script_id=script_id, engine=engine)
    except Exception as exc:  # noqa: BLE001 — best-effort fallback
        logger.warning(
            "critique_plan: _load_character_role_map failed script=%s err=%s — "
            "using empty role map",
            script_id,
            exc,
        )
        role_map = {}
    protagonists = sorted({name for name, role in role_map.items() if role == "protagonist"})
    antagonists = sorted({name for name, role in role_map.items() if role == "antagonist"})

    if improvement_brief:
        improvement_brief_text = _format_improvement_brief(improvement_brief)
    elif diagnostic_brief:
        improvement_brief_text = _format_diagnostic_brief(diagnostic_brief)
    else:
        improvement_brief_text = "（本次为按维度直接出 plan，无 improvement_brief）"

    plan_json_payload = {
        "overall_summary": overall_summary,
        "steps": [
            {
                "scene_id": step.scene_id,
                "target_dimensions": list(step.target_dimensions),
                "rationale": step.rationale,
                "expected_changes": step.expected_changes,
            }
            for step in plan_steps
        ],
    }
    plan_json_text = json.dumps(plan_json_payload, ensure_ascii=False, indent=2)

    critic_prompt = load_plan_critic(
        script_overview=_truncate(script_overview, _PLAN_OVERVIEW_MAX_CHARS),
        improvement_brief_text=improvement_brief_text,
        character_protagonist_block=" / ".join(protagonists) if protagonists else "（暂无明确主角标注）",
        character_antagonist_block=" / ".join(antagonists) if antagonists else "（暂无明确反派标注）",
        plan_json=plan_json_text,
    )

    try:
        resp = await caller.call_json(
            critic_prompt,
            tier=ModelTier.PRIMARY,
            temperature=0.2,
            max_tokens=TokenBudget.REWRITE_EXCERPT,
            chain_name="rewrite_plan_critic",
        )
    except ScoreLLMError as exc:
        logger.warning(
            "critique_plan LLM call failed script=%s err=%s — fallback to planner output",
            script_id,
            exc,
        )
        return "", plan_steps

    parsed = resp.parsed
    if not isinstance(parsed, dict):
        logger.warning(
            "critique_plan returned non-dict (type=%s) — fallback",
            type(parsed).__name__,
        )
        return "", plan_steps

    # critic 必须显式提供 steps_kept 字段（即使空）— 缺字段说明 LLM 跑偏写成
    # 了 planner 格式（"steps": [...]）而不是 critic 格式，这种情况下信任
    # planner 原始输出，不让 critic 误杀全部 step。
    if "steps_kept" not in parsed or not isinstance(parsed.get("steps_kept"), list):
        logger.warning(
            "critique_plan: missing or non-list steps_kept (got keys=%s) — "
            "fallback to planner output",
            list(parsed.keys()),
        )
        return "", plan_steps

    raw_kept = parsed.get("steps_kept") or []
    raw_dropped = parsed.get("steps_dropped") or []
    new_summary = str(parsed.get("overall_summary") or "").strip()

    # 把 critic 输出映射回 PlanStep。scene_id 必须能在原 plan_steps 里找到，
    # 否则视为 critic 幻觉，drop 这条。
    step_by_scene = {step.scene_id: step for step in plan_steps}
    new_steps: list["PlanStep"] = []
    for raw in raw_kept:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("scene_id") or "").strip()
        original = step_by_scene.get(sid)
        if original is None:
            logger.warning(
                "critique_plan kept-step with unknown scene_id=%s (script=%s) — drop",
                sid or "<empty>",
                script_id,
            )
            continue
        rationale = _truncate(str(raw.get("rationale") or original.rationale).strip(), 200)
        expected_changes = _truncate(
            str(raw.get("expected_changes") or original.expected_changes).strip(),
            240,
        )
        # target_dimensions 不允许 critic 改 — 维度是 planner 选的
        new_steps.append(
            PlanStep(
                scene_id=original.scene_id,
                scene_label=original.scene_label,
                episode_no=original.episode_no,
                scene_no=original.scene_no,
                target_dimensions=list(original.target_dimensions),
                rationale=rationale,
                expected_changes=expected_changes,
                current_excerpt=original.current_excerpt,
            )
        )

    dropped_count = len([d for d in raw_dropped if isinstance(d, dict)])
    logger.info(
        "critique_plan script=%s kept=%d dropped=%d original=%d",
        script_id,
        len(new_steps),
        dropped_count,
        len(plan_steps),
    )

    return new_summary, new_steps


def _format_character_buckets(by_role: Mapping[str, Sequence[str]]) -> str:
    """渲染单场角色按 role 分桶后的可读字符串。

    输出顺序：主角 → 反派 → 配角 → 龙套。空桶省略，没有任何角色时返回 ""。

    Note: 这串文字会出现在场次清单的每一行，长度敏感（每场 ≤ 60 字目标）。
    超过 12 个角色的群戏（按 _CHARACTERS_TOP_N 控制）只保留前 N 个并提示
    "+ M 人"，避免 prompt 长度爆炸但 LLM 仍能感知群戏密度。
    """
    label_zh = {
        "protagonist": "主角",
        "antagonist": "反派",
        "support": "配角",
        "minor": "龙套",
    }
    segments: list[str] = []
    total_omitted = 0
    for key in ("protagonist", "antagonist", "support", "minor"):
        names = list(by_role.get(key) or [])
        if not names:
            continue
        # 每桶最多展示 6 个，超出折叠成 "+N 人"
        head = names[: max(1, _CHARACTERS_TOP_N // 2)]
        omitted = len(names) - len(head)
        if omitted > 0:
            total_omitted += omitted
            segments.append(f"{label_zh[key]}:{('、'.join(head))}+{omitted}")
        else:
            segments.append(f"{label_zh[key]}:{('、'.join(head))}")
    if not segments:
        return ""
    return " | ".join(segments)


def _format_scene_catalog(catalog: List[Dict[str, Any]]) -> str:
    if not catalog:
        return "（暂无场次）"
    rows: list[str] = []
    has_priority = any(bool(sc.get("is_priority")) for sc in catalog)
    if has_priority:
        # 给 LLM 一句话提示：开头几行带 ★ 的是评分判定的短板场，优先考虑。
        rows.append(
            "（说明：行首带 ★ 的场是最近评分判定的短板证据场，"
            "应优先选作改写对象，除非确无问题。）"
        )
    rows.append(
        "（角色标注口径：主角=protagonist / 反派=antagonist / 配角=support / "
        "龙套=minor。**主角的连续出场是短剧 LoRA 成本摊薄的前提，不应被压缩**；"
        "可压缩的是配角/龙套/无台词工具人。）"
    )
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
        by_role = sc.get("characters_by_role") or {}
        chars_str = _format_character_buckets(by_role) if isinstance(by_role, Mapping) else ""
        chars_tag = f" [{chars_str}]" if chars_str else ""
        # brief_json 优先：评分阶段（或将来的 _ensure_scene_briefs）已经预消化了
        # 「场内冲突 / 主角行为 / 配角行为 / 可压缩点」结构化信息，比 110 字 digest
        # 末截裸文本对 LLM 友好得多。NULL 就 fallback。
        body = _format_scene_brief(sc.get("brief_json")) or str(sc.get("digest") or "").strip()
        prefix = "★ " if sc.get("is_priority") else "- "
        rows.append(f"{prefix}[{' | '.join(head_bits)}]{chars_tag} {body}")
    return "\n".join(rows)


def _format_scene_brief(brief: Any) -> str:
    """把 ``scenes.brief_json`` 渲染成单行 LLM 友好的简介。

    schema（与 _ensure_scene_briefs 输出对齐，由后续 commit 落地）:

    ::

        {{
          "conflict": "<= 30 字，场内核心冲突一句话",
          "protagonist_actions": ["<主角A 做了 X>"],
          "supporting_actions": ["<配角 X 做了 Y>"],
          "removable_characters": ["<可删配角名>"],
          "scene_function": "<推进主线|铺垫|过渡|高潮|闲笔>",
        }}

    现阶段 brief_json 都是 NULL（生成器还没接入），函数返回 ""，调用方 fallback
    到 digest。先在 schema 层面让 catalog 读取就绪，下游 plan/execute prompt
    无需再改一次。
    """
    if not isinstance(brief, Mapping):
        return ""
    parts: list[str] = []
    conflict = str(brief.get("conflict") or "").strip()
    if conflict:
        parts.append(f"【冲突】{conflict}")
    func = str(brief.get("scene_function") or "").strip()
    if func:
        parts.append(f"【功能】{func}")
    protagonist_actions = brief.get("protagonist_actions")
    if isinstance(protagonist_actions, list) and protagonist_actions:
        head = "；".join(str(a).strip() for a in protagonist_actions[:2] if str(a).strip())
        if head:
            parts.append(f"【主线动作】{head}")
    removable = brief.get("removable_characters")
    if isinstance(removable, list) and removable:
        names = "、".join(str(r).strip() for r in removable if str(r).strip())
        if names:
            parts.append(f"【可压缩】{names}")
    return " ".join(parts) if parts else ""


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
    """渲染 plan 阶段 output contract 段。

    实际内容由 ``prompts/script_studio/plan/_output_contract.zh.md`` 维护；
    本函数仅做参数注入。保留函数签名以避免外部 caller 改动。
    """
    return load_plan_output_contract(max_steps=max_steps)


# execute prompt 模板。
#
# 关键设计：system_message 和 dimension_guidance 是 markdown 文件加载出来的，里面
# 可能含 `{...}` 字面（比如 JSON 例子）。如果把它们和 scene_text 一起塞进 .format()，
# loader 输出里的 `{` 会被当作占位符触发 KeyError。
#
# 解法：模板里**不**把 system_message / dimension_guidance 作为占位符；先 .format()
# 注入"业务变量"，再用普通字符串拼接把 system_message / dimension_guidance
# 加在前面。这样 loader 输出永远不被二次解析。
_EXECUTE_PROMPT_TEMPLATE = """【整剧概要】
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

输出严格 JSON：
{{
  "rewritten_text": "<改写后的整段场景文本>",
  "rationale": "<= 150 字，说明主要改动及提分原因>"
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

    # 维度专属方法论：execute 阶段按 target_dimensions[0] 注入。如果调用方传了
    # 多维，只用主维（避免把 5 份方法论塞进 single-scene rewrite 撑爆 prompt）。
    primary_dim = dims[0] if dims else None
    dimension_guidance = ""
    if primary_dim:
        try:
            dimension_guidance = load_execute_dimension_guidance(primary_dim)
        except PromptNotFoundError:
            # 不在已知 5 维内：execute 仍能跑（只是没有维度专属指令）—— 不抛。
            logger.warning(
                "execute dimension guidance missing dim_key=%s; using generic prompt",
                primary_dim,
            )

    business_block = _EXECUTE_PROMPT_TEMPLATE.format(
        script_overview=ctx["script_overview"],
        characters_block=ctx["characters_block"],
        prev_scenes_block=ctx["prev_scenes_block"],
        scene_label=scene.get("scene_label") or "",
        scene_text=scene_text,
        next_scenes_block=ctx["next_scenes_block"],
        target_dimensions_text=target_dims_text,
        expected_changes=expected_changes,
    )
    # 拼接顺序：system message → dimension guidance → business block。
    # 不走 .format，避免 markdown 中的 `{...}` 字面被二次解析。
    prompt_parts = [load_execute_system()]
    if dimension_guidance:
        prompt_parts.append(dimension_guidance)
    else:
        prompt_parts.append("（本次改写未匹配到维度专属方法论。）")
    prompt_parts.append(business_block)
    prompt = "\n\n".join(prompt_parts)

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

    # 把 character_entities 的 role 字段铺到出场统计上 — 这样 execute LLM 在改写
    # 单场时也能区分"主角必须保留"和"配角可以压缩"。和 plan 阶段 _format_scene_catalog
    # 用同一份 role_map，两边判定一致，不会出现"plan 让删主角、execute 又把主角写回去"
    # 这种自相矛盾。
    role_map = _load_character_role_map(script_id=expected_script_id, engine=engine)

    scenes_list = [dict(row) for row in all_scenes]
    target_idx = next((idx for idx, row in enumerate(scenes_list) if row.get("id") == scene_id), None)
    if target_idx is None:
        return None
    prev_window = scenes_list[max(0, target_idx - _CONTEXT_WINDOW) : target_idx]
    next_window = scenes_list[target_idx + 1 : target_idx + 1 + _CONTEXT_WINDOW]
    return {
        "scene": target_dict,
        "script_overview": _load_script_overview(expected_script_id, engine=engine),
        "characters_block": _format_characters(
            [dict(row) for row in character_rows], role_map=role_map
        ),
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


_ROLE_LABEL_ZH: Dict[str, str] = {
    "protagonist": "主角",
    "antagonist": "反派",
    "support": "配角",
    "minor": "龙套",
}


def _format_characters(
    rows: list[dict[str, Any]],
    *,
    role_map: Optional[Mapping[str, str]] = None,
) -> str:
    """渲染单剧人物总表给 execute LLM。

    role_map 来自 ``_load_character_role_map`` — 把每个 canonical_name/alias 映射到
    protagonist/antagonist/support/minor。execute prompt 用这个名单判定"哪些角色
    动了主线（绝对不允许删）/ 哪些角色可以压缩"。

    role_map 缺失（character 主链还没跑过的旧剧本）时 fallback 到不带 role 标签
    的旧渲染——保证不破坏现有改写链路。
    """
    if not rows:
        return "（暂无人物统计）"
    role_map = role_map or {}
    lines: list[str] = []
    for row in rows:
        name = str(row.get("character_name") or "").strip()
        appearances = row.get("appearances")
        role = role_map.get(name, "")
        role_tag = f"，{_ROLE_LABEL_ZH[role]}" if role in _ROLE_LABEL_ZH else ""
        lines.append(f"- {name}（出场 {appearances} 场{role_tag}）")
    return "\n".join(lines)


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
