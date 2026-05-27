"""Plot tagging schemas and enums for v0/v1/v2 tag sets."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Versions and generic fields
# ---------------------------------------------------------------------------

TagSetVersion = Literal["v0.1.0", "v1.0.0", "v2.0.0"]
TagSource = Literal["llm", "human_correction", "derived", "manual", "system"]
RunStatus = Literal["pending", "success", "failed"]
TierName = Literal["excellent", "good", "weak", "poor", "insufficient"]


# drama_tags is an open enum in upstream systems.
KNOWN_DRAMA_TAGS: tuple[str, ...] = (
    "乡村",
    "亲情",
    "先婚后爱",
    "剧情",
    "历史古代",
    "古代",
    "古风权谋",
    "大女主",
    "大男主",
    "奇幻脑洞",
    "女强",
    "女性成长",
    "女频",
    "婆媳",
    "实拍",
    "家庭伦理",
    "年代",
    "年代爱情",
    "强者回归",
    "总裁",
    "恋爱",
    "悬疑推理",
    "战神",
    "战神归来",
    "打脸虐渣",
    "无敌神医",
    "极品亲戚",
    "架空",
    "沙雕漫",
    "玄幻",
    "玄幻仙侠",
    "现代",
    "现代言情",
    "现言甜宠",
    "真人AI",
    "种田",
    "穿越",
    "系统",
    "职场",
    "脑洞",
    "豪门恩怨",
    "赘婿逆袭",
    "逆袭",
    "都市",
    "都市日常",
    "都市爱情",
    "都市玄幻",
    "重生",
    "闪婚",
    "马甲",
    "黑道",
)
DramaTagValue = str


# ---------------------------------------------------------------------------
# v0 shared kernel tags (17 dims)
# ---------------------------------------------------------------------------

DialogueDensity = Literal["dense", "moderate", "sparse", "none"]
SpeechStyle = Literal["emotional", "dramatic", "narrative", "comedic", "none"]
CtaType = Literal["curiosity", "urgency", "subscribe", "none"]
VoiceoverType = Literal["character", "narrator", "mixed", "none"]
EmotionalKeywords = Literal["high", "moderate", "low", "none"]
KeywordTheme = Literal["romance", "power", "family", "comedy", "none"]

PlotHook = Literal[
    "identity_reveal",
    "reversal",
    "betrayal",
    "rescue",
    "punishment",
    "forced_marriage",
    "reunion",
    "secret_exposure",
    "conflict_escalation",
    "emotional_choice",
    "none",
]
ConflictType = Literal[
    "status_gap",
    "family_conflict",
    "romantic_conflict",
    "workplace_conflict",
    "revenge",
    "survival_crisis",
    "misunderstanding",
    "moral_judgement",
    "none",
]
StoryStage = Literal["setup", "trigger", "escalation", "climax", "payoff", "teaser", "none"]
RelationshipArc = Literal[
    "enemies_to_lovers",
    "power_flip",
    "chase_and_reject",
    "reunion_after_separation",
    "protection",
    "betrayal_and_revenge",
    "family_reconciliation",
    "none",
]
PayoffType = Literal[
    "face_slapping",
    "counterattack",
    "reveal_power",
    "romantic_payoff",
    "justice_served",
    "cliffhanger",
    "comic_relief",
    "none",
]
EmotionalDriver = Literal[
    "humiliation",
    "jealousy",
    "regret",
    "pity",
    "desire",
    "anger",
    "fear",
    "tenderness",
    "curiosity",
    "none",
]

BusinessContentArchetype = Literal[
    "power_payoff",
    "relationship_payoff",
    "survival_suspense",
    "workplace_counter",
    "comedy_light",
    "unclear",
]
BusinessConflictBucket = Literal["relationship_power", "survival", "workplace", "none"]
BusinessPayoffBucket = Literal[
    "face_slap_counter",
    "power_reveal",
    "emotional_romance",
    "rescue_protection",
    "suspense_cliffhanger",
    "justice_punishment",
    "comedy",
    "none",
]
BusinessEmotionBucket = Literal[
    "anger_humiliation",
    "curiosity_suspense",
    "fear_crisis",
    "warm_regret_pity",
    "desire_jealousy",
    "neutral",
]


# ---------------------------------------------------------------------------
# v1 script-only extension tags
# ---------------------------------------------------------------------------

GenderAxis = Literal["male_lead", "female_lead", "dual_lead", "unclear"]
WorldSetting = Literal[
    "modern_urban",
    "modern_workplace",
    "modern_rural",
    "ancient_palace",
    "ancient_jianghu",
    "ancient_folk",
    "xianxia",
    "xuanhuan",
    "apocalypse",
    "system_flow",
    "rebirth",
    "transmigration",
    "school",
    "mixed",
]
ProtagonistArchetype = Literal[
    "war_god_return",
    "son_in_law_counter",
    "reborn_revenge",
    "system_holder",
    "transmigrator",
    "hidden_heir",
    "big_female",
    "ceo_dominant",
    "sweet_pet",
    "genius_doctor",
    "weak_to_strong",
    "nanny_disguise",
    "unclear",
]
AntagonistArchetype = Literal[
    "evil_female_rival",
    "scumbag_male",
    "evil_relatives",
    "corrupt_ceo",
    "hidden_boss",
    "black_society",
    "corrupt_official",
    "ancient_villain",
    "mixed",
    "none",
]
PacingMode = Literal["high_density_conflict", "medium_density", "slow_build", "mixed"]
PaidBreakPattern = Literal[
    "no_break",
    "ep_end_cliffhanger",
    "mid_break",
    "three_stage_break",
    "dense_break",
]
StoryArcTemplate = Literal[
    "three_act_underdog_revenge",
    "rebirth_step_on_old_line",
    "system_progression",
    "sweet_romance_progression",
    "reunion_after_misunderstanding",
    "crisis_solving",
    "family_reconciliation",
    "court_struggle",
    "mixed",
    "unclear",
]

CharacterArchetype = Literal[
    "weak_start_hidden_strong",
    "invincible_dominant",
    "cold_arrogant",
    "naive_warm",
    "scheming_lotus",
    "comic_relief",
    "family_burden",
    "absolute_villain",
    "grey_villain",
    "mentor_elder",
    "tool_npc",
]
CharacterRoleInArc = Literal[
    "actor",
    "target",
    "blocker",
    "helper",
    "mentor",
    "catalyst",
    "observer",
    "comic_relief",
]
CharacterArcType = Literal[
    "power_growth",
    "moral_growth",
    "redemption",
    "corruption",
    "static",
    "identity_reveal",
    "relational_growth",
    "tragic_fall",
]
CharacterAgencyLevel = Literal["high", "medium", "low"]

RelationshipType = Literal[
    "romance",
    "family",
    "rival",
    "ally",
    "authority",
    "mentor",
    "deception",
    "blood_oath",
    "friendship",
    "servant",
]
RelationshipPolarity = Literal["positive", "negative", "mixed", "unstable"]
RelationshipDynamicArc = Literal[
    "enemies_to_lovers",
    "forced_marriage_to_real_love",
    "friends_to_lovers",
    "lovers_to_strangers",
    "master_to_betrayal",
    "rivals_to_allies",
    "family_estrangement_to_reconciliation",
    "persistent_conflict",
    "persistent_support",
    "none",
]
RelationshipTriangle = Literal["love_triangle", "rivalry_triangle", "family_triangle", "none"]

EpisodeOpeningType = Literal[
    "hook_in_3s",
    "setup_first",
    "recap_then_hook",
    "cliffhanger_resolve",
    "flashback",
]
EpisodeEndHook = Literal[
    "cliffhanger",
    "twist",
    "payoff_then_setup",
    "promise",
    "question",
    "none",
]
IntraEpisodePeakCount = Literal["0", "1", "2", "3_plus"]
PaidBreakPosition = Literal["ep_end", "ep_two_third", "ep_mid", "none"]


# ---------------------------------------------------------------------------
# v2 storyboard hints
# ---------------------------------------------------------------------------

SceneLocaleType = Literal[
    "modern_indoor",
    "modern_outdoor",
    "ancient_indoor",
    "ancient_outdoor",
    "fantasy_world",
    "school",
    "transport",
    "unclear",
]
SceneTimeOfDay = Literal["day", "night", "dawn", "dusk", "unclear"]
SceneInOut = Literal["int", "ext", "mixed"]
SceneEmotionKeynote = Literal[
    "tense",
    "romantic",
    "sad",
    "happy",
    "comedic",
    "epic",
    "eerie",
    "calm",
]
ShotSuggestion = Literal[
    "close_up",
    "medium_shot",
    "wide_shot",
    "over_shoulder",
    "push_in",
    "pull_out",
    "pan",
    "tilt_up",
    "tilt_down",
    "tracking",
    "mixed",
    "unclear",
]
PropFocus = str
CharacterStateChange = Literal[
    "none",
    "costume_change",
    "makeup_change",
    "injury",
    "status_promotion",
    "status_demotion",
    "emotion_shift",
    "identity_reveal",
]


# ---------------------------------------------------------------------------
# Common payload models
# ---------------------------------------------------------------------------


class TagEvidence(BaseModel):
    scene_ids: list[str] = Field(default_factory=list)
    line_range: tuple[int, int] | None = None
    quote: str | None = None


class TagItem(BaseModel):
    dim: str
    value: str
    score: float | None = None
    confidence: float | None = None
    source: TagSource = "llm"
    evidence: TagEvidence | None = None


class PlotUnit(BaseModel):
    id: str
    script_id: str
    episode_no: int | None = None
    idx: int
    start_scene_id: str | None = None
    end_scene_id: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    summary: str | None = None
    char_count: int | None = None
    source: TagSource = "llm"
    created_at: datetime | None = None


class PlotUnitTagSet(BaseModel):
    plot_unit_id: str
    tag_set_ver: TagSetVersion
    prompt_ver: str
    model_ver: str
    items: list[TagItem] = Field(default_factory=list)


class ScriptTagSet(BaseModel):
    script_id: str
    tag_set_ver: TagSetVersion
    prompt_ver: str
    model_ver: str
    items: list[TagItem] = Field(default_factory=list)


class EpisodeTagSet(BaseModel):
    script_id: str
    episode_no: int
    tag_set_ver: TagSetVersion
    prompt_ver: str
    model_ver: str
    items: list[TagItem] = Field(default_factory=list)


class CharacterEntityModel(BaseModel):
    id: str
    script_id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    role: str | None = None
    gender: str | None = None
    archetype: str | None = None
    arc_type: str | None = None
    agency_level: str | None = None
    tag_set_ver: str = ""
    source: TagSource = "llm"
    evidence: dict = Field(default_factory=dict)
    created_at: datetime | None = None


class CharacterRelationshipModel(BaseModel):
    id: str
    script_id: str
    src_char_id: str
    dst_char_id: str
    relationship_type: str | None = None
    polarity: str | None = None
    dynamic_arc: str | None = None
    triangle: str | None = None
    tag_set_ver: str = ""
    source: TagSource = "llm"
    evidence: dict = Field(default_factory=dict)
    created_at: datetime | None = None


class PlotUnitVideoMatchModel(BaseModel):
    id: str
    plot_unit_id: str
    video_segment_id: str
    match_score: float | None = None
    match_method: str | None = None
    evidence: dict = Field(default_factory=dict)
    created_at: datetime | None = None


class ScriptScoreModel(BaseModel):
    id: str
    script_id: str
    run_id: str | None = None
    dimension: str
    primary_dimension: str | None = None
    score: float | None = None
    percentile: float | None = None
    tier: TierName | None = None
    confidence: float | None = None
    coverage_ratio: float | None = None
    signals: dict = Field(default_factory=dict)
    signal_refs: list[dict] = Field(default_factory=list)
    weights: dict = Field(default_factory=dict)
    tag_set_ver: TagSetVersion
    score_ver: str
    model_ver: str
    created_at: datetime | None = None


class RubricVersionModel(BaseModel):
    id: str
    version: str
    status: str
    base_weight: dict = Field(default_factory=dict)
    genre_multiplier: dict = Field(default_factory=dict)
    tier_cuts: dict = Field(default_factory=dict)
    signal_catalog: dict = Field(default_factory=dict)
    prompt_version: str | None = None
    model_version: str | None = None
    effective_at: datetime | None = None
    deprecated_at: datetime | None = None
    changelog: str | None = None
    created_at: datetime | None = None


class ScoringRunModel(BaseModel):
    id: str
    script_id: str
    rubric_version: str
    tag_set_ver: str | None = None
    input_hash: str
    genre_scope: str | None = None
    episode_count: int | None = None
    plot_unit_count: int | None = None
    quality_flags: dict = Field(default_factory=dict)
    model_versions: dict = Field(default_factory=dict)
    prompt_versions: dict = Field(default_factory=dict)
    status: str
    error: str | None = None
    created_at: datetime | None = None


class ScoringImprovementActionModel(BaseModel):
    id: str
    run_id: str
    script_id: str
    dimension: str
    signal_key: str
    template_id: str | None = None
    issue: str
    target: str
    action_steps: list[str] = Field(default_factory=list)
    evidence_refs: list[dict] = Field(default_factory=list)
    estimated_lift: dict = Field(default_factory=dict)
    created_at: datetime | None = None


class TagExtractionRunModel(BaseModel):
    id: str
    script_id: str | None = None
    scope: str
    scope_id: str | None = None
    tag_set_ver: TagSetVersion
    prompt_ver: str
    model_ver: str
    seed: int | None = None
    input_hash: str
    output_hash: str | None = None
    status: RunStatus
    error: str | None = None
    metrics: dict = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
