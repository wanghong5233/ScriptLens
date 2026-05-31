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
    workspace_config: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ScriptWorkspaceUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    config: Optional[Dict[str, Any]] = Field(default=None)


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
    """评分流水线的进度快照。"""

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


# 阅文六维（Batch 3）；compliance 独立成 ReportPayload.compliance
DimensionName = Literal["story", "character", "concept", "emotion", "pacing", "dialogue"]
DecisionLabel = Literal["recommend_continue", "cautious_continue", "not_recommended"]
ConfidenceLevel = Literal["high", "medium", "low"]
TierName = Literal["excellent", "good", "weak", "poor", "insufficient"]
ComplianceLevel = Literal["high_risk", "medium_risk", "low_risk", "clean"]

# 主要看点 / 钩子 / 反转 / 爽点 / 风险点：与 reward_extractor.RewardEvent 取值对齐，
# 再补一个 'hook'（开场钩子，从 opening_hook 维度 evidence 派生）和 'risk'（风险点）。
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


class ReportDecision(BaseModel):
    """决策卡。"""

    label: DecisionLabel
    confidence: ConfidenceLevel
    one_sentence_reason: str
    summary: str = Field("", description="3-5 句剧本概览")
    decision_inputs: Dict[str, Any] = Field(default_factory=dict)


class ReportScorecardItem(BaseModel):
    """六维 scorecard 的一项（docs/08-evaluation-framework.md §3）。

    失败模式：上游信号缺失 → score=null/tier=insufficient/reason 写明缺什么。
    前端展示规则：score 为 null 时不画分数条，只显示 reason。
    """

    dimension: DimensionName
    score: Optional[float] = Field(
        None,
        ge=0,
        le=10,
        description="0-10；coverage 不足时可为 null",
    )
    tier: TierName = "insufficient"
    confidence: ConfidenceLevel = "low"
    coverage_ratio: Optional[float] = Field(None, ge=0, le=1)
    signal_refs: List[Dict[str, Any]] = Field(default_factory=list)
    top_signals: List[Dict[str, Any]] = Field(default_factory=list)
    tier_cuts: Dict[str, float] = Field(default_factory=dict)
    reason: str
    evidence_ref_ids: List[str] = Field(default_factory=list)


class ReportComplianceHit(BaseModel):
    scene_id: str
    scene_no: Optional[str] = None
    episode_no: Optional[int] = None
    level: ComplianceLevel
    category: str
    matched_term: str
    evidence_line_range: Optional[LineRange] = None
    excerpt: str = ""
    confirmed_by_llm: bool = False


class ReportCompliance(BaseModel):
    """合规审核单独字段（docs/08-evaluation-framework.md §4）。

    与 scorecard 平级、独立展示；不计入 overall_score。
    high_risk 时强制 decision label = not_recommended（在 service 层硬约束，不通过分数透传）。
    """

    dimension: Literal["compliance"] = "compliance"
    score: Optional[int] = Field(None, ge=0, le=10)
    level: Optional[ComplianceLevel] = None
    tier: TierName = "insufficient"
    confidence: ConfidenceLevel = "low"
    status: Literal["pass", "warn", "blocked"] = "pass"
    reason: str = ""
    evidence_ref_ids: List[str] = Field(default_factory=list)
    hits: List[ReportComplianceHit] = Field(default_factory=list)


