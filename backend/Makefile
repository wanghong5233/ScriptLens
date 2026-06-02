# ScriptLens backend Makefile —— 简化 docker compose / 调试 / 联调常用操作
#
# 风格对齐 ScholarMind/backend/Makefile：变量化 + 分组 help + 单一职责 target
#
# 使用前提：
#   cd ScriptLens/backend && make xxx
#   已经 docker compose -f docker-compose.dev.yml up -d 起过栈
#
# 覆盖：make PROJECT=xxx logs-api  （比如临时切换 compose project 名时）

# Compose project 名（默认与 docker-compose.dev.yml 顶层 `name: scriptlens` 对齐）
PROJECT      ?= scriptlens
COMPOSE_FILE ?= docker-compose.dev.yml
DC           := docker compose -p $(PROJECT) -f $(COMPOSE_FILE)

# compose 内部 service 名（与 docker-compose.dev.yml 对齐）
SVC_API      := scriptlens_api
SVC_DB       := scriptlens_db
SVC_REDIS    := scriptlens_redis

# 联调常用配置
API_URL      ?= http://localhost:8005
WEB_URL      ?= http://localhost:5173
DB_NAME      := scriptlens_dev
DB_USER      := postgres


# ==========================================================
# 默认目标：help
# ==========================================================
.PHONY: help
help:
	@echo Usage: make [target]
	@echo ----------------------------------------
	@echo Lifecycle:
	@echo   up                - Start all services (no build)
	@echo   up-build          - Build images and start
	@echo   down              - Stop/remove containers (keep data volumes)
	@echo   down-v            - DANGEROUS: down + remove volumes (data loss)
	@echo   stop              - Stop services (keep containers)
	@echo   restart           - Restart all services
	@echo   restart-api       - Restart api only (no rebuild; for code reload edge cases)
	@echo   rebuild-api       - Rebuild api image and recreate api only
	@echo   build             - Build images
	@echo   ps                - Show service status
	@echo   health            - Curl /health endpoint
	@echo ----------------------------------------
	@echo Logs (default tail from NOW; use *-history for past 200 lines):
	@echo   logs              - Follow logs for all services
	@echo   logs-api          - Follow api logs
	@echo   logs-api-history  - Last 200 api log lines
	@echo   logs-api-error    - Follow api logs filtered to ERROR/WARNING/Traceback
	@echo   logs-db           - Follow db logs
	@echo   logs-redis        - Follow redis logs
	@echo ----------------------------------------
	@echo Debug shells:
	@echo   shell             - sh into api container
	@echo   db-shell          - psql into postgres
	@echo   redis-cli         - redis-cli into redis
	@echo ----------------------------------------
	@echo DB:
	@echo   migrate           - Run alembic upgrade head in api container
	@echo   db-tables         - List scriptlens.* tables and row counts
	@echo   db-users          - List users (id / username / role / is_active)
	@echo   db-clear          - DANGEROUS: TRUNCATE users CASCADE + flush redis + clear storage_root + workspaces
	@echo ----------------------------------------
	@echo Smoke / E2E helpers (require curl + a registered user):
	@echo   smoke-register U=name P=pwd        - POST /api/users/register
	@echo   smoke-login    U=name P=pwd        - POST /api/users/login (prints token)
	@echo   verify-gateway                     - End-to-end probe of LiteLLM billing gateway
	@echo   verify-gateway-models              - Probe /models only (skip chat call)
	@echo ----------------------------------------
	@echo Disk cleanup:
	@echo   docker-prune-soft - Safe prune
	@echo   docker-prune-hard - Aggressive prune


# ==========================================================
# 生命周期
# ==========================================================
.PHONY: up
up:
	$(DC) up -d --no-build

.PHONY: up-build
up-build:
	$(DC) up -d --build

.PHONY: down
down:
	$(DC) down --remove-orphans

.PHONY: down-v
down-v:
	$(DC) down --remove-orphans -v

.PHONY: stop
stop:
	$(DC) stop

.PHONY: restart
restart:
	$(DC) restart

.PHONY: restart-api
restart-api:
	$(DC) restart $(SVC_API)

# 仅重建 api 镜像并 force-recreate（不影响 db/redis）
.PHONY: rebuild-api
rebuild-api:
	$(DC) build $(SVC_API)
	$(DC) up -d --no-build --force-recreate --no-deps $(SVC_API)

.PHONY: build
build:
	$(DC) build

.PHONY: ps
ps:
	$(DC) ps

.PHONY: health
health:
	@curl -sS $(API_URL)/health || echo "api not responding at $(API_URL)"


# ==========================================================
# 日志
# ==========================================================
.PHONY: logs
logs:
	$(DC) logs --tail=0 -f

.PHONY: logs-api
logs-api:
	$(DC) logs $(SVC_API) --tail=0 -f

.PHONY: logs-api-history
logs-api-history:
	$(DC) logs $(SVC_API) --tail=200

