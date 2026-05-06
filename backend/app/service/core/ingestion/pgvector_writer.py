from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text

from core.config import settings
from utils.database import engine


class PgVectorChunkWriter:
    """Write indexed RAG chunks into PostgreSQL + pgvector."""

    def __init__(self, table_name: Optional[str] = None) -> None:
        self.table_name = self._validate_table_name(
            table_name or settings.SM_PGVECTOR_TABLE
        )
        self.embedding_dimensions = int(settings.SM_EMBEDDING_DIMENSIONS or 1024)
        self.logger = logging.getLogger("rag.pgvector_writer")

    def upsert_chunks(
        self,
        *,
        records: Iterable[Dict[str, Any]],
        index_name: str,
    ) -> int:
        """Upsert chunk rows using the same index namespace semantics as ES."""

        started_at = time.perf_counter()
        rows = [self._build_row(record=record, index_name=index_name) for record in records]
        if not rows:
            return 0

        statement = text(
            f"""
            INSERT INTO {self.table_name} (
                index_name,
                chunk_id,
                kb_id,
                document_id,
                session_id,
                scope,
                text,
                embedding,
                metadata,
                chunk_index,
                prev_chunk_id,
                next_chunk_id,
                element_type,
                parser_engine,
                source,
                updated_at
            )
            VALUES (
                :index_name,
                :chunk_id,
                :kb_id,
                :document_id,
                :session_id,
                :scope,
                :text,
                CAST(:embedding AS vector),
                CAST(:metadata AS jsonb),
                :chunk_index,
                :prev_chunk_id,
                :next_chunk_id,
                :element_type,
                :parser_engine,
                :source,
                now()
            )
            ON CONFLICT (index_name, chunk_id) DO UPDATE SET
                kb_id = EXCLUDED.kb_id,
                document_id = EXCLUDED.document_id,
                session_id = EXCLUDED.session_id,
                scope = EXCLUDED.scope,
                text = EXCLUDED.text,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata,
                chunk_index = EXCLUDED.chunk_index,
                prev_chunk_id = EXCLUDED.prev_chunk_id,
                next_chunk_id = EXCLUDED.next_chunk_id,
                element_type = EXCLUDED.element_type,
                parser_engine = EXCLUDED.parser_engine,
                source = EXCLUDED.source,
                updated_at = now()
            """
        )
        with engine.begin() as connection:
            connection.execute(statement, rows)
        self.logger.info(
            "pgvector upsert completed: index=%s chunks=%s elapsed_ms=%s",
            index_name,
            len(rows),
            int((time.perf_counter() - started_at) * 1000),
        )
        return len(rows)

    def delete_document_chunks(self, *, kb_id: int, document_id: int) -> int:
        """Delete all chunks for a document across pgvector index namespaces."""

        statement = text(
            f"""
            DELETE FROM {self.table_name}
            WHERE kb_id = :kb_id AND document_id = :document_id
            """
        )
        with engine.begin() as connection:
            result = connection.execute(
                statement,
                {"kb_id": int(kb_id), "document_id": int(document_id)},
            )
        return int(result.rowcount or 0)

    def count_document_chunks(self, *, kb_id: int, document_id: int) -> int:
        """Count residual chunks for a document across pgvector index namespaces."""

        statement = text(
            f"""
            SELECT count(*) AS total
            FROM {self.table_name}
            WHERE kb_id = :kb_id AND document_id = :document_id
            """
        )
        with engine.connect() as connection:
            row = connection.execute(
                statement,
                {"kb_id": int(kb_id), "document_id": int(document_id)},
            ).first()
        return int(row.total if row else 0)

    def delete_session_chunks(self, *, session_id: str) -> int:
        """Delete chunks associated with a session-scoped index."""

        index_prefix = f"sm_sess_{session_id}"
        statement = text(
            f"""
            DELETE FROM {self.table_name}
            WHERE session_id = :session_id OR index_name LIKE :index_like
            """
        )
        with engine.begin() as connection:
            result = connection.execute(
                statement,
                {"session_id": session_id, "index_like": f"{index_prefix}%"},
            )
        return int(result.rowcount or 0)

    def fetch_document_chunks(
        self,
        *,
        kb_id: int,
        document_id: int,
        index_name: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """Fetch indexed document chunks for debug/preview screens."""

        statement = text(
            f"""
            SELECT chunk_id, text, metadata, chunk_index
            FROM {self.table_name}
            WHERE index_name = :index_name
              AND kb_id = :kb_id
              AND document_id = :document_id
            ORDER BY chunk_index ASC, id ASC
            LIMIT :limit
            """
        )
        with engine.connect() as connection:
            result = connection.execute(
                statement,
                {
                    "index_name": index_name,
                    "kb_id": int(kb_id),
                    "document_id": int(document_id),
                    "limit": int(limit),
                },
            )
            return [dict(row._mapping) for row in result]

    def _build_row(self, *, record: Dict[str, Any], index_name: str) -> Dict[str, Any]:
        chunk_id = str(record.get("id") or "").strip()
        if not chunk_id:
            raise ValueError("pgvector chunk record is missing id")

        kb_id = self._required_int(record.get("kb_id"), "kb_id")
        document_id = self._required_int(record.get("document_id"), "document_id")
        chunk_index = self._optional_int(record.get("chunk_index")) or 0
        metadata = self._metadata_payload(record)

        return {
            "index_name": index_name,
            "chunk_id": chunk_id,
            "kb_id": kb_id,
            "document_id": document_id,
            "session_id": self._session_id_from_index(index_name),
            "scope": "session" if index_name.startswith("sm_sess_") else "global",
            "text": str(record.get("text") or ""),
            "embedding": self._vector_literal(record.get("vector")),
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
            "chunk_index": chunk_index,
            "prev_chunk_id": self._optional_text(record.get("prev_chunk_id")),
            "next_chunk_id": self._optional_text(record.get("next_chunk_id")),
            "element_type": self._clip(record.get("element_type"), limit=128),
            "parser_engine": self._clip(record.get("parser_engine"), limit=64),
            "source": self._clip(record.get("source"), limit=64),
        }

    def _metadata_payload(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key not in {"id", "text", "vector"}
        }

    def _vector_literal(self, value: Any) -> Optional[str]:
        if value is None or value == []:
            return None
        if not isinstance(value, list):
            raise ValueError("pgvector embedding must be a list of floats")
        if len(value) != self.embedding_dimensions:
            raise ValueError(
                f"pgvector embedding dimension mismatch: expected "
                f"{self.embedding_dimensions}, got {len(value)}"
            )
        return "[" + ",".join(str(float(item)) for item in value) + "]"

    def _session_id_from_index(self, index_name: str) -> Optional[str]:
        prefix = "sm_sess_"
        if not index_name.startswith(prefix):
            return None
        session_id = index_name[len(prefix) :].strip()
        return session_id or None

    def _required_int(self, value: Any, field_name: str) -> int:
        parsed = self._optional_int(value)
        if parsed is None:
            raise ValueError(f"pgvector chunk record is missing {field_name}")
        return parsed

    def _optional_int(self, value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        return int(value)

    def _optional_text(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        text_value = str(value).strip()
        return text_value or None

    def _clip(self, value: Any, *, limit: int) -> Optional[str]:
        text_value = self._optional_text(value)
        if text_value is None:
            return None
        return text_value[:limit]

    def _validate_table_name(self, table_name: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name or ""):
            raise ValueError(f"Invalid pgvector table name: {table_name!r}")
        return table_name
