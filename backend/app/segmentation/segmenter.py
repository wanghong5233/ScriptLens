import re

from app.core.models import ScriptDocument, ScriptSegment


SECTION_MARKER = re.compile(r"^\s*[\u3000\s]*(\d{1,3})\s*$")


def segment_document(document: ScriptDocument) -> list[ScriptSegment]:
    lines = document.raw_text.splitlines()
    markers = _find_section_markers(lines)

    if markers:
        return _segments_from_markers(document.id, lines, markers)

    return _fallback_segments(document.id, lines)


def _find_section_markers(lines: list[str]) -> list[tuple[int, str]]:
    markers: list[tuple[int, str]] = []
    for index, line in enumerate(lines, start=1):
        match = SECTION_MARKER.match(line.strip())
        if match:
            markers.append((index, match.group(1)))
    return markers


def _segments_from_markers(
    script_id: str,
    lines: list[str],
    markers: list[tuple[int, str]],
) -> list[ScriptSegment]:
    segments: list[ScriptSegment] = []

    for marker_index, (start_line, label) in enumerate(markers):
        next_start = markers[marker_index + 1][0] if marker_index + 1 < len(markers) else len(lines) + 1
        end_line = next_start - 1
        text = "\n".join(lines[start_line - 1 : end_line]).strip()
        if not text:
            continue
        segments.append(
            ScriptSegment(
                id=f"seg_{marker_index + 1:03d}",
                script_id=script_id,
                label=label,
                start_line=start_line,
                end_line=end_line,
                text=text,
            )
        )

    return segments


def _fallback_segments(script_id: str, lines: list[str], chunk_size: int = 80) -> list[ScriptSegment]:
    non_empty_lines = [(index, line) for index, line in enumerate(lines, start=1) if line.strip()]
    if not non_empty_lines:
        raise ValueError("Script text has no segmentable content.")

    segments: list[ScriptSegment] = []
    for chunk_index, start in enumerate(range(0, len(non_empty_lines), chunk_size), start=1):
        chunk = non_empty_lines[start : start + chunk_size]
        start_line = chunk[0][0]
        end_line = chunk[-1][0]
        text = "\n".join(line for _, line in chunk).strip()
        segments.append(
            ScriptSegment(
                id=f"seg_{chunk_index:03d}",
                script_id=script_id,
                label=f"chunk-{chunk_index}",
                start_line=start_line,
                end_line=end_line,
                text=text,
            )
        )

    return segments
