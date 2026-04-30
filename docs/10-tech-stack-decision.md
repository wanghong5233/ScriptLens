# ScriptLens 技术选型决策

## 1. 决策结论

ScriptLens 不建议基于垂直开源剧本分析项目二次改造。推荐从零实现业务逻辑,但使用成熟全栈脚手架和通用库。

最终路线:

- 前端:Next.js + React + TypeScript。
- 后端:Python + FastAPI + Pydantic。
- 本地开发环境:Python `.venv` + npm。
- 部署复现环境:Docker + Docker Compose。
- 存储:SQLite 起步,必要时切 PostgreSQL。
- LLM:OpenAI-compatible provider 抽象,模型名和 key 走环境变量。
- 异步任务:MVP 先用 FastAPI BackgroundTasks 或进程内任务状态表,不引入 Celery。
- 检索:MVP 使用段落级索引 + 关键词检索 + 可选 embedding,不引入重型向量数据库作为硬依赖。
- 部署:前端 Vercel,后端 Render/Fly.io/Railway/云服务器任选其一。
- 评估:Python 脚本 + fixtures + golden labels + LLM-as-Judge。

## 2. 第一性原理

题目要求的是一个可工作的剧本解析 Agent,不是复刻某个已有产品。

核心难点不是 CRUD、登录、权限、支付或复杂基础设施,而是:

- 长文本结构化理解。
- 判断依据可追溯。
- 多视角报告。
- 多轮剧本问答。
- 低分项改写。
- 用户反馈形成 skill。
- 可评估、可展示、可部署。

因此技术选型应把时间留给 Agent 业务闭环,而不是花在适配陌生项目的历史结构上。

## 3. 开发环境决策

本项目采用:

```text
Local dev: Python .venv + npm
Deploy/reproduce: Docker + Docker Compose
```

不使用 conda。

原因:

- 项目主体是 Web Agent,不是依赖复杂二进制科学计算栈的数据科学项目。
- `.venv` 是 Python Web 后端最轻量、最标准的本地隔离方式。
- npm 是 Next.js 前端的标准包管理路径。
- Docker Compose 用于保证评估方和部署环境可复现,服务"可部署、可访问"加分项。
- D3 不采用 Docker-first,因为 Windows + Docker Desktop + 前端热更新会增加早期迭代不确定性。

环境边界:

- 本地写代码和调试优先走 `.venv` 和 npm。
- 每个可交付节点必须能解释 Docker 复现路径。
- Docker 不替代本地开发,但必须在部署前跑通。

## 4. 从零写 vs 改开源项目

### 3.1 从零写业务逻辑

优点:

- 完全贴合 `docs/source/task.md`。
- 架构和文档一致,不会被旧代码形态绑架。
- 更容易解释设计取舍。
- 更容易保证 evidence、schema、eval 这些核心约束。
- 代码规模可控,适合 10 天交付。

风险:

- 需要自己搭建基础项目骨架。
- 前后端、部署、评估都要自己串起来。

规避:

- 用成熟脚手架。
- 不写登录、权限、支付、多租户。
- 不做重型任务队列和复杂数据库。
- 每天保留可运行 demo。

### 3.2 改垂直开源项目

优点:

- 如果刚好有同领域项目,可能有 UI 或上传流程可复用。

风险:

- 领域逻辑很可能不匹配中文短剧/网文场景。
- 旧项目的 prompt、schema、数据结构会干扰当前需求。
- 改别人代码的理解成本不可控。
- 很容易为了适配旧系统牺牲 evidence、scorecard、skill、eval。
- 面试讲解时难说明哪些是你的设计。

结论:

- 不建议基于垂直开源剧本分析项目改。
- 可以借鉴竞品产品形态,不能依赖竞品工程结构。

### 3.3 使用通用开源脚手架

这是推荐方案。

可以使用:

- Next.js 官方模板。
- FastAPI 最小项目结构。
- shadcn/ui 或 Tailwind 组件。
- Pydantic schema。
- SQLite/SQLModel/SQLAlchemy。

原则:

- 复用通用工程脚手架。
- 不复用领域业务逻辑。
- 不引入需要大量理解成本的完整系统。

## 5. 推荐技术栈

### 4.1 Frontend

选择:

- Next.js。
- React。
- TypeScript。
- Tailwind CSS。
- shadcn/ui 可选。

原因:

- 最快做出可访问 demo。
- 适合左右分栏、卡片、tabs、聊天、证据高亮。
- Vercel 部署简单。
- TypeScript 能约束前端消费 schema。

