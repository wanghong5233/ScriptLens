from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol


@dataclass
class JobResult:
    succeeded: int = 0
    failed: int = 0
    total: int = 0
    details: List[Dict[str, Any]] = field(default_factory=list)
    # 任务成功处理并需要触发下一步解析的文档ID列表
    doc_ids_to_parse: List[int] = field(default_factory=list)
    # 本次 Job 触及的所有文档 ID（不论成败）。job_runner 用它做 reconcile：
    # 把 succeeded/failed 对齐到 documents.processing_status 的真实值，
    # 避免 handler 内部计数与文档生命周期漂移。
    touched_doc_ids: List[int] = field(default_factory=list)
    # 该 handler 是否对完整解析生命周期负责。
    #   True  -> 期望触及的 doc 最终都到达 'ready'（如 ParseIndex / OnlineIngestion），
    #            job_runner 据此把 succeeded 对齐到 documents.processing_status
    #   False -> 中转性 handler（如 LocalUpload 只负责文件落库，状态仍在 'pending'
    #            等待下一个 job 解析），不应该被 reconcile 误判成失败
    reconcile_with_lifecycle: bool = False


class BaseJobHandler(Protocol):
    def run(self, *, db, user_id: int, kb_id: int, payload: Dict[str, Any]) -> JobResult:
        """
        执行核心业务逻辑并返回结果，不关心 Job 状态管理。
        """
        ...
