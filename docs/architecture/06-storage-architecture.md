# ScriptLens 存储架构

> 本文是 ScriptLens 数据持久化层的**存储契约 + 演进方向**。最高准则是 [`source/task.md`](../source/task.md)。
> 当前实现保留 PostgreSQL（与 ScholarMind compose 共部署）；SQLite + FTS5 为未来优化点，触发条件见 §6。
> 与 [`04-script-pipeline.md`](../pipeline/04-script-pipeline.md) 三角：04 = 数据流 / 阶段；本文 = 存储介质 / 索引 / 检索。

## 1. 现状

ScriptLens 持久化层走 **PostgreSQL**（独立 schema `scriptlens`），与 ScholarMind 共享 compose。检索路径是 `scenes.text` 上的 GIN + `to_tsvector('simple')` 一级、jieba 关键词兜底、LLM metadata 二级兜底。无 embedding（[`04-script-pipeline.md §6`](../pipeline/04-script-pipeline.md)）。

```
┌────────────────────────────────────────────────────┐
│  PostgreSQL (scholarmind_db, schema scriptlens)    │
│                                                     │
│  scripts            scenes (text + GIN tsvector)   │
│  reports            evidence_refs                  │
│  script_operations  script_feedback                 │
└────────────────────────────────────────────────────┘
                        ▲
                        │ SQLAlchemy ORM + raw SQL
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
ingestion_service  report_service  rag_service
                                       │
                                       ▼
                       PG tsvector → jieba ILIKE → LLM metadata
                       + script_id 元数据过滤
                       + Agent locate_scenes_tool
```

## 2. 分层职责

| 层 | 负责 | 不负责 |
|---|---|---|
| `scripts` 表 | 元数据：title / episode_count / raw_storage_path / status | 全文 |
| `scenes` 表 | 场级原文 / scene_no / episode_no / scene_label / 物理行号 / characters TEXT[] | 语义理解 |
| `reports` 表 | report_payload (jsonb) / generated_at | UI 重排 |
| `evidence_refs` 表 | 单条 evidence：scene_id / quote / start_line / end_line / reason | 评分计算 |
| `script_operations` 表 | 改写 op 历史：source_op / target_dimension / modified_files / status | UI 派生（task_status） |
| `script_feedback` 表 | 用户反馈：scope / scope_ref / message | 自动训练 |
| 公共 `users` 表 | demo 用户（`testuser`） | 多用户协作 |

## 3. 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| 对照 task.md 主线 | §一 7 类需求 + §三 4 核心功能均要求报告质量、可追问、可溯源、可改写 | DB 选型不是当前主线，不能挤占报告重构 |
| 当前可部署性 | PG 与 ScholarMind compose 共部署已跑通 | 不阻塞 task.md §五 1 |
| 当前检索质量 | PG `simple` tokenizer 对中文长串不稳，但角色名/场号/关键词可通过 jieba ILIKE 兜底 | 本期加 Agent 端 jieba 关键词兜底，不改 schema |
| SQLite 收益 | 单文件部署 + FTS5 自定义 tokenizer 简洁 | 作为未来优化点保留 |
| SQLite 代价 | 15+ 文件有 `scriptlens.X` / `s.id::text` / `to_tsvector` / `ANY(characters)` / JSONB 等 PG-only SQL | 本期不切，避免 1-1.5 天偏离 task.md 主线 |
| Embedding | 已删除（[`04-script-pipeline.md §6`](../pipeline/04-script-pipeline.md)） | 不再制约存储选型 |

## 4. 检索契约（当前 PG 实现）

```python
async def retrieve_scenes(
    *, script_id: str, query: str, top_k: int = 5,
    candidate_pool: int = 20,
) -> List[ScoredScene]:
    """三级检索：PG BM25-ish → jieba 关键词兜底 → LLM metadata 兜底"""
```

**一级：PG BM25-ish**

