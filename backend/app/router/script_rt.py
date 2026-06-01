"""ScriptLens 剧本 router。

四个端点（D2-3 范围）：
- POST /api/scripts/upload         上传剧本文件，立即返回 script_id（异步两阶段）
- GET  /api/scripts                列出当前用户全部剧本
- GET  /api/scripts/{id}           剧本详情（用于轮询 status）
- DELETE /api/scripts/{id}         删除剧本及所有派生数据
- GET  /api/scripts/{id}/scenes    全部场景（前端编辑器渲染原文）
- GET  /api/scripts/{id}/report    分析报告（D2-4 实装；现在 status='ready' 但
                                    reports 表无数据时返回 not_ready）
- POST /api/scripts/{id}/reanalyze 重新跑评分（D2-4 实装；现在返回 501）

D2-4 / D2-5 / D2-6 会在此基础上加 chat / rewrite / feedback / view。
"""

from __future__ import annotations

import asyncio
import time
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Tuple

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent_runtime.factory import (
    ScriptNotFoundError as AgentScriptNotFoundError,
    ScriptNotReadyError as AgentScriptNotReadyError,
    ScriptPermissionError as AgentScriptPermissionError,
    build_chat_agent,
)
from core.config import settings
from models.user import User
from schemas.script import (
    ApiError,
    FeedbackItem,
    FeedbackListResponse,
    FeedbackRequest,
    OperationListResponse,
    OperationSnapshotResponse,
    OperationSummary,
    ReportNotReadyResponse,
    ReportPayload,
    ReportProgressResponse,
    ReportProgressSnapshot,
    ReportResponse,
    RevertOperationRequest,
    RevertOperationResponse,
    RewriteRequest,
    RewriteResponse,
    SceneContentUpdateRequest,
    SceneContentUpdateResponse,
    ScriptDeleteResponse,
    SceneItem,
    ScriptChatRequest,
    ScriptDetail,
    ScriptListItem,
    ScriptScenesResponse,
    ScriptUploadResponse,
    ScriptWorkspaceUpdateRequest,
    ViewResponse,
)
from service.auth import get_current_user
from service.core.api.utils.file_utils import get_project_base_directory
from service.core.ingestion.script_loader import UnsupportedScriptFormatError
from service.script_feedback_service import (
    FeedbackError,
    add_feedback,
    format_feedback_for_prompt,
    list_recent_feedback,
)
from service.script_delete_service import delete_script_cascade
from service.script_ingestion_service import ScriptIngestionService
from service.script_progress_tracker import tracker as report_progress_tracker
from service.script_query_service import (
    ScriptNotFoundError,
    get_report,
    get_script_detail,
    get_script_status,
    list_scenes,
    list_user_scripts,
    update_script_workspace,
    update_scene_text,
)
from service.script_report_service import generate_report
from utils.database import engine as default_engine, get_db

logger = logging.getLogger(__name__)
router = APIRouter()


# 允许的剧本文件后缀（与 script_loader 保持一致；.doc 在 loader 层会抛 friendly error）
_ALLOWED_SUFFIXES = {".docx", ".pdf", ".txt", ".md"}


# ============================================================
# 上传 + 异步处理
# ============================================================


@router.post(
    "/upload",
    response_model=ScriptUploadResponse,
    summary="上传剧本（异步两阶段）",
    description=(
        "接受 docx/pdf/txt/md。.doc 请先用 Office/WPS 另存为 .docx。"
        "上传立即返回 script_id + status='pending'，"
        "后台 BackgroundTask 跑解析+切分+检索索引落库；"
        "前端通过 GET /scripts/{id} 轮询 status。"
    ),
)
async def upload_script(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ScriptUploadResponse:
    raw_name = file.filename or "untitled"
    suffix = Path(raw_name).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        _raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "UNSUPPORTED_SCRIPT_FORMAT",
            (
                f"不支持的文件格式：{suffix}；"
                f"仅支持 {', '.join(sorted(_ALLOWED_SUFFIXES))}（.doc 请先转 .docx）"
            ),
        )

    # 1. 落盘到 <PROJECT_BASE>/storage/scriptlens/<user_id>/<uuid>.<ext>
    storage_dir = Path(get_project_base_directory(
        "storage", "scriptlens", str(current_user.id)
    ))
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{uuid.uuid4()}{suffix}"

    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    written = 0
    try:
        with storage_path.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    _raise_api_error(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        "UPLOAD_TOO_LARGE",
                        f"文件超过 {settings.MAX_UPLOAD_SIZE_MB} MB 限制",
                    )
                f.write(chunk)
    except HTTPException:
        if storage_path.exists():
            os.remove(storage_path)
        raise
    finally:
        await file.close()

    if written == 0:
        if storage_path.exists():
            os.remove(storage_path)
        _raise_api_error(status.HTTP_400_BAD_REQUEST, "EMPTY_UPLOAD", "上传文件为空")

    title = Path(raw_name).stem or "未命名剧本"
    logger.info(
        "upload_script user_id=%s file=%s bytes=%s path=%s",
        current_user.id, raw_name, written, storage_path,
    )

    # 2. INSERT scripts(status='pending')
    service = ScriptIngestionService()
    try:
        script_id = service.start_pending(
            file_path=storage_path,
            user_id=current_user.id,
            title=title,
        )
    except UnsupportedScriptFormatError as e:
        if storage_path.exists():
            os.remove(storage_path)
        _raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "UNSUPPORTED_SCRIPT_FORMAT",
            str(e),
        )

    # 3. 注册 BackgroundTask 跑完整链路：ingest（切场入库）→ 自动评分报告
    #    用户上传剧本的产品语义就是「分析这个剧本」，不应让用户上传完再手动点一次。
    background_tasks.add_task(_run_full_pipeline_task, script_id, str(storage_path))

    return ScriptUploadResponse(
        id=script_id,
        title=title,
        source_format=suffix.lstrip("."),
        status="pending",
    )


async def _run_full_pipeline_task(script_id: str, file_path_str: str) -> None:
    """BackgroundTask 入口：先跑 ingestion（同步、CPU/IO 重，下放线程池），
    ingestion 成功后立即接评分流水线（async / LLM 重）。

    任一步失败都只 log，不向上游 BackgroundTasks 抛——失败原因已写入
    scripts.failure_reason / 通过 reports 表为空让前端感知。
    """
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: ScriptIngestionService().run_ingestion(
                script_id=script_id,
                file_path=Path(file_path_str),
            ),
        )
    except Exception:
        logger.exception("background ingestion failed script_id=%s", script_id)
        return  # ingestion 失败时 status='failed'，没必要再跑评分

    try:
        await generate_report(script_id=script_id)
        logger.info("auto reanalyze after ingestion done script_id=%s", script_id)
    except Exception:
        logger.exception("auto reanalyze after ingestion failed script_id=%s", script_id)


