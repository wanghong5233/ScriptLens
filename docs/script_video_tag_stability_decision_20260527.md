# 剧本新增标签视频侧稳定性决策

日期：2026-05-27

实验：

- 模型：`qwen3.5-omni-plus`
- 样本：现有素材视频 50 条
- 重复：每条 5 次
- 字段组：`first_pass`
- 报告：`reports/script_video_tag_stability_first_pass_50x5.json`

## 决策分层

| 字段 | avg agreement | stable | Wilson 95% lower | 决策 |
| --- | ---: | ---: | ---: | --- |
| `world_setting` | 1.000 | 50/50 | 0.929 | 可进入视频侧共享候选 |
| `scene_emotion_keynote` | 0.992 | 48/50 | 0.865 | 可进入视频侧共享候选 |
| `gender_axis` | 0.952 | 44/50 | 0.762 | 可用，但建议剧级/多素材聚合 |
| `scene_locale_type` | 0.964 | 44/50 | 0.762 | 可用，但建议素材聚合或保留 unclear |
| `relationship_type` | 0.940 | 40/50 | 0.670 | 需要收紧 prompt 后复测 |
| `relationship_polarity` | 0.944 | 41/50 | 0.692 | 需要收紧 prompt 后复测 |
| `protagonist_archetype` | 0.880 | 31/50 | 0.482 | 暂不进视频共享内核 |
| `antagonist_archetype` | 0.912 | 33/50 | 0.522 | 暂不进视频共享内核 |

## 当前可用

- `world_setting`
- `scene_emotion_keynote`

## 可用但要聚合

- `gender_axis`
- `scene_locale_type`

## 需要修复复测

- `relationship_type`
- `relationship_polarity`

主要漂移：

- `family / romance / rival`
- `positive / mixed / negative`

处理：

- 增加 `relationship_core` 字段组，只复测核心关系类型和极性；
- 收紧关系字段 prompt：家庭/恋爱/对手/权威/导师的优先级和 `mixed` 触发条件。

## 关系字段复测

实验：

- 字段组：`relationship_core`
- 样本：现有素材视频 50 条
- 重复：每条 5 次
- 报告：`reports/script_video_tag_stability_relationship_core_50x5.json`

| 字段 | avg agreement | stable | Wilson 95% lower | 复测决策 |
| --- | ---: | ---: | ---: | --- |
| `relationship_type` | 0.940 | 40/50 | 0.670 | 仍未达标，继续修复或只做聚合参考 |
| `relationship_polarity` | 0.952 | 43/50 | 0.738 | 可用，进入视频侧实验候选 |

关系复测结论：

- `relationship_polarity` 经过 prompt 收紧后达到可用线，可以作为视频侧实验候选；
- `relationship_type` 仍卡在 `family / romance`、`family / rival`、`none / rival` 的边界漂移，暂不进共享内核；
- 对 `relationship_type` 的下一步不是继续加 prompt，而是考虑拆成更粗粒度或多标签，例如 `has_romance`、`has_family_conflict`、`has_power_conflict`。

## 当前不建议视频侧使用

- `protagonist_archetype`
- `antagonist_archetype`

主要漂移：

- `weak_to_strong / son_in_law_counter / hidden_heir`
- `big_female / weak_to_strong / ceo_dominant`
- `evil_female_rival / scumbag_male / evil_relatives`

建议：

- 先保留剧本侧字段；
- 视频侧若要用，改成剧级多素材 majority vote 或合并粗粒度枚举后再测。