class ReportEvidenceRef(BaseModel):
    """evidence_refs[]：每条都是带 line_range 锚点的引用，前端高亮使用。

    v3.4 起（W3C TextQuoteSelector + Anthropic Citations pattern）：
    - quote 字段是 LLM verbatim 输出，**后端在 scene_text 内 reconcile 通过**才落
      库（quote_verified=True）；reconcile 失败时 line_range 留 None，前端做整场跳转。
    - quote_verified 用于前端区分「精确跳到行 / 仅整场跳转」的视觉与 tooltip 文案。
    - start_line / end_line 仍是跳转主锚点；quote 兼作 tooltip 与可选的 verbatim 二次确认。
    """

    id: str
    scene_id: str
    episode_no: Optional[int] = None
    scene_no: Optional[str] = None
    scene_label: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    quote: str = Field(
        "",
        description=(
            "**仅 quote_verified=True 时填**：LLM 给的 verbatim 原文片段（W3C TextQuoteSelector.exact），"
            "已通过后端 reconcile 在 scene_text 内被唯一定位。"
            "verified=False 时为空字符串（不再用 claim 兜底，避免污染 quote 字段语义）"
        ),
    )
    claim: str = Field(
        "",
        description=(
            "LLM 对该 evidence 的中文诠释（≤80 字），与原文 quote 解耦。"
            "always 可用，前端 tooltip / 卡片描述主字段。"
        ),
    )
    quote_source: Optional[str] = Field(
        None,
        description=(
            "quote 来源标记：`reward:<event_type>` / `risk_hit` / `fallback_first_line`"
        ),
    )
    quote_verified: bool = Field(
        False,
        description=(
            "verbatim 是否在 scene_text 内被 reconcile 唯一定位成功。"
            "True 表示前端可精确高亮 quote/line_range；False 表示该 ref 没拿到 verbatim 行号锚点，"
            "前端应做整场跳转，tooltip 展示 claim 文本"
        ),
    )
    scene_summary: Optional[str] = Field(
        None,
        description="整场戏摘要（不是 quote 碎片），用于前端「三大看点」卡片",
    )
    reason: str
    confidence: ConfidenceLevel = "medium"


class ReportHighlight(BaseModel):
    """主要看点节点（前端按 type 分组渲染清单）。

    v3.3 line-range anchored：跳转锚点 = (scene_id, start_line, end_line)；
    `evidence` 仅作 tooltip。
    """

    id: str = Field(..., description="客户端用作高亮 key（与 evidence_refs.id 同空间）")
    type: HighlightType
    scene_id: str
    episode_no: Optional[int] = None
    scene_no: Optional[str] = None
    scene_label: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    oneliner: str = Field(..., description="一句话点题（完整展示，不截断）")
    evidence: Optional[str] = Field(
        None,
        description="≤ 80 字原文片段，仅用于 tooltip / 折叠态展示。前端跳转**不**用此字段定位",
    )


class CoveragePoint(BaseModel):
    """Coverage Card 的优劣点（v3.7.3 升级）：业内 Hollywood Coverage Report 的
    Strengths / Weaknesses 段标准做法 —— 每条 point 是 title + 一句话评价 +
    可展开的深度分析 + 维度归属 + 证据锚点提示。

    业内对照：
      - Hollywood Coverage Report: 每条加粗 title + 100-300 字展开分析 + 引用页码/场号
      - ReelShort / 抖音红果选品端: title + 维度归属（钩子/爽点/反转）+ 例子场号
      - Industrial Scripts Coverage Standard: 1 句话评价 + 段落分析 + 证据引用

    `analysis` 可选——LLM 给得出就有，给不出前端只显示 title + detail（向后兼容）。
    `dimension` 对齐 6 维评分（story/character/concept/emotion/pacing/dialogue），
    前端用对应维度色和评分卡颜色保持一致。
    """

    title: str = Field(..., description="≤ 12 字 标题")
    detail: str = Field(..., description="≤ 80 字，一句话评价")
    analysis: str = Field(
        "",
        description="≤ 300 字 展开深度分析（结合剧本具体桥段说明 why）—— 前端默认折叠，点击展开",
    )
    dimension: str = Field(
        "",
        description=(
            "维度归属（story/character/concept/emotion/pacing/dialogue），"
            "对齐 6 维评分。空字符串表示综合性 point。"
        ),
    )
    evidence_hint: str = Field(
        "",
        description="≤ 60 字 证据线索（如「第 17 集 · 姜栀枝揭面」），引导用户去原文找例子",
    )


