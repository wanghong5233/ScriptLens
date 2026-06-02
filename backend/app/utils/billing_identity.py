from __future__ import annotations

import time
from typing import Optional

from fastapi import Request

from models.user import User

RAVENWEB_USER_HEADER = "X-RavenWeb-User-Id"
RAVENWEB_TIMESTAMP_HEADER = "X-RavenWeb-Timestamp"
RAVENWEB_TIMESTAMP_MAX_SKEW_SECONDS = 300


def resolve_billing_user_id(request: Request, current_user: User) -> str:
    """
    Resolve billing user identity for gateway attribution.

    Priority:
    1) RavenWeb BFF forwarded header `X-RavenWeb-User-Id`
    2) ScriptLens local authenticated user id
    """
    forwarded = str(request.headers.get(RAVENWEB_USER_HEADER) or "").strip()
    if forwarded:
        return forwarded
    return str(getattr(current_user, "id", "") or "")


def validate_ravenweb_timestamp_header(
    request: Request,
    *,
    now_seconds: Optional[int] = None,
    max_skew_seconds: int = RAVENWEB_TIMESTAMP_MAX_SKEW_SECONDS,
) -> Optional[str]:
    """
    Best-effort timestamp guard for service-to-service requests.

    This function is intentionally non-blocking for now (warn-only rollout).
    Returns a warning reason string when header is missing/invalid/outdated.
    """
    raw = str(request.headers.get(RAVENWEB_TIMESTAMP_HEADER) or "").strip()
    if not raw:
        return "missing"

    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return "invalid"

    now = int(now_seconds if now_seconds is not None else time.time())
    if abs(now - ts) > max_skew_seconds:
        return "expired"
    return None