```sql
SELECT s.id, s.script_id, s.episode_no, s.scene_no, s.scene_label, s.text,
       ts_rank_cd(to_tsvector('simple', coalesce(s.text, '')),
                  plainto_tsquery('simple', :q)) AS score
FROM scriptlens.scenes s
WHERE s.script_id = :sid
  AND to_tsvector('simple', coalesce(s.text, '')) @@ plainto_tsquery('simple', :q)
ORDER BY score DESC LIMIT :n;
```

**二级：jieba 关键词兜底**

```sql
SELECT s.id, s.script_id, s.episode_no, s.scene_no, s.scene_label, s.text,
       keyword_hit_count AS score
FROM scriptlens.scenes s
WHERE s.script_id = :sid
  AND (s.text ILIKE :term_0 OR s.text ILIKE :term_1 ...)
ORDER BY score DESC, s.episode_no NULLS LAST, s.scene_no
LIMIT :n;
```

**三级：LLM metadata 兜底**

BM25 + keyword 都 miss 时，把全剧 scene metadata（scene_id / scene_no / scene_label / characters）给 `ModelTier.MINI`，让 LLM 选 `scene_ids`。

**不变式**：

- 所有检索强制 `script_id` 过滤；**禁止跨剧本检索**
- jieba 兜底只用于 Agent 自由检索，不参与评分链路
- LLM 兜底失败 → 返回 `[]`，caller 自行处理「没找到」

## 5. SQLite + FTS5 演进清单（未来优化点）

| 项 | 改法 |
|---|---|
| Schema 隔离 | SQLite 不支持 schema → 表名加前缀 `scriptlens_X` |
| Connection string | `DATABASE_URL=sqlite+aiosqlite:///./var/scriptlens.db` |
| Schema 初始化 | 删除 PG alembic migration，写单一 `sqlite_init.sql` |
| UUID 主键 | `TEXT` 存 UUID v4 字符串 |
| `s.id::text` | 全部删除 |
| `TEXT[] characters` | 改 JSON 数组，查询用 `json_each` |
| `JSONB / TIMESTAMPTZ / NOW()` | 改 `JSON / TEXT (ISO8601) / CURRENT_TIMESTAMP` |
| `to_tsvector / @@ / ts_rank_cd` | 改 FTS5 `MATCH` + `bm25()` |
| `ANY(characters)` | 改 `EXISTS (SELECT 1 FROM json_each(characters) WHERE value = :name)` |
| Tokenizer | 注册 jieba tokenizer |
| Docker compose | 脱离 ScholarMind overlay，独立 compose（app + redis） |

工程估算：1-1.5 天（含测试）。本期不做。

## 6. 演进触发条件

| 触发条件 | 触发后动作 |
|---|---|
| 评审反馈「部署太复杂」 | 切 SQLite + FTS5 |
| 当前 PG + jieba ILIKE 召回率 < 60% 持续 5 起 | 先加自定义剧本词典；仍不达标切 SQLite 或 PG + pg_jieba |
| 单剧本 scenes > 5000 且检索 P95 > 200 ms | 评估 partial index / 拆库 / SQLite FTS5 |
| 出现多用户并发写需求 | 保持 PG，不切 SQLite |

**触发判断以运行时指标为准，不在无数据时提前决策。**
# ScriptLens 存储架构

> 本文是 ScriptLens 数据持久化层的**存储契约**。最高准则是 [`source/task.md`](../source/task.md)。
> 与 [`04-script-pipeline.md`](../pipeline/04-script-pipeline.md) 三角：04 = 数据流 / 阶段；本文 = 存储介质 / 索引 / 检索。

## 1. 现状

ScriptLens 持久化层走 **SQLite + FTS5 (jieba tokenizer)**，单文件部署。开发期无存量数据，直接切换不做 PG 兼容。Agent 检索路径走 FTS5 虚拟表 + jieba 中文分词。

