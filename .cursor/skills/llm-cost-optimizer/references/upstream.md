# Upstream References

## Primary Source

- Repository: [alirezarezvani/claude-skills](https://github.com/alirezarezvani/claude-skills)
- Skill: `engineering/llm-cost-optimizer/skills/llm-cost-optimizer`

## What We Reused

- Measure first, optimize second.
- Main levers: model routing, prompt caching, output length control, prompt/context compression, semantic caching, batching.
- Cost optimization must preserve quality via evals and monitoring.

## What We Changed

- Shortened body to match this project's `writing-skill` budget.
- Added project-derived fields: `feature`, `route_reason`, `policy_version`, provider circuit breaker, retry budget.
- Connected cost work to Agent quality evals instead of treating it as a standalone finance task.

## Why Not Copy Verbatim

The upstream skill is comprehensive and proactive, but too long for our always-discovered project skill set. This version keeps only the production Agent cost decisions that repeatedly matter in ScriptLens-like systems.
