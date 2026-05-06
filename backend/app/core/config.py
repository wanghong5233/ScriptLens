from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache
from typing import Literal, Optional
from pydantic import model_validator
from urllib.parse import quote_plus


class Settings(BaseSettings):
    """
    应用配置类，使用 Pydantic-settings 自动从环境变量加载配置。
    单一配置入口，避免多处加载 .env 造成的时序冲突。
    """
    # Service identity
    SERVICE_NAME: str = "scholarmind-api"
    SERVICE_DISPLAY_NAME: str = "ScholarMind API"
    SERVICE_DESCRIPTION: str = (
        "ScholarMind 主站 API，提供 RAG 检索、会话管理、"
        "知识库、文档处理与内部网关能力。"
    )
    SERVICE_VERSION: str = "0.1.0"
    # Semantic Scholar
    semantic_scholar_api_key: str | None = Field(None, env="SEMANTIC_SCHOLAR_API_KEY")

    # Database
    DATABASE_URL: str | None = None

    # Redis
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    # Auth / Root path
    JWT_SECRET_KEY: Optional[str] = None
    JWT_ACCESS_TOKEN_EXPIRE_DAYS: int = 30  # 生产环境：7天，开发环境可设为30天
    JWT_ALGORITHM: str = "HS256"
    # Admin access allowlist（逗号分隔）
    # MVP 阶段采用配置白名单；后续 phase2 可平滑升级到 RBAC 模型。
    SM_ADMIN_USERNAMES: str = ""
    SM_ADMIN_USER_IDS: str = ""
    # Admin Console 独立登录账号（与主站用户体系解耦）
    SM_ADMIN_CONSOLE_USERNAME: str = "admin"
    SM_ADMIN_CONSOLE_PASSWORD: str = "admin123456"
    # Internal service token allowlist（逗号分隔）
    SM_INTERNAL_SERVICE_ALLOWLIST: str = "doc_studio,deep_research"
    # 是否保留旧的 /api/debug 路由（生产建议 false，仅保留 /api/admin/debug）
    ENABLE_DEBUG_ROUTES: bool = False
    # CORS allowlist（逗号分隔；默认 *）
    SM_CORS_ALLOW_ORIGINS: str = "*"
    # 可选：CORS 正则白名单（用于临时域名，如 *.trycloudflare.com）
    SM_CORS_ALLOW_ORIGIN_REGEX: Optional[str] = None
    # Demo mode（默认关闭，开启后建议关闭 admin/debug 路由暴露）
    SM_DEMO_MODE: bool = False
    SM_DEMO_ENTRY_ENABLED: bool = False
    SM_DEMO_USERNAME: str = "testuser"
    # 可选：Demo 入口附加校验码（为空表示不校验）
    SM_DEMO_ENTRY_CODE: Optional[str] = None
    SM_DEMO_ENTRY_RATE_PER_MINUTE: int = 20
    SM_DEMO_TOKEN_EXPIRE_HOURS: int = 2
    ROOT_PATH: str = ""
    # Internal service base URLs (Gateway/BFF)
    DEEP_RESEARCH_SERVICE_URL: str = "http://deep_research:8004"
    DOC_STUDIO_SERVICE_URL: str = "http://doc_studio:8003"

    # Elasticsearch
    ES_HOST: str = "http://localhost:9200"
    ELASTIC_PASSWORD: Optional[str] = None
    ES_URL: str = ""
    # 兼容旧代码（等全仓清理后可移除）
    ELASTICSEARCH_URL: Optional[str] = None
    ES_DEFAULT_INDEX: str = "scholarmind_default"
    SM_ES_CLIENT_TIMEOUT_SECS: int = 60
    SM_ES_SEARCH_TIMEOUT_SECS: int = 20
    SM_ES_SEARCH_RETRY_TIMES: int = 1
    # pgvector is the default vector store. The elasticsearch option is kept only
    # as a short-lived rollback path during the migration window.
    SM_VECTOR_STORE: Literal["elasticsearch", "pgvector"] = "pgvector"
    SM_PGVECTOR_TABLE: str = "rag_chunks"
    SM_PGVECTOR_DUAL_WRITE_ENABLED: bool = False
    SM_PGVECTOR_DUAL_WRITE_STRICT: bool = False

    @model_validator(mode="after")
    def build_es_url(self) -> "Settings":
        if self.ELASTIC_PASSWORD:
            user_encoded = quote_plus("elastic")
            password_encoded = quote_plus(self.ELASTIC_PASSWORD)
            if "://" in self.ES_HOST:
                protocol, host = self.ES_HOST.split("://", 1)
                self.ES_URL = f"{protocol}://{user_encoded}:{password_encoded}@{host}"
            else:
                self.ES_URL = f"http://{user_encoded}:{password_encoded}@{self.ES_HOST}"
        else:
            self.ES_URL = self.ES_HOST
        # 同步兼容字段
        self.ELASTICSEARCH_URL = self.ES_URL
        return self

    # DashScope / OpenAI 兼容
    DASHSCOPE_API_KEY: Optional[str] = None
    DASHSCOPE_BASE_URL: Optional[str] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: Optional[str] = "https://api.openai.com/v1"

    # 模型名称
    DASHSCOPE_MODEL_NAME: str = "qwen3-max"
    OPENAI_MODEL_NAME: str = "gpt-5.2"
    DASHSCOPE_MODEL_CANDIDATES: str = "qwen-plus,qwen3-max,qwen-max,qwen-turbo,qwen-vl-max,qwen-vl-plus"
    OPENAI_MODEL_CANDIDATES: str = "gpt-5.2,gpt-5,gpt-5-mini,gpt-4.1,gpt-4o"
    # 按任务拆分模型（为空时回退到 DASHSCOPE_MODEL_NAME / OPENAI_MODEL_NAME）
    SM_LLM_MODEL_ANSWER: Optional[str] = None
    SM_LLM_MODEL_AUX: Optional[str] = None
    SM_LLM_MODEL_GRAPH: Optional[str] = None
    SM_LLM_MODEL_SUMMARY: Optional[str] = None
    SM_LLM_REQUEST_TIMEOUT_SECS: int = 60

    # 组件选择
    SM_EMBEDDER_TYPE: Literal["local", "dashscope"] = "dashscope"
    SM_RERANKER_TYPE: Literal["local", "dashscope"] = "dashscope"  # local=本地独立服务（HTTP调用），dashscope=云端API
    SM_RERANKER_ENDPOINT: Optional[str] = None  # 本地精排服务 HTTP 端点（如 http://reranker:8002），SM_RERANKER_TYPE="local" 时使用
    SM_DASHSCOPE_RERANK_MODEL: str = "qwen3-rerank"                              # DashScope 精排模型名
    SM_RERANKER_FAIL_MAX: int = 3                                                # 精排失败阈值（熔断）
    SM_RERANKER_COOLDOWN_SECS: int = 120                                         # 精排熔断冷却时间（秒）
    SM_RERANKER_FALLBACK_TO_DASHSCOPE: bool = True                               # 本地 reranker 失败时是否兜底到 DashScope
    SM_ASK_TIMEOUT_SECS: int = 120                                               # 问答全链路超时（秒），<=0 表示不限制
    # Ask SSE 回放（run_id + seq）配置
    SM_ASK_REPLAY_REDIS_ENABLED: bool = True                                      # 是否启用 Redis 持久化回放
    SM_ASK_REPLAY_TTL_SECS: int = 600                                             # 回放缓存保留时长（秒）
    SM_ASK_REPLAY_MAX_RUNS: int = 256                                             # 内存最多保留 run 数
    SM_ASK_REPLAY_MAX_EVENTS_PER_RUN: int = 4096                                  # 单 run 事件上限
    SM_ASK_REPLAY_REDIS_SCAN_LIMIT: int = 3000                                    # Admin 统计扫描上限
    SM_LLM_TYPE: Literal["local", "dashscope", "openai"] = "openai"

    # RAG 策略与特性开关（T2.2）
    SM_RETRIEVAL_STRATEGY: Literal["multi_stage"] = "multi_stage"  # 检索策略
    SM_RERANKER_STRATEGY: Literal["none", "supervised", "rl"] = "none"       # 重排策略
    SM_ENABLE_CITATIONS: bool = True                                             # 是否返回引用
    SM_STREAMING_ENABLED: bool = True                                            # SSE 流式开关
    SM_DEFAULT_LANGUAGE: Literal["zh", "en"] = "zh"                           # 默认语言
    SM_AUTO_TRANSLATE_TO_EN: bool = True                                          # 中文查询是否自动翻译为英文以提升检索命中
    SM_FAST_MODE_AUTO_TRANSLATE: bool = False                                    # 快速模式是否启用自动翻译
    SM_FAST_MODE_MQ_NUM: int = 1                                                  # 快速模式 Multi-Query 数
    SM_FAST_MODE_HYDE_ENABLED: bool = False                                       # 快速模式是否启用 HyDE
    SM_FAST_MODE_RERANK_ENABLED: bool = False                                     # 快速模式是否启用精排
    SM_FAST_MODE_MAX_VARIANTS: int = 1                                            # 快速模式最多保留多少 query 变体
    SM_FAST_MODE_RECALL_SOURCES: str = "bm25,vector"                            # 快速模式参与召回的通道
    SM_FAST_MODE_RECALL_MULTIPLIER: int = 1                                       # 快速模式候选放大倍数
    SM_FAST_MODE_CHANNEL_TOPK: int = 12                                           # 快速模式每路召回上限
    SM_INDEX_EXISTS_CACHE_TTL: int = 60                                           # 索引存在性缓存（秒）
    SM_DEFAULT_RAG_PROVIDER: str = "multi_stage"                                  # 默认 RAG Provider
    SM_RAG_PROVIDER_ALLOWLIST: str = "multi_stage,graph,multimodal_graph"         # 可用 Provider 列表

    # Knowledge Graph 设置
    SM_GRAPH_ENABLED: bool = True
    SM_GRAPH_ENABLE_LLM: bool = True
    SM_GRAPH_MAX_CHUNKS_PER_DOC: int = 40
    SM_GRAPH_MIN_CHARS: int = 200
    SM_GRAPH_MAX_ENTITIES_PER_CHUNK: int = 8
    SM_GRAPH_MAX_RELATIONS_PER_CHUNK: int = 10
    SM_GRAPH_QUERY_MAX_ENTITIES: int = 6
    SM_GRAPH_LLM_MAX_TOKENS: int = 512
    SM_GRAPH_QUERY_MAX_TOKENS: int = 256
    SM_GRAPH_TEXT_TRUNCATE_CHARS: int = 1800
    SM_GRAPH_MAX_BOOST_CHUNKS: int = 30
    SM_GRAPH_CHUNK_BOOST_WEIGHT: float = 0.35
    SM_GRAPH_QUERY_EXPANSION_ENABLED: bool = True
    SM_GRAPH_QUERY_MAX_VARIANTS: int = 6
    SM_GRAPH_ENTITY_VARIANT_FALLBACK: bool = True

    # Multimodal boost (only effective for multimodal providers)
    SM_MULTIMODAL_TABLE_BOOST: float = 0.25
    SM_MULTIMODAL_EQUATION_BOOST: float = 0.3
    SM_MULTIMODAL_FIGURE_BOOST: float = 0.2
    SM_MULTIMODAL_LOGICAL_PRIORITY: str = "abstract:introduction:method:results:conclusion:related_work"
    SM_MULTIMODAL_LOGICAL_BOOST: float = 0.2
    SM_MULTIMODAL_REFERENCE_BOOST: float = 0.15

    # Retrieval evaluation
    SM_RETRIEVAL_EVAL_FILE: str = "conf/retrieval_eval_sets.json"
    SM_RETRIEVAL_EVAL_MAX_ITEMS: int = 50
    SM_MULTI_QUERY_NUM: int = 5                                                  # Multi-Query 子查询数（含 original）
    SM_MULTI_QUERY_MAX: int = 6                                                  # Multi-Query 上限（含 original）
    SM_HYDE_ENABLED: bool = True                                                 # 是否启用 HyDE
    SM_HYDE_MAX_TOKENS: int = 256                                                # HyDE 生成内容长度上限
    SM_HYDE_TEMPERATURE: float = 0.2                                             # HyDE 采样温度
    SM_HYDE_WORD_LIMIT: int = 90                                                 # HyDE 最多输出多少词
    SM_HYDE_FALLBACK_ENABLED: bool = True                                        # HyDE 失败时是否启用模板兜底
    # SM_RECALL_SOURCES: str = "bm25,vector,colbert" # 参与召回的通道集合 临时关闭 colbert
    SM_RECALL_SOURCES: str = "bm25,vector"                                      # 参与召回的通道集合（临时关闭 colbert）
    SM_BM25_FIELDS: str = "text^1.0,title^4.0,abstract^2.5,keywords^3.0,figure_caption^2.0"
    SM_BM25_TOPK: int = 30                                                       # BM25 单路召回候选数
    SM_VECTOR_TOPK: int = 30                                                     # 向量单路召回候选数
    SM_COLBERT_ENABLED: bool = False                                             # 是否启用 ColBERT 晚交互召回
    SM_COLBERT_ENDPOINT: Optional[str] = None                                    # ColBERT 服务地址
    SM_COLBERT_TOPK: int = 20                                                    # ColBERT 单路召回候选数
    SM_RRF_K: int = 60                                                           # RRF 融合平滑系数 K
    SM_RECALL_CANDIDATE_MULTIPLIER: int = 3                                      # RRF 前的候选放大量
    SM_MMR_ENABLED: bool = True                                                  # 是否启用 MMR 多样性过滤
    SM_MMR_LAMBDA: float = 0.65                                                  # MMR 权衡参数 λ
    SM_MMR_MAX_CANDIDATES: int = 60                                              # 参与 MMR 的最大候选数
    SM_METADATA_L1_ENABLED: bool = True                                          # 是否启用元数据预排
    SM_METADATA_WEIGHT_RECENCY: float = 0.05                                     # 年份加权系数
    SM_METADATA_WEIGHT_CITATIONS: float = 0.03                                   # 引用数加权系数
    SM_METADATA_SECTION_BONUS: float = 0.3                                       # 重点章节加成
    SM_METADATA_SECTION_PRIORITY: str = "abstract:introduction:methodology:results:discussion:conclusion"
    SM_L2_RERANK_TOPK: int = 20                                                  # 进入 Cross-Encoder 的候选数量
    SM_L3_RL_ENABLED: bool = False                                               # 是否启用 RL 重排阶段
    SM_RL_EVENT_BUFFER: str = "storage/rl_events.jsonl"                         # RL 反馈事件落盘路径
    SM_RETRIEVAL_MIN_TEXT_CHARS: int = 20                                        # 检索阶段最小文本长度过滤（避免单词级块）
    # 索引增强开关（默认开启，便于灰度）
    SM_SEMANTIC_CHUNKING_ENABLED: bool = True                                    # 语义感知分块
    SM_MULTIMODAL_PARSE_ENABLED: bool = True                                     # 多模态（表格/图表Caption）抽取
    
    # Chunking 参数（学术 RAG SOTA 配置 - 2024 最佳实践：800-2000 tokens）
    SM_CHUNK_TARGET_CHARS: int = 3000  # 目标大小：~750 tokens（推荐甜蜜区）
    SM_CHUNK_MIN_CHARS: int = 1800     # 最小大小：~450 tokens，避免碎片化
    SM_CHUNK_MAX_CHARS: int = 5600     # 最大大小：~1400 tokens（控制上限）
    SM_CHUNK_OVERLAP: int = 250        # 重叠：~60 tokens
    SM_SEMANTIC_SIMILARITY_THRESHOLD: float = 0.65  # 稍收紧，避免过度合并
    # 富版面解析器的混合分块：短 layout block 保留，长正文块内部再做句级语义切分。
    SM_LAYOUT_AWARE_CHUNKING_ENABLED: bool = True
    SM_LAYOUT_SEMANTIC_SPLIT_ENABLED: bool = True
    SM_LAYOUT_SEMANTIC_MIN_CHARS: int = 1800
    # Chunk 质量过滤（参考 LlamaIndex / RAGFlow 学术 RAG 标准做法）：
    # references / footer / 纯 URL / 信息密度极低的块在入库前过滤掉，
    # 避免污染检索结果与右侧引文面板。任何被过滤的 chunk 都会在日志统计。
    SM_CHUNK_QUALITY_FILTER_ENABLED: bool = True
    # 逻辑类型黑名单：这些 logical_type / element_type 的块永远不进 chunk 索引
    SM_CHUNK_DROP_LOGICAL_TYPES: str = (
        "references,reference,reference_entry,bibliography,"
        "header,footer,page_number,page_header,page_footer,"
        "author_bio,acknowledgement,acknowledgements,acknowledgments"
    )
    SM_CHUNK_MIN_INFORMATION_CHARS: int = 30   # 短于此长度的块直接丢弃
    SM_CHUNK_MIN_UNIQUE_CHARS: int = 20        # 去重后字符数 < 此值视为重复噪声
    SM_CHUNK_DROP_PURE_URL: bool = True        # 丢弃整体只是 URL 的块
    SM_CHUNK_DROP_ISOLATED_HEADING: bool = True  # 丢弃孤立的短标题块（结构信息已记入下游 chunk 的 structure_title）
    # URL + 占位符占整块字符的比例上限（> 则整块视为参考文献/占位，无信息量）
    SM_CHUNK_URL_CHAR_RATIO_MAX: float = 0.65
    # 剔除 URL / Md 前缀后，至少应有的「实质词」数（图/表/公式块在此之前已放行）
    SM_CHUNK_MIN_SUBSTANTIVE_WORDS: int = 5
    SM_CHUNK_SUBSTANTIVE_CHECK_MAX_CHARS: int = 8000
    # 嵌入前二次过滤（去重文档标题复述块等），避免 layout chunker 之后才暴露的边角料
    SM_CHUNK_POST_FILTER_ENABLED: bool = True
    # 预合并/块级合并阈值（可灰度调参）
    SM_PREMERGE_MAX_CHARS: int = 10000                # MinerU 预合并单块最大字符数（默认 10k）
    SM_BLOCK_LEVEL_ALLOW_CROSS_PAGE: bool = False     # 是否允许块级跨页合并（默认不允许，保障可溯源）
    SM_BLOCK_LEVEL_MAX_CHARS: int = 5600              # 与总体上限一致
    SM_BLOCK_LEVEL_LEN_MERGE_BELOW: int = 4200        # ~1050 tokens，长度优先阈值
    SM_ENABLE_MULTIMODAL_CHUNKS: bool = True          # 是否将图/表等多模态块写入索引
    # 解析回退控制
    SM_FORCE_PYMUPDF_FALLBACK: bool = False                                      # 强制对 PDF 启用 PyMuPDF 兜底/补强
    # 解析器编排顺序（逗号分隔，按顺序尝试）。
    # 默认优先远程/轻量解析，避免低成本演示环境依赖本地 GPU/Java 重服务。
    # 可选项：llamaparse, unstructured_api, mineru, unstructured, pymupdf
    # 注意：scholarmind_api 镜像已剔除本地 unstructured 包（瘦身），默认顺序不再包含 "unstructured"。
    # 如需走本地 unstructured 解析，请同时把 unstructured[pdf,docx] 加回 requirements.txt 并重建镜像。
    SM_PARSER_ORDER: str = "llamaparse,unstructured_api,pymupdf"

    # 远程解析 API 配置。未配置 key 时对应 parser 会自动跳过，并降级到后续解析器。
    SM_REMOTE_PARSER_STRICT_FAIL: bool = True
    SM_REMOTE_PARSER_REQUIRE_PAGE: bool = True
    SM_REMOTE_PARSER_REQUIRE_BBOX: bool = True
    # Tolerance for partial metadata loss in remote parsers. Real-world PDFs
    # virtually always produce a handful of fragments (page headers/footers,
    # form-feed glyphs, footnote markers) that LlamaParse / Unstructured emit
    # without bbox or page anchors. all-or-nothing rejection on these would
    # discard 99.x% perfectly indexable content. Defaults are conservative:
    # bbox is harder to extract than page numbers, hence the higher tolerance.
    SM_REMOTE_PARSER_MAX_MISSING_BBOX_RATIO: float = 0.05
    SM_REMOTE_PARSER_MAX_MISSING_PAGE_RATIO: float = 0.02
    SM_LLAMA_PARSE_API_KEY: Optional[str] = None
    SM_LLAMA_PARSE_BASE_URL: str = "https://api.cloud.llamaindex.ai"
    SM_LLAMA_PARSE_TIMEOUT_SECS: int = 120
    SM_LLAMA_PARSE_POLL_INTERVAL_SECS: float = 2.0
    SM_LLAMA_PARSE_MAX_POLL_ATTEMPTS: int = 60
    SM_UNSTRUCTURED_API_KEY: Optional[str] = None
    SM_UNSTRUCTURED_API_URL: str = "https://api.unstructuredapp.io/general/v0/general"
    SM_UNSTRUCTURED_STRATEGY: str = "hi_res"
    SM_UNSTRUCTURED_HI_RES_MODEL_NAME: Optional[str] = None
    SM_UNSTRUCTURED_TIMEOUT_SECS: int = 180

    # MinerU 集成配置
    SM_MINERU_MODE: Literal["auto", "http", "cli"] = "auto"                    # 自动优先 HTTP，其次 CLI
    SM_MINERU_ENDPOINT: Optional[str] = None                                      # HTTP 服务地址，例如 http://mineru:8000
    SM_MINERU_HTTP_ROUTE: str = "/parse"                                         # 解析路由路径
    SM_MINERU_HTTP_FILE_FIELD: str = "file"                                      # 上传字段名
    SM_MINERU_TIMEOUT_SECS: int = 1200                                            # HTTP/CLI 超时（20分钟，应对复杂PDF）
    SM_MINERU_MAX_PAGES: int = 30                                                 # 读取页数的上限（兜底路径）
    SM_MINERU_STRICT_FAIL: bool = False                                           # MinerU 失败是否阻断流程（False=允许降级）
    # CLI 模式下的命令模板（MinerU 的官方命令是 mineru）
    SM_MINERU_CLI_BIN: str = "mineru"
    SM_MINERU_CLI_CMD: str = "{bin} -p \"{input}\" -o \"{output}\""

    # Grobid 集成配置。低成本默认关闭；heavy-local profile 可显式开启。
    SM_GROBID_ENDPOINT: Optional[str] = None                                      # Grobid 服务地址
    SM_GROBID_TIMEOUT_SECS: int = 60                                              # Grobid 请求超时
    SM_GROBID_ENABLED: bool = False                                               # 是否启用 Grobid 元数据增强

    # RAG 超参数
    SM_RAG_TOPK_MIN: int = 4                                           # RAG 最少 chunk 数
    SM_RAG_TOPK_MAX: int = 8                                           # RAG 最多 chunk 数
    SM_RAG_TOPK: int = 6                                               # 默认 chunk 数
    SM_RETRIEVE_PAGE_SIZE: int = 5
    # Embedding 配置（远程 embedding 可无缝切换）
    SM_EMBEDDING_MODEL: str = "text-embedding-v3"
    SM_EMBEDDING_DIMENSIONS: int = 1024
    SM_EMBEDDING_ENCODING_FORMAT: str = "float"
    SM_EMBEDDING_MAX_BATCH_SIZE: int = 10
    SM_MAX_TOKENS: int = 3072  # LLM 生成上限（平衡长回答质量与延迟/成本）
    SM_TEMPERATURE: float = 0.3
    
    # 公式块上下文扩展（检索时自动附带前后文本块）
    SM_EQUATION_CONTEXT_EXPANSION: bool = True  # 是否启用公式块上下文扩展
    SM_EQUATION_EXPANSION_PREV: int = 1         # 向前扩展的块数
    SM_EQUATION_EXPANSION_NEXT: int = 1         # 向后扩展的块数
    SM_EQUATION_STANDALONE: bool = True         # 公式是否独立成块（用于检索/上下文扩展）
    # history context controls
    SM_HISTORY_MAX_TURNS: int = 8  # 兼容旧逻辑（优先使用 token 预算）
    SM_HISTORY_MAX_TOKENS: int = 24000  # 历史对话预算（避免超大上下文导致高延迟）
    SM_HISTORY_HEADROOM: int = 12000  # 预留检索上下文/系统提示/答案空间
    HISTORY_RECENT_TURNS: int = 4
    ENABLE_ROLLING_SUMMARY: bool = True
    SM_CONTEXT_PACK_MAX_TOKENS: int = 4096  # 内部上下文包最大 token 数
    SM_CONTEXT_PACK_MAX_CHARS: int = 6000  # 内部上下文包最大字符数
    # DeepResearch result context injection policy (result-only, no process trace)
    SM_DEEP_RESEARCH_CONTEXT_ENABLED: bool = True
    SM_DEEP_RESEARCH_CONTEXT_MAX_RUNS: int = 2
    SM_DEEP_RESEARCH_CONTEXT_MAX_CHARS: int = 1200
    SM_DEEP_RESEARCH_CONTEXT_TIMEOUT_SECS: int = 4

    # 短期记忆（STM）配置
    SM_STM_SCAN_MESSAGES: int = 40
    SM_STM_MAX_SELECTED: int = 6
    SM_STM_SCORE_DECAY_LAMBDA: float = 0.1
    SM_STM_SCORE_SUMMARY_THRESHOLD: float = 0.4
    SM_STM_SCORE_FULL_THRESHOLD: float = 0.6
    SM_STM_LONG_MSG_THRESHOLD: int = 200

    # 长期记忆（LTM）配置
    SM_LTM_MAX_CANDIDATES: int = 32
    SM_LTM_MAX_DOCIDS: int = 2
    SM_LTM_SCORE_DECAY_LAMBDA: float = 0.01
    SM_LTM_SEMANTIC_WEIGHT: float = 0.75
    SM_LTM_TIME_WEIGHT: float = 0.25
    SM_STM_EMBED_MISSING_ON_READ: bool = False                                   # STM 读取时是否为历史消息补 embedding（默认关闭，避免同步阻塞）
    SM_LTM_EMBED_MISSING_ON_READ: bool = False                                   # LTM 读取时是否为记忆补 embedding（默认关闭，避免同步阻塞）

    # 记忆引导检索增强
    SM_MEMORY_DOC_BOOST: float = 0.3
    SM_MEMORY_GUIDE_MAX_FAILS: int = 5

    # 本地模型路径与设备
    LOCAL_EMBEDDER_PATH: str = "/models/bge-large-zh-v1.5"
    LOCAL_LLM_PATH: str = "/models/Qwen1.5-14B-Chat"
    SM_LOCAL_EMBEDDER_DEVICE: str = "cpu"
    SM_LOCAL_EMBEDDER_BATCH_SIZE: int = 32
    # 注意：LOCAL_RERANKER_PATH 已删除，精排服务现在是独立微服务，模型路径在 reranker 服务中配置

    # 其他
    RAGFLOW_BASE_URL: Optional[str] = None
    RAG_PROJECT_BASE: Optional[str] = None
    RAG_DEPLOY_BASE: Optional[str] = None
    LOG_LEVEL: str = "INFO"

    # OCR 引擎（公式识别）
    SM_OCR_ENABLED: bool = False
    SM_OCR_ENGINE: Literal["deepseek", "paddleocr"] = "deepseek"
    SM_OCR_ENDPOINT_DEEPSEEK: Optional[str] = None  # 例如 http://ocr:9000/latex
    SM_OCR_ENDPOINT_PADDLE: Optional[str] = None    # 例如 http://paddleocr-vl:9000/latex
    SM_OCR_TIMEOUT_SECS: int = 60
    SM_OCR_TRIGGER_CONF_LT: float = 0.8             # MinerU 置信度低于该值触发

    # 图像理解引擎（图表语义摘要）
    SM_VISION_ENABLED: bool = True
    SM_VISION_TYPE: Literal["dashscope", "http"] = "dashscope"  # dashscope=DashScope API 直调, http=外部 HTTP 服务
    SM_VISION_MODEL: str = "qwen-vl-max"                        # DashScope 多模态模型名
    SM_VISION_ENGINE: Literal["qwen2-vl"] = "qwen2-vl"         # HTTP 模式引擎（兼容旧配置）
    SM_VISION_ENDPOINT: Optional[str] = None                    # HTTP 模式端点
    SM_VISION_TIMEOUT_SECS: int = 60
    SM_VISION_MAX_TOKENS: int = 256
    SM_VISION_MAX_PER_2PAGES: int = 1               # 每2页最多处理的图表数

    # 公式描述增强（摄入时用 LLM 为 LaTeX 生成自然语言描述，改善嵌入质量）
    SM_EQUATION_DESCRIPTION_ENABLED: bool = True

    # 自适应检索决策（意图分类：自动判断是否需要检索及检索策略）
    SM_ADAPTIVE_RETRIEVAL_ENABLED: bool = True

    # 通用上下文窗口扩展（检索时对所有命中块附带前后邻居块）
    SM_CONTEXT_WINDOW_EXPANSION_ENABLED: bool = True
    SM_CONTEXT_WINDOW_EXPANSION_PREV: int = 1       # 向前扩展块数
    SM_CONTEXT_WINDOW_EXPANSION_NEXT: int = 1       # 向后扩展块数

    # 相关性网关（精排后低分过滤，防止不相关检索结果导致幻觉）
    SM_RELEVANCE_GATE_ENABLED: bool = True
    SM_RELEVANCE_GATE_THRESHOLD: float = 0.3        # top-1 chunk 分数低于此值则不注入上下文

    # 分层检索（文档级摘要索引 → chunk 级精检）
    SM_HIERARCHICAL_INDEX_ENABLED: bool = True       # 摄入时是否写入文档级摘要记录
    SM_HIERARCHICAL_RETRIEVAL_ENABLED: bool = True   # 检索时是否先做文档级预检

    # 逐块上下文压缩（精排后用 LLM 提取与查询相关的核心句子）
    SM_CONTEXT_COMPRESSION_ENABLED: bool = True
    SM_COMPRESSION_MAX_CHUNKS: int = 8               # 最多压缩前 N 个块
    SM_COMPRESSION_MODEL: Optional[str] = None       # 压缩用模型（为空时回退到 SM_LLM_MODEL_AUX）

    # Deepdoc/XGBoost 模型定制（用于修复旧二进制模型不兼容问题）
    DEEPDOC_XGB_MODEL_PATH: Optional[str] = "/app/service/core/rag/res/deepdoc/updown_concat_xgb.json"  # 指向 updown_concat_xgb.json/ubj 的绝对路径
    DEEPDOC_XGB_REMOTE_REPO: Optional[str] = None  # 自定义HF仓库名（优先使用 JSON/UBJ 版本）

    # Quotas (生产级配额，根据实际需求调整)
    DAILY_UPLOAD_MB: int = 2000  # 每用户每日上传配额（MB）- 生产环境建议 2GB
    DAILY_ASK_COUNT: int = 1000  # 每用户每日提问配额 - 生产环境建议 1000 次
    MAX_CONCURRENT_UPLOADS: int = 5  # 每用户最大并发上传数
    
    # Upload limits
    MAX_UPLOAD_SIZE_MB: int = 200  # 单文件最大 200MB，支持大型文档（技术报告、书籍章节等）
    ALLOWED_FILE_EXTENSIONS: list = [".pdf", ".txt", ".md", ".docx"]  # 允许的文件类型
    
    # Rate limiting (生产环境必须启用)
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 60  # 每分钟最多 60 次请求
    RATE_LIMIT_PER_HOUR: int = 1000  # 每小时最多 1000 次请求
    # 问答类接口单独限流（分钟级）
    SM_ASK_RATE_LIMIT_PER_MINUTE: int = 60
    SM_CRITICALQ_RATE_LIMIT_PER_MINUTE: int = 30

    class Config:
        env_file_encoding = "utf-8"
        env_file = ".env"


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
