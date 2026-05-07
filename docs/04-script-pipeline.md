# ScriptLens 剧本处理流水线

> 本文是 **剧本数据生命周期** 的实现层文档，跨越「上传 → 评分 → 检索 → 任务派发」四阶段。
> 与 [`01-requirements.md`](01-requirements.md)（契约 PRD）和 [`03-system-mental-model.md`](03-system-mental-model.md)（UI / Agent 心智）形成三角：
>
> - 01 = "**做什么**"
> - 03 = "**人机怎么协作**"
> - 04 = "**数据/代码怎么走**"（本文）
>
> v1（2026-05-06）：明确**砍掉 embedding 路径**——彻底删除 `script_chunks` 表与 pgvector 召回，
> 检索退化为 PG `to_tsvector('simple')` 全文索引（BM25-ish）。理由见 §6。

---

## 1. 数据流总览

```
┌──────────────┐  upload    ┌────────────────────────────────────────┐
│  用户 docx/  │──────────▶│  POST /api/scripts/upload (router)     │
│  pdf/txt/md  │            │   ├─ 存原始文件（raw_storage_path）    │
└──────────────┘            │   └─ enqueue ingest task (asyncio)     │
                            └────────────────┬───────────────────────┘
                                             │
                                             ▼
                            ┌────────────────────────────────────────┐
                            │ script_ingestion_service.ingest()      │
                            │   1) load_script_paragraphs()          │
                            │   2) segment_script() → ParsedScene[]  │
                            │   3) ScriptDbWriter.write()            │
                            │      └─ INSERT scripts + scenes        │
                            └────────────────┬───────────────────────┘
                                             │
                                             ▼  自动触发（不再手动）
                            ┌────────────────────────────────────────┐
                            │ script_report_service.generate_report  │
                            │   ├─ build_events()                    │
                            │   ├─ score_<5 dims>() 直接读 scenes.text│
                            │   ├─ extract_quote() 给每条证据挂 quote│
                            │   └─ INSERT reports + evidence_refs    │
                            └────────────────┬───────────────────────┘
                                             │
                                             ▼
                            ┌────────────────────────────────────────┐
                            │ 用户在 doc-studio 右栏看到报告         │
                            │   点击 evidence chip → AgentTask       │
                            │   ↓                                     │
                            │ Agent 执行 (locate_scenes_tool 唯一会  │
                            │ 用到检索的入口；其它走 scene_id 直跳)  │
                            └────────────────────────────────────────┘
```

四阶段对应代码入口：

| 阶段 | 代码入口 | 数据落点 |
|---|---|---|
| **入库** | `service/script_ingestion_service.py` | `scripts` + `scenes` 两张表 |
| **评分** | `service/script_report_service.py` | `reports` + `evidence_refs` 两张表 |
| **检索** | `service/script_rag.py:retrieve_scenes()` | 只读 `scenes`（PG 全文索引） |
| **任务派发** | 前端 `agentTask.ts` + 后端 `<TASK_META>` 协议 | 写 `script_operations`（timeline） |

## 2. 入库

### 2.1 解析

| 输入 | 工具 | 输出 |
|---|---|---|
| `.docx` | `python-docx`，`p.text.splitlines()` 处理软回车 | `List[str]` 段落数组 |
| `.pdf` | `pypdf` / `pdfplumber` 兜底 | 同上 |
| `.txt` / `.md` | 直接 splitlines | 同上 |

代码：`service/core/ingestion/script_loader.py:load_script_paragraphs()`。

**不变式**：解析阶段绝不剔空行——空行是后续切分的分隔信号。

### 2.2 切分（segmenter）

`service/core/ingestion/script_segmenter.py:segment_script()` 是产品级解析器。
两层启发：先按集分组，再按场分组。识别四种锚点：

1. **集号头**：`第 N 集`（中文/阿拉伯数字），允许"第N集第M场"合并
2. **数字场号**：`X-Y` / `X-Y场` / `5-3 沈宅 日 内` （带后缀）
3. **裸场景头**：`沈宅 日内` / `客厅 夜外`（无场号但含时空关键词）
4. **角色对白**：`宁卓：...` / `苏怀瑾 OS：内心`（仅用于抽取 characters，不影响切分）

容错：

- 集号头缺失 → 插入"虚拟集号"占位（`episode_no=0`），保证下游 5 维评分按集统计可工作
- 全文都没有场号 → fallback 整集为一场（绝不按字数硬切，避免内容损失）

输出 `ParsedScene[]`：