class ComparableTitleEntry(BaseModel):
    """同类爆款条目（v3.7.1）：基于剧本题材直接搜垂直平台短剧视频，返回真实链接。

    业内对照：
      - Hollywood Coverage Comparable Titles：纯文本锚点
      - Reelytics / Parrot Analytics 选品工具：基于题材+卖点搜视频平台
      - Perplexity Discover / Tavily best practice：多 query 并发 + site 限定 + 聚合去重

    v3.7.1 实现：放弃 LLM 编造剧名（幻觉太严重），改为用 coverage.genre + logline
    构造多个搜索 query，并发跑 Tavily advanced search 限定垂直平台（抖音/西瓜/B站/快手），
    聚合去重 + 平台权重排序，保底返回 3 条。

    `platform` 用于前端按平台上色（抖音红 / 西瓜橙 / B 站蓝 / 快手浅橙 / 其他灰）。
    """

    title: str = Field(..., description="真实视频/文章标题（来自搜索结果）或 LLM 给的剧名")
    url: Optional[str] = Field(
        None,
        description="Tavily 搜索校验后的真实视频链接；失败/未配置 search key 时为 None",
    )
    platform: Optional[str] = Field(
        None,
        description="平台标识：douyin / ixigua / bilibili / kuaishou / other / fallback",
    )
    snippet: Optional[str] = Field(
        None,
        description="搜索结果摘要，前端 chip hover 时展示",
    )


class CoverageCard(BaseModel):
    """30 秒决策层（v3.7）：对齐 Hollywood Studio Coverage / ReelShort 选品端 6 段范式。

    `synopsis` 是工业 coverage 必有项：200-300 字全剧故事浓缩，前端在速览第二屏直接展开。
    `comparable_titles` v3.7 起从纯字符串升级为 `ComparableTitleEntry`（带跳转 URL）。
    """

    logline: str = Field(..., description="≤ 60 字一句话剧情概括")
    synopsis: str = Field("", description="200-300 字全剧故事浓缩")
    recommendation: Recommendation
    confidence: ConfidenceLevel = "medium"
    genre: List[str] = Field(default_factory=list, description="类型标签 1-3 个")
    core_value: str = Field("", description="≤ 30 字，这份剧本最值得关注的价值")
    strengths: List[CoveragePoint] = Field(default_factory=list)
    concerns: List[CoveragePoint] = Field(default_factory=list)
    comparable_titles: List[ComparableTitleEntry] = Field(
        default_factory=list,
        description="同类爆款 2-3 部，每条含 title + Tavily 校验后的 url（命中时）",
    )


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


class ReportDramaTag(BaseModel):
    key: str
    value: str
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class ReportPlotUnit(BaseModel):
    plot_unit_id: str
    episode_no: Optional[int] = None
    plot_unit_no: int
    summary: str = ""
    narrative_intensity: int = Field(0, ge=0, le=8)
    plot_hook: str = "none"
    conflict_type: str = "none"
    payoff_type: str = "none"
    emotional_driver: str = "none"
    story_stage: str = "none"
    scene_refs: List[str] = Field(default_factory=list)


class ReportCharacter(BaseModel):
    id: str
    name: str
    aliases: List[str] = Field(default_factory=list)
    archetype: str = ""
    role_in_arc: str = ""
    arc_type: str = ""
    agency_level: str = ""
    appearance_count: int = 0


class ReportCharacterRelationship(BaseModel):
    id: str
    a_id: str
    b_id: str
    type: str = ""
    polarity: str = ""
    dynamic_arc: str = ""
    triangle: str = ""


class CharacterBioOutfit(BaseModel):
    """外貌 → 服装结构化子字段，给下游 T2I/Seedance 拼 prompt 用。"""

    material: str = ""
    palette: str = ""
    form: str = ""


class CharacterBioAppearance(BaseModel):
    """外貌结构化字段。空字段一律保留 ""，下游拼 prompt 时按需取用。"""

    age: str = ""
    height: str = ""
    build: str = ""
    facial: str = ""
    signature_props: List[str] = Field(default_factory=list)
    outfit: CharacterBioOutfit = Field(default_factory=CharacterBioOutfit)


class CharacterBioCatchphrase(BaseModel):
    quote: str
    scene_id: str = ""


class CharacterBioRelation(BaseModel):
    other_id: str
    sentence: str


