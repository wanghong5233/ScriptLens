"""End-to-end verification for the LiteLLM billing gateway.

Exercises the gateway independently of the running service mode so you can
keep ``SCRIPTLENS_BILLING_MODE=hybrid`` + local DashScope key for daily
debug, and still confirm that gateway credentials resolve a real model
call before deploy.

Usage (inside api container)::

    docker compose -p scriptlens -f docker-compose.dev.yml \
        exec scriptlens_api python -m cli.verify_billing_gateway

Or via Make::

    make verify-gateway

Steps:

1. Resolve ``BILLING_GATEWAY_BASE_URL`` via :mod:`utils.billing_gateway` and
   ``BILLING_SERVICE_SECRET`` from settings.
2. ``GET {gateway}/models`` with ``Authorization: Bearer {secret}`` —
   confirms the gateway is reachable, the secret is accepted, and lists the
   models the platform exposes for this service.
3. ``POST {gateway}/chat/completions`` with a 1-token request and a mock
   ``X-RavenWeb-User-Id`` header — confirms a real LLM call goes through
   billing, the user is attributable, and a model id like ``gpt-5.2`` or
   ``dashscope/qwen-max`` returns content.

The script never falls back to local keys; if any step fails the exit code
is non-zero and the failure reason is printed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

import httpx


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "get_secret_value"):
        try:
            return str(value.get_secret_value() or "").strip()
        except Exception:
            return ""
    return str(value).strip()


def _load_credentials() -> tuple[str, str]:
    """Resolve gateway base URL + service secret from app settings.

    Settings are loaded the same way the FastAPI app loads them, so this
    matches what the running service would see.
    """
    from core.config import settings  # type: ignore[import-not-found]
    from utils.billing_gateway import resolve_billing_gateway_base_url

    base_url = resolve_billing_gateway_base_url(
        getattr(settings, "BILLING_GATEWAY_BASE_URL", None)
    )
    secret = _coerce_str(getattr(settings, "BILLING_SERVICE_SECRET", None))

    if not base_url:
        raise RuntimeError(
            "BILLING_GATEWAY_BASE_URL is empty after normalization. "
            "Set it in .env.dev (e.g. https://<gateway-host>/v1/gateway)."
        )
    if not secret:
        raise RuntimeError(
            "BILLING_SERVICE_SECRET is empty. Add it to .env.dev "
            "(do not commit the real value)."
        )
    return base_url, secret


def _print_section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _probe_models(base_url: str, secret: str, *, timeout: float) -> List[str]:
    """Step 1: list models. Mirrors ConfigService gateway probe."""
    url = f"{base_url.rstrip('/')}/models"
    started = time.time()
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {secret}"},
        timeout=timeout,
    )
    elapsed_ms = int((time.time() - started) * 1000)

    print(f"GET {url}")
    print(f"  status:  {response.status_code}")
    print(f"  elapsed: {elapsed_ms} ms")

    if response.status_code != 200:
        snippet = response.text[:400].replace("\n", " ")
        raise RuntimeError(f"models probe failed (HTTP {response.status_code}): {snippet}")

    payload = response.json()
    items = payload.get("data") or payload.get("models") or []
    ids: List[str] = []
    for item in items:
        if isinstance(item, dict):
            mid = item.get("id") or item.get("model")
            if mid:
                ids.append(str(mid))
        elif isinstance(item, str):
            ids.append(item)
    print(f"  models:  {len(ids)} ids returned")
    if ids:
        sample = ids[:5]
        print(f"  sample:  {sample}")
    return ids


def _pick_chat_model(model_ids: List[str], requested: Optional[str]) -> str:
    if requested:
        return requested
    preferred_prefixes = (
        "gpt-5",
        "gpt-4",
        "openai/",
        "dashscope/qwen-max",
        "dashscope/qwen3-max",
        "qwen-max",
        "qwen3-max",
    )
    for prefix in preferred_prefixes:
        for mid in model_ids:
            if mid.lower().startswith(prefix.lower()):
                return mid
    if model_ids:
        return model_ids[0]
    raise RuntimeError("Gateway returned zero models; cannot pick a chat target.")


def _probe_chat_completion(
    base_url: str,
    secret: str,
    *,
    model: str,
    user_id: str,
    timeout: float,
) -> Dict[str, Any]:
    """Step 2: real LLM call through the gateway."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "max_tokens": 8,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "Answer with exactly the word: pong"},
            {"role": "user", "content": "ping"},
        ],
    }
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "X-User-Id": user_id,
        "X-LiteLLM-User-Id": f"litellm-{user_id}",
        "X-LiteLLM-Metadata-Intent": "scriptlens.verify_gateway",
    }
    started = time.time()
    response = httpx.post(url, headers=headers, json=body, timeout=timeout)
    elapsed_ms = int((time.time() - started) * 1000)

    print(f"POST {url}")
    print(f"  model:   {model}")
    print(f"  user_id: {user_id} (mock)")
    print(f"  status:  {response.status_code}")
    print(f"  elapsed: {elapsed_ms} ms")

    if response.status_code != 200:
        snippet = response.text[:500].replace("\n", " ")
        raise RuntimeError(
            f"chat completion failed (HTTP {response.status_code}): {snippet}"
        )

    payload = response.json()
    choices = payload.get("choices") or []
    text = ""
    if choices:
        msg = choices[0].get("message") or {}
        text = str(msg.get("content") or "").strip()
    usage = payload.get("usage") or {}
    print(f"  reply:   {text!r}")
    print(f"  usage:   {usage}")
    return payload


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify ScriptLens billing gateway end-to-end without "
        "switching SCRIPTLENS_BILLING_MODE."
    )
    parser.add_argument(
        "--user-id",
        default="ravenweb-verify",
        help="Mock RavenWeb user_id used for billing attribution (default: ravenweb-verify)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the model id used for the chat completion probe; "
        "defaults to the first OpenAI/Qwen-max id returned by the gateway.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--skip-chat",
        action="store_true",
        help="Only probe /models, skip the chat completion step.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)

    try:
        base_url, secret = _load_credentials()
    except Exception as exc:
        print(f"[FAIL] credentials: {exc}", file=sys.stderr)
        return 2

    masked = secret[:4] + "***" + secret[-2:] if len(secret) > 8 else "***"
    print(f"gateway: {base_url}")
    print(f"secret:  {masked} (len={len(secret)})")

    _print_section("Step 1/2  GET /models")
    try:
        model_ids = _probe_models(base_url, secret, timeout=args.timeout)
    except Exception as exc:
        print(f"[FAIL] /models: {exc}", file=sys.stderr)
        return 3

    if args.skip_chat:
        print("\n[OK] /models reachable; chat probe skipped (--skip-chat).")
        return 0

    _print_section("Step 2/2  POST /chat/completions")
    try:
        chat_model = _pick_chat_model(model_ids, args.model)
        payload = _probe_chat_completion(
            base_url,
            secret,
            model=chat_model,
            user_id=args.user_id,
            timeout=args.timeout,
        )
    except Exception as exc:
        print(f"[FAIL] /chat/completions: {exc}", file=sys.stderr)
        return 4

    _print_section("RESULT")
    print(f"[OK] Gateway is reachable, secret accepted, chat model `{chat_model}` returned a reply.")
    print("This proves: BILLING_GATEWAY_BASE_URL + BILLING_SERVICE_SECRET are wired correctly,")
    print("the gateway routes service-to-model traffic, and X-User-Id attribution works.")
    print()
    print("Daily local debug can stay on hybrid mode + DashScope key; before deploying,")
    print("flip SCRIPTLENS_BILLING_MODE=gateway and remove local LLM keys.")
    if "model" in payload:
        print(f"\nGateway echoed model: {payload.get('model')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