# ============================================================
# 列表 + 详情
# ============================================================


@router.get(
    "",
    response_model=List[ScriptListItem],
    summary="列出当前用户的剧本（按创建时间倒序）",
)
def list_scripts(
    current_user: User = Depends(get_current_user),
    limit: int = 50,
) -> List[ScriptListItem]:
    rows = list_user_scripts(user_id=current_user.id, limit=limit)
    return [ScriptListItem(**r) for r in rows]


@router.get(
    "/{script_id}",
    response_model=ScriptDetail,
    summary="剧本详情（含 status / 统计字段 / failure_reason）",
)
def get_script(
    script_id: str,
    current_user: User = Depends(get_current_user),
) -> ScriptDetail:
    try:
        row = get_script_detail(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "剧本不存在或无权限访问",
        )
    return ScriptDetail(**row)


@router.patch(
    "/{script_id}",
    response_model=ScriptDetail,
    responses={400: {"model": ApiError}, 404: {"model": ApiError}},
    summary="更新剧本工作区元数据（title/config）",
)
def patch_script_workspace(
    script_id: str,
    payload: ScriptWorkspaceUpdateRequest,
    current_user: User = Depends(get_current_user),
) -> ScriptDetail:
    try:
        row = update_script_workspace(
            script_id=script_id,
            user_id=current_user.id,
            title=payload.title,
            config=payload.config,
        )
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "鍓ф湰涓嶅瓨鍦ㄦ垨鏃犳潈闄愯闂?",
        )
    except ValueError as exc:
        _raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_WORKSPACE_CONFIG",
            str(exc),
        )
    return ScriptDetail(**row)


@router.get(
    "/{script_id}/messages",
    summary="分页获取工作区绑定会话的消息历史",
    responses={400: {"model": ApiError}, 404: {"model": ApiError}},
)
def list_script_workspace_messages(
    script_id: str,
    session_id: str,
    page: int = 1,
    page_size: int = 200,
    current_user: User = Depends(get_current_user),
):
    if page < 1:
        page = 1
    page_size = max(1, min(page_size, 500))
    try:
        row = get_script_detail(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "鍓ф湰涓嶅瓨鍦ㄦ垨鏃犳潈闄愯闂?",
        )

    cfg = row.get("workspace_config") if isinstance(row, dict) else {}
    bound_session_id = _parse_script_workspace_session_id(
        (cfg or {}).get("session_id")
    )
    if not bound_session_id:
        _raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "SESSION_NOT_BOUND",
            "宸ヤ綔鍖烘湭缁戝畾浼氳瘽",
        )
    req_session_id = _parse_script_workspace_session_id(session_id)
    if req_session_id != bound_session_id:
        _raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "SESSION_MISMATCH",
            "璇锋眰 session_id 涓庡綋鍓嶅伐浣滃尯缁戝畾浼氳瘽涓嶄竴鑷?",
        )

    with default_engine.connect() as conn:
        session_row = conn.execute(
            text(
                """
                SELECT session_id, user_id, surface
                FROM public.sessions
                WHERE session_id = :session_id
                """
            ),
            {"session_id": req_session_id},
        ).first()
        if session_row is None:
            _raise_api_error(
                status.HTTP_404_NOT_FOUND,
                "SESSION_NOT_FOUND",
                "浼氳瘽涓嶅瓨鍦?",
            )
        if str(session_row.user_id) != str(current_user.id):
            _raise_api_error(
                status.HTTP_403_FORBIDDEN,
                "SESSION_FORBIDDEN",
                "鏃犳潈璁块棶璇ヤ細璇?",
            )
        if str(session_row.surface or "").strip() != "doc_studio":
            _raise_api_error(
                status.HTTP_400_BAD_REQUEST,
                "INVALID_SESSION_SURFACE",
                "浼氳瘽 surface 蹇呴』涓?doc_studio",
            )

        total = conn.execute(
            text(
                """
                SELECT count(1)
                FROM public.messages
                WHERE session_id = :session_id
                """
            ),
            {"session_id": req_session_id},
        ).scalar() or 0

        rows = conn.execute(
            text(
                """
                SELECT message_id::text AS message_id,
                       session_id,
                       user_question,
                       model_answer,
                       create_time,
                       retrieval_content
                FROM public.messages
                WHERE session_id = :session_id
                ORDER BY create_time ASC, message_id ASC
                OFFSET :offset
                LIMIT :limit
                """
            ),
            {
                "session_id": req_session_id,
                "offset": (page - 1) * page_size,
                "limit": page_size,
            },
        ).mappings().all()

    items = [dict(item) for item in rows]
    return {
        "total": int(total),
        "page": int(page),
        "pageSize": int(page_size),
        "items": items,
    }


@router.delete(
    "/{script_id}",
    response_model=ScriptDeleteResponse,
    summary="删除剧本及其所有派生数据",
    description=(
        "级联删除 scenes / chunks / report / evidence_refs / feedback / operations，"
        "并删除上传的原始文件。timeline 属于 operations，会一起清理。"
    ),
)
def delete_script(
    script_id: str,
    current_user: User = Depends(get_current_user),
) -> ScriptDeleteResponse:
    try:
        result = delete_script_cascade(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "剧本不存在或无权限访问",
        )
    return ScriptDeleteResponse(
        deleted=True,
        script_id=result.script_id,
        title=result.title,
        storage_deleted=result.storage_deleted,
        deleted_counts=result.deleted_counts,
    )


@router.get(
    "/{script_id}/scenes",
    response_model=ScriptScenesResponse,
    summary="列出剧本的全部场景（前端编辑器渲染原文）",
)
def get_script_scenes(
    script_id: str,
    current_user: User = Depends(get_current_user),
    limit: int = 1000,
) -> ScriptScenesResponse:
    try:
        rows = list_scenes(script_id=script_id, user_id=current_user.id, limit=limit)
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "剧本不存在或无权限访问",
        )
    return ScriptScenesResponse(
        script_id=script_id,
        total=len(rows),
        scenes=[SceneItem(**r) for r in rows],
    )