class CharacterBioNotableScene(BaseModel):
    """该角色最具代表性的某场行为；scene_id 必须命中 scenes 真实 id。

    behavior 是"该角色在该场做了什么"的一段描述，不是场景梗概；前端"关键场景"
    子区段渲染时点击可跳到 scene_id 对应位置（与台词卡的 onTraceEvidence 同一通道）。
    """

    scene_id: str
    behavior: str = ""


class ReportCharacterBio(BaseModel):
    """单个角色的小传 payload；character_id 关联 ReportCharacter.id。

    字段语义对齐 docs/prompt.jpg：身份（三段）/ 外貌（结构化）/ 性格 / 经典台词 /
    与关键角色关系。前端在「人物」tab 卡片展开态渲染；下游高光集锦
    （docs/2026-05-29-...-需求与方案.md §5.1 物料层）拼 T2I/Seedance prompt
    时直接读 appearance / signature_props / outfit。

    v2（08_extend_character_bios）新增：
    - dialogue_style：说话风格段落（对齐 Sudowrite Dialogue Style）
    - notable_scenes：3 场代表行为（带 scene_id 回链，前端可点跳原文）
    """

    id: str
    character_id: str
    # v3.7.5c (2026-05-31)：性别字段。"male" / "female" / "unknown"。
    # 抽取来源：LLM 主路径 + 本地代词频次规则兜底。"unknown" 时前端不渲染 chip。
    gender: Literal["male", "female", "unknown"] = "unknown"
    identity_present: str = ""
    identity_hidden: str = ""
    identity_origin: str = ""
    appearance: CharacterBioAppearance = Field(default_factory=CharacterBioAppearance)
    persona_surface: str = ""
    persona_core: str = ""
    weakness: str = ""
    arc_light: str = ""
    dialogue_style: str = ""
    catchphrases: List[CharacterBioCatchphrase] = Field(default_factory=list)
    relations_summary: List[CharacterBioRelation] = Field(default_factory=list)
    notable_scenes: List[CharacterBioNotableScene] = Field(default_factory=list)
    bio_ver: str = "v1"
    source: str = "llm"


class PacingCurvePoint(BaseModel):
    """场景粒度节奏点（v4 emotion-arc）。详见 docs/2026-05-30-pacing-curve-v4.md。"""

    progress: float = Field(..., ge=0, le=1)
    episode_no: Optional[int] = None
    scene_no: str
    scene_id: str
    sentiment: float = Field(0.0, ge=-1, le=1)


class PacingCurveBeat(BaseModel):
    """曲线上的节拍/反转锚点；前端点击 scene_id 跳原文。"""

    progress: float = Field(..., ge=0, le=1)
    beat_type: Literal[
        "opening",
        "inciting",
        "midpoint",
        "climax",
        "closing",
        "reward_spike",
    ]
    label: str
    summary: str = ""
    scene_id: str


class PacingCurveDeadZone(BaseModel):
    """死区段：连续 ≥6 场无情感波动且无 reward 命中。"""

    start_progress: float = Field(..., ge=0, le=1)
    end_progress: float = Field(..., ge=0, le=1)
    span_scenes: int = 0


PacingCurveShape = Literal[
    "rags_to_riches",
    "tragedy",
    "man_in_hole",
    "icarus",
    "cinderella",
    "oedipus",
    "flat",
    "complex",
]


class PacingCurve(BaseModel):
    """节奏曲线（v4）：情感命运曲线 + 节拍锚点 + 死区。"""

    shape: PacingCurveShape = "complex"
    shape_label: str = "复杂"
    climax_progress: float = Field(0.0, ge=0, le=1)
    points: List[PacingCurvePoint] = Field(default_factory=list)
    beats: List[PacingCurveBeat] = Field(default_factory=list)
    dead_zones: List[PacingCurveDeadZone] = Field(default_factory=list)