```
┌─────────────────────────────────────────────────────────┐
│  scriptlens.db  (SQLite, WAL mode, jsonb1)              │
│                                                          │
│  scripts                  scenes                         │
│  scriptlens_users         scenes_fts (FTS5 virtual)      │
│  reports                  scene_tokens (jieba 分词缓存)   │
│  evidence_refs            script_operations              │
│  script_feedback                                         │
└─────────────────────────────────────────────────────────┘
                            ▲
                            │ SQLAlchemy 2.0 ORM
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
    ingestion_service  report_service  rag_service
                                            │
                                            ▼
                                MATCH scenes_fts(jieba 分词)
                                + scene_id 元数据过滤
                                + Agent locate_scenes_tool
```

## 2. 分层职责

| 层 | 负责 | 不负责 |
|---|---|---|
| `scripts` 表 | 元数据：title / episode_count / raw_storage_path / status | 全文 |
| `scenes` 表 | 场级原文 / scene_no / episode_no / scene_label / 物理行号 | 检索倒排 |
| `scenes_fts` (FTS5 virtual) | 倒排索引 + jieba 分词，scene_id 主键关联 | 元数据存储（用 content=scenes 同步） |
| `reports` 表 | report_payload (jsonb) / generated_at / model_versions | UI 重排 |
| `evidence_refs` 表 | 单条 evidence 记录：scene_id / quote / start_line / end_line / reason | 评分计算 |
| `script_operations` 表 | 改写 op 历史：source_op / target_dimension / modified_files / status | UI 派生（task_status） |
| `script_feedback` 表 | 用户反馈：scope / scope_ref / message | 自动训练 |
| `scriptlens_users` 表 | 单 demo 用户（`testuser`） | 多用户协作 |

## 3. 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| 对照 task.md §五 1 可部署可访问 | 评审需「实际体验 Agent 工作方式」，部署越简单分越高 | SQLite 单文件部署完胜 PG |
| 对照使用场景 | 单用户 demo / 单文件剧本上传 / 检索量极低（report 1 次 + Agent 追问 ≤ 50 次） | 写并发 ≤ 1，读并发 ≤ 5 → SQLite 完全够用 |
| 中文 FTS 分词 | PG 默认 tokenizer 对中文几乎无效；SQLite FTS5 原生支持自定义 tokenize hook | jieba 接入 SQLite 是 200 行 C 或 Python 代理；接入 PG 需编译 `pg_jieba` 扩展并改 Docker 镜像 |
| 部署复杂度 | PG 需要 docker-compose 起独立服务 + 数据卷；SQLite 单文件 + 无服务 | demo 评审时 docker-compose down 后 .db 文件即可拷贝重现状态 |
| schema 演化 | 短剧分析 schema 已稳定（10 个核心表）；不需要频繁 ALTER TABLE 改类型 | SQLite ALTER 限制不影响 |
| ACID 与崩溃恢复 | 单文件 + WAL 模式提供完整 ACID + 崩溃自动恢复 | 与 PG 等价 |
| jsonb 支持 | SQLite 3.45+ 提供 `jsonb1` 扩展（jsonb 二进制存储 + 索引） | report_payload 仍可走 jsonb，不损失能力 |
| 备份 / 迁移 / 演示 | PG: pg_dump → restore；SQLite: 拷贝 .db 文件 | take-home 演示场景下 SQLite 让评审 5 秒拿到完整数据 |
| Embedding | 已删除（[`04-script-pipeline.md §6`](../pipeline/04-script-pipeline.md)） | SQLite 无 vector 扩展不再是限制 |

## 4. FTS5 + jieba 接入契约

