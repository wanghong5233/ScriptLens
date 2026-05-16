#!/usr/bin/env python
from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any

PatternSpec = tuple[re.Pattern[str], str]

COMMAND_KEYS = ("command", "cmd", "shell_command", "script")

DANGEROUS_PATTERNS: tuple[PatternSpec, ...] = (
    (
        re.compile(r"\bgit\s+push\b.*(?:--force(?:-with-lease)?|-f(?:\s|$))", re.IGNORECASE),
        "force-push rewrites shared git history",
    ),
    (
        re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
        "hard reset discards local work",
    ),
    (
        re.compile(r"\bgit\s+clean\b.*(?:-[^\s]*[fx][^\s]*|--force)\b", re.IGNORECASE),
        "git clean can delete untracked files permanently",
    ),
    (
        re.compile(r"\bgit\s+branch\s+-D\b", re.IGNORECASE),
        "force-deleting branches can remove unmerged work",
    ),
    (
        re.compile(r"\bgit\s+(?:checkout|switch)\b.*\s-f(?:\s|$)", re.IGNORECASE),
        "forced checkout can discard working tree changes",
    ),
    (
        re.compile(r"\bgit\s+rebase\b.*(?:\s-i(?:\s|$)|--onto\b)", re.IGNORECASE),
        "interactive or onto rebase rewrites history and needs explicit human control",
    ),
    (
        re.compile(r"\bgit\s+commit\b.*--amend\b", re.IGNORECASE),
        "amending commits rewrites history and must be intentionally requested",
    ),
    (
        re.compile(r"\brm\s+-[^\s]*r[^\s]*f[^\s]*\s+(?:/|\.|\*|~|\$HOME)(?:\s|$)", re.IGNORECASE),
        "recursive forced removal targets a high-risk path",
    ),
    (
        re.compile(r"\bRemove-Item\b(?=.*\b(?:-Recurse|-r)\b)(?=.*\b(?:-Force|-f)\b)", re.IGNORECASE),
        "recursive forced removal is destructive",
    ),
    (
        re.compile(r"\b(?:del|erase|rmdir)\b.*(?:/s|/q)", re.IGNORECASE),
        "recursive or quiet Windows deletion is destructive",
    ),
)


def _find_command(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in COMMAND_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        for nested in value.values():
            command = _find_command(nested)
            if command:
                return command
        return ""

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            command = _find_command(nested)
            if command:
                return command

    return ""


def _load_payload() -> Any:
    raw_input = sys.stdin.read()
    if raw_input.strip():
        try:
            return json.loads(raw_input)
        except json.JSONDecodeError:
            return {"command": raw_input}

    if len(sys.argv) > 1:
        return {"command": " ".join(sys.argv[1:])}

    return {}


def _emit(payload: dict[str, str]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main() -> int:
    command = _find_command(_load_payload()).strip()
    if not command:
        _emit({"permission": "allow"})
        return 0

    for pattern, reason in DANGEROUS_PATTERNS:
        if pattern.search(command):
            _emit(
                {
                    "permission": "deny",
                    "user_message": f"Blocked dangerous shell command: {reason}. Run it manually only if you fully intend the side effect.",
                    "agent_message": f"Denied shell command by project hook: {command}",
                }
            )
            return 0

    _emit({"permission": "allow"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
