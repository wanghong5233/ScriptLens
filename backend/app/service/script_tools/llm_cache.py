from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from utils.database import engine as default_engine


@dataclass
class CachedLLMResponse:
    raw: str
    parsed: Any
    provider: str
    model: str
    elapsed_ms: int


class LlmCache:
    """Persistent cache backed by scriptlens.llm_cache."""

    @staticmethod
    async def get(input_hash: str) -> CachedLLMResponse | None:
        return await asyncio.to_thread(LlmCache._get_sync, input_hash)

    @staticmethod
    def _get_sync(input_hash: str) -> CachedLLMResponse | None:
        with default_engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT output_raw, output_parsed, provider, model_ver, COALESCE(elapsed_ms, 0) AS elapsed_ms
                    FROM scriptlens.llm_cache
                    WHERE input_hash = :h
                    """
                ),
                {"h": input_hash},
            ).mappings().first()
            if not row:
                return None
            conn.execute(
                text(
                    """
                    UPDATE scriptlens.llm_cache
                    SET hit_count = hit_count + 1, last_hit_at = NOW()
                    WHERE input_hash = :h
                    """
                ),
                {"h": input_hash},
            )

        parsed = row["output_parsed"]
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except json.JSONDecodeError:
                parsed = {"raw": parsed}

        return CachedLLMResponse(
            raw=row["output_raw"],
            parsed=parsed,
            provider=row["provider"],
            model=row["model_ver"],
            elapsed_ms=int(row["elapsed_ms"] or 0),
        )

    @staticmethod
    async def put(
        input_hash: str,
        *,
        model_ver: str,
        prompt_ver: str,
        tag_set_ver: str,
        seed: int | None,
        raw: str,
        parsed: Any,
        provider: str,
        elapsed_ms: int,
    ) -> None:
        await asyncio.to_thread(
            LlmCache._put_sync,
            input_hash,
            model_ver,
            prompt_ver,
            tag_set_ver,
            seed,
            raw,
            parsed,
            provider,
            elapsed_ms,
        )

    @staticmethod
    def _put_sync(
        input_hash: str,
        model_ver: str,
        prompt_ver: str,
        tag_set_ver: str,
        seed: int | None,
        raw: str,
        parsed: Any,
        provider: str,
        elapsed_ms: int,
    ) -> None:
        parsed_payload = parsed
        if not isinstance(parsed_payload, (dict, list)):
            parsed_payload = {"value": parsed_payload}

        with default_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.llm_cache (
                        input_hash, model_ver, prompt_ver, tag_set_ver, seed,
                        output_raw, output_parsed, provider, elapsed_ms, hit_count,
                        created_at, last_hit_at
                    )
                    VALUES (
                        :input_hash, :model_ver, :prompt_ver, :tag_set_ver, :seed,
                        :output_raw, CAST(:output_parsed AS jsonb), :provider, :elapsed_ms,
                        0, NOW(), NOW()
                    )
                    ON CONFLICT (input_hash) DO UPDATE SET
                        model_ver = EXCLUDED.model_ver,
                        prompt_ver = EXCLUDED.prompt_ver,
                        tag_set_ver = EXCLUDED.tag_set_ver,
                        seed = EXCLUDED.seed,
                        output_raw = EXCLUDED.output_raw,
                        output_parsed = EXCLUDED.output_parsed,
                        provider = EXCLUDED.provider,
                        elapsed_ms = EXCLUDED.elapsed_ms,
                        last_hit_at = NOW()
                    """
                ),
                {
                    "input_hash": input_hash,
                    "model_ver": model_ver,
                    "prompt_ver": prompt_ver,
                    "tag_set_ver": tag_set_ver,
                    "seed": seed,
                    "output_raw": raw,
                    "output_parsed": json.dumps(parsed_payload, ensure_ascii=False),
                    "provider": provider,
                    "elapsed_ms": elapsed_ms,
                },
            )

    @staticmethod
    async def stats(model_ver: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(LlmCache._stats_sync, model_ver)

    @staticmethod
    def _stats_sync(model_ver: str | None = None) -> dict[str, Any]:
        where = ""
        params: dict[str, Any] = {}
        if model_ver:
            where = "WHERE model_ver = :model_ver"
            params["model_ver"] = model_ver
        with default_engine.connect() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT
                        COUNT(*) AS total,
                        COALESCE(SUM(hit_count), 0) AS total_hits,
                        COALESCE(AVG(elapsed_ms), 0) AS avg_elapsed_ms
                    FROM scriptlens.llm_cache
                    {where}
                    """
                ),
                params,
            ).mappings().first()
        return {
            "total": int(row["total"] if row else 0),
            "total_hits": int(row["total_hits"] if row else 0),
            "avg_elapsed_ms": float(row["avg_elapsed_ms"] if row else 0.0),
            "model_ver": model_ver,
        }
