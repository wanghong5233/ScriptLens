# ScriptLens 🎬

> 面向短剧选品 / 编剧统筹 / 平台审核的爆款短剧分析 Agent。一份长剧本进，决策卡 + 五力评分 + 关键场跳转 + 全剧改写计划出。

---

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![React 18](https://img.shields.io/badge/React-18-61DAFB.svg)](https://react.dev/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-336791.svg)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg)](https://docs.docker.com/compose/)

ScriptLens 把「读懂一本短剧」拆成可量化、可溯源、可改写的 Agent 工作流。上传 docx / pdf / txt / md，30 秒内拿到决策卡，5 分钟内看完五力诊断，深度模式可让 Agent 按维度全剧改写并自动重评。所有结论都能跳回原文场景并行级高亮。

---

## 目录

- [架构总览](#架构总览)
- [核心能力](#核心能力)
  - [1. 4 段式诊断报告](#1-4-段式诊断报告)
  - [2. Plan-then-Execute 全剧改写 Agent](#2-plan-then-execute-全剧改写-agent)
  - [3. Quote-first 双锚定溯源](#3-quote-first-双锚定溯源)
  - [4. 阅文五力评分](#4-阅文五力评分)
- [端到端数据流](#端到端数据流)
- [快速开始](#快速开始)
- [技术栈](#技术栈)
- [架构亮点](#架构亮点)
- [文档导航](#文档导航)
- [开源协议](#开源协议)

---

## 架构总览

```mermaid
%%{init: {
  'theme': 'base',
  'themeVariables': {
    'background': '#ffffff',
    'primaryColor': '#f7f7f9',
    'primaryBorderColor': '#d8d8df',
    'primaryTextColor': '#444444',
    'lineColor': '#cfcfd6',
    'fontSize': '12px',
    'fontFamily': 'Segoe UI, Microsoft YaHei, Arial'
  },
  'flowchart': { 'curve': 'linear', 'nodeSpacing': 36, 'rankSpacing': 42, 'padding': 14 }
}}%%
flowchart TB
    subgraph Frontend["前端 doc-studio (React 18 + Vite + Ant Design + valtio)"]
        direction LR
        UI_Outline["左：大纲<br/>集 → 场"]
        UI_Editor["中：Monaco 编辑器<br/>行级证据高亮"]
        UI_Right["右：三 tab<br/>Agent / 报告 / 时间线"]
    end

    API["scriptlens_api :8005<br/>FastAPI 单服务进程"]

    subgraph Pipeline["进程内组合"]
        direction LR
        Ingest["ingestion<br/>docx/pdf/txt/md → scenes"]
        Report["4 chain 并行<br/>coverage / beat / character / evaluation"]
        Agent["agent_runtime<br/>(ScholarMind doc_studio 子包复用)"]
    end

    subgraph Infra["基础设施"]
        direction LR
        PG[("PostgreSQL 15<br/>schema scriptlens<br/>GIN tsvector + jieba")]
        Redis[("Redis<br/>SSE 重放")]
        LLM["LLM<br/>OpenAI 优先 / DashScope 兜底"]
    end

    UI_Outline --> API
    UI_Editor --> API
    UI_Right --> API

    API --> Ingest
    API --> Report
    API --> Agent

    Ingest --- PG
    Report --- PG
    Report --- LLM
    Agent --- PG
    Agent --- LLM
    Agent --- Redis

    classDef gateway fill:#ece9f6,stroke:#b9b3cc,stroke-width:1px,color:#4f4a60;
    classDef node fill:#f7f7f9,stroke:#d8d8df,stroke-width:1px,color:#444444;
    classDef infra fill:#f4f5f8,stroke:#cfd6e0,stroke-width:1px,color:#444444;

    class API gateway;
    class UI_Outline,UI_Editor,UI_Right,Ingest,Report,Agent node;
    class PG,Redis,LLM infra;

    style Frontend fill:transparent,stroke:#e3e3e8,stroke-width:1px,color:#666666,stroke-dasharray: 5 5;
    style Pipeline fill:transparent,stroke:#e3e3e8,stroke-width:1px,color:#666666,stroke-dasharray: 5 5;
    style Infra fill:transparent,stroke:#e3e3e8,stroke-width:1px,color:#666666,stroke-dasharray: 5 5;
    linkStyle default stroke:#cfcfd6,stroke-width:1px,fill:none;
```

| 组件 | 端口 | 职责 |
|---|---|---|
| `scriptlens_api` | 8005 | FastAPI 单进程：上传 / 解析 / 评分流水线 / Agent ReAct / RAG |
| `app/agent_runtime/` | — | 物理来源 ScholarMind doc_studio 的 ReAct Agent 子包，进程内调用，不起独立微服务 |
| PostgreSQL `scriptlens` schema | 5432 | `scripts` / `scenes` / `reports` / `evidence_refs` / `script_operations` / `script_feedback` |
| Redis | 6379 | Agent SSE 流断线重放（`Last-Event-ID`） |

数据库与 Redis 与 ScholarMind 共部署（独立 schema，互不污染）；ScriptLens 容器只新增一个 `scriptlens_api`。

---

## 核心能力

### 1. 4 段式诊断报告

报告 = **任务派发器**，不是孤立结果页。任意结论可点击跳回原文场景并行级高亮。

| segment | 时间预算 | 核心 widget | 数据来源 |
|---|---|---|---|
| **速览** | 30 秒决策 | logline ≤ 60 字 / 三档推荐 / 题材 / 必读 3 场 / 3 优 3 劣 | `coverage_chain` |
| **故事** | 5 分钟看主线 | 三幕骨架 / 关键节拍（开场 / 激励 / 中点 / 高潮 / 收束 / 反转 / 爽点）/ 节奏曲线 / 情感弧 | `beat_chain` + `pacing_aggregator`（无 LLM） |
| **人物** | 5 分钟看关系 | 共现网络力导向图 / 节点动机+目标+阻碍 / 关系类型与极性 | `character_graph_chain` |
| **评估** | 深度审阅 | 阅文五力分（详见 §4）/ 合规四档分级 / 风险清单 / 改写候选 | `evaluation_chain` |
| **行动** | 角色级闭环 | Persona Action Card（编剧 / 选品 / 审核三选一）+ Next Action | 派生层 `script_view_service` |

4 个 chain 走 `asyncio.gather` 并行，总耗时 ≈ 最慢一条 chain。

### 2. Plan-then-Execute 全剧改写 Agent

改写不是「让 Agent 改一段」，而是 **按维度、改全剧、用户审计划、用户审 hunk、自动重评**。

```mermaid
%%{init: {
  'theme': 'base',
  'themeVariables': {
    'background': '#ffffff',
    'primaryColor': '#f7f7f9',
    'primaryBorderColor': '#d8d8df',
    'primaryTextColor': '#444444',
    'lineColor': '#cfcfd6',
    'fontSize': '12px',
    'fontFamily': 'Segoe UI, Microsoft YaHei, Arial'
  },
  'flowchart': { 'curve': 'linear', 'nodeSpacing': 26, 'rankSpacing': 36, 'padding': 12 }
}}%%
flowchart TD
    A[用户在编剧卡点 A 模式 五维一键<br/>或 B 模式 单维度按钮] --> B[dispatchAgentTask<br/>fulltext_rewrite mode=plan]
    B --> C[propose_dimension_rewrite_tool<br/>选 dim_score < 7 的候选场]
    C --> D[LLM 出 RewritePlan<br/>dimensions / overall_summary / steps]
    D --> E[RewritePlanCard 渲染<br/>用户勾选 N 场]
    E --> F[onDispatchExecute<br/>fulltext_rewrite mode=execute]
    F --> G[逐场 LLM 改写<br/>UPDATE scriptlens.scenes.text<br/>mutate AgentState]
    G --> H[_generate_script_scene_diffs<br/>scene_id 当虚拟文件路径]
    H --> I[AgentDiffReview<br/>单文件多 hunk Cursor 风格]
    I --> J{用户操作}
    J -- Keep all hunks --> K[closeDiffModal contentByPath]
    J -- Reject --> L[PUT scenes/id/content 回写 original]
    K --> M[fe_rescore_hook 自动追发 rescore]
    M --> N[score_dimension_tool 逐维度重评]
    N --> O[Agent reply 旧分 → 新分 对比]

    classDef node fill:#f7f7f9,stroke:#d8d8df,stroke-width:1px,color:#444444;
    classDef action fill:#ece9f6,stroke:#b9b3cc,stroke-width:1px,color:#4f4a60;
    classDef decision fill:#f5f5f7,stroke:#d8d8df,stroke-width:1px,color:#444444;

    class A,B,C,D,E,F,G,H,I,K,L,M,N,O node;
    class J decision;
    linkStyle default stroke:#cfcfd6,stroke-width:1px,fill:none;
```

| 设计 | 实装 |
|---|---|
| **Plan-then-Execute** | review-then-execute 不允许 LLM 直接改场，对照 Cursor Composer / Copilot Workspace plan card |
| **Diff 透明迁移** | `scene_id` (UUID) 当虚拟文件路径，骗过现有 `_generate_file_diffs` 入口；`AgentDiffReview` 组件零改动复用 |
| **DB 即文件系统** | `state.modified_files` 收 scene_id，`_generate_script_scene_diffs` 从 PG 读 modified、从 AgentState 读 original |
| **自动重评** | `pendingRescoreRef` 钩在 keep 路径上；reject 不触发；时序由 `setTimeout(0)` 避开 setFileContent 同步竞争 |
| **Brief 后端化** | user message 只发一行意图 + `<TASK_META>{...}</TASK_META>`，800 字 brief 不污染 chat 流 |

详见 [`docs/10-rewrite-agent.md`](docs/10-rewrite-agent.md)。

### 3. Quote-first 双锚定溯源

报告里所有可点元素携带 **(scene_id, line_range, quote)** 三元组，前端 `traceEvidence` 用 quote 作 ground truth，line_range 作 fallback。

| 阶段 | 行为 |
|---|---|
| **打开目标 scene** | `openFile(scene_id)` → Monaco Editor key 重建 |
| **retry loop** | 校验 `model.getValue() === scene.text` 才认定 model 已切换，避开 stale model race |
| **quote 命中** | `model.getValue().indexOf(quote)` 算真实 line range |
| **line_range fallback** | quote 没命中（LLM 漂移）走后端 line_range |
| **持久高亮** | trace 类 `ttlMs=0` 持久；dispatch 类 `ttlMs=3000` 淡出 |

业内对照：W3C TextQuoteSelector / Hypothes.is / Notion block reference / Cursor `@file` references。

### 4. 阅文五力评分

| 维度 | 衡量 | 阈值锚点 |
|---|---|---|
| **故事力 story** | 主线清晰度 + 反转密度 + 关键节拍完整性 | 反转 / 集 ≥ 0.5（高）/ 0.33（合格）/ 0.12（保底） |
| **人物力 character** | 主角动机 + 弧光 + 关键关系冲突 | OOC = 0 占 80% (高) / OOC ≤ 2 占 60% (合格) |
| **题材力 concept** | 赛道辨识度 + 卖点钩子 + 商业可行性 | 主流赛道（重生 / 战神 / 豪门 / 甜宠 等）落到具象场景 |
| **情感力 emotion** | 钩子密度 + 爽点 + 情感弧 | ≥ 1 钩子 / 集，最长无爽点段 ≤ 2 集 |
| **叙事力 pacing** | 开场建立悬念 + 节奏方差 + 集尾留钩 | 开场 ≤ 3 场建立悬念，回报方差控制在合理区间 |

合规审核独立四档分级（`high_risk / medium_risk / low_risk / clean`），不进创作质量加权。

业内出处：阅文集团「五力模型」（中文网文 / 短剧改编工业最广为使用的体系）+ 抖音 / 快手 StreamLake 选品手册阈值。详见 [`docs/08-evaluation-framework.md`](docs/08-evaluation-framework.md)。

---

## 端到端数据流

```mermaid
%%{init: {
  'theme': 'base',
  'themeVariables': {
    'background': '#ffffff',
    'primaryColor': '#f7f7f9',
    'primaryBorderColor': '#d8d8df',
    'primaryTextColor': '#444444',
    'lineColor': '#cfcfd6',
    'fontSize': '12px',
    'fontFamily': 'Segoe UI, Microsoft YaHei, Arial'
  },
  'flowchart': { 'curve': 'linear', 'nodeSpacing': 28, 'rankSpacing': 38, 'padding': 12 }
}}%%
flowchart TD
    U[用户上传<br/>docx / pdf / txt / md] --> P1[POST /api/scripts/upload<br/>存 raw_storage_path<br/>enqueue ingest]
    P1 --> P2[load_script_paragraphs<br/>docx → python-docx<br/>pdf → pypdf+pdfplumber]
    P2 --> P3[segment_script<br/>四锚点：第N集 / X-Y / 场景头 / 角色对白]
    P3 --> P4[INSERT scripts + scenes<br/>schema scriptlens]
    P4 --> P5[generate_report<br/>4 chain asyncio.gather]
    P5 --> P6[INSERT reports + evidence_refs<br/>scene_id + line_range + quote 三锚]

    P6 --> R[doc-studio 右栏渲染<br/>速览 / 故事 / 人物 / 评估 / 行动]
    R --> T1[点证据 chip<br/>kind=evidence_lookup]
    R --> T2[点维度卡<br/>kind=dim_inquiry]
    R --> T3[点改写按钮<br/>kind=fulltext_rewrite mode=plan]

    T1 --> D[dispatchAgentTask]
    T2 --> D
    T3 --> D

    D --> E1[Monaco 行级高亮<br/>quote-first 双锚定]
    D --> E2[切右栏 Agent tab]
    D --> E3[composer 注入 prompt + TASK_META]

    E3 --> AG[Agent ReAct loop<br/>识别 TASK_META 跳过模糊定位]
    AG --> AT1[locate_scenes_tool<br/>BM25 + jieba 兜底]
    AG --> AT2[score_dimension_tool<br/>逐维度评分]
    AG --> AT3[propose_dimension_rewrite_tool<br/>plan / execute 两阶段]
    AG --> AT4[web_search_tool<br/>市场 / 法规 / 同类爆款]

    AT3 --> AD[AgentDiffReview<br/>单文件多 hunk]
    AD --> KP[Keep all hunks]
    AD --> RJ[Reject hunks]
    KP --> RS[fe_rescore_hook<br/>autoSubmit rescore]
    RS --> AT2

    classDef node fill:#f7f7f9,stroke:#d8d8df,stroke-width:1px,color:#444444;
    classDef agent fill:#ece9f6,stroke:#b9b3cc,stroke-width:1px,color:#4f4a60;
    classDef store fill:#f4f5f8,stroke:#cfd6e0,stroke-width:1px,color:#444444;

    class U,P1,P2,P3,R,T1,T2,T3,D,E1,E2,E3,KP,RJ node;
    class AG,AT1,AT2,AT3,AT4,AD,RS agent;
    class P4,P5,P6 store;

    linkStyle default stroke:#cfcfd6,stroke-width:1px,fill:none;
```

---

## 快速开始

**环境**：Docker Compose、4GB+ RAM、OpenAI 或 DashScope API Key（任一）

```bash
git clone https://github.com/<your-handle>/ScriptLens.git
cd ScriptLens/backend
cp .env.example .env
# 编辑 .env，至少配置：OPENAI_API_KEY 或 DASHSCOPE_API_KEY、JWT_SECRET_KEY、SCRIPTLENS_DB_URL
make up-build && make migrate
cd ../frontend && npm install && npm run dev
```

**访问**：前端 http://localhost:5173 | API 文档 http://localhost:8005/docs

**必填环境变量**：

| 变量 | 用途 | 备注 |
|---|---|---|
| `OPENAI_API_KEY` 或 `DASHSCOPE_API_KEY` | LLM 调用（评分 / Agent / 改写） | 优先 OpenAI，失败兜底 DashScope |
| `JWT_SECRET_KEY` | 用户鉴权 | `python -c "import secrets; print(secrets.token_hex(32))"` 生成 |
| `SCRIPTLENS_DB_URL` | PostgreSQL 连接串 | `postgresql://user:pass@localhost:5432/scholarmind_db` |
| `SCRIPTLENS_REDIS_URL` | Agent SSE 流重放 | 可选，默认 `redis://localhost:6379/0` |

测试集 `eval/短剧剧本/爆款短剧剧本（完整本）/` 共 43 份真实爆款短剧（docx 23 / pdf 18 / 老 doc 2，老 doc 不支持，提示用户另存为 docx）。

部署详见 [`backend/README.deploy.md`](backend/README.deploy.md)（dev 3 容器 / prod 复用 ScholarMind ECS）。

---

## 技术栈

| 层 | 技术 |
|---|---|
| **后端** | FastAPI、Python 3.11+、SQLAlchemy、asyncio |
| **数据库** | PostgreSQL 15（独立 schema `scriptlens`、GIN tsvector + jieba 兜底）、Redis 7 |
| **前端** | React 18、TypeScript、Vite、Ant Design、valtio、Monaco Editor、ECharts、react-force-graph-2d |
| **AI** | OpenAI（GPT 系，主力）、DashScope（Qwen 系，兜底）、jieba 分词；**无 embedding** 路径 |
| **解析** | python-docx、pypdf、pdfplumber |
| **运维** | Docker Compose（与 ScholarMind compose 共部署） |

---

## 架构亮点

| 亮点 | 说明 |
|---|---|
| **单服务 + 子包复用** | 1 个 FastAPI 进程组合 ingestion / report / RAG / Agent；`agent_runtime` 物理来源 ScholarMind doc_studio，进程内调用，零 IPC 损耗 |
| **Diff 机制透明迁移** | `scene_id` 当虚拟文件路径，PG 行当虚拟文件内容；`AgentDiffReview` / `_generate_file_diffs` / hunk 审阅链路全部复用，剧本场景零改动 |
| **(scene_id, line_range, quote) 三锚定** | LLM 输出 evidence 时同时给行号和 quote，前端 quote-first 命中、line_range fallback；对照 GitHub PR review hunk / NotebookLM citation |
| **TASK_META 协议** | 报告点击 → composer 注入 `<TASK_META>{...}</TASK_META>` → Agent 跳过模糊定位直调 tool；少一轮 ReAct，响应快且不会跳错场 |
| **Plan-then-Execute** | 全剧改写强制 plan → review → execute → rescore 四步；用户审核计划与 hunk，LLM 不直改 DB |
| **后端化 brief** | user message 只发一行意图，800 字 brief 完全在后端 system prompt 注入，chat 流不被污染 |
| **PG 全文索引 + jieba 兜底** | 已删 embedding 路径；`scenes.text` 上 GIN tsvector 一级、jieba 关键词二级、LLM metadata 三级兜底，覆盖中文长串 tokenizer 短板 |
| **4 chain 并行** | `coverage` / `beat` / `character_graph` / `evaluation` 走 `asyncio.gather`，总耗时 ≈ 最慢一条 |
| **per-(scene, dim) task status** | 改写状态从 `script_operations` 派生，零 schema 改动支持「已尝试 N 次 / 已采纳 / 上次拒绝」徽章 |
| **SSE 断线重放** | Agent 流 `Last-Event-ID` 协议无感续流，复用 ScholarMind `AskStreamReplayBuffer` |

---

## 文档导航

| 文档 | 作用 |
|---|---|
| [`docs/source/task.md`](docs/source/task.md) | 题目原文，最高准则 |
| [`docs/01-requirements.md`](docs/01-requirements.md) | PRD：契约 + Agent 输出 + 验收清单 |
| [`docs/03-system-mental-model.md`](docs/03-system-mental-model.md) | UI / Agent 协作心智模型 + AgentTask 协议 |
| [`docs/04-script-pipeline.md`](docs/04-script-pipeline.md) | 上传 → 评分 → 检索 → 派发 数据流水线 |
| [`docs/05-report-architecture.md`](docs/05-report-architecture.md) | 4 segment 结构契约 + 4 chain 并行 |
| [`docs/06-storage-architecture.md`](docs/06-storage-architecture.md) | PG 存储层 + 检索契约 |
| [`docs/08-evaluation-framework.md`](docs/08-evaluation-framework.md) | 阅文五力 rubric + 短剧场景化档位锚点 |
| [`docs/09-action-lens.md`](docs/09-action-lens.md) | 行动 segment + Persona Action Card |
| [`docs/10-rewrite-agent.md`](docs/10-rewrite-agent.md) | Plan-then-Execute 改写 + diff 透明迁移 + rescore 链路 |
| [`docs/00-reuse-matrix.md`](docs/00-reuse-matrix.md) | ScholarMind → ScriptLens 模块复用矩阵 |
| [`backend/README.deploy.md`](backend/README.deploy.md) | Docker 部署手册 |

---

## 开源协议

本项目采用 [MIT License](./LICENSE)。如对你有帮助，欢迎 ⭐ Star 支持。
