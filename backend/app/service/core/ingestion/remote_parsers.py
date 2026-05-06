from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from core.config import settings
from service.core.ingestion.interfaces import DocumentParser, ParsedBlock
from utils.get_logger import log


def _clip_text(value: Any) -> str:
    """Normalize provider text fields to a non-empty string."""

    if value is None:
        return ""
    return str(value).strip()


def _optional_int(value: Any) -> Optional[int]:
    """Parse provider page values without hiding invalid data paths."""

    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_trivial_unlocated_text(text: str, bbox: Optional[List[float]]) -> bool:
    """Drop punctuation noise that cannot become a useful citation target."""

    return not bbox and len(text.strip()) <= 2 and not any(char.isalnum() for char in text)


def _bbox_from_coordinates(value: Any) -> Optional[List[float]]:
    """Convert common remote parser coordinate payloads to [x0, y0, x1, y1]."""

    if not value:
        return None
    if isinstance(value, dict):
        if isinstance(value.get("points"), list):
            return _bbox_from_points(value.get("points"))
        if all(key in value for key in ("x1", "y1", "x2", "y2")):
            try:
                return [
                    float(value["x1"]),
                    float(value["y1"]),
                    float(value["x2"]),
                    float(value["y2"]),
                ]
            except (TypeError, ValueError):
                return None
        if all(key in value for key in ("x", "y", "width", "height")):
            try:
                x0 = float(value["x"])
                y0 = float(value["y"])
                return [x0, y0, x0 + float(value["width"]), y0 + float(value["height"])]
            except (TypeError, ValueError):
                return None
        if all(key in value for key in ("x", "y", "w", "h")):
            try:
                x0 = float(value["x"])
                y0 = float(value["y"])
                return [x0, y0, x0 + float(value["w"]), y0 + float(value["h"])]
            except (TypeError, ValueError):
                return None
    if isinstance(value, list) and len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        return [float(item) for item in value]
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        box_values: List[List[float]] = []
        for item in value:
            parsed_box = _bbox_from_coordinates(item)
            if parsed_box:
                box_values.append(parsed_box)
        if box_values:
            return [
                min(box[0] for box in box_values),
                min(box[1] for box in box_values),
                max(box[2] for box in box_values),
                max(box[3] for box in box_values),
            ]
    if isinstance(value, list) and value and isinstance(value[0], (list, tuple, dict)):
        return _bbox_from_points(value)
    return None


def _bbox_from_points(points: Any) -> Optional[List[float]]:
    """Convert polygon points to a bounding box."""

    parsed_points: List[tuple[float, float]] = []
    if not isinstance(points, list):
        return None
    for point in points:
        try:
            if isinstance(point, dict):
                parsed_points.append((float(point["x"]), float(point["y"])))
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                parsed_points.append((float(point[0]), float(point[1])))
        except (KeyError, TypeError, ValueError):
            continue
    if not parsed_points:
        return None
    xs = [point[0] for point in parsed_points]
    ys = [point[1] for point in parsed_points]
    return [min(xs), min(ys), max(xs), max(ys)]


