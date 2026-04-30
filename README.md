# ScriptLens

ScriptLens is a grounded story understanding agent for long scripts, short-drama drafts, and web-novel style texts.

Current stage: architecture-first frontend/backend skeleton.

## Development Environment

Local development uses `backend/.venv` and npm. Conda is not used.

Docker Compose is the reproducible deployment path.

## Architecture

- `backend/`: FastAPI Agent backend.
- `frontend/`: Next.js frontend.
- `docs/`: product, architecture, evaluation, and engineering notes.
- `docs/source/task.md`: original task requirement, treated as the source of truth.
- `samples/`: demo script fixtures used by local smoke tests and sample APIs.
- `docker-compose.yml`: reproducible local deployment path.

## Backend

```bash
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn app.api.main:app --reload
```

## Frontend

```bash
cd frontend
npm install
npm run dev
```

## Smoke Test

```bash
cd backend
.\.venv\Scripts\python.exe -m pytest
cd ..
.\backend\.venv\Scripts\python.exe scripts\smoke_d3.py
cd frontend
npm run build
```

## Docker

```bash
docker compose config
docker compose up --build
```
