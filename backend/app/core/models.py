from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    UNKNOWN = "unknown"
    WEB_NOVEL = "web_novel"
    SCREENPLAY = "screenplay"
    SHORT_DRAMA = "short_drama"


class ScriptMetadata(BaseModel):
    author: str | None = None
    category: str | None = None
    description: str | None = None


class ScriptDocument(BaseModel):
    id: str = Field(default_factory=lambda: f"script_{uuid4().hex[:12]}")
    title: str
    source_type: SourceType = SourceType.UNKNOWN
    raw_text: str
    metadata: ScriptMetadata = Field(default_factory=ScriptMetadata)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ScriptSegment(BaseModel):
    id: str
    script_id: str
    label: str
    start_line: int
    end_line: int
    text: str


class BasicReport(BaseModel):
    script_id: str
    title: str
    summary: str
    core_plot: str
    main_characters: list[str]
    key_conflicts: list[str]
    hooks: list[str]
    risks: list[str]
    next_step: str
    segments: list[ScriptSegment]


class CreateScriptRequest(BaseModel):
    text: str
    title: str | None = None
    source_type: SourceType = SourceType.UNKNOWN


class SampleResponse(BaseModel):
    title: str
    text: str
    source_type: SourceType


class ScriptCreateResponse(BaseModel):
    script: ScriptDocument
    segments: list[ScriptSegment]
