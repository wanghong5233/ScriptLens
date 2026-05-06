from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Protocol


@dataclass
class ParsedBlock:
    text: str
    metadata: Dict[str, Any]


class DocumentParser(Protocol):
    def parse(self, *, file_path: str) -> List[ParsedBlock]:
        ...

    def name(self) -> str:
        """返回解析器名称（用于日志与观测）。"""
        return self.__class__.__name__


class Chunker(Protocol):
    def chunk(self, *, blocks: Iterable[ParsedBlock]) -> List[ParsedBlock]:
        ...


class Embedder(Protocol):
    def embed(self, *, chunks: Iterable[ParsedBlock]) -> List[Dict[str, Any]]:
        """
        返回带有向量与元数据的记录，例如：
        [{"vector": List[float], "text": str, "metadata": {...}}]
        """
        ...


class Indexer(Protocol):
    def index(self, *, records: Iterable[Dict[str, Any]], kb_id: int, document_id: int) -> None:
        ...


class MetadataExtractor(Protocol):
    def extract_and_enrich(self, *, db, document, blocks: List[ParsedBlock]) -> Any:
        """
        基于解析结果抽取并补全元数据，返回更新后的 Document。
        """
        ...


