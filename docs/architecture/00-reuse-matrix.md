# ScholarMind → ScriptLens 模块复用矩阵

## 0. 工程路线

ScriptLens 后端基于 ScholarMind 既有模块物理复用，前端基于 ScholarMind 既有 Vite + React 18 工程物理复用。

### 0.1 关键架构决策：单服务 + agent_runtime 子包

ScholarMind 是 4 服务架构（API:8000 / deep_research:8004 / doc_studio:8003 / reranker:8002），每个独立服务都有强 SRP 理由（长任务 / 跨产品面共享 / GPU / 独立扩缩容）。

ScriptLens 逐项审视后，**这些拆分理由全部不成立**：

| ScholarMind 拆分理由 | ScriptLens 是否成立 |
|---|---|
| 长任务（>5 min） | ❌ 5 维报告 30s-3min，BackgroundTask 已够；chat ReAct 一轮 5-15s |
| 跨产品面共享 | ❌ 只有 1 个产品面 = script_studio |
| GPU 资源 | ❌ 全 API 调（DashScope / OpenAI），无本地推理 |
| 独立扩缩容 | ❌ 单部署、单租户 demo |
| HitL 工作区文件锁 | ❌ 没有 LaTeX 工作区，短剧改写 patch 走 DB 不走 fs |

→ **ScriptLens 是单服务架构**：1 个 FastAPI 容器 `scriptlens_api`（dev/prod 一致），ReAct Agent 作为子包 `app/agent_runtime/` 嵌入主进程，**chat / rewrite 端点 in-process 调用 Agent，不走 HTTP / 不做 SSE 反向代理**。

未来扩展性保留：`app/agent_runtime/` 是独立子包（自包含 core/config + service + tools + prompts），未来真要拆出 `scriptlens_agent` 微服务，只需把目录搬出 + 加 main.py + 改 import 前缀，5 分钟内完成。

### 0.2 新增代码量

- 短剧专用 segmenter（识别第N集 / 场号 / 角色对白）
- 短剧报告 schema + 5 个评分维度的 prompt
- 4 个剧本专属 ReAct 工具（评分 / 定位 / 角色 / 改写），与既有的 `web_search_tool` + `reply_to_user_tool` 共 6 个工具组成 ScriptLens Agent **实际工具栈**（详见 §5.2）
- `view_rt`：返回 `ReportPayload` 全字段（不接受 `?role=`），视角由前端「行动」segment 派生 Persona Action Card（[`09-action-lens.md`](09-action-lens.md)）
- 用户反馈机制（`feedback_rt` + `feedback_service` + `script_feedback` 表，下次 chat 注入）
- 前端的「场景树 + 证据高亮 + 反馈按钮」交互
- 评估脚本（`eval/run_eval.py`：证据召回率 + 维度分一致性）

不新增容器、不新增数据库、不新增第三方服务（Tavily 仅用于 web_search 加分项，可选）。

## 1. 部署形态

### 1.1 ScholarMind prod 真实组件 → ScriptLens 复用决策

来源：`ScholarMind/backend/docker-compose.prod.yml`。

| 容器 | ScholarMind 实跑 | ScriptLens 复用 |
|---|---|---|
| `scholarmind_api` (FastAPI 8000) | ✅ | ✅ 拷部分模块为 `scriptlens_api`（端口 8005） |
| `scholarmind_db` (pgvector PG15) | ✅ | ✅ prod 共享同实例，独立 schema `scriptlens`；dev 独立 `scriptlens_db_dev` |
| `scholarmind_redis` | ✅ | ✅ prod 共享同实例，独立 db `REDIS_DB=1`；dev 独立 `scriptlens_redis_dev` |
| `doc_studio` (FastAPI 8003) | ✅ | ❌ **不起独立容器**，代码作为 `app/agent_runtime/` 子包嵌入 `scriptlens_api` 进程（理由见 §0.1） |
| `deep_research` (FastAPI 8004) | ✅ | ❌ 短剧场景不需要多轮闭环 Agent |
| `reranker` (FastAPI 8002) | ✅ | ❌ §3 决策已砍 cross-encoder rerank |
| `cloudflared` | ✅ | ✅ 同 Tunnel + 新 hostname `api-scriptlens.wh5233.me` 指向 `scriptlens_api:8005` |

