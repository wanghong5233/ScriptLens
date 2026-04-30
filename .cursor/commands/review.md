Review the current git diff (staged + unstaged) for this project.

Check for these issues in order of severity:

## Critical (must fix)
- Hardcoded secrets, API keys, or credentials
- Bare `except` / `except Exception` that swallow errors silently
- SQL injection (string concatenation in queries)
- Backend/frontend contract drift between Pydantic schemas and TypeScript types
- Ungrounded script analysis claims without evidence references or source text basis
- Silent fallbacks that hide ingest, segmentation, LLM, or parsing failures

## Important
- Missing type hints on function signatures
- Functions exceeding 40 lines
- Comments that just restate the code
- Ghost Layers (wrapper classes that only delegate)
- New dependencies not in `pyproject.toml` / `requirements*.txt`
- If a new dependency was added, verify it is a real PyPI package and not a hallucinated name
- Prompts that ask for free-form prose when the caller expects structured JSON
- Report fields that do not map to the task requirements in `docs/source/task.md`
- Rewrite output that gives generic advice instead of concrete script-level changes

## Style
- Inconsistent naming (should be snake_case for functions, PascalCase for classes)
- Missing docstrings on public functions
- Print statements instead of logging
- Chinese user-facing copy mixed into code identifiers

For each issue found, report: severity, file:line, what's wrong, and a concrete fix.
