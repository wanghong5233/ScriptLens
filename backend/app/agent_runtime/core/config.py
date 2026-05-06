"""
配置管理模块
管理 Agent 服务的配置项
"""
from pydantic_settings import BaseSettings
from typing import Optional, Dict
import os


class Settings(BaseSettings):
    """应用配置"""

    # 服务配置（agent_runtime 子包内嵌运行，不再独立监听端口；保留 NAME/VERSION 用于日志/metrics）
    SERVICE_NAME: str = "script-studio"
    SERVICE_VERSION: str = "1.0.0"

    # LLM 配置（使用和主API服务相同的环境变量名）
    DASHSCOPE_API_KEY: Optional[str] = None  # 从 .env 加载
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DASHSCOPE_MODEL_NAME: str = "qwen3-max"  # 基础模型（API 调用）
    DASHSCOPE_MODEL_CANDIDATES: str = "qwen-plus,qwen3-max,qwen-max,qwen-turbo"
    DASHSCOPE_VISION_MODEL_NAME: str = "qwen-vl-max"  # 图片问答默认模型
    OPENAI_API_KEY: Optional[str] = None  # 可选：OpenAI API Key
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL_NAME: str = "gpt-5.2"
    OPENAI_MODEL_CANDIDATES: str = "gpt-5.2,gpt-5-mini,gpt-4.1,gpt-4o"

    # LLM 请求超时与健康策略
    LLM_REQUEST_TIMEOUT: int = 75
    LLM_FALLBACK_ENABLED: bool = True
    LLM_FALLBACK_ALLOW_EXPLICIT_PROVIDER: bool = True
    LLM_HEALTH_FAILURE_THRESHOLD: int = 3
    LLM_HEALTH_COOLDOWN_SECONDS: int = 90

    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_TOKENS: int = 3072

    # LLM 成本统计（默认 0，按需在环境变量配置）
    # LLM_COST_CONFIG 示例：
    # {
    #   "dashscope": {"qwen-plus": {"input": 0.0, "output": 0.0}, "default": {"input": 0.0, "output": 0.0}},
    #   "openai": {"gpt-4o": {"input": 0.0, "output": 0.0}}
    # }
    LLM_COST_CONFIG: Dict[str, Dict[str, Dict[str, float]]] = {}
    LLM_COST_PER_1K_INPUT_TOKENS: float = 0.0
    LLM_COST_PER_1K_OUTPUT_TOKENS: float = 0.0

    # Agent 配置
    AGENT_MAX_ITERATIONS: int = 10
    AGENT_TIMEOUT: int = 300  # 秒
    AGENT_WORKSPACE_CACHE_TTL: int = 60  # 秒
    AGENT_WORKSPACE_CACHE_SIZE: int = 16  # 缓存条目数
    AGENT_HISTORY_MAX_ENTRIES: int = 300  # 历史记录最大条数（0 表示不限制）
    AGENT_HISTORY_MAX_BYTES: int = 268435456  # 历史记录最大磁盘占用（默认 256MB，0 表示不限制）
    AGENT_HISTORY_MAX_ENTRIES_PER_FILE: int = 80  # 单文件最大版本条数（0 表示不限制）
    AGENT_HISTORY_RECORD_EMPTY_OPS: bool = False  # 无文件变更的操作是否记录到历史
    AGENT_HISTORY_PERSIST_AFTER_SNAPSHOT: bool = False  # 是否保存 after 快照（默认关闭，节省空间）
    AGENT_MANUAL_HISTORY_MIN_INTERVAL_SECONDS: int = 45  # 手动编辑最小入库间隔
    AGENT_MANUAL_HISTORY_FORCE_INTERVAL_SECONDS: int = 300  # 手动编辑强制入库间隔
    AGENT_WORKSPACE_LOCK_TTL: int = 600  # 工作区锁最大持续时间（秒）

    # 语义检索（embedding 索引）配置
    SEMANTIC_SEARCH_ENABLED: bool = True
    SEMANTIC_SEARCH_EMBED_PROVIDER: str = "auto"  # auto|dashscope|openai
    SEMANTIC_SEARCH_EMBED_MODEL: str = ""  # 为空时：dashscope->text-embedding-v3, openai->text-embedding-3-small
    SEMANTIC_SEARCH_EMBED_BATCH_SIZE: int = 24
    SEMANTIC_SEARCH_INDEX_TTL_SECONDS: int = 900  # 内存索引空闲 TTL
    SEMANTIC_SEARCH_INDEX_PERSIST_ENABLED: bool = True
    SEMANTIC_SEARCH_INDEX_DIR: str = "/tmp/script_studio_semantic_index"
    SEMANTIC_SEARCH_INDEX_PERSIST_MIN_INTERVAL_SECONDS: int = 30
    SEMANTIC_SEARCH_MAX_FILE_BYTES: int = 786432  # 单文件参与索引最大字节数（默认 768KB）
    SEMANTIC_SEARCH_MAX_CHUNKS_PER_FILE: int = 120
    SEMANTIC_SEARCH_CHUNK_LINES: int = 36
    SEMANTIC_SEARCH_CHUNK_OVERLAP_LINES: int = 8
    SEMANTIC_SEARCH_COLD_START_PREWARM_ENABLED: bool = True
    SEMANTIC_SEARCH_COLD_START_PREWARM_MAX_FILES: int = 120

    # Web Search 配置
    ENABLE_WEB_SEARCH: bool = True
    WEB_SEARCH_PROVIDER: str = "tavily"
    WEB_SEARCH_API_KEY: Optional[str] = None
    WEB_SEARCH_BASE_URL: str = "https://api.tavily.com/search"
    WEB_SEARCH_MAX_RESULTS: int = 8
    WEB_SEARCH_TIMEOUT: int = 20

    # 工作区配置
    WORKSPACES_ROOT: str = "/app/workspaces"

    # 日志配置
    LOG_LEVEL: str = "INFO"

    # ==================== LLM Prompt 日志配置 ====================
    # 用于调试 Agent 决策流程，查看发送给 LLM 的完整上下文
    LOG_FULL_PROMPT: bool = True  # 是否在日志中输出完整 Prompt/响应

    # 工具参数详情日志配置（影响日志大小）
    # - True: 输出每个工具的完整 JSON Schema（参数类型、描述、required 等）
    #   优点：可以看到 LLM 接收到的完整工具定义
    #   缺点：日志量大（13个工具 × ~20行 = ~260行），且这些 Schema 是固定的，调试价值低
    # - False: 只输出工具名称和简短描述
    #   优点：日志简洁（13个工具 × 1行 = 13行），减少 60-70% 的日志量
    #   缺点：看不到参数定义细节（但可以在代码中查看）
    #
    # 🎯 推荐设置：
    # - 开发/调试工具问题时：True
    # - 正常使用/生产环境：False（默认）
    LOG_PROMPT_INCLUDE_TOOL_PARAMS: bool = False  # 是否在 Prompt 日志中包含工具参数详情

    # Auth / JWT（用于调用主 RAG 服务需要认证的接口）
    JWT_SECRET_KEY: Optional[str] = None
    JWT_ACCESS_TOKEN_EXPIRE_DAYS: int = 30

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


# 全局配置实例
settings = Settings()


def refresh_settings() -> Settings:
    """Reload settings from environment into the existing instance."""

    updated = Settings()
    for name in settings.model_fields:
        setattr(settings, name, getattr(updated, name))
    return settings