### 1.2 ScriptLens 自身容器拓扑

| 部署 | 容器数 | 容器清单 |
|---|---|---|
| dev（`docker-compose.dev.yml`） | 3 | `scriptlens_db_dev` / `scriptlens_redis_dev` / `scriptlens_api_dev` |
| prod（`docker-compose.scriptlens.yml`，叠在 ScholarMind compose 上） | 1（其余复用 ScholarMind 既有） | `scriptlens_api` |

**架构本质**：ScriptLens 是单 FastAPI 进程应用，进程内组合「主 API 路由 + RAG + 报告流水线 + ReAct Agent 子包」。ReAct 子包语义边界清晰（`app/agent_runtime/`），未来可拆。

## 2. 主 API 路由层复用决策

ScholarMind `app_main.py` 注册的 10 个 router：

| Router | 用途 | 复用 | 改造点 |
|---|---|---|---|
| `user_rt` | 注册 / 登录 / demo-entry | ✅ 全拷 | 共用 `users` 表 |
| `knowledgebase_rt` | 论文知识库 | ✅ 拷，改名 `script_library` | 删 `kb_type` 区分；删 KG 关联 |
| `document_rt` | 论文上传 / 列表 / 删除 | ✅ 拷，改名 `script_rt` | 删在线导入（Semantic Scholar / arXiv），只本地上传 |
| `job_rt` | 异步解析任务进度 | ✅ 全拷 | 不动 |
| `session_rt` | 对话会话 | ✅ 拷，只保留 `surface=script` | 删 `surface=doc_studio`、删跨会话 LTM |
| `internal_rt` | 微服务间调用 | ⚠️ 全拷但 MVP 不挂载 | 单服务架构下无跨进程调用；代码保留以备未来拆服务 |
| `history_rt` | 消息历史 | ✅ 拷 | 不动 |
| `config_rt` | 前端读配置 | ✅ 拷 | 删与短剧无关的 flag |
| `view_rt`（**自建**） | 报告视图 + Persona Action Card 数据源 | ✅ 新建 | `GET /api/scripts/{id}/view`（无 `?role=` 参数），返回 `ReportPayload` 全字段；视角由前端「行动」segment 派生（[`09-action-lens.md`](09-action-lens.md)） |
| `feedback_rt`（**自建**） | 用户反馈记录 | ✅ 新建 | `POST /api/scripts/{id}/feedback` → 写 `script_feedback` 表（按 scope 标记），下次 chat 注入 prompt |
| `admin_rt` / `gateway_rt` / `debug_rt` | 管理 / 网关 / 调试 | ❌ MVP 不要 | — |

## 3. 主 API 服务层复用决策