@router.put(
    "/{script_id}/scenes/{scene_id}/content",
    response_model=SceneContentUpdateResponse,
    summary="写回场景全文（AgentDiffReview reject hunk 路径用）",
    description=(
        "前端 updateFileContent(path=scene_id) 走本端点直接 UPDATE scriptlens.scenes.text。"
        "与 LaTeX 工作区写磁盘对称，承接 docs/10-rewrite-agent.md §6 diff 透明迁移机制。"
        "权限：必须是当前用户的剧本（404 / 越权统一报 not_found）。"
    ),
)
def put_scene_content(
    script_id: str,
    scene_id: str,
    payload: SceneContentUpdateRequest,
    current_user: User = Depends(get_current_user),
) -> SceneContentUpdateResponse:
    try:
        result = update_scene_text(
            script_id=script_id,
            scene_id=scene_id,
            user_id=current_user.id,
            content=payload.content,
        )
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCENE_NOT_FOUND",
            "剧本或场景不存在",
        )
    except ValueError as e:
        _raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_SCENE_CONTENT",
            str(e),
        )

    return SceneContentUpdateResponse(**result)


@router.get(
    "/{script_id}/export",
    summary="导出完整剧本（应用所有改写历史后的最新版本）",
    description=(
        "F3：拼装最终全文 + 渲染。format ∈ {docx, pdf, txt}。"
        "对每个 scene，若有最新成功 rewrite 则用 snapshot_after，否则用 scenes.text 原文。"
    ),
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {},
                "application/pdf": {},
                "text/plain": {},
            }
        }
    },
)
def export_script_full_text(
    script_id: str,
    format: str = "docx",  # noqa: A002 — 与 query 参数语义一致
    current_user: User = Depends(get_current_user),
):
    from urllib.parse import quote

    from fastapi.responses import Response

    from service.script_export_service import (
        ExportError,
        ScriptNotFoundError as ExportScriptNotFoundError,
        ScriptPermissionError as ExportScriptPermissionError,
        render_export,
    )

    try:
        payload, content_type, filename = render_export(
            script_id=script_id, user_id=current_user.id, fmt=format.lower()
        )
    except ExportScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "剧本不存在",
        )
    except ExportScriptPermissionError:
        _raise_api_error(
            status.HTTP_403_FORBIDDEN,
            "SCRIPT_FORBIDDEN",
            "无权导出该剧本",
        )
    except ExportError as exc:
        _raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_EXPORT_REQUEST",
            str(exc),
        )

    encoded_name = quote(filename)
    return Response(
        content=payload,
        media_type=content_type,
        headers={
            # RFC 5987 兼容写法，让前端能取到中文文件名
            "Content-Disposition": (
                f"attachment; filename=\"{filename}\"; filename*=UTF-8''{encoded_name}"
            ),
        },
    )


# ============================================================
# 报告（D2-4 才会真正写入；这里先给 not_ready 分支）
# ============================================================


@router.get(
    "/{script_id}/report",
    response_model=ReportResponse | ReportNotReadyResponse,
    summary="读取分析报告",
    description=(
        "ready 且 reports 表已生成 → 200 + ReportResponse；"
        "其他状态 → 200 + ReportNotReadyResponse(status, failure_reason)，前端继续轮询。"
    ),
)
def get_script_report(
    script_id: str,
    current_user: User = Depends(get_current_user),
):
    try:
        s_status, failure_reason = get_script_status(
            script_id=script_id, user_id=current_user.id
        )
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "剧本不存在或无权限访问",
        )

    if s_status != "ready":
        return ReportNotReadyResponse(
            script_id=script_id,
            status=s_status,
            failure_reason=failure_reason,
        )

    payload = get_report(script_id=script_id, user_id=current_user.id)
    if payload is None:
        # status='ready' 但 reports 表空 —— 通常是上传链路里自动触发的评分还在跑、
        # 或者上一次评分失败/被清空。前端继续轮询本接口；不再要求用户手动 POST /reanalyze。
        return ReportNotReadyResponse(
            script_id=script_id,
            status="ready",
            failure_reason="评分报告正在自动生成中，请稍候",
        )
    report_json, generated_at = payload
    return ReportResponse(
        script_id=script_id,
        report=ReportPayload.model_validate(report_json),
        generated_at=generated_at,
    )


@router.post(
    "/{script_id}/reanalyze",
    summary="触发评分分析（异步）",
    description=(
        "拉起后台 BackgroundTask 跑评分流水线，立即返回 202。"
        "前端继续轮询 GET /report 直到拿到 ReportResponse。"
        "重新触发会 DELETE 旧报告（连带 evidence_refs CASCADE）后重写。"
    ),
    status_code=status.HTTP_202_ACCEPTED,
)
def reanalyze_script(
    script_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    try:
        s_status, _ = get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "剧本不存在或无权限访问",
        )
    if s_status != "ready":
        _raise_api_error(
            status.HTTP_409_CONFLICT,
            "SCRIPT_NOT_READY",
            f"剧本当前 status={s_status}，需先等解析完成（status=ready）才能触发评分",
        )
    background_tasks.add_task(_run_report_task, script_id)
    return {"script_id": script_id, "status": "analyzing"}


async def _run_report_task(script_id: str) -> None:
    """BackgroundTask 入口：跑评分流水线，全部异常吞入 log（前端通过 GET /report 看不到结果即知失败）。"""
    try:
        await generate_report(script_id=script_id)
    except Exception:
        logger.exception("background report generation failed script_id=%s", script_id)


@router.get(
    "/{script_id}/progress",
    response_model=ReportProgressResponse,
    summary="读取评分流水线的实时进度",
    description=(
        "返回内存里 progress_tracker 的快照（7 阶段时间轴 + 当前 detail）。"
        "snapshot 为 null 表示当前没有评分任务在跑（也没有 5 分钟内的旧快照）。"
        "前端在 reports 表为空时轮询此接口可视化进度。"
    ),
)
def get_script_progress(
    script_id: str,
    current_user: User = Depends(get_current_user),
) -> ReportProgressResponse:
    # 验证用户拥有这个剧本（防止其他用户偷看进度）
    try:
        get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_op_error_detail("SCRIPT_NOT_FOUND", "剧本不存在或无权限访问"),
        )

    snap_dict = report_progress_tracker.to_dict(script_id)
    if snap_dict is None:
        return ReportProgressResponse(script_id=script_id, snapshot=None)
    return ReportProgressResponse(
        script_id=script_id,
        snapshot=ReportProgressSnapshot.model_validate(snap_dict),
    )


# ============================================================
# Chat（D2-6a）：ReAct Agent SSE 流
# ============================================================


