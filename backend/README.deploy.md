# ScriptLens Docker 部署手册

两阶段部署：**先本地 dev 跑通**，再上**云端 prod**。

| 阶段 | 文件 | 形态 | 数据库 | 端口 |
|---|---|---|---|---|
| 本地 dev | `docker-compose.dev.yml` | 自包含独立栈（自带 PG / Redis） | `scriptlens_db_dev` | 8005 / 25432 / 26379 |
| 云端 prod | `docker-compose.prod.yml` | 自包含独立栈（自带 PG / Redis / cloudflared） | `scriptlens_db` | 仅容器内（公网走 cloudflared） |

ScriptLens 与同主机其他项目（ScholarMind 等）**完全独立**：独立 docker network、独立 volume、独立 cloudflared tunnel、独立 Compose project name = `scriptlens`（已写入 compose 文件，避免因在 `backend/` 目录启动而显示为 `backend`）。两者之间不共享任何 service。

---

## 1. 依赖装在哪（重要不变式）

| 层 | 依赖位置 | 不污染谁 |
|---|---|---|
| `scriptlens_api` 容器 | image 内 `/usr/local/lib/python3.11/site-packages` | 不污染宿主机 / 不污染同主机其他项目 |
| 宿主机（开发机 / ECS） | 只装 docker / docker compose，**不装任何 Python 包** | — |
| eval / probe 离线脚本 | 在宿主机 conda（已装 pymupdf / python-docx），不连 DB 直接跑 | 与容器无关，纯本地脚本 |

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
# 期望输出：evidence_refs / reports / scenes / script_feedback / script_operations / scripts
# 注：v1 起 script_chunks 已删除（embedding 路径拆除，详见 docs/04-script-pipeline.md §4.4）

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

> dev compose 已固定 `name: scriptlens`，Docker Desktop / `docker compose ls` 中应显示为 `scriptlens`，不是启动目录名 `backend`。

### 2.5 dev 收敛标准

满足下列才能进 prod：

- [ ] `curl localhost:8005/health` 返回 200
- [ ] alembic 6 张表全部存在
- [ ] eval 8 文件 segmenter 全部 PASS
- [ ] 上传一份真实 docx → 报告生成 → 报告里 `evidence_ref_ids` 能跳回原文场景
- [ ] 多轮 chat 走通一次

---

## 3. 阶段 B：云端 prod 部署（dev 通过后再做）

ScriptLens 在 ECS 上是一个**自包含独立栈**：自己的 PG / Redis / cloudflared / network / volumes / project name。与同主机其他项目无任何 docker 资源共享。

### 3.1 前置

- 一台已初始化好的 ECS（Docker / Compose 可用、swap 已生效、Asia/Shanghai 时区）
- 一个 Cloudflare 账号（Zero Trust 已开通，已托管 `wh5233.me`）
- 部署目录：

```text
/opt/apps/scriptlens/backend/   ← compose 文件 + .env.production
/opt/data/scriptlens/storage/   ← 剧本原始文件（容器挂载）
/opt/backups/scriptlens/        ← PG dump 备份位置
```

### 3.2 ECS 数据目录

```bash
sudo mkdir -p /opt/apps /opt/data/scriptlens/storage /opt/backups/scriptlens
sudo chown -R $USER:$USER /opt/data/scriptlens /opt/backups/scriptlens

cd /opt/apps
git clone <你的 ScriptLens 仓库地址> scriptlens
cd scriptlens/backend
cp .env.production.example .env.production
vim .env.production
```

`.env.production` 必填项（对应 `<CHANGE_ME>`）：

| 字段 | 怎么填 |
|---|---|
| `POSTGRES_PASSWORD` | 随机长串：`openssl rand -hex 16` |
| `CF_TUNNEL_TOKEN` | 见 3.3，Cloudflare 控制台新建独立 tunnel 后复制 |
| `JWT_SECRET_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `OPENAI_API_KEY` / `DASHSCOPE_API_KEY` | 自有 key |
| `SM_LLAMA_PARSE_API_KEY` / `SM_UNSTRUCTURED_API_KEY` | 自有 key |
| `TAVILY_API_KEY` / `WEB_SEARCH_API_KEY` | 同一个 Tavily key；可留空（Agent 联网工具自动降级） |
| `SM_CORS_ALLOW_ORIGINS` | Vercel 真实域名，先填占位也能起来 |

### 3.3 Cloudflare Zero Trust 新建独立 tunnel

控制台 → Zero Trust → Networks → Tunnels → **Create a tunnel**：

1. Tunnel type: `Cloudflared`
2. Tunnel name: `scriptlens-prod`
3. 跳到 "Install and run a connector" 页面，**复制 docker run 命令里 `--token` 后面的长字符串**，粘到 `.env.production` 的 `CF_TUNNEL_TOKEN`
4. Public Hostnames → **Add a public hostname**：

| 字段 | 值 |
|---|---|
| Subdomain | `api-scriptlens` |
| Domain | `wh5233.me` |
| Service Type | `HTTP` |
| URL | `scriptlens_api:8005` |

DNS 那条 CNAME 由 Cloudflare 自动创建，保持 `Proxied`。

### 3.4 启动（前台 build → 后台 up，不用 timeout / nohup / `&`）

```bash
cd /opt/apps/scriptlens/backend

