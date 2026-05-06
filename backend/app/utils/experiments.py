from __future__ import annotations
import hashlib
from typing import Tuple


def assign_variant(user_id: str | int, session_id: str, key: str = "exp_default", buckets: Tuple[str, str] = ("A", "B")) -> str:
    """Deterministically assign an A/B bucket for a (user, session, key).

    Args:
        user_id: Current user ID
        session_id: Current session ID
        key: Experiment key (changing it re-buckets)
        buckets: Tuple of variant labels

    Returns:
        One of the provided bucket labels.
    """
    base = f"{key}|{user_id}|{session_id}".encode("utf-8")
    h = hashlib.sha256(base).hexdigest()
    val = int(h[:8], 16)  # 32-bit slice
    idx = val % max(len(buckets), 1)
    return buckets[idx]