# 角色 -> 注入到 user_intent 头部的 system 角色提示（D2-6c 会扩展更细的 prompt 注入）
_ROLE_HINT = {
    "selection": "（用户角色：内容选品 / 采购）你应该重点关注前 5 集钩子、爽点密度、市场卖点；引用必须带 scene 标号。",
    "writer": "（用户角色：编剧）回答应聚焦剧作结构、动机自洽、节奏控制；引用必须带 scene 标号；如需改写请走 propose_rewrite_tool。",
    "review": "（用户角色：平台审核）回答应突出风险等级、合规红线、可能的整改建议；不得为高风险内容洗白。",
    "rewrite": "（用户角色：改写）必须先 locate_scenes 确认 scene_id，再 propose_rewrite_tool 给具体改写。",
    "general": "",
}


def _format_history_into_intent(
    question: str,
    history: List,
    role: str,
    feedback_block: str = "",
) -> str:
    """把 chat 历史 + 当前 question + 角色提示 + feedback 注入打包成单段 user_intent。

    LaTeXEditAgent.execute() 期望单字符串 user_intent；我们将 history 内联进去，
    用清晰分段保留对话语义。后续若上 chat_sessions 表，可改走 state.conversation_history。
    """
    parts: List[str] = []
    role_hint = _ROLE_HINT.get(role, "")
    if role_hint:
        parts.append(role_hint)
    if feedback_block:
        parts.append(feedback_block)
        parts.append("")
    if history:
        parts.append("【历史对话】")
        for item in history[-10:]:  # 只取最近 10 条，避免 prompt 爆炸
            tag = "用户" if item.role == "user" else "助手"
            text = (item.content or "").strip()
            if not text:
                continue
            parts.append(f"{tag}：{text}")
        parts.append("")
    parts.append(f"【本轮问题】\n{question.strip()}")
    return "\n".join(parts)


def _sse_format(event_type: str, payload: Dict[str, Any]) -> bytes:
    """SSE event 格式化：`event: <type>\\ndata: <json>\\n\\n`。"""
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")


def _op_error_detail(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def _raise_api_error(status_code: int, code: str, message: str) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _raise_operation_http_error(msg: str) -> None:
    text = str(msg or "").strip()
    if "不存在" in text:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_op_error_detail("OPERATION_NOT_FOUND", text),
        )
    if "无权" in text:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_op_error_detail("OPERATION_FORBIDDEN", text),
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=_op_error_detail("INVALID_OPERATION_REQUEST", text or "操作请求非法"),
    )


def _parse_script_workspace_session_id(value: Any) -> str | None:
    text_value = str(value or "").strip()
    return text_value or None


def _extract_final_agent_reply(raw_result: Dict[str, Any]) -> str:
    finish_reply = ""
    history = raw_result.get("execution_history")
    if isinstance(history, list):
        for step in reversed(history):
            if not isinstance(step, dict):
                continue
            if str(step.get("type") or "").strip().lower() != "finish":
                continue
            content = str(step.get("content") or "").strip()
            if content:
                finish_reply = content
                break
            result_payload = step.get("result")
            if isinstance(result_payload, dict):
                reply = str(result_payload.get("reply") or "").strip()
                if reply:
                    finish_reply = reply
                    break
    if finish_reply:
        return finish_reply
    return ""


def _build_retrieval_content_payload(
    *,
    script_id: str,
    session_id: str,
    role: str,
    context: Dict[str, Any],
    raw_result: Dict[str, Any],
) -> str | None:
    payload: Dict[str, Any] = {
        "source": "scriptlens_chat",
        "workspace_id": script_id,
        "script_id": script_id,
        "session_id": session_id,
        "role": role,
        "trace_id": raw_result.get("trace_id"),
        "run_id": raw_result.get("operation_id"),
        "operation_id": raw_result.get("operation_id"),
    }
    if isinstance(context, dict):
        if isinstance(context.get("image_attachments"), list):
            payload["images"] = context.get("image_attachments")
        if isinstance(context.get("selections"), list):
            payload["selections"] = context.get("selections")
        elif isinstance(context.get("selection"), dict):
            payload["selections"] = [context.get("selection")]
        if isinstance(context.get("file_mentions"), list):
            payload["file_mentions"] = context.get("file_mentions")
        file_path = str(context.get("file_path") or "").strip()
        if file_path:
            payload["file_path"] = file_path
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return None


def _persist_chat_message(
    *,
    script_id: str,
    user_id: int,
    session_id: str,
    question: str,
    answer: str,
    retrieval_content: str | None,
    display_text: str | None = None,
) -> str | None:
    """持久化 user/assistant 一对消息。

    `question` 是发给 agent / LLM 的真 prompt，可能包含 <SELECTION> block 等
    内联上下文。`display_text` 是 UI 端的展示版本，含 @selection1 / @scene1
    短 placeholder，UI 刷新后由前端 renderPromptWithMentionTags 还原成 chip。
    我们写入 messages.user_question 的是 display_text（缺省回落到 question），
    确保用户在 chat 历史里看到的是 chip，而不是赤裸的 inline XML。
    """
    if not answer.strip():
        return None
    persisted_question = display_text if (display_text and display_text.strip()) else question
    with default_engine.begin() as conn:
        script_row = conn.execute(
            text(
                """
                SELECT user_id
                FROM scriptlens.scripts
                WHERE id = :sid
                """
            ),
            {"sid": script_id},
        ).first()
        if script_row is None:
            raise ScriptNotFoundError(script_id)
        if int(script_row.user_id) != int(user_id):
            _raise_api_error(
                status.HTTP_403_FORBIDDEN,
                "SCRIPT_FORBIDDEN",
                "鏃犳潈璁块棶璇ュ墽鏈?",
            )
        session_row = conn.execute(
            text(
                """
                SELECT session_id, user_id, surface
                FROM public.sessions
                WHERE session_id = :session_id
                """
            ),
            {"session_id": session_id},
        ).first()
        if session_row is None:
            _raise_api_error(
                status.HTTP_404_NOT_FOUND,
                "SESSION_NOT_FOUND",
                "浼氳瘽涓嶅瓨鍦?",
            )
        if str(session_row.user_id) != str(user_id):
            _raise_api_error(
                status.HTTP_403_FORBIDDEN,
                "SESSION_FORBIDDEN",
                "鏃犳潈璁块棶璇ヤ細璇?",
            )
        if str(session_row.surface or "").strip() != "doc_studio":
            _raise_api_error(
                status.HTTP_400_BAD_REQUEST,
                "INVALID_SESSION_SURFACE",
                "浼氳瘽 surface 蹇呴』涓?doc_studio",
            )
        message_id = conn.execute(
            text(
                """
                INSERT INTO public.messages (
                    session_id, user_question, model_answer, retrieval_content
                ) VALUES (
                    :session_id, :user_question, :model_answer, :retrieval_content
                )
                RETURNING message_id::text
                """
            ),
            {
                "session_id": session_id,
                "user_question": persisted_question,
                "model_answer": answer,
                "retrieval_content": retrieval_content,
            },
        ).scalar()
    return str(message_id) if message_id else None


