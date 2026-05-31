# Coverage Card v3.7 — comparable_titles 升级为带 URL 链接对象

## 一、契约变更（破坏性）

`CoverageCard.comparable_titles` 从 `List[str]` 升级为 `List[ComparableTitleEntry]`：

```python
@dataclass
class ComparableTitleEntry:
    title: str
    url: Optional[str] = None
```

对应 Pydantic schema 在 `schemas/script.py`：

```python
class ComparableTitleEntry(BaseModel):
    title: str
    url: Optional[str] = None

class CoverageCard(BaseModel):
    comparable_titles: List[ComparableTitleEntry] = Field(default_factory=list)
```

## 二、生成流程（v3.7.1）

业内调研结论：**不依赖 LLM 编造剧名**（幻觉太严重），改为用 coverage 自身的题材+卖点直接搜垂直短剧/漫剧平台。

1. LLM 仍在 prompt 中输出 `comparable_titles: List[str]`，但**作为候选锚点之一**，不是唯一来源
2. `extract_coverage_card` 调用 `_resolve_comparable_videos(llm_titles, logline, genre, core_value, target_count=3)`：
   - **第一阶段 - 多 query 构造**（`_build_search_queries`）：
     * LLM 候选剧名 → `{剧名} 短剧`
     * 题材组合 → `{genre[0]} {genre[1]} 短剧 爆款`（最稳信号）
     * logline 卖点 → `{logline[:18]} 短剧`
     * core_value 兜底 → `{core_value[:16]} 短剧 爆款`
     * 上限 5 条 query
   - **第二阶段 - 并发 Tavily advanced search**：
     * `search_depth="advanced"`
     * `include_domains=[douyin.com, v.douyin.com, ixigua.com, bilibili.com, b23.tv, kuaishou.com, v.kuaishou.com, haokan.baidu.com, weibo.com, weishi.qq.com]`
     * `exclude_domains=[baike.baidu.com, zhihu.com, wikipedia.org]`
     * `max_results=8` per query
   - **第三阶段 - 聚合排序**（`_aggregate_search_results`）：
     * 平台优先级权重 desc → Tavily score desc → title 字典序
     * URL `host + path[:40]` 去重
     * 截断 Top max(target_count, 3)
   - **第四阶段 - 兜底**：若限定平台命中 < 3 条 → 跑前 2 个最相关 query 的不限制搜索补齐
   - **降级路径**（任一）→ 返回 LLM 原始剧名 + platform="fallback"：
     * `agent_runtime` 不可用 / `WEB_SEARCH_API_KEY` 未配置
     * 整体异常被 fail-soft 兜住

## 三、平台优先级权重

`_VIDEO_PLATFORM_PRIORITY`（按短剧选品流量池排序）：

| 平台 | 权重 | 备注 |
|---|---|---|
| douyin / v.douyin.com | 100 | 短剧最大流量池 |
| ixigua.com | 95 | 字节生态短剧次要分发 |
| haokan.baidu.com | 85 | 百度系短剧聚合 |
| bilibili / b23.tv | 80 | 漫剧 + AI 漫剧主战场 |
| kuaishou / v.kuaishou.com | 70 | 老铁短剧 |
| weibo.com | 50 | |
| weishi.qq.com | 45 | |
| iqiyi / youku / 其他 tencent | 35-40 | 长视频平台短剧分支 |
| 不在白名单 | 10 | 兜底 |

## 四、性能 & 容错

- 第一轮：5 个 query 并发 Tavily advanced search → 总耗时 ≈ 单次 advanced 延迟（~3-5s）
- 兜底：仅在第一轮 < 3 条时再跑 2 个无限制 query → 额外 ≈ 2-3s
- 全部异常被 fail-soft，**不影响 coverage 主流程**
- 日志：每一阶段都有 `logger.info` / `logger.warning` 记录

## 五、Prompt 规则（保持 v3.6 Rule 7）

`coverage_chain._PROMPT` 中 `comparable_titles` 仍要求 LLM 给 2-3 部题材接近的已成爆款，但只作为搜索 query 候选之一。LLM 是否给得准已不再影响最终 chip 结果（搜索阶段会基于题材+卖点重新发现真实视频）。

## 六、前端渲染

由 RavenWeb `scriptlens-report-rail.tsx` 的 `ComparableVideoChip` 消费：

- 平台 brand 色系（轻量化）：抖音红 / 西瓜橙 / B 站蓝 / 快手橙 / 好看蓝
- chip 结构：平台单字 icon + 真实视频标题（ellipsis） + 外链 icon
- Tooltip：平台名 + 视频标题 + snippet 摘要 + 完整 URL
- 未命中（platform="fallback"）：muted 虚线 chip + 提示

## 6.x v3.7.4：beat_chain 节拍数据驱动 + fallback summary 重写

### 关键修复

| 位置 | 旧 | 新 |
|---|---|---|
| `_scene_label_summary` | path 2 拼 `"{type}：{角色} 关键场"` 死分支 | 删除该分支，改用 `_extract_plot_excerpt` 从 scene.text 抽 12-38 字真实剧情 |
| `_SCENE_HEADER_LINE_RE` | 无 | 跳过 "第X集 / X-Y / 人物：xxx / 卧室日内" 等 metadata 行 |
| `_MAX_CANDIDATES` | 12（写死） | `_candidate_cap(n_scenes)`：6/9/12/15 按场数分级 |
| 每幕 beat 上限 | prompt 硬写「1-3」 | `_max_beats_per_act(n_scenes)`：act1/act2/act3 分级注入 prompt |

### 数据驱动配比

```python
_max_beats_per_act(20)  # {1: 2, 2: 3, 3: 2}
_max_beats_per_act(35)  # {1: 2, 2: 4, 3: 2}
_max_beats_per_act(100) # {1: 3, 2: 5, 3: 3}
_max_beats_per_act(250) # {1: 3, 2: 6, 3: 3}
```

依据：
- Save the Cat 15-beat（电影 120 分钟基准）
- Truby 22 Building Blocks
- Linda Aronson 短剧节拍密度调研：每 6-10 分钟 1 个核心 beat（1 集 ~ 6 分钟）

### 关于 LLM cache

`call_json` （beat_chain / coverage_chain 实际用的方法）**不走 LlmCache**。
LlmCache 只服务于 `call_json_deterministic`（tag 抽取实验接口）。
**用户重新上传一定会重新调 LLM**，不存在「缓存了垃圾数据」。

## 七、后续 follow-up

- P1：接入自有平台短剧库（用户原话：「后续其实会接自己平台的视频库」）
- P2：直连抖音 / B 站开放平台 API → 替代 Tavily 通用搜索
- P3：LLM tool calling 让 coverage_chain 生成时实时验证

## 八、关联文档

- 前端 / 整体设计：`dcccloud/docs/2026-05-31-coverage-card-v3.6.md`（v3.7 + v3.7.1 完整说明）
