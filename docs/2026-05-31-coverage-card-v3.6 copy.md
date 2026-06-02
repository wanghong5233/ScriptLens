# 速览 30 秒判断 v3.7 — 回归行业标准 + Tavily 链接化同类爆款

## 一、为什么从 v3.6 推倒重做

v3.6 我 invent 了三个**不在行业惯例中**的范式，且实测视觉效果差：

| v3.6 invent 范式 | 为什么砍 |
|---|---|
| 「三段速读」🎣 钩子 / 🚀 高潮 / 🎬 结局 | 翻遍 Hollywood Coverage / ReelShort 选品端，**5/5 来源**都没有这个范式。LLM 数据缺失时 3 个空 slot 极丑。 |
| 「数据画像 KPI」关键人物 N / 故事节拍 N | vanity metrics，对决策无帮助。买手不在乎人物个数。 |
| 「剧情摘要折叠在底部」 | synopsis 是 Coverage 第二屏核心，**5/5 行业来源都不折叠**。藏起来是反模式。 |
| 「一句话理由 callout」6 维加权 X/10 + 总评 | Hollywood 反模式 —— 维度分析应展开为 strengths/concerns，不应压成一句空话。 |
| 「同类参考」纯文本 chip | 用户原话「写死的一个题目肯定不行」—— 命名不准（应是「爆款」），且无跳转无价值。 |

## 二、v3.7 行业标准 5 段结构

```
Tier 0  决策头     强推/慎选/弃 + 综合分 + 题材 chip
Tier 1  Logline    一句话定位
Tier 2  剧情简介   ★ 直接展开 200-300 字（行业 5/5 都不折叠）
Tier 3  优势 / 风险 3 优 3 劣（Hollywood Coverage Comments 等价）
Tier 4  同类爆款   ★ chip 跳 Tavily 校验后的真实剧目链接
```

## 三、调研依据

5 份业内权威 Coverage Report 范式分析，**结构完全一致**：

| 来源 | 范式 |
|---|---|
| Verve Coverage Guide | Logline → Synopsis → Comments（分维度） → Recommendation |
| Industrial Scripts Standard | Title / Logline / Synopsis（必有）→ Strengths → Weaknesses → Recommend / Pass / Consider |
| Saks Picture Co Template | 同上 + Comparable Titles |
| FreeScreenwriter Template | 同上 |
| Reelytics ReelShort 选品指南 | Logline → Synopsis → 优势/风险 → 类比剧目 → 决策建议 |

5/5 行业来源中**没有任何一个**包含「三段速读 / KPI / 折叠 synopsis」。

## 四、同类爆款 Tavily 链接化（v3.7 关键升级）

### 设计动机

用户原话：「同类参考应该是同类爆款吧？？这应该是有链接跳转吧？？你当前实现肯定不行啊？？写死的一个题目？？」

回应：把 v3.6 的纯字符串 `comparable_titles: List[str]` 升级为带 URL 的对象数组，由 backend 在 coverage 生成时调用 Tavily 拿真实链接。

### 后端链路

`coverage_chain.extract_coverage_card` → LLM 给剧名 List[str] → `_enrich_comparable_titles_with_urls` 并发跑 Tavily search → 拼装 `List[ComparableTitleEntry]`。

```python
@dataclass
class ComparableTitleEntry:
    title: str
    url: Optional[str] = None
```

- 并发：`asyncio.gather(... return_exceptions=True)`，单条失败不影响整体
- 配置：复用 `agent_runtime.core.config.settings.WEB_SEARCH_*`（不重复挂 env）
- 降级：API key 未配置 / 网络失败 → url=None，前端展示 muted chip + tooltip 提示
- query：`"{title} 短剧"` 提高短剧场景命中率

### 前端渲染

- `url` 有值：渲染为 `<a target="_blank">` 包裹的 Tag，加 LinkOutlined 外链 icon，hover 高亮
- `url` 为空：渲染为 muted 虚线 chip + tooltip「LLM 推荐，未命中真实剧目链接」
- 底部 disclaimer：「LLM 推荐 · Tavily 自动校验首条结果（未命中显示为不可点击）」

## 五、v3.7.1 升级 —— 同类爆款重做（不依赖 LLM 编造剧名）

### 5.1 v3.7 实现的 3 个问题（用户原话「naive」）

| 问题 | 根因 |
|---|---|
| **只有 1 条** | 每个 LLM 给的剧名只取 first result，LLM 给少 / 搜不到就掉了 |
| **题材跑偏** | 没用 `site:` 限定，可能跳到 wiki / 新闻聚合页 |
| **不是爆款** | first result 不代表爆款，可能是冷门页面 |

### 5.2 行业成熟方案调研