```python
@dataclass
class ParsedScene:
    episode_no: Optional[int]
    scene_no: str                 # "5-3" / "5-3a" 等
    scene_label: str              # "沈宅 日内"
    characters: List[str]
    text: str                     # 该场的完整正文（"\n".join 后）
    start_idx: int                # 该场首段在原始 paragraphs 数组中的下标
    end_idx: int                  # 同上，末段下标
```

⚠️ **`start_idx` / `end_idx` 是原始 paragraphs 数组下标，与 `scene.text.splitlines()` 内部行号
是两个坐标系**。前端编辑器打开的是 `scene.text`，所以任何"行级高亮"都必须用 `scene.text` 内的
1-indexed 行号——见 §4.2 `extract_quote` 的实现。

### 2.3 落库（writer）

`service/core/ingestion/script_pgvector_writer.py:ScriptDbWriter`（类名沿用历史，写入路径已不再涉及 pgvector）。

单事务写入：

```sql
INSERT INTO scriptlens.scripts (id, user_id, title, source_format, raw_storage_path,
                                total_episodes, total_scenes, total_chars, status='ready')
INSERT INTO scriptlens.scenes (id, script_id, episode_no, scene_no, scene_label,
                               characters, start_line, end_line, text)  -- N 行
```

不再写 `script_chunks` 表（v1 起删除整张表，见 alembic `03_drop_script_chunks.py`）。

### 2.4 索引

PostgreSQL 自动维护两个索引（建于 alembic `01_init_scriptlens.py`）：

```sql
-- 元数据查找：按集号+场号 O(log N) 定位
CREATE INDEX idx_scenes_script ON scriptlens.scenes (script_id, episode_no, scene_no);

-- 全文检索：'simple' 配置按字符切分，对中文短剧已经够用（角色名是稀疏 token，命中率高）
CREATE INDEX idx_scenes_text_fts
  ON scriptlens.scenes USING gin (to_tsvector('simple', coalesce(text, '')));
```

## 3. 评分

`service/script_report_service.py:generate_report()` 串联 5 个维度评分器。

5 维评分器位于 `service/script_tools/dimension_scorer.py`，每个都是**直接读 `scenes.text` 整集事件流喂给 LLM**——**不查任何检索**：

| 维度 | 输入 LLM 的内容 | 输出 |
|---|---|---|
| `opening_hook` | 前 3 集所有场 text | score + level + reason + evidence_scene_nos |
| `reward_density` | reward_events（前置量化抽取） + 每集事件密度 | 同上 |
| `motivation` | 主线人物的所有出场（按角色聚类） | 同上 |
| `pacing` | 分集事件数序列 + 中段比 | score + 数值 reason |
| `risk` | 全剧扫描敏感词分布 | score + 命中场号列表 |

LLM 输出的 `evidence_scene_nos: ["5-3", "8-2", ...]` 反查到 `scene_id` 后调用 `extract_quote(scene_id)`，得到：

```python
{
  "quote": str,        # ≤90 字
  "scene_id": str,
  "scene_no": str,
  "scene_label": str,
  "start_line": int,   # quote 在 scene.text 内的 1-indexed 行号
  "end_line": int,     # 单行 quote 时 = start_line
}
```

落库到 `evidence_refs`，前端拿 `start_line/end_line` 在 Monaco 编辑器对应行做半透明黄色高亮。

## 4. 检索

### 4.1 谁会用到检索？

只有一个地方：**Agent 自由对话里的 `locate_scenes_tool`**（`agent_runtime/service/tools/script_tools.py`）。

| 用户查询 | 工具行为 |
|---|---|
| "前 5 集的钩子在哪？" | BM25 命中带"钩子/反转/打脸"等词的场 |
| "宁卓苏怀瑾对峙是哪场？" | BM25 命中带这两个角色名的场 |
| "5-3" / "第 5 集第 3 场" | 元数据精确匹配 `episode_no=5 AND scene_no='5-3'`，**不走全文检索** |
| "令人破防的桥段在哪" | BM25 大概率 miss → 走 §4.3 LLM metadata 二级兜底 |

报告里**所有可点元素**已通过 `<TASK_META>` 协议携带 `scene_id`，Agent 直接用 ID 跳工具，**不会触发 `locate_scenes_tool`**（见 03 文档 §6 任务派发协议）。

### 4.2 一级检索：BM25（PG 全文索引）

`service/script_rag.py:retrieve_scenes()`：