| 模块 | 用途 | 复用 | 改造点 |
|---|---|---|---|
| `service/auth.py` | JWT + Internal Service Token | ✅ 全拷 | 单服务架构下 `INTERNAL_SERVICE_WHITELIST` 实际不再被跨进程调用，但代码保留以备未来拆服务 |
| `service/core/ingestion/parser_orchestrator.py` | 解析编排（llamaparse / unstructured / pymupdf 三路） | ✅ 拷 | 删 MinerU/Grobid 路径；新加 `python-docx` + `docx2txt` 处理短剧 docx |
| `service/core/ingestion/structured_doc_builder.py` | 拼接版面+语义结构 | ⚠️ 重写 | 改为「大纲段 + 场景段 + 对白行」三类块 |
| `service/core/ingestion/chunker.py` | 分块 | ✅ 拷骨架 | 改 chunk 策略：以「场景」为天然 chunk，1 场 = 1 chunk |
| `service/core/ingestion/embedder.py` | DashScope embedding | ✅ 全拷 | 不动 |
| `service/core/implementations/vector_stores/pgvector.py` | pgvector 写入/查询 | ✅ 全拷 | 表名 `script_chunks`，与 `rag_chunks` 隔离 |
| `service/core/conversation/chat_ask_orchestrator.py` | 六层 RAG 编排 + SSE | ✅ 拷骨架 | 砍 query variants / HyDE / metadata boost / cross-encoder rerank；只保留 embedding+BM25 → RRF → top-k |
| `service/core/rag/llm/client.py` | LLM 客户端 | ✅ 全拷 | 不动 |
| `service/core/rag/history/long_term_memory.py` | 跨会话长期记忆 | ❌ MVP 不要 | — |
| `service/job_handler/local_upload_handler.py` | 本地上传 → 解析 → 分块 → embedding | ✅ 全拷 | 替换 chunker 为短剧 chunker |
| `service/job_handler/online_ingestion_handler.py` | arXiv / Semantic Scholar 在线导入 | ❌ MVP 不要 | — |
| `service/document_lifecycle.py` | 文档状态机 | ✅ 全拷 | 不动 |
| `service/memory_service.py` / `arxiv_service.py` / `semantic_scholar_service.py` | 记忆 / 学术检索 | ❌ MVP 不要 | — |
| `service/feedback_service.py`（**自建**） | 用户反馈写入 + 注入 chat prompt | ✅ 新建 | 写 `script_feedback` 表；`chat_ask_orchestrator` 拉取该 script 最近 N 条反馈拼到 system prompt |

## 4. ReAct Agent 子包复用决策（`app/agent_runtime/`）

物理路径：ScholarMind `services/doc_studio/` → ScriptLens `backend/app/agent_runtime/`。

复用形态：作为 `scriptlens_api` 进程内的 Python 子包，**不起独立 FastAPI 进程**。删除独立微服务专用的启动入口与依赖（main.py / dependencies.py），保留 ReAct 框架壳与工具栈。chat / rewrite 端点 in-process 调用 `agent_runtime.service.agent_service`，无 HTTP / 无 SSE 反代。

import 改造：所有原本 `from core.config import settings` / `from metrics import ...` / `from utils.trace import ...` / `from workspace_cache import ...` / `from schemas.common import ...` / `from service.X import ...` 改为相对导入（`from .core.config` / `from .metrics` / `from .utils.trace` 等）。子包内自包含一份独立 `core/config.py`（pydantic_settings 从同一 `.env` 读，字段名与主 API settings 不冲突）。