| 来源 | 关键做法 |
|---|---|
| **Tavily 官方 best practice** | `search_depth=advanced` + `include_domains` + `max_results=10` |
| **Perplexity Discover** | 多 query 并发 + 聚合去重 + 重排（按 score / authority） |
| **Reelytics / Parrot Analytics 选品工具** | 基于题材+卖点搜视频平台，不依赖 LLM 编造剧名 |
| **抖音红果选品端** | 类比剧目 = 自有剧库 fuzzy match（我们没库，用搜索代替） |

### 5.3 v3.7.1 实现

**Pipeline**：
1. **多 query 构造**（`_build_search_queries`）：
   - LLM 给的剧名候选（保留作为锚点） → `{剧名} 短剧`
   - **题材组合** → `{genre[0]} {genre[1]} 短剧 爆款`（行业最稳定的"找同类"信号）
   - **logline 卖点** → `{logline[:18]} 短剧`
   - core_value 兜底 → `{core_value[:16]} 短剧 爆款`
   - 上限 5 条 query，去重
2. **并发 Tavily advanced search**（`include_domains` 限定垂直平台白名单）：
   - 抖音 / v.douyin.com / 西瓜（ixigua）/ B 站（bilibili+b23.tv）/ 快手 / 好看视频 / 微博 / 微视
   - `search_depth=advanced` + `max_results=8` 每条
   - `exclude_domains=[baike.baidu.com, zhihu.com, wikipedia.org]`
3. **聚合排序**（`_aggregate_search_results`）：
   - 平台优先级权重：抖音 100 / 西瓜 95 / 好看 85 / B 站 80 / 快手 70 / 其他 < 50
   - 二级排序：Tavily score desc
   - URL host+path 前 40 字去重
4. **兜底**：若限定平台命中 < 3 条 → 跑前 2 个最相关 query 的"无限制"搜索补齐
5. **返回**：保底 ≥ 3 条，上限 5 条

**新数据契约**：

```python
@dataclass
class ComparableTitleEntry:
    title: str                          # 真实视频标题（非 LLM 编造）
    url: Optional[str] = None           # Tavily 返回的真实链接
    platform: Optional[str] = None      # douyin / ixigua / bilibili / kuaishou / haokan / ...
    snippet: Optional[str] = None       # 搜索 snippet 摘要
```

**前端 chip 视觉**：

- 平台 brand 色系（轻量化）：抖音红 / 西瓜橙 / B 站蓝 / 快手橙 / 好看蓝 / 微博红
- chip 内：平台单字 icon + 真实视频标题（ellipsis） + 外链 icon
- Tooltip：平台名 + 视频标题 + snippet + URL
- 未命中（fallback）：muted 虚线 chip

## 六、v3.7.2 — chip 色彩语义修正 + 故事 tab 数据质量根治

### 6.1 chip 颜色修正

用户反馈：「这里链接为什么是红色？？语义不对，红色一般表示警告的意思」。v3.7.1 我直接用了
平台 brand 色（抖音红 / 微博红），但**红色在我们速览卡里已经被「亮点 / 风险 / 决策」占用**，
让用户误以为这些 chip 是 warning。

修正：
- chip 主体统一为中性米/灰色背景 (`#FBF8F4` / 边框 `#ECE3D8` / 文字 `#4A4039`)
- 平台 brand 色**只**出现在左侧 18×18 的「平台 dot」上
- 业内对照：Linear / GitHub / Notion 的源/标签 chip 都是「中性主色 + 小色片做品牌识别」

### 6.2 故事 tab 数据质量根治

用户反馈：「三幕节拍是否准确？为什么节拍没有具体内容？？？下面的钩子也是没有具体内容！！」

#### DB 数据查证

```
opening   "开端：关键场"       ← 场景标题残留
midpoint  "中点反转：办公室日内" ← scene heading 残留
climax    "高潮：卧室日内"      ← scene heading 残留
```

每幕只 1 个 beat，全是 LLM 直接输出的低质量 summary。
hook 类型 highlight 的 `episode_no / scene_no / scene_label` 全 NULL，
导致前端只能 fallback 到 `scene_id.slice(0, 6) = "9ad1e2"` 这种垃圾定位符。

#### 三层修复

**A. beat_chain LLM 输出质量门槛**：
- `_is_low_quality_summary`：detect 「X：场景头」「X：纯标签」「日内/日外」≤14 字 等模式
- 命中 → reject + fallback 到 `_scene_label_summary` 升级版
- prompt 加正/反例（5 个 ✅ 范例 + 5 个 ❌ 反例）+ 明确硬规则

**B. `_scene_label_summary` 升级**：
- 旧版：`f"{type_zh}：{scene_label}"` → "高潮：卧室日内"
- 新版：抽 `scene.text` 首句剧情正文，去掉行首场景头，截 ≤ 38 字，组成「{type}：{剧情正文片段}」

