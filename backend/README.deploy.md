# ScriptLens Docker 部署手册

两阶段部署：**先本地 dev 跑通**，再上**云端 prod**。

| 阶段 | 文件 | 形态 | 数据库 | 端口 |
|---|---|---|---|---|
| 本地 dev | `docker-compose.dev.yml` | 自包含独立栈（自带 PG / Redis） | `scriptlens_db_dev` | 8005 / 25432 / 26379 |
| 云端 prod | `docker-compose.scriptlens.yml` | overlay 叠加到 ScholarMind compose | 复用 `scholarmind_db`（独立 schema） | 8005（仅容器内） |

---

## 1. 依赖装在哪（重要不变式）

| 层 | 依赖位置 | 不污染谁 |
|---|---|---|
| `scriptlens_api` 容器 | image 内 `/usr/local/lib/python3.11/site-packages` | 不污染宿主机 / 不污染 ScholarMind 容器 |
| 宿主机（开发机 / ECS） | 只装 docker / docker compose，**不装任何 Python 包** | — |
| eval / probe 离线脚本 | 在宿主机 conda（已装 pymupdf / python-docx），不连 DB 直接跑 | 与容器无关，纯本地脚本 |

**Docker layer cache**：ScholarMind 已经 build 过相同 base + apt + pip 大部分依赖；ScriptLens 第二次 build 时复用底层 layer，**实际只多产生 ~50MB 代码层**，不会真重装一份依赖。

---

## 2. 阶段 A：本地 dev 部署

### 2.1 前置

- Windows / Mac：Docker Desktop ≥ 24
- Linux：docker-ce + docker compose plugin
- 必备 key：
  - **`OPENAI_API_KEY`** — LLM 推理优先 GPT（评分 / 决策 / 改写 / 对话）
  - **`DASHSCOPE_API_KEY`** — embedding 走这里 + LLM 兜底
- 强烈建议 key（task.md §六 "真正可工作的 Agent" 加分项）：
  - **`WEB_SEARCH_API_KEY`** — Tavily 联网搜索，Free tier 1000 次/月。短剧选品 / 编剧查爆款 / 审核查法规场景必备；留空时 Agent 自动降级，但终答里只能基于剧本本身回答，扣加分项
- 端口要空：`8005` / `25432` / `26379`

### 2.2 第一次启动

```bash
cd ScriptLens/backend

# 复制 env 模板并填 DASHSCOPE_API_KEY
cp .env.dev.example .env.dev
# Windows PowerShell: notepad .env.dev
# Mac/Linux:          vim .env.dev

# 创建 dev 期剧本落盘目录
mkdir -p storage_root

# 起栈（首次会 build image，~3-5 分钟）
docker compose -f docker-compose.dev.yml up -d --build
```

启动后容器名 / 端口：

| 容器 | 容器内端口 | 宿主机端口 | 作用 |
|---|---|---|---|
| `scriptlens_db_dev` | 5432 | 25432 | PG + pgvector |
| `scriptlens_redis_dev` | 6379 | 26379 | Redis |
| `scriptlens_api_dev` | 8005 | 8005 | FastAPI（uvicorn --reload） |

### 2.3 验证

```bash
# 1. 容器健康
docker ps --filter name=scriptlens_

# 2. 6 张表自动建好（alembic 在 start.sh 里跑）
docker exec scriptlens_db_dev psql -U postgres -d scriptlens_dev \
  -c "SELECT tablename FROM pg_tables WHERE schemaname='scriptlens' ORDER BY tablename"
# 期望输出：evidence_refs / reports / scenes / script_chunks / script_feedback / scripts

# 3. pgvector 扩展已装
docker exec scriptlens_db_dev psql -U postgres -d scriptlens_dev \
  -c "SELECT extname, extversion FROM pg_extension WHERE extname='vector'"

# 4. 健康端点
curl http://localhost:8005/health

# 5. eval 脚本跑通（在宿主机本地 conda 环境跑，已装 pymupdf/python-docx）
cd ../  # 回到 ScriptLens 根
python eval/_validate_segmenter.py    # → eval/_validate_segmenter_out.txt
python eval/_e2e_dryrun.py            # 不连 DB 的结构验证
```

### 2.4 日常开发

| 操作 | 命令 |
|---|---|
| 改代码看日志 | `docker logs -f scriptlens_api_dev` —— Python 文件改了 uvicorn 自动 reload |
| 进容器调试 | `docker exec -it scriptlens_api_dev bash` |
| 重启 api | `docker restart scriptlens_api_dev` |
| 跑 alembic 新增 migration | `docker exec scriptlens_api_dev alembic revision -m "msg"` 然后 `alembic upgrade head` |
| 连 PG 看数据 | DBeaver / psql：`localhost:25432` user=postgres pwd=pg123456 db=scriptlens_dev |
| 连 Redis | `redis-cli -h localhost -p 26379` |
| 关停（保留数据） | `docker compose -f docker-compose.dev.yml stop` |
| 销毁含数据 | `docker compose -f docker-compose.dev.yml down -v` |

