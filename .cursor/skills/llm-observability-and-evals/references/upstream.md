# Upstream References

## Primary Sources

- Internal basis: `docs/private/engineering-playbook/05-observability-as-contract.md`
- Reference skill: [alirezarezvani/claude-skills](https://github.com/alirezarezvani/claude-skills) → `engineering/prompt-governance`
- Related upstream: SLO / prompt governance / eval pipeline skills in community repositories

## What We Reused

- Prompt changes and LLM behavior must be versioned, evaluated, and gated.
- Golden datasets are the minimum unit of quality regression testing.
- Production prompts/features require rollback and monitoring.

## What We Changed

- Expanded from prompt governance to full Agent observability:
  route decisions, retrieval plans, tool calls, provider failures, async jobs, UI states.
- Added project-derived contracts: `accepted/running/failed/timed_out`, `index_mode`, `route_reason`, `policy_version`.
- Kept prompt registry details out of the body; they can become a separate `prompt-governance` skill later if needed.

## Why Not Copy Verbatim

Prompt governance is one slice of production Agent quality. ScriptLens needs a broader contract that explains why a request behaved the way it did across ingest, evidence, tools, model providers, persistence, and frontend state.