@router.post(
    "/{script_id}/chat",
    summary="多轮追问 ReAct Agent（SSE 流）",
    description=(
        "in-process 调 agent_runtime（doc_studio 派生），返回 SSE 流。"
        "事件类型：start / step / delta / status / runtime_model / finish / result / run_error / complete / error。"
        "前端按 EventSource 协议解析；reply 文本通过 delta 事件流式拼接，"
        "完整 execution_history 在 finish 事件里。"
    ),
)
async def chat_with_script(
    script_id: str,
    body: ScriptChatRequest,
    current_user: User = Depends(get_current_user),
):
    try:
        script_row = get_script_detail(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "鍓ф湰涓嶅瓨鍦ㄦ垨鏃犳潈闄愯闂?",
        )
    bound_session_id = _parse_script_workspace_session_id(
        (script_row.get("workspace_config") or {}).get("session_id")
    )
    session_id = _parse_script_workspace_session_id(body.session_id) or bound_session_id
    if session_id is None:
        _raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "SESSION_ID_REQUIRED",
            "chat 蹇呴』鎻愪緵 session_id 鎴栦簨鍏堢粦瀹?workspace_config.session_id",
        )
    if bound_session_id and session_id != bound_session_id:
        _raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "SESSION_MISMATCH",
            "chat session_id 涓庡綋鍓嶅伐浣滃尯缁戝畾浼氳瘽涓嶄竴鑷?",
        )

    try:
        s_status, _ = get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "剧本不存在或无权限访问",
        )
    if s_status != "ready":
        _raise_api_error(
            status.HTTP_409_CONFLICT,
            "SCRIPT_NOT_READY",
            f"剧本当前 status={s_status}，需 ready 后才能 chat",
        )

    # 注入用户既往反馈到 prompt 头部（PRD §10 P3 轻量 skill 机制）
    # fail aloud：DB 查询失败让 chat 整体失败，让用户感知问题，禁止静默吞错
    recent_fb = list_recent_feedback(
        script_id=script_id, user_id=current_user.id, limit=8
    )
    feedback_block = format_feedback_for_prompt(recent_fb)

    user_intent = _format_history_into_intent(
        body.question, body.history, body.role, feedback_block=feedback_block
    )
    context_payload: Dict[str, Any] = (
        dict(body.context) if isinstance(body.context, dict) else {}
    )
    # script_id / role 由路由层强约束，禁止被客户端 context 覆盖。
    context_payload["script_id"] = script_id
    context_payload["role"] = body.role
    context_payload["session_id"] = session_id
    agent = build_chat_agent(script_id=script_id)
    queue: asyncio.Queue = asyncio.Queue()
    sentinel: Tuple[str, Dict[str, Any]] = ("__END__", {})
    # chat_session_start 一行总览：排障的第一锚点。trace_id 此时由 agent
    # 内部生成（agent_service._build_operation_id 之后），所以在 _runner
    # 拿到 raw_result 后再补一条 chat_session_end 总览（包含 trace_id）。
    chat_start_ts = time.monotonic()
    logger.info(
        "chat_session_start script_id=%s session_id=%s user_id=%s intent_chars=%d role=%s",
        script_id,
        session_id,
        current_user.id,
        len(user_intent or ""),
        body.role,
    )

    async def _progress_callback(event_type: str, payload: Dict[str, Any]) -> None:
        # 关键观测点：每个 SSE 事件落 queue 前先 debug log，便于在 docker logs
        # 里看到 agent 真实推送了哪些 event（start/step/delta/finish/result）。
        logger.debug(
            "chat: progress event script_id=%s session_id=%s event=%s payload_keys=%s",
            script_id,
            session_id,
            event_type,
            list(payload.keys()) if isinstance(payload, dict) else None,
        )
        await queue.put((event_type, dict(payload) if isinstance(payload, dict) else {"data": payload}))

    async def _runner() -> None:
        # 进入 _runner 的第一行 log。如果 docker logs 里看不到这条，说明
        # asyncio.create_task(_runner()) 创建的 task 根本没获得调度（通常是
        # client 早早 disconnect 或者上层 BFF 把 fetch 整个 abort 了）。
        logger.info(
            "chat: _runner started script_id=%s session_id=%s",
            script_id,
            session_id,
        )

        async def _emit_error_events(payload: Dict[str, Any]) -> None:
            err_payload = dict(payload) if isinstance(payload, dict) else {"message": str(payload)}
            error_text = str(err_payload.get("message") or "执行失败")
            # 兼容 doc-studio async 事件名（run_error）。
            await queue.put(("run_error", {"error": error_text, **err_payload}))
            # 保留 ScriptLens 原始事件名（error）。
            await queue.put(("error", err_payload))

        try:
            raw_result = await agent.execute(
                user_intent=user_intent,
                workspace_id=script_id,  # ScriptChatAgent 把它直接当 script_id 用
                user_id=current_user.id,
                context=context_payload,
                progress_callback=_progress_callback,
            )
            result_payload = {
                "success": bool(raw_result.get("success")),
                "changes": raw_result.get("changes") or [],
                "file_diffs": raw_result.get("file_diffs") or [],
                "bibliography_updates": raw_result.get("bibliography_updates"),
                "execution_history": raw_result.get("execution_history") or [],
                "episode_id": raw_result.get("episode_id"),
                "intent_type": raw_result.get("intent_type"),
                "plan": raw_result.get("plan"),
                "warnings": raw_result.get("warnings") or [],
                "trace_id": raw_result.get("trace_id"),
                "operation_id": raw_result.get("operation_id"),
                "history_path": raw_result.get("history_path"),
                "intent_confidence": raw_result.get("intent_confidence"),
                "runtime_model": raw_result.get("runtime_model"),
            }
            try:
                answer = _extract_final_agent_reply(raw_result)
                retrieval_content = _build_retrieval_content_payload(
                    script_id=script_id,
                    session_id=session_id,
                    role=body.role,
                    context=context_payload,
                    raw_result=raw_result,
                )
                message_id = _persist_chat_message(
                    script_id=script_id,
                    user_id=current_user.id,
                    session_id=session_id,
                    question=body.question,
                    display_text=body.display_text,
                    answer=answer,
                    retrieval_content=retrieval_content,
                )
                if message_id:
                    result_payload["message_id"] = message_id
            except HTTPException:
                raise
            except Exception:
                logger.exception(
                    "chat persistence failed script_id=%s session_id=%s",
                    script_id,
                    session_id,
                )
                raise
            # chat_session_end 一行总览：含 trace_id，是排障的主索引行。
            # 用户报错时给我截图，我从 docker logs grep trace_id=<hex> 一次定位。
            execution_history = result_payload.get("execution_history") or []
            tools_used = [
                step.get("tool_name")
                for step in execution_history
                if isinstance(step, dict)
                and step.get("type") == "action"
                and step.get("tool_name")
            ]
            logger.info(
                "chat_session_end script_id=%s session_id=%s trace_id=%s "
                "success=%s intent_type=%s elapsed_ms=%d steps=%d tools=%s "
                "changes=%d file_diffs=%d operation_id=%s",
                script_id,
                session_id,
                result_payload.get("trace_id") or "<missing>",
                result_payload["success"],
                result_payload.get("intent_type"),
                int((time.monotonic() - chat_start_ts) * 1000),
                len(execution_history),
                tools_used,
                len(result_payload.get("changes") or []),
                len(result_payload.get("file_diffs") or []),
                result_payload.get("operation_id"),
            )
            # 兼容 doc-studio async 事件名（result），payload 结构与其一致：{ result: {...} }。
            await queue.put(("result", {"result": result_payload}))
            # 保留 ScriptLens 现有 complete 事件，避免破坏旧客户端。
            await queue.put((
                "complete",
                result_payload,
            ))
        except AgentScriptNotFoundError as exc:
            logger.warning(
                "chat: script not found script_id=%s session_id=%s",
                script_id,
                session_id,
            )
            await _emit_error_events({"message": str(exc), "type": "ScriptNotFoundError", "http_status": 404})
        except AgentScriptPermissionError as exc:
            logger.warning(
                "chat: permission denied script_id=%s session_id=%s user_id=%s",
                script_id,
                session_id,
                current_user.id,
            )
            await _emit_error_events({"message": str(exc), "type": "ScriptPermissionError", "http_status": 403})
        except AgentScriptNotReadyError as exc:
            logger.warning(
                "chat: script not ready script_id=%s session_id=%s",
                script_id,
                session_id,
            )
            await _emit_error_events({"message": str(exc), "type": "ScriptNotReadyError", "http_status": 409})
        except Exception as exc:
            # 其他未预期异常：明确发 error 事件并写完整堆栈日志，绝不吞掉
            logger.exception(
                "chat agent execute failed script_id=%s session_id=%s",
                script_id,
                session_id,
            )
            await _emit_error_events({
                "message": str(exc),
                "type": exc.__class__.__name__,
                "http_status": 500,
            })
        finally:
            await queue.put(sentinel)
            logger.info(
                "chat: _runner exited script_id=%s session_id=%s",
                script_id,
                session_id,
            )

    runner_task = asyncio.create_task(_runner())

    async def _stream() -> AsyncIterator[bytes]:
        events_yielded = 0
        try:
            while True:
                event_type, payload = await queue.get()
                if event_type == "__END__":
                    break
                events_yielded += 1
                yield _sse_format(event_type, payload)
        finally:
            # 关键观测：如果 events_yielded=0 + runner_task 还没 done，
            # 几乎可以确定是 client 端（BFF / 浏览器）提前断开导致 stream
            # 被 starlette cancel。这正是 BFF AbortSignal.timeout 击穿
            # SSE 长连接时的特征——上次 chat 测试就死在这里。
            logger.info(
                "chat: _stream closing script_id=%s session_id=%s events_yielded=%d runner_done=%s",
                script_id,
                session_id,
                events_yielded,
                runner_task.done(),
            )
            if not runner_task.done():
                runner_task.cancel()
                try:
                    await runner_task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 关闭缓冲
        },
    )