```python
SELECT s.id::text AS scene_id,
       ts_rank_cd(
         to_tsvector('simple', coalesce(s.text, '')),
         plainto_tsquery('simple', :q)
       ) AS score
FROM scriptlens.scenes s
WHERE s.script_id = :sid
  AND to_tsvector('simple', coalesce(s.text, '')) @@ plainto_tsquery('simple', :q)
ORDER BY score DESC
LIMIT :n
```

- 召回容量 ≤ top_k（默认 5）
- 角色名稀疏命中：`'simple'` 配置按字符切分，"宁卓" 这种角色名几乎是唯一 token，命中率极高
- 索引：`idx_scenes_text_fts`（GIN，alembic `01_init_scriptlens.py` 已建）

### 4.3 二级兜底：LLM 看 metadata 列表挑

BM25 命中 0 条时（典型场景：纯抽象语义查询如"令人破防的桥段"），不再返回空，而是构造一份**全剧 scene metadata 清单**喂给 Agent：

```
2000 场 × （scene_no + scene_label + characters[≤5]）≈ 100KB ≈ 30K token
单次 LLM call 即可挑出 top-5 scene_id
```

为什么不预先 embed？

- **成本**：embedding 一次性 1500 次 API call（120 集长剧），LLM 兜底是按需 1 次
- **质量**：短剧 chunk 100–300 字，向量召回质量本就有限；LLM 看人话 metadata 比看几何空间更准
- **冷启动**：用户刚上传立即可问；embedding 路径要等几分钟索引

实施：`locate_scenes_tool` 检测 BM25 0 命中 → 自动调 `_llm_pick_scenes(query, all_scene_metadata)` → 返回 scene_id 列表。

### 4.4 为什么不要 embedding（第一性原理 + 真实数据）

#### 业务规模建模（基于 eval 42 份真实样本）

| 维度 | 主流分布 | 长尾上限 | 测算依据 |
|---|---|---|---|
| 集数 | 80–100 集 | **120 集** | eval 最长样本《战甲爱人 1-120 集》 |
| 文件体积 | docx 100–300KB / pdf 1–3MB | doc 29MB / pdf 16MB | 大文件多含老 doc 格式或 pdf 扫描图层 |
| 正文字数 | 8–18 万字 | **~20 万字** | 100 集 × 5 分钟 × 30 字/秒 ≈ 15 万字 |
| 场数 | 800–1500 场 | **~2000 场** | 主流每集 8–15 场 |
| 每场字数 | **100–300 字** | 极端 500 字 | 短剧节奏密、场密、句短 |

**MVP 边界**：120 集 / 20 万字 / 2000 场。超出此边界就不是"短剧"了——网文（无场号锚点 segmenter 失效）、传统 50 集年代剧（不在 task.md "短剧"语义内）都不归 ScriptLens 管。

#### ScriptLens 检索 vs 通用 RAG

| 维度 | 通用 RAG（论文/网页/知识库） | ScriptLens 剧本检索 |
|---|---|---|
| chunk 粒度 | 几百字到一千字 | **整场 100–300 字**（短，且语义密度低） |
| 单库规模 | 万级 chunk | 单部剧 800–2000 场 |
| query 类型 | 抽象概念为主 | **角色名 / 集场号 / 关键事件** 为主 |
| 同义词偏移 | 高（论文术语别名） | 低（演员名就是演员名） |
| 检索失败成本 | 答错 | 用户换个词，或 LLM 兜底挑 |

#### 各链路对 embedding 的需求

| 链路 | 是否需要 embedding | 替代方案 |
|---|---|---|
| 5 维评分（dimension_scorer） | ❌ | 分维度按需读 scenes.text，永远不全量 |
| 证据生成（extract_quote） | ❌ | LLM 输出 scene_no 反查 |
| 报告 → Agent 任务派发（`<TASK_META>`） | ❌ | 已携带 scene_id |
| Agent locate_scenes_tool | ❌ | BM25（一级）+ LLM metadata（二级兜底） |

#### 长剧场景下 ROI 完全倒置

| 项 | 保留 embedding | 拆除 embedding |
|---|---|---|
| 120 集 ingestion 时间 | +2–3 分钟（1500 次 DashScope call） | 节省 |
| ingestion 失败概率 | DashScope rate limit / 网络抖动 → 部分场缺向量 → 召回不稳 | 0 |
| 存储 | 6MB / 剧 vector | 0 |
| 抽象查询命中率 | ~70%（短 chunk 召回质量差） | BM25 + LLM 兜底 ≥ 80% |
| 应对压测 | 长剧上传更慢 | 长剧上传更快 |

