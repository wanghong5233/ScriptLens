from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Iterator, Optional


@dataclass(frozen=True)
class BillingIdentity:
    """Billing identity bound to a single request lifecycle."""

    user_id: str


@dataclass(frozen=True)
class BillingMetadata:
    script_id: Optional[str] = None
    intent: Optional[str] = None
    tool_name: Optional[str] = None
    trace_id: Optional[str] = None


@dataclass(frozen=True)
class BillingContext:
    identity: BillingIdentity
    metadata: BillingMetadata = field(default_factory=BillingMetadata)


_BILLING_CTX: contextvars.ContextVar[Optional[BillingContext]] = contextvars.ContextVar(
    "scriptlens_billing_ctx",
    default=None,
)
_UNSET = object()


def get_current_billing() -> Optional[BillingContext]:
    return _BILLING_CTX.get()


def set_current_billing(ctx: Optional[BillingContext]) -> contextvars.Token[Optional[BillingContext]]:
    return _BILLING_CTX.set(ctx)


def clear_current_billing() -> None:
    _BILLING_CTX.set(None)


@contextmanager
def use_billing(ctx: Optional[BillingContext]) -> Iterator[Optional[BillingContext]]:
    token = set_current_billing(ctx)
    try:
        yield ctx
    finally:
        _BILLING_CTX.reset(token)


@contextmanager
def with_metadata_overrides(
    *,
    script_id: object = _UNSET,
    intent: object = _UNSET,
    tool_name: object = _UNSET,
    trace_id: object = _UNSET,
) -> Iterator[Optional[BillingContext]]:
    current = get_current_billing()
    if current is None:
        yield None
        return

    next_meta = replace(
        current.metadata,
        script_id=current.metadata.script_id if script_id is _UNSET else script_id,
        intent=current.metadata.intent if intent is _UNSET else intent,
        tool_name=current.metadata.tool_name if tool_name is _UNSET else tool_name,
        trace_id=current.metadata.trace_id if trace_id is _UNSET else trace_id,
    )
    next_ctx = BillingContext(identity=current.identity, metadata=next_meta)
    token = set_current_billing(next_ctx)
    try:
        yield next_ctx
    finally:
        _BILLING_CTX.reset(token)

