# Upstream References

## Primary Source

- Repository: [anthropics/skills](https://github.com/anthropics/skills)
- Skill: `skills/mcp-builder`
- Standard: [Model Context Protocol](https://modelcontextprotocol.io)

## What We Reused

- MCP/tool quality is measured by whether agents can complete real-world tasks.
- Tool descriptions, structured schemas, actionable errors, pagination, and evals are first-class design concerns.
- Read-only vs write/destructive annotations matter for safe tool use.

## What We Changed

- Compressed the official multi-phase MCP guide into a production tool-contract checklist.
- Added enterprise Agent concerns: idempotency, permission model, retry semantics, destructive guards, and secret handling.
- Generalized from MCP server building to any Agent tool boundary, including parser, search, database, and external API tools.

## Why Not Copy Verbatim

The upstream `mcp-builder` is an implementation guide. This project needs a reusable design skill that triggers before code exists, so tool contracts are correct before MCP/server implementation begins.
