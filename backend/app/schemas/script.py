"""ScriptLens 剧本 / 报告 / 评分相关 Pydantic Schema。

对应 router/script_rt.py 的请求/响应契约；与数据库表 scriptlens.{scripts,reports}
对齐。报告内部 schema 与 PRD §7 一致。

----
v3.3 line-range anchored citation（业内一致的"卡片+跳转高亮"基础设施）

不变量：
- 任何"卡片 → 跳转高亮"链路统一用 (scene_id, evidence_line_range) 双锚定
- evidence_line_range = [start_line, end_line]（1-based，闭区间，scene 内行号）
- evidence_quote / evidence 字符串字段**只用于 tooltip / preview 展示**，绝不参与跳转计算
- LLM 必须在写卡片同次输出时给出 line_range（场文本带 [L{n}] 行号标注后让 LLM 引用）
- 业内对照：GitHub PR review hunk / Cursor codebase index / NotebookLM citation /
  Sider AI PDF citation / Hypothesis 标注 都是 (container, range) 锚定

详见 docs/08 §3.8。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field


# evidence_line_range 的统一类型：[start_line, end_line]，1-based，闭区间
# 用 Tuple[int, int] 做约束，但 Pydantic v2 序列化为 list，前端 DTO 也用 [number, number]
LineRange = Tuple[int, int]


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


class ScriptDeleteResponse(BaseModel):
    """DELETE /api/scripts/{id} 响应。"""

    deleted: bool = True
    script_id: str
    title: str
    storage_deleted: bool
    deleted_counts: Dict[str, int] = Field(default_factory=dict)


# ============================================================
# 报告生成进度（GET /api/scripts/{id}/progress）
# ============================================================


ReportStageState = Literal["pending", "running", "done", "failed"]


class ReportStageInfo(BaseModel):
    """流水线一个阶段的状态。

    `description` 是给前端 tooltip 用的"这一步在做什么"，与 detail 的区别：
    - description 静态、面向所有人解释这一步的语义
    - detail 动态、运行时回写（如"已识别到 14 个爽点事件"）
    """

    id: str
    label: str
    description: str
    state: ReportStageState
    detail: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


class ReportProgressSnapshot(BaseModel):
    """5 维评分流水线的进度快照。"""

    script_id: str
    started_at: float
    updated_at: float
    final: bool = False
    error: Optional[str] = None
    current_index: int = 0
    stages: List[ReportStageInfo] = Field(default_factory=list)


class ReportProgressResponse(BaseModel):
    """GET /api/scripts/{id}/progress。

    snapshot 为 None 表示当前没有评分任务在跑（也没有 5 分钟内的旧快照）。
    """

    script_id: str
    snapshot: Optional[ReportProgressSnapshot] = None


# ============================================================
# 报告（PRD §7 schema —— 与 service.script_report_service 1:1 对齐）
# ============================================================


# 阅文五力（docs/08-evaluation-framework.md §3）；compliance 独立成 ReportPayload.compliance
DimensionName = Literal["story", "character", "concept", "emotion", "pacing"]
DecisionLabel = Literal["recommend_continue", "cautious_continue", "not_recommended"]
ConfidenceLevel = Literal["high", "medium", "low"]
DimensionLevel = Literal["high", "medium", "low"]
ComplianceLevel = Literal["high_risk", "medium_risk", "low_risk", "clean"]


class ReportDecision(BaseModel):
    """决策卡。`must_read_scene_ids` 引用 evidence_refs.id（前端点击跳原文）。"""

    label: DecisionLabel
    confidence: ConfidenceLevel
    one_sentence_reason: str
    summary: str = Field("", description="3-5 句剧本概览")


class ReportScorecardItem(BaseModel):
    """阅文五力 scorecard 的一项（docs/08-evaluation-framework.md §3）。

    失败模式：上游信号缺失 → score=null/level=null/reason 写明缺什么。
    前端展示规则：score 为 null 时不画分数条，只显示 reason。
    """

    dimension: DimensionName
    score: Optional[int] = Field(
        None,
        ge=0,
        le=10,
        description="0-10；上游信号缺失或维度不可评时为 null（不能伪造默认值）",
    )
    level: Optional[DimensionLevel] = Field(
        None,
        description="档位 high/medium/low；score=null 时同步为 null",
    )
    reason: str
    evidence_ref_ids: List[str] = Field(default_factory=list)


class ReportCompliance(BaseModel):
    """合规审核单独字段（docs/08-evaluation-framework.md §4）。

    与五力 scorecard 平级、独立展示；不计入 overall_score。
    high_risk 时强制 decision label = not_recommended（在 service 层硬约束，不通过分数透传）。
    """

    dimension: Literal["compliance"] = "compliance"
    score: Optional[int] = Field(None, ge=0, le=10)
    level: Optional[ComplianceLevel] = None
    reason: str = ""
    evidence_ref_ids: List[str] = Field(default_factory=list)


class ReportEvidenceRef(BaseModel):
    """evidence_refs[]：每条都是带 line_range 锚点的引用，前端高亮使用。

    v3.3 起：start_line/end_line 是**主锚点**，必须由 LLM 在产 evidence 时同次给出
    （而不是后端字符匹配反推）；quote 仅用于 tooltip 展示，前端绝不再做 quote 字符
    串匹配。详见 docs/08 §3.8。

    episode_no 单独暴露：前端要把"第 10 集第 3 场"这种人话坐标渲染给非技术用户，
    没有 episode_no 就只能裸显 scene_no="10-3"，对内容策划/审核完全是黑话。
    """

    id: str
    scene_id: str
    episode_no: Optional[int] = None
    scene_no: Optional[str] = None
    scene_label: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    quote: str = Field(
        ...,
        description="该 line_range 对应的原文片段，用作 tooltip / preview。前端跳转**不**依赖此字段",
    )
    quote_source: Optional[str] = Field(
        None,
        description=(
            "quote 来源标记，用于前端区分跳转含义："
            "`reward:<event_type>` = LLM 二筛识别的爽点 / 反转 evidence；"
            "`risk_hit` = 合规命中片段；"
            "`fallback_first_line` = 该场未被语义化匹配，用 extract_quote 兜底"
        ),
    )
    scene_summary: Optional[str] = Field(
        None,
        description="整场戏摘要（不是 quote 碎片），用于前端「三大看点」卡片",
    )
    reason: str
    confidence: ConfidenceLevel = "medium"


# 主要看点 / 钩子 / 反转 / 爽点 / 风险点：剧本叙事节点的统一抽象。
# 与 service.script_tools.reward_extractor.RewardEvent 的 event_type 取值对齐，
# 再补一个 'hook'（开场钩子，从 opening_hook 维度的 evidence 派生）和 'risk'（风险点）。
HighlightType = Literal[
    "hook",
    "face_slap",
    "reversal",
    "revenge",
    "cp_progress",
    "identity_reveal",
    "villain_fall",
    "underdog_rise",
    "scheme_exposed",
    "risk",
]

Recommendation = Literal["recommend", "consider", "pass"]
BeatType = Literal["opening", "inciting", "midpoint", "climax", "closing", "twist", "reward"]
CharacterRole = Literal["protagonist", "antagonist", "support", "minor"]
CharacterRelationType = Literal[
    "family",
    "romance",
    "rival",
    "ally",
    "authority",
    "deception",
    "mentor",
]
RelationPolarity = Literal["positive", "negative", "mixed"]


class ReportHighlight(BaseModel):
    """主要看点节点（前端按 type 分组渲染清单）。

    v3.3 line-range anchored：跳转锚点 = (scene_id, start_line, end_line)；
    `evidence` 仅作 tooltip。详见 docs/08 §3.8。

    给 task.md §三 列的"看点 / 钩子 / 反转 / 爽点"提供结构化数据：
    每条带 episode_no/scene_no/scene_label/scene_id（人话坐标 + 跳转锚点）+ oneliner（一句话点题）
    + 可选 evidence（原文片段，给 tooltip 用）。
    """

    id: str = Field(..., description="客户端用作高亮 key（与 evidence_refs.id 同空间）")
    type: HighlightType
    scene_id: str
    episode_no: Optional[int] = None
    scene_no: Optional[str] = None
    scene_label: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    oneliner: str = Field(..., description="≤ 40 字一句话点题")
    evidence: Optional[str] = Field(
        None,
        description="≤ 80 字原文片段，仅用于 tooltip / 折叠态展示。前端跳转**不**用此字段定位",
    )


class CoveragePoint(BaseModel):
    """Coverage Card 的优劣点。

    v3.3 line-range anchored citation：
    - `evidence_line_range` 是**主锚点**：[start_line, end_line]，LLM 写 detail 时同次给出
    - `evidence_quote` 仅用于 hover tooltip / preview 展示，**不**参与跳转计算
    - anchor_scene_id 为 null 时 evidence_line_range / evidence_quote 都为 null

    业内对照（GitHub PR review / Cursor codebase index / NotebookLM citation）：
    卡片描述 + 跳转锚点 + 展示文本必须由同一次 LLM 输出同时给出，下游不允许"反查另
    一个 evidence 表拿 quote"补救。
    """

    title: str = Field(..., description="≤ 12 字")
    detail: str = Field(..., description="≤ 80 字，面向选品/编剧/审核的人话说明")
    anchor_scene_id: Optional[str] = None
    evidence_line_range: Optional[LineRange] = Field(
        None,
        description=(
            "anchor_scene_id 那场内的行号区间 [start, end]（1-based 闭区间）。"
            "前端跳转高亮的**主锚点**——直接用 deltaDecorations 高亮这一区间。"
            "anchor_scene_id 为 null 时本字段也为 null。"
        ),
    )
    evidence_quote: Optional[str] = Field(
        None,
        description=(
            "evidence_line_range 对应的原文片段（≤ 80 字），仅用于 hover tooltip 展示。"
            "前端绝不要再用此字段做 quote 字符串匹配定位。"
        ),
    )


class CoverageCard(BaseModel):
    """30 秒决策层：借鉴 studio coverage 的 logline + recommendation + 优劣点。"""

    logline: str = Field(..., description="≤ 60 字一句话剧情概括")
    recommendation: Recommendation
    confidence: ConfidenceLevel = "medium"
    genre: List[str] = Field(default_factory=list, description="类型标签 1-3 个")
    core_value: str = Field("", description="≤ 30 字，这份剧本最值得关注的价值")
    strengths: List[CoveragePoint] = Field(default_factory=list)
    concerns: List[CoveragePoint] = Field(default_factory=list)


class BeatNode(BaseModel):
    """故事节拍节点，前端点击 anchor_scene_id 跳原文。"""

    type: BeatType
    summary: str = Field(..., description="≤ 50 字")
    anchor_scene_id: str


class BeatAct(BaseModel):
    """三幕骨架：开局 / 发展 / 收束。"""

    act: Literal[1, 2, 3]
    title: str
    scene_range: List[str] = Field(default_factory=list, min_length=0, max_length=2)
    beats: List[BeatNode] = Field(default_factory=list)


class BeatSheet(BaseModel):
    acts: List[BeatAct] = Field(default_factory=list)


class CharacterGraphNode(BaseModel):
    id: str
    name: str
    role: CharacterRole = "support"
    motivation: str = ""
    goal: str = ""
    obstacle: str = ""
    first_scene_id: Optional[str] = None
    appearance_count: int = 0


class CharacterGraphEdge(BaseModel):
    source_id: str
    target_id: str
    type: CharacterRelationType = "ally"
    weight: float = Field(0.0, ge=0.0, le=1.0)
    polarity: RelationPolarity = "mixed"


class CharacterGraph(BaseModel):
    nodes: List[CharacterGraphNode] = Field(default_factory=list)
    edges: List[CharacterGraphEdge] = Field(default_factory=list)


class PacingCurvePoint(BaseModel):
    episode_no: int
    scene_count: int = 0
    event_count: int = 0
    hooks: int = 0
    twists: int = 0
    reward_events: int = 0
    sentiment: float = Field(0.0, ge=-1.0, le=1.0)


class EvaluationDimension(BaseModel):
    key: DimensionName
    label: str
    score: Optional[int] = Field(None, ge=0, le=10)
    level: Optional[DimensionLevel] = None
    reason: str
    evidence_ref_ids: List[str] = Field(default_factory=list)


class EvaluationPayload(BaseModel):
    dimensions: List[EvaluationDimension] = Field(default_factory=list)
    risk_flags: List[str] = Field(default_factory=list)
    rewrite_seeds: List[Dict[str, Any]] = Field(default_factory=list)


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
    compliance: Optional[ReportCompliance] = Field(
        None,
        description=(
            "合规审核（docs/08-evaluation-framework.md §4），与五力 scorecard 平级独立。"
            "前端在右栏单独的「合规审核」面板展示；不参与 overall_score。"
        ),
    )
    evidence_refs: List[ReportEvidenceRef] = Field(default_factory=list)
    highlights: List[ReportHighlight] = Field(
        default_factory=list,
        description=(
            "主要看点 / 钩子 / 反转 / 爽点 / 风险节点清单；"
            "task.md §三 要求把『主要看点、钩子、反转、爽点』作为头等公民呈现给用户。"
        ),
    )
    coverage_card: Optional[CoverageCard] = None
    beat_sheet: Optional[BeatSheet] = None
    character_graph: Optional[CharacterGraph] = None
    pacing_curve: List[PacingCurvePoint] = Field(default_factory=list)
    evaluation: Optional[EvaluationPayload] = None
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


class SceneContentUpdateRequest(BaseModel):
    """PUT /api/scripts/{id}/scenes/{scene_id}/content 请求。

    AgentDiffReview reject hunk 时前端会算出 reverted text 调本端点写库；
    accept 全量保留路径不调本端点（改写工具已在 LLM 调用末尾 UPDATE 过）。
    详见 docs/10-rewrite-agent.md §6 diff 透明迁移机制。
    """

    content: str = Field(..., description="场景全文（无长度上限：剧本场常达数千字）")


class SceneContentUpdateResponse(BaseModel):
    scene_id: str
    char_count: int


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
        "story", "character", "concept", "emotion", "pacing"
    ] = Field(..., description="改写聚焦维度（阅文五力）")
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


# ViewRole 已移除：视角切换由前端「行动」segment 派生 Persona Action Card
# （详见 docs/09-action-lens.md），ViewResponse 不再按角色重排。


class RewriteSeed(BaseModel):
    """改写候选（任务派发器入口，详见 docs/03-system-mental-model.md §6）。

    报告**只产候选定位 + 触发**，不预生成 rewritten_excerpt。
    rewritten_excerpt / diff / rationale 由用户在 chat 触发 propose_rewrite_tool 实时生产。
    """

    dimension: DimensionName
    scene_id: str
    scene_label: Optional[str] = None
    issue: str = Field(..., description="一句话点明该场该维度的问题；派生自 scorecard.reason 第一句")
    evidence_ref_id: str = Field(..., description="对应报告里的某条证据，用于在编辑器联动高亮")


class RewriteTaskStatus(BaseModel):
    """单个 (scene_id, dimension) 上的改写任务状态（从 script_operations group by 派生）。

    报告卡片右上角徽章映射（详见 docs/03-system-mental-model.md §8）：
        - attempts=0                                                   → "未处理"
        - last_status=accepted                                          → "已采纳改写"
        - last_status in (proposed, rejected)，attempts>0                → "已尝试 N 次"
    """

    attempts: int = Field(0, description="该 (scene, dim) 上的改写次数")
    last_op_id: Optional[str] = Field(None, description="最近一次改写 op，前端可跳 timeline")
    last_status: Optional[Literal["proposed", "accepted", "rejected"]] = None
    last_at: Optional[datetime] = None


class ViewResponse(BaseModel):
    """GET /api/scripts/{id}/view 响应。

    透传 reports.report_json 全字段（scorecard 顺序固定为五力声明序，不按角色重排）；
    rewrite_seeds / task_status 为派生字段（不进 reports.report_json 持久层），
    详见 docs/03-system-mental-model.md §6 §8。

    视角由前端「行动」segment 派生 Persona Action Card，详见 docs/09-action-lens.md。
    """

    script_id: str
    decision: ReportDecision
    overall_score: Optional[float] = Field(
        None,
        ge=0,
        le=10,
        description="5 维加权聚合；当 ≥3 维评分证据不足时为 null（rubric §6）",
    )
    summary: str
    scorecard: List[ReportScorecardItem]
    compliance: Optional[ReportCompliance] = None
    must_read_scene_ids: List[str]
    risk_flags: List[str]
    evidence_refs: List[ReportEvidenceRef] = Field(
        default_factory=list,
        description=(
            "原报告里的全部证据片段；前端拿 must_read_scene_ids / scorecard.evidence_ref_ids "
            "去 join 这里的 id 拿 quote / scene_label，避免再多调一次 GET /report。"
        ),
    )
    highlights: List[ReportHighlight] = Field(
        default_factory=list,
        description="主要看点 / 钩子 / 反转 / 爽点 / 风险节点清单（透传自 ReportPayload.highlights）",
    )
    coverage_card: Optional[CoverageCard] = None
    beat_sheet: Optional[BeatSheet] = None
    character_graph: Optional[CharacterGraph] = None
    pacing_curve: List[PacingCurvePoint] = Field(default_factory=list)
    evaluation: Optional[EvaluationPayload] = None
    rewrite_seeds: List[RewriteSeed] = Field(
        default_factory=list,
        description=(
            "派生：从 score<7 的五力维度第一条 evidence 派生的改写候选，"
            "前端在报告里渲染「最值得改的 N 场」卡组，点击后 dispatchTask({kind:'rewrite_seed'})。"
            "合规违规不进改写候选（合规问题需人工二次审核，不交给 LLM 改写）。"
        ),
    )
    task_status: Dict[str, RewriteTaskStatus] = Field(
        default_factory=dict,
        description=(
            "派生：key=`{scene_id}:{dimension}`，value=该维度该场上的改写任务状态。"
            "前端按此 key lookup 渲染卡片右上角徽章。"
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
