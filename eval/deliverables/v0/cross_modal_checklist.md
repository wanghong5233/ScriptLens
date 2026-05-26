# Cross-Modal Checklist (v0.1.0 / dev)

## Script-Side Stability Snapshot

| dim | intra_alpha | inter_alpha | verdict | video_check_suggestion |
| --- | ---: | ---: | --- | --- |
| business_conflict_bucket | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| business_content_archetype | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| business_emotion_bucket | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| business_payoff_bucket | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| conflict_type | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| cta_type | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| dialogue_density | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| drama_tags | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| emotional_driver | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| emotional_keywords | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| keyword_theme | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| payoff_type | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| plot_hook | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| relationship_arc | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| speech_style | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| story_stage | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |
| voiceover_type | 1.000 | 1.000 | online | video侧同 enum 跑 5 seed + 3 prompt variant 一致性，复核低一致样本 |

## Rule Baseline Comparison (business_*)

## LLM vs Rule Baseline

| dim | n | PAR | kappa | llm_verdict |
| --- | ---: | ---: | ---: | --- |
| business_conflict_bucket | 149 | 0.248 | 0.036 | online |
| business_content_archetype | 149 | 0.141 | -0.017 | online |
| business_emotion_bucket | 149 | 0.168 | -0.006 | online |
| business_payoff_bucket | 149 | 0.161 | 0.032 | online |

## Notes

- 当前输出来自本地可用脚本样本（scripts 表已有数据），可持续增量更新。
- 当 script 侧与 video 侧都达标后，维度进入共享内核；单侧达标则保留单侧使用。