# ============================================================
# Rewrite（D2-6b）：单次定向改写（同步返回，前端直接拿 diff）
# ============================================================


@router.post(
    "/{script_id}/rewrite",
    response_model=RewriteResponse,
    summary="对单个场景做定向改写",
    description=(
        "同步调用：直接复用 propose_rewrite_tool 的底层逻辑（LLM 单次调用 + diff 生成）。"
        "适合前端按钮点选、不需要 ReAct 多轮推理的场景。"
        "需要 ReAct 思路（先定位再改写）请走 chat 端点。"
    ),
)
async def rewrite_scene(
    script_id: str,
    body: RewriteRequest,
    current_user: User = Depends(get_current_user),
) -> RewriteResponse:
    # 校验剧本存在 + 归属
    try:
        s_status, _ = get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "剧本不存在或无权限访问",
        )
    if s_status != "ready":
        _raise_api_error(
            status.HTTP_409_CONFLICT,
            "SCRIPT_NOT_READY",
            f"剧本当前 status={s_status}，需 ready 后才能 rewrite",
        )

    # 直接调底层（避开 ReAct 套娃；ProposeRewriteTool.execute 是 async，内部即纯函数）
    from agent_runtime.service.tools.script_tools import ProposeRewriteTool

    tool = ProposeRewriteTool()
    result = await tool.execute(
        parameters={
            "script_id": script_id,
            "scene_id": body.scene_id,
            "target_dimension": body.target_dimension,
            "issue": body.issue,
        },
        agent_state=None,
    )
    if not result.success:
        _raise_api_error(
            status.HTTP_400_BAD_REQUEST,
            "REWRITE_FAILED",
            result.error or "改写失败",
        )
    data = result.data or {}

    # M4 timeline：写一条 op 记录，用于 doc-studio 时间线复用
    from service import script_operation_service

    try:
        script_operation_service.record_rewrite_op(
            script_id=script_id,
            user_id=current_user.id,
            scene_id=body.scene_id,
            target_dimension=body.target_dimension,
            issue=body.issue,
            original_text=data.get("original_excerpt", ""),
            rewritten_text=data.get("rewritten_excerpt", ""),
            rationale=data.get("rationale", ""),
        )
    except script_operation_service.OperationError as exc:
        # 不阻断 rewrite 主流程；timeline 落库失败时打 warning 即可
        # （fail aloud 是面向用户能看到的链路；timeline 是辅助记录，丢一条不影响业务）
        import logging
        logging.getLogger(__name__).warning(
            "rewrite op 落库失败（不阻断响应）：script=%s scene=%s err=%s",
            script_id, body.scene_id, exc,
        )

    return RewriteResponse(
        script_id=script_id,
        scene_id=body.scene_id,
        target_dimension=body.target_dimension,
        issue=body.issue,
        original_text=data.get("original_excerpt", ""),
        rewritten_text=data.get("rewritten_excerpt", ""),
        rationale=data.get("rationale", ""),
        diff=data.get("diff", ""),
    )


# ============================================================
# Feedback（D2-6c）：写表 + 列最近反馈（chat 自动注入 prompt）
# ============================================================


