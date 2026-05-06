"""Document processing lifecycle helpers.

Single source of truth for transitioning a `Document.processing_status`
between `pending`, `parsing`, `ready`, and `failed`. Handlers must use these
helpers instead of mutating the field directly so that timestamps,
chunk_count and failure metadata stay consistent across all writers.

Why a dedicated module:
- Handlers used to call ``db.delete(doc)`` whenever a stage failed, which
  silently dropped user data and made every new failure path require its own
  cleanup logic. The state machine replaces those scattered deletes with a
  single, observable failure record.
- Job runner / retry endpoint / API serializers all need the same status
  semantics; centralising them avoids drift.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from models.document import Document, DocumentProcessingStatus
from utils.get_logger import log


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def mark_pending(db: Session, doc: Document) -> None:
    """Newly created doc, awaiting parse."""
    doc.processing_status = DocumentProcessingStatus.PENDING.value
    doc.failure_stage = None
    doc.failure_reason = None
    db.add(doc)
    db.commit()


def mark_parsing(db: Session, doc: Document) -> None:
    """Parser/Indexer began work on this doc."""
    doc.processing_status = DocumentProcessingStatus.PARSING.value
    doc.failure_stage = None
    doc.failure_reason = None
    db.add(doc)
    db.commit()


def mark_ready(db: Session, doc: Document, *, chunk_count: int) -> None:
    """Pipeline finished; chunks live in rag_chunks and the doc is RAG-able."""
    doc.processing_status = DocumentProcessingStatus.READY.value
    doc.chunk_count = int(chunk_count)
    doc.failure_stage = None
    doc.failure_reason = None
    doc.last_processed_at = _now()
    db.add(doc)
    db.commit()


def mark_failed(
    db: Session,
    doc: Document,
    *,
    stage: str,
    reason: str,
) -> None:
    """Pipeline failed at ``stage`` with ``reason``.

    The row is *not* deleted. The UI surfaces the failure with a tooltip and a
    retry button so the user can decide what to do; this preserves the
    download cost and any partial metadata the parser already extracted.

    Any rag_chunks rows that may have been written before the failure are
    purged here to keep ``chunk_count`` and the ``ready`` invariant honest:
    a doc with status='ready' must have chunk_count>0, and vice versa we don't
    want a 'failed' doc to leak half-written chunks into similarity search.
    """
    short_reason = (reason or "").strip()
    if len(short_reason) > 2000:
        short_reason = short_reason[:1997] + "..."

    try:
        db.execute(
            sql_text("DELETE FROM rag_chunks WHERE document_id = :doc_id"),
            {"doc_id": doc.id},
        )
    except Exception as exc:
        log.warning(f"[LIFECYCLE] failed to clean rag_chunks for doc_id={doc.id}: {exc}")
        db.rollback()

    doc.processing_status = DocumentProcessingStatus.FAILED.value
    doc.failure_stage = stage
    doc.failure_reason = short_reason
    doc.chunk_count = 0
    doc.last_processed_at = _now()
    db.add(doc)
    db.commit()
    log.info(
        f"[LIFECYCLE] doc_id={doc.id} -> failed (stage={stage}, reason={short_reason[:120]!r})"
    )


def reset_for_retry(db: Session, doc: Document) -> None:
    """User-triggered retry: clear failure state and put the doc back in pending."""
    doc.processing_status = DocumentProcessingStatus.PENDING.value
    doc.failure_stage = None
    doc.failure_reason = None
    doc.chunk_count = 0
    db.add(doc)
    db.commit()


def count_chunks(db: Session, doc_id: int) -> int:
    """Authoritative chunk count by querying rag_chunks. Used to refresh the
    cached ``chunk_count`` field whenever drift is suspected."""
    row = db.execute(
        sql_text("SELECT COUNT(*) FROM rag_chunks WHERE document_id = :doc_id"),
        {"doc_id": doc_id},
    ).scalar()
    return int(row or 0)


def refresh_chunk_count(db: Session, doc: Document) -> int:
    """Recompute and persist chunk_count from rag_chunks; returns the count."""
    cnt = count_chunks(db, doc.id)
    if doc.chunk_count != cnt:
        doc.chunk_count = cnt
        db.add(doc)
        db.commit()
    return cnt
