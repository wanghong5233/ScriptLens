# Batch1 Acceptance

- tag_set: `v0.1.0`
- scripts_checked: `3`

## Metrics

- segmenter pair-wise boundary F1: `1.000` (threshold `>= 0.700`)
- resolver pair-wise consistency: `1.000` (threshold `>= 0.850`)
- v0 stable ratio (intra_alpha >= 0.7): `17/17` = `1.000` (threshold `>= 0.600`)
- MVP regression (`build_script_ir` smoke): `PASS`

## Checks

- segmenter_f1_ge_0_7: `PASS`
- resolver_consistency_ge_0_85: `PASS`
- v0_dims_alpha_ge_0_7_ratio_ge_0_6: `PASS`
- mvp_regression_pass: `PASS`

## Overall

- result: `PASS`
- note: this acceptance run uses deterministic mode (`SM_TAGGING_DISABLE_LLM=1`) for reproducible CI/local validation.