**C. hook highlight 字段补齐**：
- `_build_highlights_minimal` 新增 `scenes_by_id` 参数
- `generate_report` 把 `scenes` 转 `{s.id: s for s in scenes}` 传下去
- hook 类型从 scene 反查 `episode_no / scene_no / scene_label`

## 七、v3.7.3 — Strengths / Weaknesses 升级为可展开深度分析

### 7.1 用户痛点

「正反对比太单薄了，没有价值，就是为了凑数吗？？这里每个论点，要能够展开详情，查看具体分析」

旧版 `CoveragePoint` 只有 `title + detail (≤80 字)`。一句话评价 + 没有展开、没有维度归属、
没有证据锚点 = 跟"凑数"没区别。

### 7.2 业内成熟做法

| 来源 | Strengths/Weaknesses 段做法 |
|---|---|
| Hollywood Coverage Report | 每条加粗 title + 100-300 字段落分析 + 引用页码/场号 |
| Industrial Scripts Standard | 1 句评价 + 段落分析 + 证据引用 |
| ReelShort / 抖音红果选品端 | title + 维度归属（钩子/爽点/反转）chip + 例子集数 |

### 7.3 v3.7.3 schema 升级

```python
class CoveragePoint(BaseModel):
    title: str                  # ≤ 12 字 标题
    detail: str                 # ≤ 80 字 一句话评价（默认展示）
    analysis: str = ""          # ≤ 300 字 展开深度分析
    dimension: str = ""         # story|character|concept|emotion|pacing|dialogue 或空
    evidence_hint: str = ""     # ≤ 60 字 证据线索，引导用户去原文找
```

### 7.4 前端交互

- 默认状态：title + detail（3 行）+ 维度 chip + 「展开」按钮
- 展开状态：增加深度分析（虚线框 200-300 字）+ 证据线索（带「证据线索」label 的小条）
- 维度 chip 颜色与评估 tab 6 维评分卡保持一致（认知锚定原则）
- 数据缺失 → 优雅降级（analysis 缺则不显示展开按钮）

### 7.5 维度色映射（前端 `POINT_DIMENSION_META`）

| dimension | label | bg | color |
|---|---|---|---|
| story | 故事 | #E0EDF7 | #1F4A78 |
| character | 人物 | #FBE9D6 | #8A4A12 |
| concept | 题材 | #F1E0F5 | #6B2A82 |
| emotion | 情感 | #FCE0E6 | #8A1F3B |
| pacing | 节奏 | #E0F0E5 | #1F6B3A |
| dialogue | 对白 | #E8E0F0 | #4A2E78 |

## 7.6 v3.7.4：故事 tab 节拍数据驱动 + fallback summary 彻底重写

### 起因（用户原话）

> 「我已经重新上传并且解析了，这里的故事tab的bug还是没有解决啊？？仍然是没有价值的信息这里明明有多余空间，为什么还是截断成了省略号？」
> 「你说的什么llm缓存是什么鬼东西？？是不是缓存了垃圾数据？」
> 「你是写死的三幕多少个节拍让大模型输出吗？？这里我感觉不太对，像是naive的实现，这里不是应该数据驱动吗？」

### 根因复盘

DB 实际数据：

```json
{ "summary": "开端：姜栀枝裴鹤年 关键场" }
{ "summary": "中点反转：陆斯言 关键场" }
{ "summary": "高潮：姜栀枝 关键场" }
```

这些**全部是规则层 `_scene_label_summary` 的 path 2 输出**：

```python
# v3.7.2 旧版：
if scene.characters:
    return f"{type_zh}：{scene.characters[0]} 关键场"[:_SUMMARY_MAX_LEN]
```

链路：LLM 给出"X：人物 关键场"垃圾 → 被 `_BAD_BEAT_SUMMARY_RE` reject →
回退到 `_scene_label_summary` → fallback 自己又拼出"X：人物 关键场"。
**fallback 自己就是垃圾，跟 LLM 没关系**。

#### 关于"LLM 缓存"疑问

`call_json`（beat_chain / coverage_chain 用的）**不走缓存**，每次都新调 LLM。
只有 `call_json_deterministic`（tag 实验用）才有 LlmCache。用户重新上传一定是新调 LLM。

### 改动 1：彻底重写 `_scene_label_summary`

旧 path 1 用 `re.split(r"[。！？；\n]", text, maxsplit=1)[0]` 只截到首行换行。
但实际剧本头 4-5 行都是 metadata：

```
第一集
1-1
酒店夜内
人物：姜栀枝裴鹤年
△裴鹤年被蒙着双眼，姜栀枝解开裴鹤年衣服扣子   ← 第 5 行才是剧情
```

新 `_extract_plot_excerpt` 策略：