```python
# 入口（service/storage/sqlite_fts.py）
def register_jieba_tokenizer(conn: sqlite3.Connection) -> None:
    """注册 'jieba' tokenizer 到当前 connection。

    在 sqlite3.connect 后立即调用，否则 CREATE VIRTUAL TABLE ... USING fts5(..., tokenize='jieba') 会失败。
    """

# 建表（migrations/sqlite_init.sql）
CREATE VIRTUAL TABLE scenes_fts USING fts5(
    scene_id UNINDEXED,
    script_id UNINDEXED,
    text,
    tokenize = 'jieba'
);

# 同步（service/storage/scene_repo.py）
INSERT INTO scenes_fts(scene_id, script_id, text) VALUES (?, ?, ?);
# 删 scene → 同步 DELETE scenes_fts WHERE scene_id = ?

# 检索（service/script_rag.py）
SELECT scene_id, script_id, rank
FROM scenes_fts
WHERE scenes_fts MATCH :query AND script_id = :script_id
ORDER BY rank
LIMIT :top_k;
```

**不变式**：

- `scenes` 与 `scenes_fts` 写入必须同事务（原子性）；任何 INSERT / DELETE / UPDATE 通过 `SceneRepo` 走，不绕过
- jieba 词典固定为 `jieba` 默认 + `data/dict/scriptlens.txt`（剧本人名 / 职位 / 网络流行词）
- `MATCH` 查询前由调用方做 `query` 转义（去除 `"` `*` `:` 等 FTS5 保留符），失败抛 `RagQueryError`
- 所有 Agent 工具检索走 `script_id` 过滤，**禁止跨剧本检索**（PRD §11 多用户隔离）
- FTS5 索引重建命令：`INSERT INTO scenes_fts(scenes_fts) VALUES('rebuild')`，仅迁移工具调用

## 5. 切换契约

开发期无存量数据，直接重建 schema：

| 项 | 处理 |
|---|---|
| Connection string | `DATABASE_URL=sqlite+aiosqlite:///./var/scriptlens.db` |
| Schema 初始化 | `migrations/sqlite_init.sql` 一次性建表 + `scenes_fts` FTS5 虚拟表；alembic 简化为单 baseline revision，旧 PG migrations 删除 |
| UUID 主键 | `TEXT` 存 UUID v4 字符串；ORM 用 `sqlalchemy.types.Uuid(as_uuid=True)` |
| 时间戳 | `DateTime(timezone=True)`，存 ISO8601 字符串 |
| jsonb | SQLite 3.45+ jsonb1 扩展；ORM 用 `sqlalchemy.JSON()` |
| 全文索引 | FTS5 + jieba tokenizer，注册时机：`event.listen(Engine, "connect", register_jieba_tokenizer)` |
| 并发 | WAL 模式 + connection pool size = 1（单写）+ read pool size = 5 |
| Docker compose | 移除 `postgres` 服务；保留 `redis` |
| 删除 | `pgvector` / `tsvector` / `to_tsvector('simple')` 旧索引、PG-only DDL、alembic PG migrations |

## 6. 可逆性

| 触发条件 | 触发后动作 |
|---|---|
| 用户量 > 1（多用户场景）出现 | 切回 PostgreSQL（重建 schema，开发期不保留 PG 兼容） |
| 单剧本 scenes > 5000（超长剧） | SQLite 仍可用；检索 P95 > 200 ms 则切 PG |
| jieba 分词召回率 < 60% | 升级为「jieba + 自定义剧本词典」；仍不达标切 PG + pg_jieba |
| 出现并发写冲突 ≥ 3 起 | 切 PG |
| `scenes_fts` 索引体积 > 单文件 1 GB | 拆库（按 script_id 分库）或切 PG |

**触发判断以运行时指标为准，不在无数据时提前决策。**

## 7. 与既有文档的关系

- [`04-script-pipeline.md`](../pipeline/04-script-pipeline.md) §3 `ScriptDbWriter.write()` 仍是数据落点入口；本文换的是底层引擎与索引实现，pipeline 不变
- [`05-report-architecture.md §5`](05-report-architecture.md#5-数据契约) `ReportPayload` 写入 `reports.report_payload` 字段（jsonb），SQLite jsonb1 扩展无差别
- [`01-requirements.md §11`](../requirement/01-requirements.md#11-非目标) 「不做多用户协作权限」与本文 SQLite 单写选择一致
