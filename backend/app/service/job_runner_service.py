from __future__ import annotations
from typing import Tuple, Type

from models.document import Document, DocumentProcessingStatus
from models.job import Job, JobStatus, JobType
from service.job_service import job_service
from utils.database import SessionLocal
from service.job_handler.interfaces import BaseJobHandler, JobResult
from utils.get_logger import log


def _reconcile_with_documents(db, doc_ids: list[int]) -> Tuple[int, int]:
    """Recompute (succeeded, failed) from documents.processing_status.

    Single source of truth: a doc is "succeeded" iff the lifecycle says it
    landed in 'ready' (i.e. has chunks and is RAG-able). Anything else (still
    parsing because the user retried, failed at any stage, or was reset to
    pending mid-flight) is "failed" from the job's point of view.

    This stops job counters from drifting when a handler short-circuits or a
    later coordinator (e.g. OnlineIngestion->ParseIndex chain) mutates the
    same docs.
    """
    if not doc_ids:
        return 0, 0
    rows = (
        db.query(Document.processing_status)
        .filter(Document.id.in_(doc_ids))
        .all()
    )
    succeeded = sum(1 for (status,) in rows if status == DocumentProcessingStatus.READY.value)
    failed = len(doc_ids) - succeeded
    return succeeded, failed

def execute_job(job_id: int, handler_cls: Type[BaseJobHandler]):
    """
    通用 Job 执行器：
    - 管理 Job 生命周期（状态更新、错误处理）
    - 调用具体的 Handler 执行业务逻辑
    - 处理后续任务的触发
    """
    db = SessionLocal()
    handler = handler_cls()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return
        if (job.status or "").lower() == JobStatus.CANCELLED.value:
            log.info("JobRunner: skip cancelled job_id=%s", job_id)
            return

        job_service.update_progress(db, job_id=job.id, user_id=job.user_id, status=JobStatus.RUNNING.value, progress=0)
        try:
            log.info(f"JobRunner: start handler={handler_cls.__name__} job_id={job.id} kb_id={job.knowledge_base_id} user_id={job.user_id}")
        except Exception:
            pass

        result = handler.run(db=db, user_id=job.user_id, kb_id=job.knowledge_base_id, payload=job.payload or {})

        # Reconcile counters against documents.processing_status only for
        # handlers that own the lifecycle to 'ready'. For middle-of-pipeline
        # handlers (e.g. LocalUpload, which legitimately leaves docs in
        # 'pending' for the next job to consume), trusting the handler's own
        # accounting is correct.
        if result.reconcile_with_lifecycle and result.touched_doc_ids:
            reconciled_ok, reconciled_failed = _reconcile_with_documents(db, result.touched_doc_ids)
            if (reconciled_ok, reconciled_failed) != (result.succeeded, result.failed):
                log.info(
                    f"JobRunner: reconcile job_id={job.id} "
                    f"handler_reported=(ok={result.succeeded}, failed={result.failed}) "
                    f"-> documents=(ok={reconciled_ok}, failed={reconciled_failed})"
                )
                result.succeeded = reconciled_ok
                result.failed = reconciled_failed
                result.total = max(result.total, reconciled_ok + reconciled_failed)

        try:
            log.info(f"JobRunner: handler finished id={job.id} succeeded={result.succeeded} failed={result.failed} total={result.total}")
        except Exception:
            pass

        final_status = (
            JobStatus.SUCCESS.value
            if result.failed == 0 and result.total > 0
            else (
                JobStatus.PARTIAL.value
                if result.succeeded > 0
                else JobStatus.FAILED.value
            )
        )
        latest_job = db.query(Job).filter(Job.id == job.id).first()
        if latest_job and (latest_job.status or "").lower() == JobStatus.CANCELLED.value:
            log.info("JobRunner: job cancelled during execution job_id=%s", job.id)
            return
        job_service.update_progress(
            db,
            job_id=job.id,
            user_id=job.user_id,
            status=final_status,
            progress=100,
            total=result.total,
            succeeded=result.succeeded,
            failed=result.failed,
        )

        job = db.query(Job).filter(Job.id == job.id).first()
        if job:
            from sqlalchemy.orm.attributes import flag_modified
            payload = job.payload or {}
            payload["resultDetails"] = result.details
            job.payload = payload
            flag_modified(job, "payload")  # 强制标记 JSON 字段为已修改
            db.add(job)
            db.commit()
            db.refresh(job)
            log.info(f"JobRunner: saved resultDetails count={len(result.details)} to job_id={job.id}")

        # 触发后续解析任务
        if result.doc_ids_to_parse:
            from service.job_handler.parse_index_handler import ParseIndexHandler
            parse_job = job_service.create_job(
                db,
                user_id=job.user_id,
                kb_id=job.knowledge_base_id,
                type=JobType.PARSE_INDEX.value,
                payload={"fromJobId": job.id, "docs": result.doc_ids_to_parse, "sessionId": (job.payload or {}).get("sessionId")},
            )
            try:
                log.info(f"JobRunner: schedule ParseIndexHandler parse_job_id={parse_job.id} docs={result.doc_ids_to_parse}")
            except Exception:
                pass
            # 注意：这里直接在当前后台任务中执行，未来可优化为独立任务
            execute_job(job_id=parse_job.id, handler_cls=ParseIndexHandler)

    except Exception as e:
        # 无法读取 job.user_id 时无法更新权限校验的进度，这里尽力而为
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job_service.update_progress(db, job_id=job_id, user_id=job.user_id, status=JobStatus.FAILED.value, error=str(e))
        finally:
            pass
    finally:
        db.close()
