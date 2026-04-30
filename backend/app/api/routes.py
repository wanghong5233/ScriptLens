from fastapi import APIRouter, HTTPException

from app.core.models import BasicReport, CreateScriptRequest, SampleResponse, ScriptCreateResponse
from app.core.store import store
from app.ingest.loader import build_document, load_sample_script
from app.reporting.basic import generate_basic_report
from app.segmentation.segmenter import segment_document


router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/sample", response_model=SampleResponse)
def sample() -> SampleResponse:
    try:
        return load_sample_script()
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/scripts", response_model=ScriptCreateResponse)
def create_script(request: CreateScriptRequest) -> ScriptCreateResponse:
    try:
        document = build_document(request.text, request.title, request.source_type)
        segments = segment_document(document)
        store.save_script(document, segments)
        return ScriptCreateResponse(script=document, segments=segments)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/scripts/{script_id}/analyze", response_model=BasicReport)
def analyze_script(script_id: str) -> BasicReport:
    try:
        document = store.get_script(script_id)
        segments = store.get_segments(script_id)
        report = generate_basic_report(document, segments)
        store.save_report(report)
        return report
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/scripts/{script_id}/report", response_model=BasicReport)
def get_report(script_id: str) -> BasicReport:
    try:
        return store.get_report(script_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
