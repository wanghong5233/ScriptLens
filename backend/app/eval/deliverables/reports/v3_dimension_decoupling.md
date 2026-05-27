# V3 Dimension Decoupling (Dev)

- rubric_version: `v3.0.0`
- scripts_evaluated: `3`
- threshold: non-diagonal `|rho| < 0.6`
- measured max non-diagonal `|rho|`: `0.9930`
- verdict: `FAIL`（当前样本下未满足去耦阈值）

## Correlation Highlights

- `story ↔ dialogue`: `+0.9930`（最高耦合）
- `concept ↔ emotion`: `+0.9635`
- `emotion ↔ pacing`: `+0.9295`
- `character ↔ dialogue`: `-0.8352`
- `story ↔ character`: `-0.7645`

## Full 6x6 Correlation Matrix

- dimensions order: `[story, character, concept, emotion, pacing, dialogue]`
- matrix (rounded to 4 decimals):
  - `[1.0000, -0.7645, -0.7116, -0.4974, -0.1423, 0.9930]`
  - `[-0.7645, 1.0000, 0.0911, -0.1790, -0.5293, -0.8352]`
  - `[-0.7116, 0.0911, 1.0000, 0.9635, 0.7967, -0.6237]`
  - `[-0.4974, -0.1790, 0.9635, 1.0000, 0.9295, -0.3915]`
  - `[-0.1423, -0.5293, 0.7967, 0.9295, 1.0000, -0.0244]`
  - `[0.9930, -0.8352, -0.6237, -0.3915, -0.0244, 1.0000]`

## Notes

- 数据来源：`eval/deliverables/reports/batch3_acceptance.json`
- 当前 dev 样本量较小（`n=3`），建议下一轮扩展样本并优先审查 `story/dialogue` 与 `concept/emotion` 的信号重叠度。
