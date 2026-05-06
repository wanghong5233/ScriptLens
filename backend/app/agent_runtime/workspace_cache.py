"""工作区上下文缓存，降低重复 IO 开销。"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from .metrics import record_workspace_cache_event


CacheKey = Tuple[int, str]


@dataclass
class WorkspaceSnapshot:
    """工作区快照，包含计划复用的上下文。"""

    file_list: List[str]
    citation_mappings: Dict[str, str]
    workspace_config: Dict[str, Any]
    original_file_contents: Dict[str, str]
    signature: str
    timestamp: float = field(default_factory=lambda: time.time())

    def clone(self) -> "WorkspaceSnapshot":
        """返回浅拷贝，避免不同请求共享可变引用。"""
        return WorkspaceSnapshot(
            file_list=list(self.file_list),
            citation_mappings=dict(self.citation_mappings),
            workspace_config=dict(self.workspace_config),
            original_file_contents=dict(self.original_file_contents),
            signature=self.signature,
        )


class WorkspaceContextCache:
    """LRU + TTL 结合的工作区缓存。"""

    def __init__(self, max_entries: int = 16, ttl_seconds: int = 60) -> None:
        self.max_entries = max(1, max_entries)
        self.ttl_seconds = max(0, ttl_seconds)
        self._cache: "OrderedDict[CacheKey, WorkspaceSnapshot]" = OrderedDict()
        self._lock = Lock()

    def get(self, key: CacheKey, signature: str) -> Optional[WorkspaceSnapshot]:
        """获取缓存，如果 ttl 或签名不匹配则返回 None。"""
        with self._lock:
            snapshot = self._cache.get(key)
            if not snapshot:
                record_workspace_cache_event("miss")
                return None

            if not self._is_fresh(snapshot, signature):
                self._cache.pop(key, None)
                return None

            self._cache.move_to_end(key)
            record_workspace_cache_event("hit")
            return snapshot.clone()

    def set(self, key: CacheKey, snapshot: WorkspaceSnapshot) -> None:
        """写入缓存，必要时淘汰最旧条目。"""
        snapshot.timestamp = time.time()
        with self._lock:
            if key in self._cache:
                self._cache.pop(key, None)
            self._cache[key] = snapshot
            self._trim_locked()
            record_workspace_cache_event("store")

    def invalidate(self, key: CacheKey) -> None:
        with self._lock:
            if key in self._cache:
                self._cache.pop(key, None)
                record_workspace_cache_event("invalidate")

    def _trim_locked(self) -> None:
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)
            record_workspace_cache_event("evict")

    def _is_fresh(self, snapshot: WorkspaceSnapshot, signature: str) -> bool:
        if snapshot.signature != signature:
            record_workspace_cache_event("stale")
            return False
        if self.ttl_seconds and time.time() - snapshot.timestamp > self.ttl_seconds:
            record_workspace_cache_event("expired")
            return False
        return True