| 子模块 | 复用 | 备注 |
|---|---|---|
| `service/agent_service.py` | ✅ 全拷 | ReAct 主循环 + 多模态 + 预算守卫 + 失败计数 |
| `service/async_run_manager.py` | ✅ 全拷 | HitL Future 协议 + SSE 重放 + 快照 |
| `service/base_agent.py` / `tool_registry.py` | ✅ 全拷 | — |
| `service/plan_builder.py` / `intent_classifier.py` | ⚠️ 可选裁剪 | MVP 单 agent 模式可省 ask/agent 分类；单轮 reactive 可省 plan-then-execute。**留接口位，先空跑** |
| `service/llm_client.py` | ✅ 全拷 | — |
| `service/rag_api_client.py` | ⚠️ 改造 | 原本通过 HTTP 调主 API 拿 RAG 结果；现单进程，**重写为直接 import `app.service.script_rag.retrieve_scenes`**，砍掉 httpx 调用 |
| `service/diff_generator.py` / `error_handler.py` | ✅ 全拷 | — |
| `service/reward_calculator.py` / `training_data_collector.py` | ❌ 删 | task 没要求 RL 训练 / reward model；与 P3 轻量 skill 反馈无关 |
| `service/web_search_client.py` | ✅ 全拷 | 短剧 Agent 必备：选品查市场 / 编剧查爆款 / 审核查法规 / 改写借参考 |
| `service/tools/base_tool.py` | ✅ 全拷并注册到工具栈 | 工具基类 + ToolRegistry，必备 |
| `service/tools/response_tools.py`（`ReplyToUserTool`） | ✅ 全拷并注册到工具栈 | ReAct 终止工具，必备 |
| `service/tools/workspace_utils.py` | ⚠️ 保留代码不删但 MVP 不被工具栈使用 | 短剧无工作区文件树；代码留着以备未来需要 |
| `service/tools/file_ops_tools.py`（mkdir / 创建 / 删除 / 重命名 / 列表） | ⚠️ 保留代码不删但 MVP **不注册到工具栈** | 全是工作区文件 CRUD，短剧 demo 不写 fs，注册了反而误导 LLM |
| `service/tools/analysis_tools.py`（工作区文件内 grep / semantic search / 读区间） | ⚠️ 保留代码不删但 MVP **不注册到工具栈** | 针对工作区文件的内部搜索；ScriptLens RAG 走 `script_rag` 接 `locate_scenes_tool`，跟这个语义不同 |
| `service/tools/retrieval_tools.py`（`SearchPapersTool` / `BatchSearchPapersTool`） | ⚠️ 改造完留代码不注册 | `rag_api_client.retrieve` 已 in-process 化，工具技术上能跑通；但语义被 §5 `locate_scenes_tool` 取代，避免重复 |
| `service/tools/editing_tools.py` 中 `RewriteSelectionTool` / `RewriteLineRangeTool` / `InsertTextTool` | ⚠️ 保留代码不删但 MVP 不注册 | 这三类绑定 doc_studio 工作区文件树（fs lock + diff patch）；ScriptLens 改写走 DB 不写 fs，由 §5 `propose_rewrite_tool` 接管。代码留着以备未来真要做工作区写盘 |
| `service/tools/editing_tools.py` 中 `InsertCitationTool` / `UpdateBibliographyTool` | ❌ 删 | 学术专用 |
| `service/tools/validation_tools.py`（LaTeX 编译 / CJK ctex / bibtex） | ❌ 全删 | — |
| `service/tools/web_search_tool.py` | ✅ 全拷 | 详见 §5.1 短剧场景调用边界（对应 prompts/zh.yaml 注入） |
| `latex_utils.py` | ❌ 删 | — |
| `router/agent_rt.py` | ❌ 不要 | 原是独立微服务的 8003 路由；单服务架构下，chat / rewrite 端点直接写在 `app/router/`（`chat_rt.py` / `rewrite_rt.py`），in-process 调 `agent_runtime.service.agent_service` |
| `router/training_rt.py` | ❌ 删 | 与 reward_calculator 配套，不要 |
| `main.py` | ❌ 删 | 独立 FastAPI 启动入口，子包不需要 |
| `dependencies.py` | ❌ 删 | 独立微服务的 `X-User-Id` header 鉴权依赖，子包内直接接收主 API 的 `current_user` |
| `prompts/doc_studio/zh.yaml` | ⚠️ 重写 | 替换为短剧场景 prompt（含 §5.1 web_search 调用触发条件） |
| `core/config.py` | ✅ 拷 | 删 RL_ / RAG_SERVICE_URL 字段；保留 LLM_ / AGENT_ / WEB_SEARCH_ / SEMANTIC_SEARCH_ |
| `metrics.py` / `security.py` / `workspace_cache.py` | ✅ 全拷 | — |
| `schemas/common.py` | ✅ 全拷 | — |
| `tests/*` | ❌ 不拷 | ScholarMind 断言绑定 LaTeX 工作区，全部要改；MVP 阶段优先用 5 份真实剧本人工验收覆盖，迭代期再补单测 |

## 5. 4 个剧本专属工具

物理位置：`app/agent_runtime/service/tools/script_tools.py`（单文件 4 个类），统一继承 doc_studio `BaseTool` → 自动获得预算守卫、连续失败计数、`reply_to_user` 兜底。