1. 按行逐句扫描，跳过 `_SCENE_HEADER_LINE_RE` 命中的 metadata 行
2. 从第 1 个 `△…`（动作行）或 `角色：…`（对白行）开始累积
3. 累积到 ≥ 28 字时停，最多取 ≤ 38 字
4. **完全删除"X：人物 关键场"死分支**

实测产出：

| scene | 旧 fallback | 新 fallback |
|---|---|---|
| 第 1 集 1-1 | 开端：姜栀枝裴鹤年 关键场 | 开端：裴鹤年被蒙着双眼，姜栀枝解开裴鹤年衣服扣子；姜栀枝靠近裴鹤年嘴唇，亲吻脖子 |
| 第 30 集 30-1 | 中点反转：陆斯言 关键场 | 中点反转：裴鹤年坐在椅子上，其他人都毕恭毕敬站着；张院长：裴总，他真的是你的侄女儿 |
| 第 84 集 84-1 | 高潮：姜栀枝 关键场 | 高潮：姜栀枝：系统！他们是怎么回事？；系统VO：剧情线彻底崩坏，系统修复中 |

### 改动 2：节拍数量数据驱动（不再写死「每幕 1-3」）

#### 候选锚点上限 `_candidate_cap(n_scenes)`

| 场数区间 | 候选总数上限 | 业内对照 |
|---|---|---|
| ≤ 30 | 6 | Save the Cat 简化版 |
| 30-80 | 9 | 短剧典型 |
| 80-200 | 12 | Save the Cat 15-beat |
| > 200 | 15 | Truby 22 Building Blocks 简化 |

#### 每幕 beat 上限 `_max_beats_per_act(n_scenes)`

| 场数 | act1 | act2 | act3 |
|---|---|---|---|
| ≤ 30 | 2 | 3 | 2 |
| 30-80 | 2 | 4 | 2 |
| 80-200 | 3 | 5 | 3 |
| > 200 | 3 | 6 | 3 |

设计理由：
- act2（发展）总是节拍最密集段，应分配最大比例 ≈ 50%
- act1/act3 各占 25%，符合 Field《Screenplay》三幕分配
- prompt 模板插值 `{max_act1}/{max_act2}/{max_act3}` 传给 LLM

### 改动 3：前端 highlight oneliner 取消硬截断

`.highlightOneliner` 原本 `line-clamp: 2`，主要看点空间充足但被强行省略。
v3.7.4 改为 `white-space: normal` 自然换行 + `word-break: break-word`，
配合 `humanizeReportText` 清洗后给用户看完整剧情，避免「明明有空间却被截断」的体验。

### 验证

启动后跑：

```bash
docker exec scriptlens_api_dev python -c "
from service.script_tools.beat_chain import _extract_plot_excerpt, _candidate_cap, _max_beats_per_act
print(_candidate_cap(100), _max_beats_per_act(100))
# 期望：12 {1:3, 2:5, 3:3}
"
```

用户需点 RavenWeb「重新诊断」让 ScriptLens 重新跑一次 beat_chain，
看 DB `report_json -> 'beat_sheet'` 应该出现真实剧情概括，不再是「人物 关键场」。

---

## 八、后续 follow-up（v3.8+）

| 阶段 | 内容 |
|---|---|
| P1 | 接入自有平台短剧库（用户原话：「后续其实会接自己平台的视频库」），fuzzy match 后命中库内剧目 → 显示库内卡片（海报 + 数据） |
| P2 | 平台 API 直连（抖音开放平台 / B 站 API）→ 用平台原生搜索结果替代 Tavily 通用搜索 |
| P3 | LLM tool calling 在 coverage_chain 生成时实时验证剧名存在性，进一步消除幻觉 |
| 6 维评分对照 | 用户提出：短剧选品「钩子/爽点/反转/共情/合规」5 维 vs 现有 6 维（故事/人物/题材/情感/叙事/对白）是否冲突 —— 留作下一轮 |

## 六、契约变更

### 后端（ScriptLens）

`schemas/script.py`：

```python
class ComparableTitleEntry(BaseModel):
    title: str
    url: Optional[str] = None

class CoverageCard(BaseModel):
    ...
    comparable_titles: List[ComparableTitleEntry]
```

### 前端（RavenWeb）

`api/docStudio.ts`：

```ts
interface ComparableTitleEntryDTO {
  title: string;
  url?: string | null;
}

interface CoverageCardDTO {
  comparable_titles?: ComparableTitleEntryDTO[];
  ...
}
```

### 已删除组件 / 函数

- `SpeedreadSlot`
- `CollapsibleCoveragePanel`
- `extractBeatSummary`
- `isLikelyCritique`
- `KpiCell`
- SCSS：`speedread*` / `coverageCollapse*` / `kpi*` / `heroReasonCallout` / `heroKpiRow` 全部
