"""ScriptLens 剧本 router。

四个端点（D2-3 范围）：
- POST /api/scripts/upload         上传剧本文件，立即返回 script_id（异步两阶段）
- GET  /api/scripts                列出当前用户全部剧本
- GET  /api/scripts/{id}           剧本详情（用于轮询 status）
- GET  /api/scripts/{id}/scenes    全部场景（前端编辑器渲染原文）
- GET  /api/scripts/{id}/report    分析报告（D2-4 实装；现在 status='ready' 但
                                    reports 表无数据时返回 not_ready）
- POST /api/scripts/{id}/reanalyze 重新跑评分（D2-4 实装；现在返回 501）

D2-4 / D2-5 / D2-6 会在此基础上加 chat / rewrite / feedback / view。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Tuple

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
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
    FeedbackItem,
    FeedbackListResponse,
    FeedbackRequest,
    OperationListResponse,
    OperationSnapshotResponse,
    OperationSummary,
    ReportNotReadyResponse,
    ReportPayload,
    ReportResponse,
    RevertOperationResponse,
    RewriteRequest,
    RewriteResponse,
    SceneItem,
    ScriptChatRequest,
    ScriptDetail,
    ScriptListItem,
    ScriptScenesResponse,
    ScriptUploadResponse,
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
from service.script_ingestion_service import ScriptIngestionService
from service.script_query_service import (
    ScriptNotFoundError,
    get_report,
    get_script_detail,
    get_script_status,
    list_scenes,
    list_user_scripts,
)
from service.script_report_service import generate_report
from utils.database import get_db

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
        "后台 BackgroundTask 跑解析+切分+embedding+落库；"
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
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
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"文件超过 {settings.MAX_UPLOAD_SIZE_MB} MB 限制",
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件为空"
        )

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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # 3. 注册 BackgroundTask 跑完整链路
    background_tasks.add_task(_run_ingestion_task, script_id, str(storage_path))

    return ScriptUploadResponse(
        id=script_id,
        title=title,
        source_format=suffix.lstrip("."),
        status="pending",
    )


def _run_ingestion_task(script_id: str, file_path_str: str) -> None:
    """BackgroundTask 入口（不抛错给上游，全部错误已写入 scripts.failure_reason）。"""
    try:
        ScriptIngestionService().run_ingestion(
            script_id=script_id,
            file_path=Path(file_path_str),
        )
    except Exception:
        logger.exception("background ingestion failed script_id=%s", script_id)


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在或无权限访问")
    return ScriptDetail(**row)


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在或无权限访问")
    return ScriptScenesResponse(
        script_id=script_id,
        total=len(rows),
        scenes=[SceneItem(**r) for r in rows],
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在或无权限访问")

    if s_status != "ready":
        return ReportNotReadyResponse(
            script_id=script_id,
            status=s_status,
            failure_reason=failure_reason,
        )

    payload = get_report(script_id=script_id, user_id=current_user.id)
    if payload is None:
        # status='ready' 但 reports 表空 —— 用户尚未触发 reanalyze（或正在生成中）
        return ReportNotReadyResponse(
            script_id=script_id,
            status="ready",
            failure_reason="报告尚未生成或正在生成中，请调用 POST /api/scripts/{id}/reanalyze 触发或继续轮询",
        )
    report_json, generated_at = payload
    return ReportResponse(
        script_id=script_id,
        report=ReportPayload.model_validate(report_json),
        generated_at=generated_at,
    )


@router.post(
    "/{script_id}/reanalyze",
    summary="触发 5 维评分（异步）",
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在或无权限访问")
    if s_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"剧本当前 status={s_status}，需先等解析完成（status=ready）才能触发评分",
        )
    background_tasks.add_task(_run_report_task, script_id)
    return {"script_id": script_id, "status": "analyzing"}


async def _run_report_task(script_id: str) -> None:
    """BackgroundTask 入口：跑评分流水线，全部异常吞入 log（前端通过 GET /report 看不到结果即知失败）。"""
    try:
        await generate_report(script_id=script_id)
    except Exception:
        logger.exception("background report generation failed script_id=%s", script_id)


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


@router.post(
    "/{script_id}/chat",
    summary="多轮追问 ReAct Agent（SSE 流）",
    description=(
        "in-process 调 agent_runtime（doc_studio 派生），返回 SSE 流。"
        "事件类型：start / step / delta / status / runtime_model / finish / complete / error。"
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
        s_status, _ = get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在或无权限访问")
    if s_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"剧本当前 status={s_status}，需 ready 后才能 chat",
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
    agent = build_chat_agent(script_id=script_id)
    queue: asyncio.Queue = asyncio.Queue()
    sentinel: Tuple[str, Dict[str, Any]] = ("__END__", {})

    async def _progress_callback(event_type: str, payload: Dict[str, Any]) -> None:
        await queue.put((event_type, dict(payload) if isinstance(payload, dict) else {"data": payload}))

    async def _runner() -> None:
        try:
            result = await agent.execute(
                user_intent=user_intent,
                workspace_id=script_id,  # ScriptChatAgent 把它直接当 script_id 用
                user_id=current_user.id,
                context={"script_id": script_id, "role": body.role},
                progress_callback=_progress_callback,
            )
            # 把最终结果（execution_history / warnings / runtime_model）作为单独事件发送
            await queue.put((
                "complete",
                {
                    "success": bool(result.get("success")),
                    "execution_history": result.get("execution_history") or [],
                    "warnings": result.get("warnings") or [],
                    "runtime_model": result.get("runtime_model"),
                    "operation_id": result.get("operation_id"),
                },
            ))
        except AgentScriptNotFoundError as exc:
            logger.warning("chat: script not found script_id=%s", script_id)
            await queue.put(("error", {"message": str(exc), "type": "ScriptNotFoundError", "http_status": 404}))
        except AgentScriptPermissionError as exc:
            logger.warning("chat: permission denied script_id=%s user_id=%s", script_id, current_user.id)
            await queue.put(("error", {"message": str(exc), "type": "ScriptPermissionError", "http_status": 403}))
        except AgentScriptNotReadyError as exc:
            logger.warning("chat: script not ready script_id=%s", script_id)
            await queue.put(("error", {"message": str(exc), "type": "ScriptNotReadyError", "http_status": 409}))
        except Exception as exc:
            # 其他未预期异常：明确发 error 事件并写完整堆栈日志，绝不吞掉
            logger.exception("chat agent execute failed script_id=%s", script_id)
            await queue.put(("error", {
                "message": str(exc),
                "type": exc.__class__.__name__,
                "http_status": 500,
            }))
        finally:
            await queue.put(sentinel)

    runner_task = asyncio.create_task(_runner())

    async def _stream() -> AsyncIterator[bytes]:
        try:
            while True:
                event_type, payload = await queue.get()
                if event_type == "__END__":
                    break
                yield _sse_format(event_type, payload)
        finally:
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在或无权限访问")
    if s_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"剧本当前 status={s_status}，需 ready 后才能 rewrite",
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.error or "改写失败",
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
        code = (
            status.HTTP_404_NOT_FOUND
            if "不存在" in msg
            else status.HTTP_403_FORBIDDEN
            if "无权" in msg
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=msg)
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在或无权限访问")

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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在或无权限访问")

    try:
        rows = script_operation_service.list_operations(
            script_id=script_id,
            user_id=current_user.id,
            limit=max(1, min(int(limit), 200)),
        )
    except script_operation_service.OperationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    return OperationListResponse(
        script_id=script_id,
        items=[OperationSummary(**r) for r in rows],
    )


@router.get(
    "/{script_id}/operations/{operation_id}/snapshot",
    response_model=OperationSnapshotResponse,
    summary="取某 op 在某场景的 before/after 文本快照",
    description=(
        "对齐前端 `fetchOperationSnapshotFile` 协议（`file_path` 在 ScriptLens 里就是 scene_id）。"
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在或无权限访问")

    try:
        snapshot = script_operation_service.get_operation_snapshot(
            operation_id=operation_id,
            user_id=current_user.id,
            file_path=file_path,
            version=version,
        )
    except script_operation_service.OperationError as exc:
        msg = str(exc)
        code = (
            status.HTTP_404_NOT_FOUND
            if "不存在" in msg
            else status.HTTP_403_FORBIDDEN
            if "无权" in msg
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=msg)

    return OperationSnapshotResponse(**snapshot)


@router.post(
    "/{script_id}/operations/{operation_id}/revert",
    response_model=RevertOperationResponse,
    summary="回退某 op（短剧场景目前 no-op：不改 scenes.text）",
    description=(
        "ScriptLens 暂不持久化 keep 后的改写到 scenes 表（避免覆盖用户原始上传）。"
        "本端点保留以兼容 doc-studio timeline UI，统一返回三份空数组（前端会显示"
        "「该时间点没有可恢复内容」提示）。后续如果加 scene_versions 表，这里"
        "再切到真实回退逻辑。"
    ),
)
def revert_script_operation(
    script_id: str,
    operation_id: str,
    current_user: User = Depends(get_current_user),
) -> RevertOperationResponse:
    from service import script_operation_service

    try:
        get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在或无权限访问")

    # 仅校验 op 归属（让"操作不存在 / 无权"的报错走真实路径）
    try:
        script_operation_service.get_operation_snapshot(
            operation_id=operation_id,
            user_id=current_user.id,
            file_path="__validate_owner_only__",
            version="before",
        )
    except script_operation_service.OperationError as exc:
        msg = str(exc)
        if "不存在" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        if "无权" in msg:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)
        # 其他校验错误（如 version 非法）这里走不到

    return RevertOperationResponse(
        operation_id=operation_id,
        reverted_files=[],
        deleted_files=[],
        skipped_files=[],
    )


# ============================================================
# View（D2-6d）：按角色重排报告（不重生成评分）
# ============================================================


# 角色 -> 优先关注的维度（按权重排序）
_ROLE_DIMENSION_PRIORITY = {
    "selection": ("opening_hook", "reward_density", "risk", "pacing", "motivation"),
    "writer": ("motivation", "pacing", "opening_hook", "reward_density", "risk"),
    "review": ("risk", "motivation", "pacing", "opening_hook", "reward_density"),
}


@router.get(
    "/{script_id}/view",
    response_model=ViewResponse,
    summary="按角色重排报告（selection/writer/review）",
    description=(
        "不重新生成评分。只在已生成的 reports.report_json 基础上："
        "1) 按角色优先级重排 scorecard "
        "2) 把该角色优先关注维度对应的 evidence_ref_ids 推到 must_read_scene_ids 头部。"
        "报告未生成时返回 409。"
    ),
)
def get_script_view(
    script_id: str,
    role: str,
    current_user: User = Depends(get_current_user),
) -> ViewResponse:
    if role not in _ROLE_DIMENSION_PRIORITY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"非法 role={role}；可选：{sorted(_ROLE_DIMENSION_PRIORITY.keys())}",
        )

    try:
        s_status, _ = get_script_status(script_id=script_id, user_id=current_user.id)
    except ScriptNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在或无权限访问")
    if s_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"剧本当前 status={s_status}，需 ready 后才有报告可看",
        )

    payload = get_report(script_id=script_id, user_id=current_user.id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="报告尚未生成，请先调用 POST /reanalyze",
        )
    report_json, _ = payload
    report = ReportPayload.model_validate(report_json)

    priority = _ROLE_DIMENSION_PRIORITY[role]

    # 重排 scorecard：把优先维度排在前面，保持其他原顺序
    pri_index = {dim: i for i, dim in enumerate(priority)}
    scorecard_sorted = sorted(
        report.scorecard,
        key=lambda item: pri_index.get(item.dimension, 99),
    )

    # 重选 must_read：把优先维度对应的 evidence_ref_ids 提前
    role_focus_dims = [d for d in priority[:3]]  # 前 3 维是 role 真正强关注的
    focused_ref_ids: List[str] = []
    seen = set()
    for dim_name in role_focus_dims:
        for sc in report.scorecard:
            if sc.dimension == dim_name:
                for rid in sc.evidence_ref_ids or []:
                    if rid not in seen:
                        seen.add(rid)
                        focused_ref_ids.append(rid)
    # 兜底：如果重排后不足 3 条，把原始 must_read 补上
    for rid in (report.must_read_scene_ids or []):
        if rid not in seen and len(focused_ref_ids) < 3:
            seen.add(rid)
            focused_ref_ids.append(rid)

    return ViewResponse(
        script_id=script_id,
        role=role,  # type: ignore[arg-type]
        decision=report.decision,
        overall_score=report.overall_score,
        summary=report.summary or report.decision.summary,
        scorecard=scorecard_sorted,
        must_read_scene_ids=focused_ref_ids[:3],
        risk_flags=report.risk_flags,
        role_focus=list(role_focus_dims),
        evidence_refs=report.evidence_refs,
    )