class EvaluationDimension(BaseModel):
    key: DimensionName
    label: str
    score: Optional[float] = Field(None, ge=0, le=10)
    tier: TierName = "insufficient"
    confidence: ConfidenceLevel = "low"
    coverage_ratio: Optional[float] = Field(None, ge=0, le=1)
    reason: str
    signal_refs: List[Dict[str, Any]] = Field(default_factory=list)
    evidence_ref_ids: List[str] = Field(default_factory=list)
    top_signals: List[Dict[str, Any]] = Field(default_factory=list)
    tier_cuts: Dict[str, float] = Field(default_factory=dict)


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
        description="6 维加权聚合；当关键维证据不足时可为 null",
    )
    summary: str = Field("", description="冗余字段：与 decision.summary 一致")
    scorecard: List[ReportScorecardItem]
    compliance: Optional[ReportCompliance] = Field(
        None,
        description=(
            "合规审核（docs/08-evaluation-framework.md §4），与 scorecard 平级独立。"
            "前端在右栏单独的「合规审核」面板展示；不参与 overall_score。"
        ),
    )
    drama_tags: List[ReportDramaTag] = Field(default_factory=list)
    plot_units: List[ReportPlotUnit] = Field(default_factory=list)
    characters: List[ReportCharacter] = Field(default_factory=list)
    character_relationships: List[ReportCharacterRelationship] = Field(default_factory=list)
    character_bios: List[ReportCharacterBio] = Field(
        default_factory=list,
        description=(
            "人物小传清单（character_id 关联 characters[].id）。"
            "前端「人物」tab 卡片展开态渲染；下游高光集锦物料层用 appearance 拼 T2I prompt。"
        ),
    )
    must_read_scene_ids: List[str] = Field(
        default_factory=list,
        description="evidence_refs.id 列表（最多 3 个），不是 scene_id",
    )
    evidence_refs: List[ReportEvidenceRef] = Field(default_factory=list)
    highlights: List[ReportHighlight] = Field(
        default_factory=list,
        description="主要看点 / 钩子 / 反转 / 爽点 / 风险节点清单",
    )
    coverage_card: Optional[CoverageCard] = None
    beat_sheet: Optional[BeatSheet] = None
    character_graph: Optional[CharacterGraph] = None
    pacing_curve: Optional[PacingCurve] = None
    evaluation: Optional[EvaluationPayload] = None
    risk_flags: List[str] = Field(default_factory=list)
    report_id: Optional[str] = None
    generated_at: Optional[str] = None
    # W1.3 (2026-05-31)：报告级 provenance 元数据（chain_status + overall_status）。
    # 由 generate_report 写入；前端用来渲染降级提示条。详见 chain_result.py。
    meta: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "报告级元数据，含 chain_status (每个 chain 的 status/source/fallback_reasons)"
            " 和 overall_status (ok | degraded)。"
        ),
    )


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
    """POST /api/scripts/{id}/chat request."""

    # 给 agent / LLM 看的 prompt：前端会把 <SELECTION> block 内联进来，可能很长。
    # 因此 max_length 调大到 32k，避免单选区/多选区时直接被 422 拒绝。
    question: str = Field(..., min_length=1, max_length=32000)
    # 持久化到 messages.user_question 的"展示版本"，含 @selection1 / @scene1 这种
    # 短 placeholder，UI 刷新后还原成 chip。缺省 fallback 到 question——
    # 保持对旧客户端兼容，但旧客户端刷新后会看到 inline 的 <SELECTION> XML。
    display_text: Optional[str] = Field(default=None, max_length=4000)
    history: List[ChatHistoryItem] = Field(default_factory=list)
    role: ChatRole = Field("general")
    context: Optional[Dict[str, Any]] = Field(default=None)
    session_id: Optional[str] = Field(default=None)


class RewriteRequest(BaseModel):
    """POST /api/scripts/{id}/rewrite 请求。"""

    scene_id: str = Field(..., description="目标场景 ID（来自 /scenes 列表）")
    target_dimension: Literal[
        "story", "character", "concept", "emotion", "pacing", "dialogue"
    ] = Field(..., description="改写聚焦维度（阅文六维）")
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

    id: Optional[str] = None
    dimension: DimensionName
    signal_key: str = ""
    scene_id: Optional[str] = None
    scene_label: Optional[str] = None
    issue: str = Field(..., description="一句话点明该场该维度的问题；派生自 scorecard.reason 第一句")
    target: str = ""
    action_steps: List[str] = Field(default_factory=list)
    evidence_refs: List[Dict[str, Any]] = Field(default_factory=list)
    estimated_lift: Dict[str, float] = Field(default_factory=dict)
    evidence_ref_id: Optional[str] = Field(
        None,
        description="兼容字段：旧链路的单证据引用 ID",
    )