| 工具 | 输入 | 输出 | 预算 | 实现要点 |
|---|---|---|---|---|
| `score_dimension_tool` | `dimension ∈ {opening_hook, reward_density, motivation, pacing, risk}` | `{score, level, reason, evidence_ref_ids[]}` | 5 | 薄包装：直接调 `app.service.script_report_service.score_one_dimension(script_id, dim)`（5 维评分流水线按 dim 路由到 `dimension_scorer` / `motivation_chain` / `risk_screener`）；评分契约保证 `evidence_ref_ids` 非空 |
| `locate_scenes_tool` | `query`（如「前 5 集钩子」「打脸场景」） | `[{scene_id, scene_label, episode_no, scene_no, text, score}]` | 6 | 直接调 `app.service.script_rag.retrieve_scenes`（embedding + BM25 → RRF → top-k）；script_id 从 agent_state 取 |
| `extract_characters_tool` | `script_id`（默认从 agent_state 取） | `[{name, role, first_appear_scene_id, scene_count, ...}]` | 1 | LLM 一次性抽取：拉前 N 集 / 全部 scenes 的 `characters[]` 字段聚合 + 首次出现场景 + 出现频次；segmenter 已抽好 `characters` 字段，工具只做去重统计 + LLM 标注 role |
| `propose_rewrite_tool` | `scene_id, target_dimension, issue` | `{original, rewritten, diff, rationale}` | 3 | 拉对应 scene 的 `text` + 当前维度 reason → LLM 改写 → unified diff；不依赖 `editing_tools` 的工作区文件锁（短剧改写 patch 走 DB 不写 fs，见 architecture rule） |

### 5.2 ScriptLens Agent 实际工具栈（注册到 ToolRegistry）

| 工具 | 来源 | 类 | 默认预算 | 用途 |
|---|---|---|---|---|
| `score_dimension_tool` | §5 新写 | `script_tools.ScoreDimensionTool` | 5 | 复跑/复核某一维度评分；薄包 `script_report_service.score_one_dimension` |
| `locate_scenes_tool` | §5 新写 | `script_tools.LocateScenesTool` | 6 | 自然语言查询定位场景；薄包 `script_rag.retrieve_scenes` |
| `extract_characters_tool` | §5 新写 | `script_tools.ExtractCharactersTool` | 1 | 全剧人物清单（首次出现场景 + 出现频次） |
| `propose_rewrite_tool` | §5 新写 | `script_tools.ProposeRewriteTool` | 3 | 单步 LLM 改写指定场景，输出 diff |
| `web_search_tool` | doc_studio 全拷 | `web_search_tool.WebSearchTool` | 3 | 联网检索（剧本之外查询），见 §5.1 |
| `reply_to_user_tool` | doc_studio 全拷 | `response_tools.ReplyToUserTool` | — | ReAct 终止工具，必备 |

共 6 个工具。**不注册** doc_studio 时代的工作区类工具（file_ops / analysis / editing 工作区改写 / SearchPapers），代码保留以备未来扩展，但不出现在 LLM 工具列表中。

### 5.1 web_search_tool 短剧场景调用边界

为什么保留：task.md §六明确"真正可工作的 Agent"。短剧分析的盲区不在剧本本身，而在剧本**之外**——市场、法规、演员、同类爆款。这些靠剧本 RAG 解不了，必须联网。

| 用户角色 | 应该联网的 query 模式 | 不应该联网的 query（走剧本 RAG） |
|---|---|---|
| 选品 | "<剧名/主演> 抖音/快手 同类型 数据"、"近 3 个月 X 题材 爆款 完播率" | "本剧主角是谁"、"前 3 集钩子在哪" |
| 编剧 | "2025-2026 反转剧 桥段 案例"、"虐恋甜宠 平台 流量 倾向" | "本剧人物动机是否成立" |
| 审核 | "广电 2026 短剧 审核 细则"、"<具体红线词> 平台 处理 案例" | "本剧第 12 场风险打分" |
| 改写 | "甜宠 钩子 经典 开场 模式"、"<同题材爆款剧名> 反转 设计" | "把第 5 场改写更紧凑" |