@router.post(
    "/{script_id}/feedback",
    summary="提交反馈（PRD §10 P3 轻量 skill 机制）",
    description=(
        "scope=general/dimension/rewrite/scene；scope_ref 用于指定维度名 / scene_id / rewrite_id。"
        "下次 chat 调用会自动把最近 N 条反馈注入 system prompt，让 Agent 感知用户偏好。"
    ),
    status_code=status.HTTP_201_CREATED,
)
def submit_feedback(
    script_id: str,
    body: FeedbackRequest,
    current_user: User = Depends(get_current_user),
):
    try:
        record = add_feedback(
            script_id=script_id,
            user_id=current_user.id,
            scope=body.scope,
            scope_ref=body.scope_ref,
            message=body.message,
        )
    except FeedbackError as e:
        msg = str(e)
        if "不存在" in msg:
            _raise_api_error(status.HTTP_404_NOT_FOUND, "SCRIPT_NOT_FOUND", msg)
        if "无权" in msg:
            _raise_api_error(status.HTTP_403_FORBIDDEN, "SCRIPT_FORBIDDEN", msg)
        _raise_api_error(status.HTTP_400_BAD_REQUEST, "INVALID_FEEDBACK_REQUEST", msg)
    return record


@router.get(
    "/{script_id}/feedback",
    response_model=FeedbackListResponse,
    summary="列出当前用户对该剧本的反馈（最近 N 条）",
)
def list_feedback(
    script_id: str,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
) -> FeedbackListResponse:
    # 校验剧本归属
    try:
        get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_op_error_detail("SCRIPT_NOT_FOUND", "剧本不存在或无权限访问"),
        )

    rows = list_recent_feedback(script_id=script_id, user_id=current_user.id, limit=limit)
    return FeedbackListResponse(
        script_id=script_id,
        items=[
            FeedbackItem(
                id=r["id"],
                scope=r["scope"],
                scope_ref=r.get("scope_ref"),
                message=r["message"],
                created_at=r["created_at"],
            )
            for r in rows
        ],
    )


# ============================================================
# Operations（M4 timeline）：改写历史 / 快照预览 / 回退
# ============================================================


@router.get(
    "/{script_id}/operations",
    response_model=OperationListResponse,
    summary="列出剧本的全部操作历史（doc-studio timeline 复用）",
    description=(
        "返回字段对齐前端 `DocStudioAPI.OperationSummary`。"
        "目前只有 rewrite 类型的 op；未来可扩展 manual_edit / upload。"
        "snapshot 字段仅返回紧凑摘要，需要原文请走 GET /operations/{op_id}/snapshot。"
    ),
)
def list_script_operations(
    script_id: str,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
) -> OperationListResponse:
    from service import script_operation_service

    # 先校验剧本归属（防止 404 -> 误认为是 op 列表为空）
    try:
        get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_op_error_detail("SCRIPT_NOT_FOUND", "剧本不存在或无权限访问"),
        )

    try:
        rows = script_operation_service.list_operations(
            script_id=script_id,
            user_id=current_user.id,
            limit=max(1, min(int(limit), 200)),
        )
    except script_operation_service.OperationError as exc:
        _raise_operation_http_error(str(exc))

    return OperationListResponse(
        script_id=script_id,
        items=[OperationSummary(**r) for r in rows],
    )


@router.get(
    "/{script_id}/operations/{operation_id}/snapshot",
    response_model=OperationSnapshotResponse,
    summary="取某 op 在某场景的 before/after 文本快照",
    description=(
        "对齐前端 `fetchOperationSnapshotFile` 协议。"
        "operation_id 使用显式来源协议：db:<uuid> 或 history:<operation_id>。"
        "version=before 表示改写之前的原文；version=after 表示改写后的版本。"
    ),
)
def get_script_operation_snapshot(
    script_id: str,
    operation_id: str,
    file_path: str,
    version: str = "before",
    current_user: User = Depends(get_current_user),
) -> OperationSnapshotResponse:
    from service import script_operation_service

    try:
        get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "剧本不存在或无权限访问",
        )

    try:
        snapshot = script_operation_service.get_operation_snapshot(
            script_id=script_id,
            operation_id=operation_id,
            user_id=current_user.id,
            file_path=file_path,
            version=version,
        )
    except script_operation_service.OperationError as exc:
        _raise_operation_http_error(str(exc))

    return OperationSnapshotResponse(**snapshot)


