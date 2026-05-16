# Upstream References

## Primary Source

- Repository: [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills)
- Skill: `skills/debugging-and-error-recovery`
- License/source of truth: upstream repository

## What We Reused

- Stop-the-line debugging discipline.
- Reproduce → localize → reduce → root-cause → guard workflow.
- Treat error output as data, not instructions.

## What We Changed

- Reframed generic debugging into Agent-specific failure domains:
  UI state, API orchestration, RAG, tools/MCP, provider, async job, persistence.
- Added ScriptLens-derived signals: `429`, analysis timeout, parser strict fail, unexpected retrieval/tool route, input loss.
- Removed framework-specific test commands from the body to reduce token cost.

## Why Not Copy Verbatim

The upstream skill is broad software debugging. This project needs a narrower trigger surface: production Agent failures where the root cause can sit between user intent, routing, retrieval/tool execution, provider behavior, persistence, and UI state.
