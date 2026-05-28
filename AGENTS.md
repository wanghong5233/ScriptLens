# ScriptLens Development Guidelines

Guidelines for AI coding agents working in this repository.

## Workspace context

ScriptLens is a **standalone Git repo** inside the `dcccloud` super-workspace. When launched from `dcccloud/`, also read the parent [`../AGENTS.md`](../AGENTS.md) for multi-repo boundaries, git safety, and env/secret rules.

- **Write target**: files under `ScriptLens/` only unless the user explicitly asks for cross-repo edits (e.g. RavenWeb integration).
- **Read-only references**: `dcccloud/docs/` for Batch3/4/5 tag & scoring decisions; `RavenWeb/` for BFF/DocStudio contract alignment.
- Never `git add .`; stage explicit paths. Preserve unrelated dirty files.

## Tech stack

- **Backend**: Python 3.11+, FastAPI, SQLAlchemy (async), Alembic, asyncio
- **DB**: PostgreSQL 15 (`scriptlens` schema), Redis (Agent SSE replay)
- **Frontend**: React 18, TypeScript, Vite, Ant Design, valtio, Monaco (`frontend/`)
- **LLM**: OpenAI primary, DashScope fallback; jieba for Chinese tokenization
- **Lint**: Ruff (`backend/pyproject.toml`, line-length 100)

## Project layout

```plaintext
ScriptLens/
├── backend/
│   ├── app/
│   │   ├── api/                    # FastAPI routers (script_rt, etc.)
│   │   ├── service/
│   │   │   ├── script_tools/       # ingestion, segmenter, report chains
│   │   │   ├── tag_registry/       # script.yaml, prompts, bundle extractor
│   │   │   └── script_report_service.py  # main report pipeline entry
│   │   ├── eval/                   # stability / acceptance scripts (code only)
│   │   └── tests/                  # pytest
│   ├── Makefile                    # docker compose helpers
│   └── docker-compose.dev.yml
├── frontend/                       # doc-studio UI (legacy standalone)
├── docs/                           # ScriptLens-scoped architecture & requirements
└── eval/                           # LOCAL datasets & run outputs — gitignored
```

Cross-cutting product decisions live in **`dcccloud/docs/`** (e.g. `2026-05-26-剧本标签体系.md`, `2026-05-28-剧本标签稳定性.md`).

## Current tag & scoring conventions

- Active tag set version: **`script`** (file: `backend/app/service/tag_registry/tag_sets/script.yaml`).
- Do **not** reintroduce `v1.0.0`, `v0.1.0`, or bundle ids like `v1_script_structure`; use names in `script.yaml` (`script_structure`, `episode_structure`, `character_attrs`, `relationship_attrs`, …).
- Scoring/report payload follows **Batch 3 six dimensions** + separate compliance; tier enum is 5-level (`excellent` / `good` / `weak` / `poor` / `insufficient`), not legacy `high/medium/low`.
- Tag stability gates: `backend/app/service/script_tools/match_config.py` (aligned with `dcccloud/docs/2026-05-28-剧本标签稳定性.md`).

## Development

### Backend (preferred path)

```powershell
cd ScriptLens/backend
# Copy .env.example → .env; never commit real secrets
make up-build && make migrate
make health          # http://localhost:8005/health
```

Run commands from **`ScriptLens/backend`** (or inside the api container via `make shell`).

### Tests

Run **targeted** pytest — do not run the entire suite unless asked:

```powershell
cd ScriptLens/backend
python -m pytest app/tests/test_bundle_extractor.py -q
python -m pytest app/tests/test_tag_pipeline.py -q
python -m pytest app/tests/test_tag_registry_loader.py -q
```

After tag/schema changes, include the smallest relevant test file in the same change.

### Eval & datasets

- `eval/`, `剧本数据集/`, `backend/app/eval/reports/` are **local-only** (gitignored).
- Eval **code** lives in `backend/app/eval/` and is tracked.
- Do not commit copyrighted script files or large run artifacts.

## Integration with RavenWeb

RavenWeb DocStudio consumes ScriptLens HTTP APIs (`:8005`). Contract types are mirrored in:

`RavenWeb/src/features/ScriptAnalysis/DocStudioWorkbench/api/docStudio.ts`

When changing report/tag JSON shapes, coordinate both sides or flag the frontend follow-up explicitly.

## Agent workflow hints

1. Identify whether the task is **production pipeline** (`script_report_service`, tag bundles) or **eval-only** (`backend/app/eval/`).
2. Read the relevant decision doc under `dcccloud/docs/` before changing enums or prompts.
3. Prefer editing `tag_registry/prompts/*.jinja` + `script.yaml` together; keep tests in sync.
4. Use `rg` for reference search; avoid broad refactors outside the stated scope.
5. Verify with targeted pytest; for pipeline changes, smoke via `make health` or documented e2e steps.

## Documentation map

| Doc | Purpose |
|-----|---------|
| `README.md` | Product overview & quick start |
| `docs/requirement/08-evaluation-framework.md` | Scoring rubric (may lag Batch 3 — check code + dcccloud docs) |
| `docs/architecture/05-report-architecture.md` | Report segments & chains |
| `backend/README.deploy.md` | Docker deploy |
| `dcccloud/docs/2026-05-26-剧本标签体系.md` | Tag system source of truth |
| `dcccloud/docs/2026-05-28-剧本标签稳定性.md` | Stability experiment decision |

## Review checklist

Before finishing:

- Tag set version and bundle ids still resolve via `load_tag_set("script")`?
- Prompt changes paired with `script.yaml` enum closure?
- No secrets, eval datasets, or unrelated repo files in the diff?
- Tests updated for enum/prompt contract changes?