预算与降级：
- `max_iterations` 内单次会话 `web_search_tool` 调用上限 = 3（防 budget 击穿）
- `max_results` 默认 5（剧本场景不需要更多）
- 缺 `WEB_SEARCH_API_KEY` 时 `is_configured() = False`，工具返回 `{skipped: true, reason: "missing_api_key"}`，**Agent 必须在终答里声明"网络信息暂不可用，本结论仅基于剧本本身"**（写进 prompts/zh.yaml）
- 联网结果使用规范：返回结论时 **必须列出来源 URL**，不允许只引用 snippet 内容当作既成事实

何时强制必走 web_search（写进 prompt 触发条件）：
1. 用户问题里含「市场 / 数据 / 抖音 / 快手 / 平台 / 排播 / 同类 / 对比」等关键词
2. 用户问题里含「最新 / 最近 / 近期 / 当前 / 现在」等时效词
3. 用户问题里含「广电 / 审核 / 合规 / 法规 / 政策」+ 不在已知风险词典内的具体词

## 6. 数据库 Schema

独立 schema `scriptlens`，最小 6 张表：

```sql
CREATE SCHEMA scriptlens;

CREATE TABLE scriptlens.scripts (
  id UUID PRIMARY KEY,
  user_id INT REFERENCES public.users(id),
  title TEXT NOT NULL,
  source_format TEXT,         -- docx | pdf | txt | md（老 doc 不支持）
  raw_storage_path TEXT,      -- /opt/data/scriptlens/storage/...
  total_episodes INT,
  total_scenes INT,
  total_chars INT,
  status TEXT,                -- pending | parsing | indexing | ready | failed
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE scriptlens.scenes (
  id UUID PRIMARY KEY,
  script_id UUID REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
  episode_no INT,             -- 「第 5 集」
  scene_no TEXT,              -- 「5-3」
  scene_label TEXT,           -- 「沈宅 夜 内」
  characters TEXT[],
  start_line INT,
  end_line INT,
  text TEXT
);

CREATE TABLE scriptlens.script_chunks (
  id UUID PRIMARY KEY,
  scene_id UUID REFERENCES scriptlens.scenes(id) ON DELETE CASCADE,
  embedding VECTOR(1024),     -- DashScope text-embedding-v3
  text TEXT
);
CREATE INDEX ON scriptlens.script_chunks USING ivfflat (embedding vector_cosine_ops);

CREATE TABLE scriptlens.reports (
  id UUID PRIMARY KEY,
  script_id UUID UNIQUE REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
  report_json JSONB,          -- 整份报告，schema 见 01-requirements.md §7
  generated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE scriptlens.evidence_refs (
  id UUID PRIMARY KEY,
  report_id UUID REFERENCES scriptlens.reports(id) ON DELETE CASCADE,
  scene_id UUID REFERENCES scriptlens.scenes(id),
  quote TEXT,
  reason TEXT,
  confidence TEXT
);

CREATE TABLE scriptlens.script_feedback (
  id UUID PRIMARY KEY,
  script_id UUID REFERENCES scriptlens.scripts(id) ON DELETE CASCADE,
  user_id INT REFERENCES public.users(id),
  message TEXT NOT NULL,
  scope TEXT,                 -- general | dimension | rewrite | scene
  scope_ref TEXT,             -- 维度名 / scene_id / rewrite_id
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ON scriptlens.script_feedback (script_id, created_at DESC);
```

会话 / 消息复用 ScholarMind 的 `sessions` / `messages` 表，加 `surface='script'` 区分。

## 7. 前端复用决策

