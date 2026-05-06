from typing import Optional, Literal
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Any
from core.config import settings

SessionSurface = Literal["deep_chat", "doc_studio"]


class SessionDefaults(BaseModel):
    """会话级默认参数（可保存/回读）。"""

    retrievalStrategy: Literal["multi_stage", "graph", "multimodal_graph"] = Field("multi_stage")
    rerankerStrategy: Literal["none", "supervised", "rl"] = Field("none")
    topK: int = Field(default_factory=lambda: getattr(settings, "SM_RAG_TOPK", 6), ge=1, le=50)
    language: Literal["zh", "en"] = Field("zh")
    streaming: bool = Field(True)
    useSessionKnowledgeBase: bool = Field(
        True, description="是否启用当前会话的会话知识库用于检索"
    )
    useUserKnowledgeBase: bool = Field(
        False, description="是否启用用户已有知识库用于检索"
    )
    userKnowledgeBaseId: Optional[int] = Field(
        None, description="启用用户知识库时所绑定的知识库ID"
    )
    llmProvider: Literal["dashscope", "openai", "local"] = Field(
        "openai",
        description="本轮问答默认使用的 LLM Provider",
    )
    llmModel: Optional[str] = Field(
        None,
        description="本轮问答默认使用的模型（为空时由后端路由自动回退）",
    )


class CreateSessionRequest(BaseModel):
    """创建会话请求体。
    - 每个 session 都会创建并绑定一个 Session KB（会话级知识库）；
    - 可选传入 kbId，作为默认关联知识库（用于用户知识库检索，不替代 Session KB）。
    """
    model_config = ConfigDict(extra="forbid")
    kbId: Optional[int] = Field(None, description="默认关联知识库ID（不会替代会话知识库）")
    ephemeral: bool = Field(True, description="保留字段；会话始终创建并绑定 Session KB")
    defaults: Optional[SessionDefaults] = Field(None, description="会话默认检索/生成参数")
    surface: SessionSurface = Field(
        "deep_chat",
        description="会话所属产品面（deep_chat/doc_studio）",
    )


class CreateSessionResponse(BaseModel):
    sessionId: str
    kbId: Optional[int] = None
    ephemeral: bool
    defaults: SessionDefaults
    surface: SessionSurface = "deep_chat"


class SessionDetail(BaseModel):
    sessionId: str
    kbId: Optional[int] = None
    sessionName: str
    surface: SessionSurface = "deep_chat"


class SessionRenameRequest(BaseModel):
    """会话重命名请求体。"""

    session_name: str = Field(..., min_length=1, max_length=120, description="新的会话名称")


class AskImageAttachment(BaseModel):
    """聊天输入中的图片附件。"""

    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=255)
    dataUrl: str = Field(..., min_length=16, description="data URL 格式的图片内容")
    mimeType: Optional[str] = Field(None, description="图片 MIME 类型")
    size: Optional[int] = Field(None, ge=0, description="图片字节大小")


class AskRequest(BaseModel):
    """会话问答请求体。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, description="用户输入问题")
    runId: Optional[str] = Field(None, description="前端生成的请求 run_id（用于取消与重连）")
    stream: bool = Field(True, description="是否启用 SSE 流式返回")
    persistHistory: bool = Field(
        True,
        description="是否将本次问答写入会话历史（内部 Agent 调用可关闭）",
    )
    focusDocIds: Optional[List[int]] = Field(None, description="聚焦文档 ID 列表")
    topK: Optional[int] = Field(None, ge=1, le=50, description="检索 TopK")
    temperature: Optional[float] = Field(None, ge=0, le=2, description="采样温度")
    maxTokens: Optional[int] = Field(None, ge=1, le=65536, description="最大输出 token")
    compressHistory: bool = Field(False, description="是否压缩历史上下文")
    indexMode: Optional[Literal["auto", "session_only", "global_only", "hybrid", "disabled"]] = Field(
        None,
        description="索引检索模式",
    )
    replaceFromMessageId: Optional[str] = Field(None, description="历史编辑替换起点 message_id")
    ragProvider: Optional[str] = Field(None, description="RAG provider 覆盖")
    provider: Optional[str] = Field(None, description="兼容字段：RAG provider 覆盖")
    llmProvider: Optional[Literal["dashscope", "openai", "local"]] = Field(
        None,
        description="本次请求的 LLM Provider 覆盖",
    )
    llmModel: Optional[str] = Field(None, description="本次请求的 LLM 模型覆盖")
    imageAttachments: Optional[List[AskImageAttachment]] = Field(
        None,
        description="图片附件列表（最多 4 张，单张不超过前端限制）",
    )


class SessionRewindRequest(BaseModel):
    """会话回卷请求体。"""

    model_config = ConfigDict(extra="forbid")

    keep_messages: Optional[int] = Field(
        None,
        ge=0,
        description="保留前 N 条消息（可选）",
    )
    before_message_id: Optional[str] = Field(
        None,
        description="保留该消息之前的历史（不含该消息）",
    )


# --- Compare API ---
class CompareRequest(BaseModel):
    """跨论文对比请求。
    - docIds: 需要对比的文档 ID 列表（同一会话/知识库下）。
    - dimensions: 对比维度（如 ["Methodology", "Results", "Limitations"]）。
    """
    docIds: List[int] = Field(..., min_items=2, description="待对比的 document_id 列表（至少2篇）")
    dimensions: List[str] = Field(..., min_items=1, description="对比维度列表")


class CompareResponse(BaseModel):
    answer: str = Field(..., description="Markdown 表格形式的对比结果")
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    usage: Dict[str, Any] = Field(default_factory=dict)
    debug: Dict[str, Any] = Field(default_factory=dict)