@router.post(
    "/{script_id}/operations/{operation_id}/revert",
    response_model=RevertOperationResponse,
    summary="回退某 op：按 snapshot_before 还原 scenes.text",
    description=(
        "db:<uuid> 操作：从 script_operations.snapshot_before 回写到 scenes.text。"
        "history:<id> 操作：走 .agent_history 快照做 best-effort 回退。"
        "可通过 payload.files 指定回退文件子集；为空时回退该操作可恢复的全部文件。"
    ),
)
def revert_script_operation(
    script_id: str,
    operation_id: str,
    payload: RevertOperationRequest = Body(default_factory=RevertOperationRequest),
    current_user: User = Depends(get_current_user),
) -> RevertOperationResponse:
    from service import script_operation_service

    try:
        get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_op_error_detail("SCRIPT_NOT_FOUND", "剧本不存在或无权限访问"),
        )

    requested_files: List[str] = []
    requested_seen: set[str] = set()
    for raw_file in payload.files or []:
        file_path = str(raw_file or "").strip()
        if not file_path or file_path in requested_seen:
            continue
        requested_seen.add(file_path)
        requested_files.append(file_path)

    # 先校验 op 归属（让"操作不存在 / 无权"的报错走真实路径）
    try:
        locator = script_operation_service.validate_operation_access(
            script_id=script_id,
            operation_id=operation_id,
            user_id=current_user.id,
        )
    except script_operation_service.OperationError as exc:
        _raise_operation_http_error(str(exc))

    from agent_runtime.service.script_vfs import ScriptVFS, ScriptVFSError

    try:
        vfs = ScriptVFS(script_id=script_id)
    except ScriptVFSError as exc:
        _raise_operation_http_error(str(exc))

    reverted_files: List[str] = []
    skipped_files: List[str] = []

    if locator.source == "history":
        target_files = list(requested_files)
        if not target_files:
            try:
                _, history_payload = script_operation_service._load_agent_history_operation_payload(
                    script_id=script_id,
                    operation_id=locator.raw_id,
                    user_id=current_user.id,
                )
            except script_operation_service.OperationError:
                history_payload = {}
            modified_files = history_payload.get("modified_files")
            if isinstance(modified_files, list):
                for raw_file in modified_files:
                    file_path = str(raw_file or "").strip()
                    if file_path and file_path not in target_files:
                        target_files.append(file_path)

        with default_engine.begin() as conn:
            for raw_file in target_files:
                raw_path = str(raw_file or "").strip()
                if not raw_path:
                    continue
                try:
                    normalized_path = vfs.coerce_file_path(raw_path)
                    scene_id = vfs.resolve_scene_id(normalized_path)
                    display_path = vfs.resolve_file_path(scene_id)
                except ScriptVFSError:
                    skipped_files.append(raw_path)
                    continue
                try:
                    snapshot = script_operation_service.get_operation_snapshot(
                        script_id=script_id,
                        operation_id=operation_id,
                        user_id=current_user.id,
                        file_path=normalized_path,
                        version="before",
                    )
                except script_operation_service.OperationError:
                    skipped_files.append(display_path)
                    continue

                update_result = conn.execute(
                    text(
                        """
                        UPDATE scriptlens.scenes
                           SET text = :txt
                         WHERE id = :sid
                           AND script_id = :script_id
                        """
                    ),
                    {
                        "txt": str(snapshot.get("content") or ""),
                        "sid": scene_id,
                        "script_id": script_id,
                    },
                )
                if update_result.rowcount == 0:
                    skipped_files.append(display_path)
                    continue
                reverted_files.append(display_path)

        return RevertOperationResponse(
            operation_id=operation_id,
            reverted_files=reverted_files,
            deleted_files=[],
            skipped_files=skipped_files,
        )

    try:
        with default_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT op.snapshot_before
                    FROM scriptlens.script_operations op
                    WHERE op.id = :op
                      AND op.script_id = :sid
                    """
                ),
                {"op": locator.raw_id, "sid": script_id},
            ).mappings().first()
    except SQLAlchemyError as exc:
        _raise_operation_http_error(f"查询回滚快照失败: {exc}")

    if row is None:
        _raise_operation_http_error("操作记录不存在")

    snapshot_before = row.get("snapshot_before")
    if not isinstance(snapshot_before, dict):
        snapshot_before = {}

    target_files = list(requested_files)
    if not target_files:
        for key in snapshot_before.keys():
            key_str = str(key or "").strip()
            if key_str and key_str not in target_files:
                target_files.append(key_str)

    with default_engine.begin() as conn:
        for raw_file in target_files:
            raw_path = str(raw_file or "").strip()
            if not raw_path:
                continue

            try:
                normalized_path = vfs.coerce_file_path(raw_path)
            except ScriptVFSError:
                normalized_path = raw_path

            scene_id: str | None = None
            display_path = normalized_path
            for candidate in (normalized_path, raw_path):
                try:
                    scene_id = vfs.resolve_scene_id(candidate)
                    display_path = vfs.resolve_file_path(scene_id)
                    break
                except ScriptVFSError:
                    continue

            candidate_keys: List[str] = []
            for candidate in (raw_path, normalized_path, display_path, scene_id or ""):
                key = str(candidate or "").strip()
                if key and key not in candidate_keys:
                    candidate_keys.append(key)

            snapshot_text: Any = None
            for key in candidate_keys:
                if key in snapshot_before:
                    snapshot_text = snapshot_before.get(key)
                    break

            if scene_id is None or snapshot_text is None:
                skipped_files.append(display_path)
                continue

            update_result = conn.execute(
                text(
                    """
                    UPDATE scriptlens.scenes
                       SET text = :txt
                     WHERE id = :sid
                       AND script_id = :script_id
                    """
                ),
                {"txt": str(snapshot_text), "sid": scene_id, "script_id": script_id},
            )
            if update_result.rowcount == 0:
                skipped_files.append(display_path)
                continue
            reverted_files.append(display_path)

    return RevertOperationResponse(
        operation_id=operation_id,
        reverted_files=reverted_files,
        deleted_files=[],
        skipped_files=skipped_files,
    )


# ============================================================
# View（D2-6d）：按角色重排报告（不重生成评分）
# 派生逻辑（含 rewrite_seeds / task_status）见 service/script_view_service.py
# ============================================================


@router.get(
    "/{script_id}/view",
    response_model=ViewResponse,
    summary="返回报告全字段（含派生 rewrite_seeds / task_status）",
    description=(
        "不重新生成评分。基于已生成的 reports.report_json："
        "1) 透传 scorecard（顺序固定）+ drama_tags / plot_units / characters / character_relationships "
        "2) 派生 rewrite_seeds（最值得改的 N 场）+ task_status（已尝试改写次数 / 状态）。"
        "视角切换由前端「行动」segment 派生 Persona Action Card（详见 docs/09-action-lens.md），"
        "本接口不接受 role 参数、不按角色重排。报告未生成时返回 409。"
    ),
)
def get_script_view(
    script_id: str,
    current_user: User = Depends(get_current_user),
) -> ViewResponse:
    from service import script_view_service  # noqa: PLC0415  局部 import 与同模块其他 service 一致

    try:
        s_status, _ = get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        _raise_api_error(
            status.HTTP_404_NOT_FOUND,
            "SCRIPT_NOT_FOUND",
            "剧本不存在或无权限访问",
        )
    if s_status != "ready":
        _raise_api_error(
            status.HTTP_409_CONFLICT,
            "SCRIPT_NOT_READY",
            f"剧本当前 status={s_status}，需 ready 后才有报告可看",
        )

    payload = get_report(script_id=script_id, user_id=current_user.id)
    if payload is None:
        _raise_api_error(
            status.HTTP_409_CONFLICT,
            "REPORT_NOT_READY",
            "评分报告正在自动生成中，请稍候",
        )
    report_json, _ = payload
    # 兼容旧版（pre-v1-mvp-6d）写入的过期 payload：schema 升级后 tier 等字段格式变更，
    # 直接 500 会让前端"加载报告失败"卡死。这里把校验失败统一降级成 409，让前端走
    # ScriptlensReportProgress 自动重新触发分析；后台用 ERROR 级日志保留现场。
    from pydantic import ValidationError as _PydanticValidationError  # noqa: PLC0415
    try:
        report = ReportPayload.model_validate(report_json)
    except _PydanticValidationError as exc:
        logger.error(
            "ReportPayload schema mismatch (legacy payload), forcing reanalyze",
            extra={"script_id": script_id, "error": str(exc)},
        )
        _raise_api_error(
            status.HTTP_409_CONFLICT,
            "REPORT_SCHEMA_OUTDATED",
            "评分报告格式已升级，请重新生成（页面会自动重新分析）",
        )

    return script_view_service.build_view(
        script_id=script_id,
        user_id=current_user.id,
        report=report,
    )