| 模块 | 复用 | 改造 |
|---|---|---|
| `pages/login` / `pages/demo-entry` | ✅ 全拷 | — |
| `pages/repository` | ✅ 拷为 `pages/script-library` | 删在线导入 tab |
| `pages/repository/components/upload.tsx` | ✅ 全拷 | — |
| `pages/doc-studio/index.tsx` | ✅ 全拷为 `pages/script-studio` | 左 LaTeX 编辑器 → txt 视图 + 场景树高亮 |
| `pages/doc-studio/AgentDiffReview.tsx` | ✅ 全拷 | — |
| `pages/chat/component/citations.tsx` | ✅ 全拷为「证据高亮」 | — |
| `pages/chat/component/chat-message.tsx` / `source.tsx` | ✅ 全拷 | — |
| `pages/chat/component/notebook-drawer.tsx` / `deep-research-card.tsx` / `deep-research-process-panel.tsx` | ❌ 删 | — |
| `pages/admin/*` / `pages/idea-generation` | ❌ 删 | — |
| `pages/index/index.tsx`（侧边栏壳） | ✅ 拷 | 菜单仅留：剧本库 / Script Studio / 历史会话 |

技术栈：Vite + React 18 + Ant Design + valtio + SCSS Modules，与 ScholarMind 共用。

## 8. 部署

| 项 | 值 |
|---|---|
| 宿主机 | `portfolio-mvp-shanghai-01`（既有 2C2G） |
| 代码目录 | `/opt/apps/scriptlens` |
| 数据目录 | `/opt/data/scriptlens/storage` |
| 备份目录 | `/opt/backups/scriptlens` |
| PostgreSQL | 复用 `scholarmind_db`，独立 schema `scriptlens` |
| Redis | 复用 `scholarmind_redis`，独立 db `REDIS_DB=1` |
| Cloudflare Tunnel | 复用 `cf_tunnel_scholarmind`，加 hostname `api-scriptlens.wh5233.me` 指向 `http://scriptlens_api:8005` |
| 前端域名 | `scriptlens.wh5233.me`（Vercel） |
| Demo 入口 | `https://scriptlens.wh5233.me/demo` → 公共 `testuser` |

## 9. 实施清单（按特性优先级，不绑定时间）

> 优先级语义：**P0 = MVP 必备**（缺它则核心闭环不通），**P1 = 体验完整性**（缺它产品仍可演示但有明显缺口），**P2 = 加分项**（迭代期再做不影响验收）。已完成项标 ✅，进行中项标 ▶。

### P0-A 解析与存储 ✅

- [x] 物理拷贝 ScholarMind 主 API 模块到 `backend/app/`，按 §2-§3 删剪
- [x] 物理拷贝 ScholarMind `services/doc_studio/` 到 `backend/services/script_studio/`（后续按 §0.1 决策再迁移到 `backend/app/agent_runtime/`）
- [x] 写 6 张表 alembic migration（schema `scriptlens`，含 `script_feedback`）
- [x] 写短剧专用 segmenter（识别 `第N集` / `X-Y 场号` / `角色：对白` / `角色 os：内心`，5 份真实 docx + 3 份 pdf 已验证）
- [x] 跑通：上传 docx → 解析 → 分场景 → embedding → pgvector（dry-run E2E 已验证）

### P0-B 评分流水线 ✅

5 维报告流水线（确定性而非 ReAct，原因见架构备注）+ 7 个评分内部工具 + `script_report_service` + `POST /api/scripts/{id}/reanalyze`。

### P0-C RAG + Agent 子包 + 业务端点 ▶

- [x] 简化 RAG `app/service/script_rag.py`（embedding + BM25 → RRF → top-k）
- [x] `services/script_studio/` 整体迁移到 `backend/app/agent_runtime/` 子包（详见 §0.1）
  - 删 `main.py` / `dependencies.py` / `router/agent_rt.py` / `router/training_rt.py` / `service/reward_calculator.py` / `service/training_data_collector.py` / `service/tools/validation_tools.py` / `latex_utils.py`
  - 改 `service/rag_api_client.py` 与 `service/tools/retrieval_tools.py`：去掉 httpx，直接 import `app.service.script_rag.retrieve_scenes`
  - 删 `editing_tools.py` 中 `InsertCitationTool` / `UpdateBibliographyTool`
  - 批量改 import 为相对导入（`from core.X` → `from .core.X` 等）
  - 删 `core/config.py` 中 `RL_*` / `RAG_SERVICE_URL` 字段