# 1) 前台 build（必须前台，方便观察阿里云镜像源拉包进度）
docker compose -f docker-compose.prod.yml --env-file .env.production -p scriptlens build scriptlens_api

# 2) 起栈
docker compose -f docker-compose.prod.yml --env-file .env.production -p scriptlens up -d

# 3) 看状态
docker compose -f docker-compose.prod.yml --env-file .env.production -p scriptlens ps
```

如果 SSH 经常掉线，把 build 步骤放进 `tmux`：

```bash
tmux new -s scriptlens
# 进入 tmux 后执行 build；断线后 tmux attach -t scriptlens 即可恢复
```

### 3.5 验证

```bash
# 容器健康
docker ps --filter name=scriptlens_

# scriptlens schema 6 张表自动 migrate（start.sh 跑 alembic upgrade head）
docker exec scriptlens_db psql -U postgres -d scriptlens \
  -c "SELECT tablename FROM pg_tables WHERE schemaname='scriptlens' ORDER BY tablename"

# 健康检查
docker exec scriptlens_api python -c "import urllib.request as u; print(u.urlopen('http://localhost:8005/health', timeout=5).read())"
curl -s https://api-scriptlens.wh5233.me/health         # 公网（CF tunnel 健康后）

# cloudflared 状态
docker logs --tail 50 cf_tunnel_scriptlens
```

### 3.6 Vercel 前端

- 复制 `frontend/.env.production.example` 到 Vercel Project → Environment Variables
- `VITE_API_BASE` 必须等于 `https://api-scriptlens.wh5233.me/api`
- 给 Vercel 绑定自定义域名 `scriptlens.wh5233.me`（在 Cloudflare DNS 把这条 CNAME 设为 **DNS only**，不要 Proxied，否则与 Vercel 双向 TLS 冲突）
- 部署后回到 ECS 把 `.env.production` 的 `SM_CORS_ALLOW_ORIGINS` 改成真实 origin，重启 api：

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production -p scriptlens up -d --force-recreate scriptlens_api
```

---

## 4. 不变式（违反即视为部署事故）

- 不在宿主机 / 系统 Python 安装 ScriptLens 后端依赖
- 不在 dev compose 暴露 prod 密钥（`.env.dev` 与 `.env.production` 严格分离）
- 不在 prod Dockerfile 里下载 NLTK / 模型权重 / parser 数据等外部大件（ECS 出口不走本机 VPN，会卡死）
- prod compose 必须用阿里云 apt + pip 镜像源（`app/Dockerfile` 已写死，不要改回默认源）
- `CF_TUNNEL_TOKEN` 是独立 tunnel 的 token，与同主机其他项目的 tunnel 互不复用
- 公网入口仅经 cloudflared，不要把 8005 / 5432 / 6379 直接暴露到 ECS 公网

---

## 5. 故障速查

### 5.1 dev

| 现象 | 排查 |
|---|---|
| `port 25432 already in use` | 改 `docker-compose.dev.yml` 里 `25432:5432` 为别的端口 |
| `scriptlens_api_dev` 反复重启 | `docker logs scriptlens_api_dev`；常见是 `.env.dev` 没填 OPENAI_API_KEY / DASHSCOPE_API_KEY / JWT_SECRET_KEY |
| 上传剧本时报 embedding 错 | 检查 `DASHSCOPE_API_KEY` 是否填了 |
| alembic `relation already exists` | 之前手动建过表；`docker compose -f docker-compose.dev.yml down -v` 重置 volume |
| dev 想清空所有剧本数据 | `docker exec scriptlens_db_dev psql -U postgres -d scriptlens_dev -c "TRUNCATE scriptlens.scripts CASCADE"` |
| 改 Python 代码不生效 | 检查 `docker logs scriptlens_api_dev` 是否有 reload 行；Windows + Docker Desktop volume 性能差时直接 `make restart-api` |

### 5.2 prod

| 现象 | 排查 |
|---|---|
| build 阶段 apt 卡住 / 长时间无输出 | Dockerfile 阿里云源被改回默认源；恢复 `mirrors.aliyun.com` |
| build 阶段 pip 卡住 | 同上，pip 必须用 `mirrors.aliyun.com/pypi/simple/` |
| `cf_tunnel_scriptlens` 一直 unhealthy / 公网 502 | `docker logs cf_tunnel_scriptlens`；`CF_TUNNEL_TOKEN` 错或对应 tunnel 在控制台被删 |
| `api-scriptlens.wh5233.me` 解析失败 | Cloudflare DNS 里没有自动生成 CNAME，回 Zero Trust → Tunnel → Public Hostnames 重建 |
| 公网 CORS 报错 | `.env.production` 的 `SM_CORS_ALLOW_ORIGINS` 要写完整 origin（含 https://），不写 path |
| ECS OOM | 确认 swap 已生效、`--workers 1`、Redis maxmemory=96mb、PG `shared_buffers=64MB` 没被改大 |
| pgvector 扩展不存在 | image 必须是 `pgvector/pgvector:pg15`，不能换成原生 postgres |