def _metadata_with_location(
    *,
    parser_engine: str,
    element_type: str,
    index: int,
    page: Optional[int],
    bbox: Optional[List[float]],
    structure_title: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the chunk metadata contract needed by citation navigation."""

    logical_type = (element_type or "paragraph").lower()
    metadata: Dict[str, Any] = {
        "parser_engine": parser_engine,
        "element_type": logical_type,
        "logical_type": logical_type,
        "structure_title": structure_title or logical_type.title(),
        "structure_path": f"remote.{parser_engine}.{page or 1}.{logical_type}.{index}",
        "source": parser_engine,
    }
    if page is not None:
        metadata["page"] = page
        metadata["page_range"] = [page]
    if bbox:
        metadata["bbox"] = bbox
        metadata["bbox_list"] = [bbox]
    if extra:
        metadata.update({key: value for key, value in extra.items() if value is not None})
    return metadata


class UnstructuredApiParser(DocumentParser):
    """Remote Unstructured API parser that preserves typed elements and coordinates."""

    def name(self) -> str:
        return "UnstructuredApiParser"

    def parse(self, *, file_path: str) -> List[ParsedBlock]:
        api_key = (settings.SM_UNSTRUCTURED_API_KEY or "").strip()
        if not api_key:
            log.info("UnstructuredApiParser.skip reason=missing_api_key")
            return []

        data = self._request_elements(file_path=file_path, api_key=api_key)
        blocks = self._to_blocks(data)
        log.info(f"UnstructuredApiParser.ok blocks={len(blocks)}")
        return blocks

    def _request_elements(self, *, file_path: str, api_key: str) -> Any:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "unstructured-api-key": api_key,
            "Accept": "application/json",
        }
        payload = {
            "strategy": settings.SM_UNSTRUCTURED_STRATEGY,
            "coordinates": "true",
            "unique_element_ids": "true",
            "pdf_infer_table_structure": "true",
            "skip_infer_table_types": "[]",
        }
        if settings.SM_UNSTRUCTURED_HI_RES_MODEL_NAME:
            payload["hi_res_model_name"] = settings.SM_UNSTRUCTURED_HI_RES_MODEL_NAME

        with open(file_path, "rb") as handle:
            response = requests.post(
                settings.SM_UNSTRUCTURED_API_URL,
                headers=headers,
                files={"files": handle},
                data=payload,
                timeout=settings.SM_UNSTRUCTURED_TIMEOUT_SECS,
            )
        response.raise_for_status()
        return response.json()

    def _to_blocks(self, data: Any) -> List[ParsedBlock]:
        if not isinstance(data, list):
            return []

        blocks: List[ParsedBlock] = []
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            text = _clip_text(item.get("text"))
            if not text:
                continue
            item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            element_type = _clip_text(item.get("type") or item.get("category") or "paragraph").lower()
            page = _optional_int(
                item_metadata.get("page_number")
                or item_metadata.get("page")
                or item.get("page_number")
                or item.get("page")
            )
            bbox = _bbox_from_coordinates(
                item_metadata.get("coordinates")
                or item_metadata.get("bbox")
                or item.get("coordinates")
                or item.get("bbox")
            )
            if _is_trivial_unlocated_text(text, bbox):
                continue
            extra: Dict[str, Any] = {
                "remote_element_id": item.get("element_id") or item.get("id"),
            }
            if element_type == "table":
                extra["table_json"] = {
                    "text": text,
                    "html": item_metadata.get("text_as_html"),
                }
            if item_metadata.get("filename"):
                extra["source_file"] = item_metadata.get("filename")

            blocks.append(
                ParsedBlock(
                    text=text,
                    metadata=_metadata_with_location(
                        parser_engine="unstructured_api",
                        element_type=element_type,
                        index=index,
                        page=page,
                        bbox=bbox,
                        structure_title=element_type.title(),
                        extra=extra,
                    ),
                )
            )
        return blocks


class LlamaParseParser(DocumentParser):
    """Remote LlamaParse adapter using REST endpoints without adding SDK dependencies."""

    def name(self) -> str:
        return "LlamaParseParser"

    def parse(self, *, file_path: str) -> List[ParsedBlock]:
        api_key = (settings.SM_LLAMA_PARSE_API_KEY or "").strip()
        if not api_key:
            log.info("LlamaParseParser.skip reason=missing_api_key")
            return []

        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        file_id = self._upload_file(file_path=file_path, headers=headers)
        job_id = self._start_parse_job(file_id=file_id, headers=headers)
        result = self._wait_for_result(job_id=job_id, headers=headers)
        blocks = self._to_blocks(result)
        log.info(f"LlamaParseParser.ok blocks={len(blocks)}")
        return blocks

    def _base_url(self) -> str:
        return settings.SM_LLAMA_PARSE_BASE_URL.rstrip("/")

    def _upload_file(self, *, file_path: str, headers: Dict[str, str]) -> str:
        with open(file_path, "rb") as handle:
            response = requests.post(
                f"{self._base_url()}/api/v1/files/",
                headers=headers,
                files={"upload_file": handle},
                data={"purpose": "parse"},
                timeout=settings.SM_LLAMA_PARSE_TIMEOUT_SECS,
            )
        response.raise_for_status()
        data = response.json()
        file_id = data.get("id") or data.get("file_id")
        if not file_id:
            raise RuntimeError(f"LlamaParse upload response missing file id: {data}")
        return str(file_id)

    def _start_parse_job(self, *, file_id: str, headers: Dict[str, str]) -> str:
        response = requests.post(
            f"{self._base_url()}/api/v2/parse",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "file_id": file_id,
                "tier": "agentic",
                "version": "latest",
            },
            timeout=settings.SM_LLAMA_PARSE_TIMEOUT_SECS,
        )
        response.raise_for_status()
        data = response.json()
        job = data.get("job") if isinstance(data.get("job"), dict) else {}
        job_id = data.get("id") or data.get("job_id") or job.get("id")
        if not job_id:
            raise RuntimeError(f"LlamaParse parse response missing job id: {data}")
        return str(job_id)

    def _wait_for_result(self, *, job_id: str, headers: Dict[str, str]) -> Any:
        for _ in range(max(int(settings.SM_LLAMA_PARSE_MAX_POLL_ATTEMPTS), 1)):
            status_payload = self._get_json(
                url=f"{self._base_url()}/api/v2/parse/{job_id}?expand=markdown,items,metadata",
                headers=headers,
            )
            job = status_payload.get("job") if isinstance(status_payload.get("job"), dict) else {}
            status = str(job.get("status") or status_payload.get("status") or "").lower()
            if status in {"completed", "success", "succeeded", "done"}:
                return status_payload
            if status in {"error", "failed", "failure"}:
                raise RuntimeError(f"LlamaParse job failed: {status_payload}")
            time.sleep(max(float(settings.SM_LLAMA_PARSE_POLL_INTERVAL_SECS), 0.1))
        raise TimeoutError(f"LlamaParse job timed out: {job_id}")

    def _get_json(self, *, url: str, headers: Dict[str, str]) -> Dict[str, Any]:
        response = requests.get(url, headers=headers, timeout=settings.SM_LLAMA_PARSE_TIMEOUT_SECS)
        response.raise_for_status()
        return response.json()

    def _to_blocks(self, data: Any) -> List[ParsedBlock]:
        items = self._extract_items(data)
        blocks: List[ParsedBlock] = []
        for index, item in enumerate(items):
            text = _clip_text(
                item.get("text")
                or item.get("markdown")
                or item.get("md")
                or item.get("content")
                or item.get("value")
            )
            if not text:
                continue
            element_type = _clip_text(item.get("type") or item.get("kind") or item.get("category") or "paragraph")
            page = _optional_int(item.get("page") or item.get("page_number") or item.get("pageIndex"))
            bbox = _bbox_from_coordinates(item.get("bbox") or item.get("bounding_box") or item.get("coordinates"))
            if _is_trivial_unlocated_text(text, bbox):
                continue
            extra: Dict[str, Any] = {}
            lowered = element_type.lower()
            if "table" in lowered:
                extra["table_json"] = item
            if "equation" in lowered or "latex" in item:
                extra["equation_latex"] = item.get("latex") or text
            if "figure" in lowered or "image" in lowered:
                extra["figure_caption"] = text
            blocks.append(
                ParsedBlock(
                    text=text,
                    metadata=_metadata_with_location(
                        parser_engine="llamaparse",
                        element_type=element_type,
                        index=index,
                        page=page,
                        bbox=bbox,
                        structure_title=_clip_text(item.get("heading") or item.get("title") or element_type.title()),
                        extra=extra,
                    ),
                )
            )
        return blocks

    def _extract_items(self, data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []

        for key in ("items", "elements", "chunks"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict) and isinstance(value.get("pages"), list):
                return self._extract_page_items(value.get("pages"))

        pages = data.get("pages")
        markdown_payload = data.get("markdown")
        if pages is None and isinstance(markdown_payload, dict):
            pages = markdown_payload.get("pages")
        if isinstance(pages, list):
            return self._extract_page_items(pages)

        for key in ("markdown", "text", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return [{"text": value, "type": key, "page": 1}]
        return []

    def _extract_page_items(self, pages: Any) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        if not isinstance(pages, list):
            return items
        for page_index, page in enumerate(pages, start=1):
            if not isinstance(page, dict):
                continue
            page_no = _optional_int(page.get("page") or page.get("page_number")) or page_index
            page_item_count = 0
            page_items = page.get("items")
            if isinstance(page_items, list):
                for item in page_items:
                    if isinstance(item, dict):
                        item.setdefault("page", page_no)
                        items.append(item)
                        page_item_count += 1
            markdown = page.get("markdown") or page.get("text")
            if markdown and page_item_count == 0:
                items.append({"text": markdown, "type": "page", "page": page_no})
        return items