class RewriteTaskStatus(BaseModel):
    """单个 (scene_id, dimension) 上的改写任务状态（从 script_operations group by 派生）。

    报告卡片右上角徽章映射（详见 docs/03-system-mental-model.md §8）：
        - attempts=0                                                   → "未处理"
        - last_status=accepted                                          → "已采纳改写"
        - last_status in (proposed, rejected)，attempts>0                → "已尝试 N 次"
    """

    attempts: int = Field(0, description="该 (scene, dim) 上的改写次数")
    last_op_id: Optional[str] = Field(
        None,
        description="最近一次改写 op（显式来源协议：db:<uuid> 或 history:<id>）",
    )
    last_status: Optional[Literal["proposed", "accepted", "rejected"]] = None
    last_at: Optional[datetime] = None


class ViewResponse(BaseModel):
    """GET /api/scripts/{id}/view 响应。

    透传 reports.report_json 全字段（scorecard 顺序固定，不按角色重排）；
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
        description="6 维加权聚合；当关键维证据不足时可为 null",
    )
    summary: str
    scorecard: List[ReportScorecardItem]
    compliance: Optional[ReportCompliance] = None
    drama_tags: List[ReportDramaTag] = Field(default_factory=list)
    plot_units: List[ReportPlotUnit] = Field(default_factory=list)
    characters: List[ReportCharacter] = Field(default_factory=list)
    character_relationships: List[ReportCharacterRelationship] = Field(default_factory=list)
    character_bios: List[ReportCharacterBio] = Field(
        default_factory=list,
        description="人物小传清单；透传自 ReportPayload.character_bios。",
    )
    must_read_scene_ids: List[str] = Field(default_factory=list)
    risk_flags: List[str] = Field(default_factory=list)
    evidence_refs: List[ReportEvidenceRef] = Field(
        default_factory=list,
        description="原报告里的全部证据片段；前端拿 must_read_scene_ids / scorecard.evidence_ref_ids 去 join",
    )
    highlights: List[ReportHighlight] = Field(
        default_factory=list,
        description="主要看点 / 钩子 / 反转 / 爽点 / 风险节点清单（透传自 ReportPayload.highlights）",
    )
    coverage_card: Optional[CoverageCard] = None
    beat_sheet: Optional[BeatSheet] = None
    character_graph: Optional[CharacterGraph] = None
    pacing_curve: Optional[PacingCurve] = None
    evaluation: Optional[EvaluationPayload] = None
    rewrite_seeds: List[RewriteSeed] = Field(
        default_factory=list,
        description=(
            "派生：从 score<7 的维度第一条 evidence 派生的改写候选，"
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
    # W1.3 (2026-05-31)：透传 ReportPayload.meta。
    meta: Optional[Dict[str, Any]] = Field(
        default=None,
        description="报告级 provenance 元数据（chain_status + overall_status）",
    )


# ============================================================
# Operations（M4 timeline）：改写/上传/编辑历史
# ============================================================


OperationIntentType = Literal["rewrite", "upload", "manual_edit"]
OperationVersion = Literal["before", "after"]


class OperationSummary(BaseModel):
    """字段对齐前端 `DocStudioAPI.OperationSummary`，doc-studio timeline 直接消费。"""

    operation_id: str = Field(
        ...,
        description="操作引用 ID（显式来源协议）：db:<uuid> 或 history:<operation_id>",
    )
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


class RevertOperationRequest(BaseModel):
    files: List[str] = Field(
        default_factory=list,
        description="可选：仅回滚这些文件；为空表示按该操作快照可恢复的全部文件回滚。",
    )


class RevertOperationResponse(BaseModel):
    """回退某次操作的快照到 scenes.text。"""

    operation_id: str = Field(
        ...,
        description="操作引用 ID（显式来源协议）：db:<uuid> 或 history:<operation_id>",
    )
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
