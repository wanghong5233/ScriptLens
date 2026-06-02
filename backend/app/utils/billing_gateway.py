from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse, urlunparse

BILLING_GATEWAY_PATH = "/v1/gateway"
DEFAULT_BILLING_GATEWAY_BASE_URL = (
    "https://sd845nq1do72q591cfvsg.apigateway-cn-beijing.volceapi.com/v1/gateway"
)


def normalize_billing_gateway_base_url(base_url: Optional[str]) -> Optional[str]:
    normalized = str(base_url or "").strip().rstrip("/")
    return normalized or None


def resolve_billing_gateway_base_url(base_url: Optional[str]) -> Optional[str]:
    """
    Normalize billing gateway URL to end with `/v1/gateway`.

    Mirrors RavenWeb's `resolveBillingGatewayBaseURL` behavior:
    - already ends with `/v1/gateway` => keep
    - ends with `/v1` => append `/gateway`
    - otherwise => append `/v1/gateway`
    """
    normalized = normalize_billing_gateway_base_url(
        base_url or DEFAULT_BILLING_GATEWAY_BASE_URL
    )
    if not normalized:
        return None

    parsed = urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid BILLING_GATEWAY_BASE_URL: {normalized!r}")

    normalized_path = (parsed.path or "").rstrip("/")
    if normalized_path.endswith(BILLING_GATEWAY_PATH):
        next_path = normalized_path
    elif normalized_path.endswith("/v1"):
        next_path = f"{normalized_path}/gateway"
    elif normalized_path:
        next_path = f"{normalized_path}{BILLING_GATEWAY_PATH}"
    else:
        next_path = BILLING_GATEWAY_PATH

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            next_path,
            "",
            "",
            "",
        )
    )