# 跟随 api 日志，但只让 ERROR / WARNING / Traceback / 4xx-5xx 行透传
# grep --line-buffered 在宿主机 grep 一般有；BusyBox 没有可去掉
.PHONY: logs-api-error
logs-api-error:
	$(DC) logs $(SVC_API) --tail=0 -f | grep --line-buffered -E "ERROR|WARNING|Traceback|Exception| 4[0-9][0-9] | 5[0-9][0-9] "

.PHONY: logs-db
logs-db:
	$(DC) logs $(SVC_DB) --tail=0 -f

.PHONY: logs-redis
logs-redis:
	$(DC) logs $(SVC_REDIS) --tail=0 -f


# ==========================================================
# Debug shell
# ==========================================================
.PHONY: shell
shell:
	$(DC) exec $(SVC_API) sh

.PHONY: db-shell
db-shell:
	$(DC) exec $(SVC_DB) psql -U $(DB_USER) -d $(DB_NAME)

.PHONY: redis-cli
redis-cli:
	$(DC) exec $(SVC_REDIS) redis-cli


# ==========================================================
# DB
# ==========================================================
.PHONY: migrate
migrate:
	$(DC) exec $(SVC_API) alembic upgrade head

# 一行打印 scriptlens schema 各表行数
.PHONY: db-tables
db-tables:
	@$(DC) exec -T $(SVC_DB) psql -U $(DB_USER) -d $(DB_NAME) -tAc "SELECT 'users(public)' || ': ' || COUNT(*) FROM public.users UNION ALL SELECT 'scripts' || ': ' || COUNT(*) FROM scriptlens.scripts UNION ALL SELECT 'scenes' || ': ' || COUNT(*) FROM scriptlens.scenes UNION ALL SELECT 'reports' || ': ' || COUNT(*) FROM scriptlens.reports UNION ALL SELECT 'evidence_refs' || ': ' || COUNT(*) FROM scriptlens.evidence_refs UNION ALL SELECT 'script_feedback' || ': ' || COUNT(*) FROM scriptlens.script_feedback UNION ALL SELECT 'script_operations' || ': ' || COUNT(*) FROM scriptlens.script_operations;"

.PHONY: db-users
db-users:
	@$(DC) exec -T $(SVC_DB) psql -U $(DB_USER) -d $(DB_NAME) -c "SELECT id, username, role, is_active FROM public.users ORDER BY id;"

# 清空所有用户 + 级联剧本 + redis 缓存 + 落盘文件 / agent runtime
# 用于：每次重新跑联调前快速归零；线上千万别用
.PHONY: db-clear
db-clear:
	@echo ">>> TRUNCATE public.users CASCADE ..."
	$(DC) exec -T $(SVC_DB) psql -U $(DB_USER) -d $(DB_NAME) -c "TRUNCATE public.users RESTART IDENTITY CASCADE;"
	@echo ">>> redis FLUSHALL ..."
	$(DC) exec -T $(SVC_REDIS) redis-cli FLUSHALL
	@echo ">>> rm storage_root/* ..."
	-rm -rf storage_root/*
	@echo ">>> rm app/workspaces ..."
	-rm -rf app/workspaces
	@echo ">>> done. db-tables:"
	@$(MAKE) --no-print-directory db-tables


# ==========================================================
# Smoke / E2E 联调辅助
# 用法： make smoke-register U=alice P=Alice12345
#        make smoke-login    U=alice P=Alice12345
# ==========================================================
U ?= testuser
P ?= Test123456

.PHONY: smoke-register
smoke-register:
	curl -sS -X POST $(API_URL)/api/users/register \
		-H "Content-Type: application/json" \
		-d "{\"username\":\"$(U)\",\"password\":\"$(P)\"}"
	@echo

.PHONY: smoke-login
smoke-login:
	curl -sS -X POST $(API_URL)/api/users/login \
		-H "Content-Type: application/json" \
		-d "{\"username\":\"$(U)\",\"password\":\"$(P)\"}"
	@echo

# 端到端验证 billing gateway 是否真的接通：在 api 容器里读 .env.dev 里的
# BILLING_GATEWAY_BASE_URL + BILLING_SERVICE_SECRET，发 GET /models 和一次
# 1-token chat 请求，全程不依赖 SCRIPTLENS_BILLING_MODE，不走本地 key。
.PHONY: verify-gateway
verify-gateway:
	$(DC) exec -T $(SVC_API) python -m cli.verify_billing_gateway

.PHONY: verify-gateway-models
verify-gateway-models:
	$(DC) exec -T $(SVC_API) python -m cli.verify_billing_gateway --skip-chat


# ==========================================================
# Docker 磁盘清理
# ==========================================================
.PHONY: docker-prune-soft
docker-prune-soft:
	docker system prune -f

.PHONY: docker-prune-hard
docker-prune-hard:
	docker system prune -f
	docker image prune -a -f
