# ScriptLens

面向短剧选品 / 编剧统筹 / 平台审核的爆款短剧分析 Agent。

输入一份长剧本（docx / pdf / txt / md，不支持老 .doc），输出：

- 决策卡（推荐 / 谨慎 / 不建议）+ 一句话理由 + 必读 3 场
- 5 维 scorecard（开场钩子 / 爽点密度 / 动机自洽 / 节奏控制 / 审核风险）
- 每条结论可点击跳转到原文场景，带证据高亮
- 多轮追问，回答必须引用原文（剧本之外的市场 / 法规 / 同类爆款问题，Agent 调 web search 联网检索并附源 URL）
- 低评级维度的定向改写建议

## 文档入口

- [`docs/source/task.md`](docs/source/task.md) — 题目原文，唯一最高需求源
- [`docs/01-requirements.md`](docs/01-requirements.md) — 产品需求 + Agent 输出契约 + 验收清单
- [`docs/02-script-evaluation-rubric.md`](docs/02-script-evaluation-rubric.md) — 5 维评分工业判据 / 档位锚点 / prompt 模板（基于抖音 / 快手 / 广电调研）
- [`docs/00-reuse-matrix.md`](docs/00-reuse-matrix.md) — ScholarMind → ScriptLens 模块复用矩阵 + 3 天执行清单
- [`backend/README.deploy.md`](backend/README.deploy.md) — Docker 部署手册（本地 dev → 云端 prod 两阶段）

## 评测数据

`eval/短剧剧本/爆款短剧剧本（完整本）/` 共 43 份真实爆款短剧（docx 23 / pdf 18 / doc 2）。

## 架构概览

**单服务架构**：1 个 FastAPI 进程（`scriptlens_api:8005`），进程内组合主 API 路由 + 简化 RAG（embedding+BM25+RRF）+ 5 维评分流水线 + ReAct Agent 子包（`app/agent_runtime/`，物理来源于 ScholarMind doc_studio）。chat / rewrite 端点 in-process 调用 Agent，不起独立微服务、不做 SSE 反代。LLM 优先 OpenAI（GPT 系），DashScope 兜底；Embedding 固定 DashScope `text-embedding-v3`。前端 Vite + React 18 + Ant Design + valtio。

dev 部署 3 容器（db/redis/api），prod 复用既有阿里云 ECS 上的 ScholarMind PostgreSQL / Redis / Cloudflare Tunnel（独立 schema `scriptlens`），仅新增 1 个 `scriptlens_api` 容器。

详见 [`docs/00-reuse-matrix.md §0.1`](docs/00-reuse-matrix.md) 「单服务 + agent_runtime 子包」决策。
