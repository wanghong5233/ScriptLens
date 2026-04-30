import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.ingest.loader import build_document, load_sample_script
from app.reporting.basic import generate_basic_report
from app.segmentation.segmenter import segment_document


def main() -> None:
    sample = load_sample_script()
    document = build_document(sample.text, sample.title, sample.source_type)
    segments = segment_document(document)
    report = generate_basic_report(document, segments)

    if len(segments) < 5:
        raise AssertionError(f"Expected at least 5 segments, got {len(segments)}.")

    print(f"Loaded: {report.title}")
    print(f"Segments: {len(report.segments)}")
    print(f"Core plot: {report.core_plot}")
    print(f"Characters: {', '.join(report.main_characters)}")
    print(f"Next step: {report.next_step}")


if __name__ == "__main__":
    main()