- [x] §5 剧本专属 ReAct 工具（继承 BaseTool），与 `web_search_tool` / `analysis_tools` / 改写工具一起注册到 ToolRegistry
- [x] 编写 `agent_runtime/prompts/zh.yaml` 短剧场景 prompt（含 §5.1 web_search 触发条件）
- [x] `POST /api/scripts/{id}/chat` SSE，in-process 调 `agent_runtime.service.agent_service`，复用 `sessions` / `messages` 表，`surface='script'`
- [x] `POST /api/scripts/{id}/rewrite`，单步调 `propose_rewrite_tool`
- [x] `POST /api/scripts/{id}/feedback` + `feedback_service` + chat prompt 注入
- [x] `GET /api/scripts/{id}/view`（无 `?role=`，返回 ReportPayload 全字段，前端「行动」segment 派生 Persona Action Card）

### P0-D 前端 MVP ▶

- [x] 在 `frontend/` 物理拷贝 ScholarMind `frontend/`，按 §7 删剪
- [x] 替换 LaTeX 编辑器为 txt + 场景树视图，证据高亮接 `evidence_refs`
- [x] 反馈交互：报告 / 维度 / 改写 / 场景上挂「反馈」按钮 → `feedback_rt`
- [x] 部署到既有 ECS（独立 schema + Tunnel hostname）

### P1 改写 Agent 结构化重构

- [ ] **ScriptVFS 虚拟文件契约**：scene 投影成 `scenes/E{ep:02}-S{sc:03}.txt` 路径；session 入口一次性 `snapshot_all` → `original_file_contents`
- [ ] **改写工具三件套**（read_scene / propose_full_script_plan / rewrite_scene）替换单一 `propose_dimension_rewrite_tool`，让 ReAct 真正成立
- [ ] **Prompt 解耦 UI**：`zh.yaml` 只描述工具能力 + TASK_META 协议；前端 `handleAgentResponse` 按 data shape 自动渲染
- [ ] **统一 LLMRuntime**：合并 `agent_runtime/llm_client` 与 `script_tools/llm_caller` 的 candidate / blacklist 配置源；启动期对首位 candidate 发 1-token 探测，全败拒绝启动

### P2 评估、可观测性与扩展

- [ ] `eval/run_eval.py`：跑真实剧本人工标注 → 自动计算证据召回率 + 维度分一致性，结果写入 README
- [ ] 5 份真实剧本端到端验收，写 README + 录 demo
- [ ] 报告级 / 维度级反馈上下游全链路埋点 + Grafana 面板

## 10. 不做的事

- 不 fork ScholarMind 改名
- **不起独立 Agent 微服务**（不复刻 ScholarMind 的 `doc_studio:8003` 独立进程；ReAct 框架作为 `app/agent_runtime/` 子包嵌入主 API 进程，理由见 §0.1。未来若真需要拆，子包语义边界已就位）
- 不保留 ScriptLens 任何现存 `backend/app/{api,chat,core,evaluation,feedback,ingest,perspectives,reporting,rewrite,segmentation}/` 代码（全部删除重建）
- 不保留 ScriptLens 现有 `frontend/`（全部删除重建）
- 不做 DeepResearch / Notebook / Admin / IdeaGen / 学术检索 / 在线导入任何一个（ScholarMind 学术专用模块，与短剧无关）
- 不做完整 skill 调度库 / RL training pipeline / reward model（仅 PRD §10 P3 的轻量反馈注入）
- 不做 6 个用户角色全套视角（仅做 3 个：选品 / 编剧 / 审核）
- 不做 3 套时延预算分层（30s / 3min / 10min）—— MVP 默认走单档实时推理，分层等线上压测后再决定
- 不解析老 `.doc`（5% 边角案例，提示用户另存为 docx）
