from pathlib import Path

from app.core.models import SampleResponse, ScriptDocument, ScriptMetadata, SourceType


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_CANDIDATES = (
    WORKSPACE_ROOT / "samples" / "xiaoqie.txt",
)


def load_sample_script() -> SampleResponse:
    for sample_path in SAMPLE_CANDIDATES:
        if sample_path.exists():
            text = sample_path.read_text(encoding="utf-8")
            return SampleResponse(title="小妾", text=text, source_type=SourceType.WEB_NOVEL)

    searched = ", ".join(str(path) for path in SAMPLE_CANDIDATES)
    raise FileNotFoundError(f"Sample script not found. Searched: {searched}")


def build_document(text: str, title: str | None, source_type: SourceType) -> ScriptDocument:
    normalized = text.strip()
    if len(normalized) < 100:
        raise ValueError("Script text is too short for D3 analysis.")

    metadata = _extract_metadata(normalized)
    document_title = title or metadata.description or _first_non_empty_line(normalized)

    return ScriptDocument(
        title=document_title,
        source_type=source_type,
        raw_text=normalized,
        metadata=metadata,
    )


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:80]
    raise ValueError("Script text does not contain non-empty lines.")


def _extract_metadata(text: str) -> ScriptMetadata:
    author: str | None = None
    category: str | None = None
    description: str | None = None

    for line in text.splitlines()[:12]:
        stripped = line.strip()
        if stripped.startswith("作者："):
            author = stripped.removeprefix("作者：").strip()
        elif stripped.startswith("分类："):
            category = stripped.removeprefix("分类：").strip()
        elif stripped.startswith("简介："):
            description = stripped.removeprefix("简介：").strip()

    return ScriptMetadata(author=author, category=category, description=description)