### 2.5 dev 收敛标准

满足下列才能进 prod：

- [ ] `curl localhost:8005/health` 返回 200
- [ ] alembic 6 张表全部存在
- [ ] eval 8 文件 segmenter 全部 PASS
- [ ] 上传一份真实 docx → 报告生成 → 报告里 `evidence_ref_ids` 能跳回原文场景
- [ ] 多轮 chat 走通一次

---

## 3. 阶段 B：云端 prod 部署（dev 通过后再做）

### 3.1 前置

- ECS 上 ScholarMind compose 已经在跑（`scholarmind_db` / `scholarmind_redis` / `cf_tunnel_scholarmind` 健康）
- 部署目录约定（兄弟目录）：

```text
/opt/apps/
├── scholarmind/backend/  (docker-compose.prod.yml + .env.production)
└── scriptlens/backend/   (docker-compose.scriptlens.yml + .env.scriptlens)
```

### 3.2 数据准备

```bash
sudo mkdir -p /opt/data/scriptlens/storage
sudo chown -R $USER:$USER /opt/data/scriptlens
sudo mkdir -p /opt/backups/scriptlens

cd /opt/apps/scriptlens/backend
cp .env.scriptlens.example .env.scriptlens
```

### 3.3 Cloudflare Zero Trust 加 hostname

控制台 → Zero Trust → Networks → Tunnels → 选 `cf_tunnel_scholarmind` → Public Hostnames → Add：

| 字段 | 值 |
|---|---|
| Subdomain | `api-scriptlens` |
| Domain | `wh5233.me` |
| Service | `http://scriptlens_api:8005` |

### 3.4 启动（关键：两个 `-f` 叠加 + `--project-name`）

```bash
cd /opt/apps

docker compose \
  -f scholarmind/backend/docker-compose.prod.yml \
  -f scriptlens/backend/docker-compose.scriptlens.yml \
  --project-name scholarmind \
  --env-file scholarmind/backend/.env.production \
  --env-file scriptlens/backend/.env.scriptlens \
  up -d --build scriptlens_api
```

### 3.5 验证

```bash
docker ps --filter name=scriptlens_api

docker exec scriptlens_api python -c "
from sqlalchemy import create_engine, text
import os
e = create_engine(os.environ['DATABASE_URL'])
with e.connect() as c:
    r = c.execute(text(\"SELECT tablename FROM pg_tables WHERE schemaname='scriptlens'\"))
    print(sorted(row.tablename for row in r))
"

curl -s http://localhost:8005/health                  # 内网
curl -s https://api-scriptlens.wh5233.me/health       # 公网（CF tunnel 配置后）
```

---

## 4. 不变式（违反即视为部署事故）

- 不在宿主机 / 系统 Python 安装 ScriptLens 后端依赖
- 不动 ScholarMind `docker-compose.prod.yml` 任何字节
- 不在 ScholarMind compose project 之外启动 `scriptlens_api`（prod 模式）
- 不在 dev compose 中暴露 prod 密钥（`.env.dev` 与 `.env.scriptlens` 分离）
- 不在 `rag_chunks` 公共表写剧本数据；剧本只走 `scriptlens.script_chunks`
- 不删除 ScholarMind 的 PG / Redis volume（会同时摧毁 ScriptLens prod 数据）

---

## 5. 故障速查

| 现象 | 排查 |
|---|---|
| `port 25432 already in use` | 改 `docker-compose.dev.yml` 里 `25432:5432` 为别的端口 |
| `scriptlens_api_dev` 反复重启 | `docker logs scriptlens_api_dev`；常见是 `.env.dev` 没填 OPENAI_API_KEY / DASHSCOPE_API_KEY / JWT_SECRET_KEY |
| 上传剧本时报 embedding 错 | 检查 `DASHSCOPE_API_KEY` 是否填了 |
| alembic `relation already exists` | 之前手动建过表；`docker compose -f docker-compose.dev.yml down -v` 重置 volume |
| dev 想清空所有剧本数据 | `docker exec scriptlens_db_dev psql -U postgres -d scriptlens_dev -c "TRUNCATE scriptlens.scripts CASCADE"` |
| 改 Python 代码不生效 | 检查 `docker logs scriptlens_api_dev` 是否有 reload 行；如果 Windows + Docker Desktop volume 性能差，可改 `--reload` 为重启容器 |