**结论：拆 embedding 在长剧场景下优势更明显——保留它反而是负担**。

如果将来出现"跨剧本相似检索 / 风格聚类"这类**真正需要语义空间几何**的功能，再重新接入 embedding（届时建议直接 BM25 召回 + LLM rerank，仍不必持久化 pgvector）。

## 5. 任务派发（与 03 文档交叉引用）

简述：报告里的可点元素 → `dispatchAgentTask(task)` → 切到 chat tab + 注入 prompt + Monaco 高亮。详见 [`03-system-mental-model.md`](03-system-mental-model.md) §6 (AgentTask 协议) 与 §7 (端到端任务流)。

本节只补充**与数据流相关**的两个不变式：

1. **task.scene_id 必须是 `evidence_refs.scene_id`**（即 `scenes.id`）。前端用 `findSceneById`（按 UUID 精确匹配）做存在性校验，**不要**用 `findSceneByRef`（那是给 LLM 输出"5-3"这类人类引用做模糊匹配的）。
2. **task.start_line / end_line 必须是 `scene.text` 物理行号**（来自 `extract_quote` 返回值），不要混入 `scenes.start_line`（那是原始 paragraphs 数组下标，含空行/重组，与 Monaco 打开的内容坐标不一致）。

## 6. 拆 embedding：迁移清单

v1 一次性删除以下内容：

### 后端代码

| 文件 | 改动 |
|---|---|
| `service/script_ingestion_service.py` | 删 `_embed_batch`、`_default_embed_fn`、`embed_fn` 参数；`_load_segment_embed` 改为 `_load_segment`，返回 `(paragraphs, seg)` |
| `service/core/ingestion/script_pgvector_writer.py` | 删 `scene_embeddings` 参数与 `chunk_rows` 写入路径，仅保留 scene_rows |
| `service/script_rag.py` | 删 `_embedding_recall`/`_embed_query`/`_embedding_sql`/`_RawHit`/`_rrf_fuse`；`_bm25_sql` 改查 `scriptlens.scenes`；`retrieve_scenes` 单路 BM25 |
| `service/script_delete_service.py` | 删 `script_chunks` 计数与 DELETE |
| `core/config.py` | 删 `SCRIPTLENS_USE_EMBEDDING` flag |

### 数据库

新增 alembic `03_drop_script_chunks.py`：

```sql
DROP TABLE scriptlens.script_chunks;  -- 含 idx_script_chunks_* 一并随表删除
```

`scenes` 表保留 `start_line / end_line` 字段（仍是 paragraphs 数组下标，留作未来溯源原始文档用）。

### 运维

| 文件 | 改动 |
|---|---|
| `Makefile` | health-check 删 `script_chunks` 行 |
| `README.deploy.md` | 期望表清单删 `script_chunks` |

### 不动的部分（避免误伤）

以下虽然名字含 embedding，但与 ScriptLens **剧本入库链路完全无关**，是 ScholarMind 通用文档 ingestion 链路（论文/PDF 知识库）共用代码：

- `service/job_handler/parse_index_handler.py`（ScholarMind ParseIndexHandler）
- `service/core/ingestion/embedder.py:SimpleAPIEmbedder`
- `service/core/rag/nlp/model.py:generate_embedding`
- `agent_runtime/service/tools/analysis_tools.py:SemanticCodeSearchTool`（Agent 通用工具，扫文件系统）

## 7. 验证清单

清理后做一次端到端验证：

1. ✅ 上传剧本 → ingestion 不再调 DashScope embedding API（看日志 `ingest.embedded` 应消失）
2. ✅ 评分 → 5 维分数 + evidence_refs 正常落库
3. ✅ 报告面板：点 evidence chip → 编辑器跳到 scene + 高亮 quote 行
4. ✅ Agent 对话："给我找前 5 集的钩子" → `locate_scenes_tool` 返回 top-5 scenes（走 BM25）
5. ✅ 删剧本 → `scenes` / `reports` / `evidence_refs` / `script_operations` 全部级联清掉，无残留
6. ✅ `\d scriptlens.*` 不再有 `script_chunks` 表

## 8. 相关文档

- [`00-reuse-matrix.md`](00-reuse-matrix.md) · ScholarMind 模块复用矩阵
- [`01-requirements.md`](01-requirements.md) · 契约 PRD（5 维评分 / API / schema）
- [`02-script-evaluation-rubric.md`](02-script-evaluation-rubric.md) · 5 维评分工业判据
- [`03-system-mental-model.md`](03-system-mental-model.md) · UI / Agent 协作心智模型
