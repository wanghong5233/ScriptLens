# 节奏曲线 v4 — 情感命运曲线

## 一、产品定位

故事 tab 的「节奏曲线」section，回答四个问题：

1. 这部剧的「故事形状」是什么？（Vonnegut 6 种之一）
2. 高潮位置在哪里？是否健康？
3. 哪些段落是「死区」（可能流失观众）？
4. 关键节拍 / 强反转在曲线上是哪几个点？

## 二、数据契约

`pacing_curve` 由 `List[PacingCurvePoint]` 升级为 `PacingCurve` object：

```python
class PacingCurvePoint:        # 场景粒度，连续曲线
    progress: float             # 0.0 ~ 1.0
    episode_no: int | None
    scene_no: str
    scene_id: str
    sentiment: float            # -1 ~ +1，主角情感张力

class PacingCurveBeat:         # 离散锚点，可点击跳原文
    progress: float
    beat_type: str              # opening|inciting|midpoint|climax|closing|reward_spike
    label: str                  # "开场" / "中点" / "强反转" 等
    summary: str                # tooltip 文案（来自 beat.summary 或 reward.claim）
    scene_id: str

class PacingCurveDeadZone:     # 死区段，背景色块
    start_progress: float
    end_progress: float
    span_scenes: int            # 段内场景数（hint）

class PacingCurve:
    shape: str                  # rags_to_riches|tragedy|man_in_hole|icarus|cinderella|oedipus|flat|complex
    shape_label: str            # "逆袭型" / "悲剧型" / "绝处逢生" / "巅峰跌落" / "灰姑娘" / "复杂双弧" / "平铺型" / "复杂"
    climax_progress: float      # 0~1，全剧 sentiment 最高点位置
    points: list[PacingCurvePoint]
    beats: list[PacingCurveBeat]
    dead_zones: list[PacingCurveDeadZone]
```

## 三、计算口径

### 1. progress

`progress = scene_index / max(1, total_scenes - 1)`，跨集统一 0~1。

### 2. 单场景 sentiment

- 基线：关键词法 `(pos_count - neg_count) / max(1, pos+neg)`，词表沿用 v3.5 `_POSITIVE_TERMS` / `_NEGATIVE_TERMS`。
- 命中 reward 时取 `event_type` 固定情感值，符号覆盖基线：
  - `face_slap` +0.85 · `revenge` +0.9 · `underdog_rise` +0.9
  - `romantic_progress` +0.75 · `humiliate_villain` +0.7
  - `scheme_exposed` +0.55 · `identity_reveal` +0.4
  - `reversal` ±0.65（取符号 = 基线符号，无基线则 +0.65）
- 仅 `confidence=high` 的 reward 参与覆盖。

### 3. 曲线平滑

`smooth[i] = 0.5*raw[i] + 0.25*raw[i-1] + 0.25*raw[i+1]`，端点退化为 2 项。
平滑后再做 EMA(α=0.4)，输出到 `points[i].sentiment`。

### 4. 节拍锚点

- 5 个关键节拍：`beat_sheet.acts[].beats[]` 中 `type ∈ {opening, inciting, midpoint, climax, closing}`，通过 `anchor_scene_id` 反查 progress。
- 强反转散点：`event_type ∈ {reversal, face_slap, revenge, underdog_rise}` 且 `quote_verified=True` 的 reward，按 |sentiment| 降序取前 8。
- 同一场景两类标记重叠时，beat 优先。

### 5. 死区检测

滑动窗口：连续 ≥6 场场景同时满足
- `|sentiment| < 0.15`
- 无 reward_event 命中
→ 视为 dead_zone，输出 `start_progress` / `end_progress` / `span_scenes`。

### 6. Shape 识别

把 `points` 等分三段，计算 `start_mean / mid_mean / end_mean`：

| start | mid | end | shape | label |
|---|---|---|---|---|
| ≤ -0.2 | — | ≥ +0.4 | rags_to_riches | 逆袭型 |
| ≥ +0.3 | — | ≤ -0.3 | tragedy | 悲剧型 |
| 低 | 低 | 高 | man_in_hole | 绝处逢生 |
| 低 | 高 | 低 | icarus | 巅峰跌落 |
| 高 | 低 | 高 | cinderella | 灰姑娘 |
| 高 | 高 | 低 | oedipus | 渐入低谷 |
| max-min < 0.3 | — | — | flat | 平铺型（红灯） |
| 其他 | — | — | complex | 复杂双弧 |

`climax_progress = argmax(points[i].sentiment) / len(points)`。

## 四、零依赖

- 零 `plot_unit` / `tag_pipeline` 依赖（v3.5 已废弃）
- 零额外 LLM 调用（沿用 reward_events / beat_sheet / scenes 现有产出）

## 五、业内对照

- Vonnegut "Shapes of Stories" (1947) — y 轴 = 主角命运
- Reagan et al. "Six basic emotional arcs" (EPJ Data Science 2016) — shape 分类学术依据
- Sudowrite Story Engine / YouTube Studio Audience Retention — beat marker on curve
- Save the Cat 15 节拍 — 5 个关键节拍选型
- 抖音番茄短剧"完播率塌陷段" — dead zone 命名
