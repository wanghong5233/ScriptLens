import json
import re
from typing import Any, Dict, List, Tuple

from sqlalchemy import text

from core.config import settings
from exceptions.base import VectorStoreError
from schemas.rag import Chunk
from service.core.abstractions.vector_store import BaseVectorStore
from service.core.ingestion.pgvector_writer import PgVectorChunkWriter
from utils.database import engine
from utils.get_logger import log


class PgVectorVectorStore(BaseVectorStore):
    """BaseVectorStore adapter backed by PostgreSQL + pgvector."""

    def __init__(self) -> None:
        self.default_index = settings.ES_DEFAULT_INDEX
        self.table_name = self._validate_table_name(settings.SM_PGVECTOR_TABLE)
        self.embedding_dimensions = int(settings.SM_EMBEDDING_DIMENSIONS or 1024)
        self.writer = PgVectorChunkWriter(table_name=self.table_name)
        log.info(f"PgVectorVectorStore initialized with table: {self.table_name}")

    async def add_chunks(self, chunks: List[Chunk], index_name: str = None) -> List[str]:
        index = index_name or self.default_index
        records = [self._chunk_to_record(chunk) for chunk in chunks if chunk.embedding is not None]
        if not records:
            return []
        self.writer.upsert_chunks(records=records, index_name=index)
        return [str(record["id"]) for record in records]

    async def search(self, query_embedding: List[float], top_k: int, index_name: str = None) -> List[Tuple[Chunk, float]]:
        index = index_name or self.default_index
        limit = max(int(top_k or 0), 1)
        vector_literal = self._vector_literal(query_embedding)
        statement = text(
            f"""
            SELECT
                chunk_id,
                document_id,
                text,
                metadata,
                1 - (embedding <=> CAST(:embedding AS vector)) AS score
            FROM {self.table_name}
            WHERE index_name = :index_name
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
            """
        )
        with engine.connect() as connection:
            rows = connection.execute(
                statement,
                {"index_name": index, "embedding": vector_literal, "limit": limit},
            ).mappings().all()

        results: List[Tuple[Chunk, float]] = []
        for row in rows:
            metadata = self._metadata(row.get("metadata"))
            chunk = Chunk(
                chunk_id=str(row.get("chunk_id") or ""),
                document_id=str(row.get("document_id") or metadata.get("document_id") or ""),
                content=str(row.get("text") or ""),
                metadata=metadata,
            )
            results.append((chunk, float(row.get("score") or 0.0)))
        return results

    async def delete_by_document_id(self, document_id: str, index_name: str = None) -> None:
        index = index_name or self.default_index
        try:
            document_id_int = int(document_id)
        except ValueError as exc:
            raise VectorStoreError(
                operation="delete_by_document_id",
                message=f"pgvector document_id must be an integer, got: {document_id}",
            ) from exc

        statement = text(
            f"""
            DELETE FROM {self.table_name}
            WHERE index_name = :index_name
              AND document_id = :document_id
            """
        )
        with engine.begin() as connection:
            connection.execute(
                statement,
                {"index_name": index, "document_id": document_id_int},
            )

    def _chunk_to_record(self, chunk: Chunk) -> Dict[str, Any]:
        metadata = dict(chunk.metadata or {})
        kb_id = metadata.get("kb_id")
        if kb_id is None:
            raise VectorStoreError(
                operation="add_chunks",
                message="pgvector add_chunks requires metadata.kb_id",
            )
        return {
            "id": chunk.chunk_id,
            "text": chunk.content,
            "vector": chunk.embedding,
            "kb_id": int(kb_id),
            "document_id": int(metadata.get("document_id") or chunk.document_id),
            **metadata,
        }

    def _vector_literal(self, value: List[float]) -> str:
        if len(value) != self.embedding_dimensions:
            raise VectorStoreError(
                operation="search",
                message=(
                    f"pgvector embedding dimension mismatch: expected "
                    f"{self.embedding_dimensions}, got {len(value)}"
                ),
            )
        return "[" + ",".join(str(float(item)) for item in value) + "]"

    def _metadata(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            return json.loads(value)
        return {}

    def _validate_table_name(self, table_name: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name or ""):
            raise ValueError(f"Invalid pgvector table name: {table_name!r}")
        return table_name
