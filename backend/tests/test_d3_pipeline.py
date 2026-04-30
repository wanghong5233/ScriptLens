from app.ingest.loader import build_document, load_sample_script
from app.reporting.basic import generate_basic_report
from app.segmentation.segmenter import segment_document


def test_sample_generates_basic_report() -> None:
    sample = load_sample_script()
    document = build_document(sample.text, sample.title, sample.source_type)
    segments = segment_document(document)
    report = generate_basic_report(document, segments)

    assert report.title == "小妾"
    assert len(report.segments) >= 5
    assert report.core_plot
    assert report.main_characters
    assert report.key_conflicts
    assert report.hooks
    assert report.risks