前端重点:

- 上传页。
- 报告页。
- 左原文右分析。
- 证据高亮。
- 视角切换。
- scorecard。
- chat panel。
- rewrite panel。
- skill/feedback panel。

不做:

- 登录注册。
- 团队协作。
- 支付。
- 复杂动画。

### 4.2 Backend

选择:

- Python。
- FastAPI。
- Pydantic。

原因:

- Agent 和 LLM 调用生态成熟。
- Pydantic 适合结构化输出约束。
- FastAPI 适合快速提供 demo API。
- 与 eval 脚本共享模型和 pipeline。

后端重点:

- 数据模型。
- 上传/分段。
- LLM provider。
- segment extraction。
- report aggregation。
- evidence index。
- chat。
- rewrite。
- feedback skill。
- eval。

### 4.3 Storage

MVP 选择:

- SQLite。

原因:

- 零运维。
- 适合 demo。
- 能存 scripts、segments、reports、feedback、skills、eval runs。

切换条件:

- 需要多人并发。
- 线上 SQLite 文件权限或持久化不稳定。
- 部署平台不适合本地文件。

届时切:

- PostgreSQL。

### 4.4 LLM Provider

选择:

- OpenAI-compatible API abstraction。

原因:

- 国内外模型可替换。
- 环境变量切模型。
- 便于成本和质量对比。

配置:

- `LLM_BASE_URL`
- `LLM_API_KEY`
- `LLM_MODEL`
- `LLM_TIMEOUT_SECONDS`

原则:

- key 不进前端。
- prompt 版本化。
- schema 校验失败要重试或失败。

### 4.5 Retrieval

MVP 选择:

- 段落级 evidence refs。
- 关键词检索。
- 可选 embedding。

不默认引入:

- Milvus。
- Elasticsearch。
- 复杂向量数据库。

原因:

- 当前主要是单剧本内检索。
- 数据量小。
- 证据定位比大规模召回更重要。
- 重型检索基础设施会拖慢 10 天交付。

### 4.6 Async Jobs

MVP 选择:

- FastAPI BackgroundTasks 或进程内任务管理。
- 数据库记录任务状态。

不默认引入:

- Celery。
- Redis Queue。
- Kafka。

原因:

- demo 并发低。
- 分析任务数量可控。
- 复杂队列增加部署风险。

切换条件:

- 分析耗时明显超过 HTTP 可接受范围。
- 需要可靠重试和并发 worker。

## 6. 建议目录结构

```text
ScriptLens/
  backend/
    app/
      api/
      core/
      ingest/
      segmentation/
      extraction/
      reporting/
      evidence/
      chat/
      rewrite/
      feedback/
      skills/
      prompts/
    tests/
    pyproject.toml
    Dockerfile
  frontend/
    app/
    package.json
    package-lock.json
    Dockerfile
  eval/
    fixtures/
    golden/
    runs/
  samples/
  docs/
  tests/
```

说明:

- `backend` 放 FastAPI Agent 后端。
- `frontend` 放 Next.js 前端。
- `eval` 放评估数据和运行结果。
- `samples` 放样本剧本。
- `docs` 放设计文档。

## 7. D3 最小 build 目标

D3 不追求完整 Agent,只要跑通主链路:

1. 初始化后端和前端。
2. 上传或加载示例文本。
3. 后端保存 script。
4. 分段生成 segments。
5. 调用 LLM 生成基础 report。
6. 前端展示 report。

D3 结束必须能演示:

- 打开本地 Web。
- 加载 `samples/xiaoqie.txt`。
- 点击分析。
- 看到结构化报告。

## 8. 风险排序

从工程风险看:

1. 最大风险:改开源项目导致需求被旧结构绑架。
2. 次大风险:过早引入重型基础设施。
3. 第三风险:没有 schema,LLM 输出漂移。
4. 第四风险:证据定位后补,导致报告无法验证。
5. 第五风险:前端做成普通聊天壳。

对应策略:

- 从零写业务逻辑。
- 只用轻量基础设施。
- 先写 schema。
- D4 前完成 evidence refs。
- D6 前完成左右分栏报告页。

## 9. 最终判断

最快的方式不是"找一个相似开源项目改",而是:

- 用成熟脚手架快速搭工程。
- 按本文档从零写 ScriptLens 的领域逻辑。
- 严格用 schema、evidence、eval 控制质量。
- 每天保持可运行 demo。

这条路径对 10 天考核最稳,也最容易在面试中解释为自己的完整 Agent 项目。
