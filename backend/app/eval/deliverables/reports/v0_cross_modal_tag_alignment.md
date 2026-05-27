# Cross-Modal Tag Alignment

## Gate Summary

| dim | scope | tag_set_ver | script_verdict | video_verdict | gate | reason |
| --- | --- | --- | --- | --- | --- | --- |
| world_setting | script | v1.0.0 | online | video_stable | stable_shared | both sides are stable |
| scene_emotion_keynote | plot_unit | v2.0.0 | online | video_stable | stable_shared | both sides are stable |
| relationship_polarity | relationship | v1.0.0 | online | video_experimental | experimental | at least one side is experimental/fix |
| gender_axis | script | v1.0.0 | online | video_aggregate | aggregate_only | video side requires aggregation |
| scene_locale_type | plot_unit | v2.0.0 | online | video_aggregate | aggregate_only | video side requires aggregation |

## Shared Gate Lists

- `stable_shared`: world_setting, scene_emotion_keynote
- `aggregate_only`: gender_axis, scene_locale_type
- `experimental`: relationship_polarity
- `blocked`: (empty)

## Script-Side Metrics

| dim | intra_alpha | inter_alpha | kappa_mean | par | unstable_values |
| --- | ---: | ---: | ---: | ---: | --- |
| world_setting | 1.000 | 1.000 | 1.000 | 1.000 | - |
| scene_emotion_keynote | 1.000 | 1.000 | 1.000 | 1.000 | - |
| relationship_polarity | 1.000 | 1.000 | 1.000 | 1.000 | - |
| gender_axis | 1.000 | 1.000 | 1.000 | 1.000 | - |
| scene_locale_type | 1.000 | 1.000 | 1.000 | 1.000 | - |

## Video-Side Snapshot

| dim | verdict | par | stable | stable_total | wilson_low |
| --- | --- | ---: | ---: | ---: | ---: |
| world_setting | video_stable | 1.000 | 50 | 50 | 0.929 |
| scene_emotion_keynote | video_stable | 0.992 | 48 | 50 | 0.865 |
| relationship_polarity | video_experimental | 0.952 | 43 | 50 | 0.738 |
| gender_axis | video_aggregate | 0.952 | 44 | 50 | 0.762 |
| scene_locale_type | video_aggregate | 0.964 | 44 | 50 | 0.762 |