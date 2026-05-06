"""ScriptLens 剧本 / 报告 / 评分相关 Pydantic Schema。

对应 router/script_rt.py 的请求/响应契约；与数据库表 scriptlens.{scripts,reports}
对齐。报告内部 schema 与 PRD §7 一致。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ============================================================
# 上传 / 状态
# ============================================================


ScriptStatus = Literal["pending", "parsing", "indexing", "ready", "failed"]


class ScriptUploadResponse(BaseModel):
    """POST /api/scripts/upload 响应。

    立即返回（异步两阶段）。前端拿 id 后轮询 GET /scripts/{id}。
    """

    id: str
    title: str
    source_format: str
    status: ScriptStatus = "pending"


class ScriptDetail(BaseModel):
    """GET /api/scripts/{id} 详情。

    ready 状态时 total_* 字段非空；failed 状态时 failure_reason 非空。
    """

    id: str
    title: str
    source_format: str
    status: ScriptStatus
    total_episodes: Optional[int] = None
    total_scenes: Optional[int] = None
    total_chars: Optional[int] = None
    failure_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ScriptListItem(BaseModel):
    """GET /api/scripts 列表项（精简版）。"""

    id: str
    title: str
    status: ScriptStatus
    total_episodes: Optional[int] = None
    total_scenes: Optional[int] = None
    created_at: datetime


# ============================================================
# 报告（PRD §7 schema —— 与 service.script_report_service 1:1 对齐）
# ============================================================


DimensionName = Literal["opening_hook", "reward_density", "motivation", "pacing", "risk"]
DecisionLabel = Literal["recommend_continue", "cautious_continue", "not_recommended"]
ConfidenceLevel = Literal["high", "medium", "low"]
DimensionLevel = Literal["high", "medium", "low", "high_risk", "medium_risk", "low_risk", "clean"]


class ReportDecision(BaseModel):
    """决策卡。`must_read_scene_ids` 引用 evidence_refs.id（前端点击跳原文）。"""

    label: DecisionLabel
    confidence: ConfidenceLevel
    one_sentence_reason: str
    summary: str = Field("", description="3-5 句剧本概览")


class ReportScorecardItem(BaseModel):
    """5 维 scorecard 的一项。

    rubric §6 失败模式：LLM 二次未给出 evidence → score=null/level=null/reason="证据不足"。
    前端展示规则：score 为 null 时不画分数条，只显示 reason。
    """

    dimension: DimensionName
    score: Optional[int] = Field(
        None,
        ge=0,
        le=10,
        description="0-10；rubric §6 证据不足或维度不可评时为 null（不能伪造默认值）",
    )
    level: Optional[DimensionLevel] = Field(
        None,
        description="档位；score=null 时同步为 null",
    )
    reason: str
    evidence_ref_ids: List[str] = Field(default_factory=list)


class ReportEvidenceRef(BaseModel):
    """evidence_refs[]：每条都是带原文 quote 的引用，前端高亮使用。"""

    id: str
    scene_id: str
    scene_no: Optional[str] = None
    scene_label: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    quote: str
    reason: str
    confidence: ConfidenceLevel = "medium"


class ReportPayload(BaseModel):
    """整份分析报告 JSON（落库到 reports.report_json）。对应 PRD §7。"""

    script_id: str
    title: str
    decision: ReportDecision
    decision_reason: str = Field("", description="兼容字段；与 decision.one_sentence_reason 一致")
    overall_score: Optional[float] = Field(
        None,
        ge=0,
        le=10,
        description="5 维加权聚合；当 ≥3 维评分证据不足时为 null（rubric §6）",
    )
    summary: str = Field("", description="冗余字段：与 decision.summary 一致")
    must_read_scene_ids: List[str] = Field(
        default_factory=list,
        description="evidence_refs.id 列表（最多 3 个），不是 scene_id",
    )
    scorecard: List[ReportScorecardItem]
    evidence_refs: List[ReportEvidenceRef] = Field(default_factory=list)
    risk_flags: List[str] = Field(default_factory=list)
    report_id: Optional[str] = None
    generated_at: Optional[str] = None


class ReportResponse(BaseModel):
    """GET /api/scripts/{id}/report 响应（status='ready' + 报告已生成）。"""

    script_id: str
    report: ReportPayload
    generated_at: datetime


class ReportNotReadyResponse(BaseModel):
    """status != 'ready' 或评分尚未生成时的响应（200 + status，让前端轮询）。"""

    script_id: str
    status: ScriptStatus
    failure_reason: Optional[str] = None


# ============================================================
# 场景视图（GET /scripts/{id}/scenes，前端编辑器渲染原文用）
# ============================================================


class SceneItem(BaseModel):
    id: str
    episode_no: Optional[int] = None
    scene_no: str
    scene_label: str = ""
    characters: List[str] = Field(default_factory=list)
    text: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


class ScriptScenesResponse(BaseModel):
    script_id: str
    total: int
    scenes: List[SceneItem]


# ============================================================
# Chat / Rewrite（D2-6）
# ============================================================


ChatRole = Literal["selection", "writer", "review", "rewrite", "general"]


class ChatHistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ScriptChatRequest(BaseModel):
    """POST /api/scripts/{id}/chat 请求。

    历史由前端维护并随每次请求传入（后端 chat 无状态）。后续如需服务端
    会话持久化再加 chat_sessions 表。
    """

    question: str = Field(..., min_length=1, max_length=4000)
    history: List[ChatHistoryItem] = Field(
        default_factory=list,
        description="按时间正序的历史消息（最近 N 条由前端裁剪）",
    )
    role: ChatRole = Field(
        "general",
        description="用户角色（影响 prompt 注入）：selection/writer/review/rewrite/general",
    )


class RewriteRequest(BaseModel):
    """POST /api/scripts/{id}/rewrite 请求。"""

    scene_id: str = Field(..., description="目标场景 ID（来自 /scenes 列表）")
    target_dimension: Literal[
        "opening_hook", "reward_density", "motivation", "pacing", "risk"
    ] = Field(..., description="改写聚焦维度")
    issue: str = Field(..., min_length=1, max_length=500, description="问题描述（如'动机不成立'）")


class RewriteResponse(BaseModel):
    """POST /api/scripts/{id}/rewrite 响应。"""

    script_id: str
    scene_id: str
    target_dimension: str
    issue: str
    original_text: str
    rewritten_text: str
    rationale: str
    diff: str = Field(..., description="unified diff（含原文 vs 改写）")


# ============================================================
# Feedback（D2-6c）：P3 加分项 - 轻量 skill 机制
# ============================================================


FeedbackScope = Literal["general", "dimension", "rewrite", "scene"]


class FeedbackRequest(BaseModel):
    """POST /api/scripts/{id}/feedback 请求。"""

    scope: FeedbackScope = Field(..., description="反馈类型：general/dimension/rewrite/scene")
    scope_ref: Optional[str] = Field(
        None,
        description="作用域引用：dimension=维度名 / scene=scene_id / rewrite=rewrite_id；general 可空",
    )
    message: str = Field(..., min_length=1, max_length=2000, description="用户反馈正文")


class FeedbackItem(BaseModel):
    id: str
    scope: FeedbackScope
    scope_ref: Optional[str] = None
    message: str
    created_at: datetime


class FeedbackListResponse(BaseModel):
    script_id: str
    items: List[FeedbackItem]


# ============================================================
# View（D2-6d）：按角色重排报告
# ============================================================


ViewRole = Literal["selection", "writer", "review"]


class ViewResponse(BaseModel):
    """GET /api/scripts/{id}/view?role=... 响应。

    不重生成评分，仅基于 reports.report_json 按 role 重排 scorecard 优先级
    + 重选 must_read_scene_ids。报告未生成时返回 not_ready 兜底。
    """

    script_id: str
    role: ViewRole
    decision: ReportDecision
    overall_score: Optional[float] = Field(
        None,
        ge=0,
        le=10,
        description="5 维加权聚合；当 ≥3 维评分证据不足时为 null（rubric §6）",
    )
    summary: str
    scorecard: List[ReportScorecardItem]
    must_read_scene_ids: List[str]
    risk_flags: List[str]
    role_focus: List[str] = Field(
        default_factory=list,
        description="该角色优先关注的维度（已排在 scorecard 前面）",
    )
    evidence_refs: List[ReportEvidenceRef] = Field(
        default_factory=list,
        description=(
            "原报告里的全部证据片段；前端拿 must_read_scene_ids / scorecard.evidence_ref_ids "
            "去 join 这里的 id 拿 quote / scene_label，避免再多调一次 GET /report。"
        ),
    )


# ============================================================
# Operations（M4 timeline）：改写/上传/编辑历史
# ============================================================


OperationIntentType = Literal["rewrite", "upload", "manual_edit"]
OperationVersion = Literal["before", "after"]


class OperationSummary(BaseModel):
    """字段对齐前端 `DocStudioAPI.OperationSummary`，doc-studio timeline 直接消费。"""

    operation_id: str
    workspace_id: str = Field(..., description="即 script_id（doc-studio 协议命名）")
    user_id: int
    timestamp: datetime
    success: bool = True
    intent_type: Optional[OperationIntentType] = None
    user_intent: str
    modified_files: List[str] = Field(default_factory=list)
    snapshot: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "紧凑摘要（target_dimension / rationale / issue），不含完整 before/after 文本；"
            "需要原文请走 GET /operations/{op_id}/snapshot"
        ),
    )


class OperationListResponse(BaseModel):
    script_id: str
    items: List[OperationSummary]


class OperationSnapshotResponse(BaseModel):
    """对齐前端 `DocStudioAPI.FileContentResponse`。"""

    path: str
    content: str
    encoding: str = "utf-8"


class RevertOperationResponse(BaseModel):
    """ScriptLens 不真改 scenes.text，所以回退是 no-op：始终返回三份空数组。

    前端复用 ScholarMind 协议会照常显示 toast；调用方会根据 reverted_files 长度
    判断"是否生效"，因此空数组等价于"无变更"，不会触发 file refetch。
    """

    operation_id: str
    reverted_files: List[str] = Field(default_factory=list)
    deleted_files: List[str] = Field(default_factory=list)
    skipped_files: List[str] = Field(default_factory=list)


# ============================================================
# 通用
# ============================================================


class ApiError(BaseModel):
    code: str
    message: str
    detail: Optional[Any] = None
