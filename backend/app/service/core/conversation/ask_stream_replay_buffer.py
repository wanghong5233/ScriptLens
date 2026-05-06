"""Replay buffer for session ask SSE streams (memory + optional Redis)."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Dict, Iterator, List, Optional, Tuple

try:
    import redis
except Exception:  # pragma: no cover - optional dependency fallback
    redis = None  # type: ignore[assignment]

try:
    from core.config import settings as _settings
except Exception:  # pragma: no cover - optional dependency fallback
    _settings = None


def _cfg(name: str, default):
    if _settings is not None and hasattr(_settings, name):
        value = getattr(_settings, name)
        if value is not None:
            return value
    return default


def _cfg_bool(name: str, default: bool) -> bool:
    value = _cfg(name, default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(_cfg(name, default))
    except Exception:
        return int(default)

@dataclass
class AskStreamEvent:
    """Cached SSE frame entry."""

    seq: int
    frame: str
    timestamp: float


@dataclass
class AskStreamRun:
    """Replay state for one ask run."""

    run_id: str
    session_id: str
    user_id: int
    created_at: float
    updated_at: float
    completed: bool = False
    events: List[AskStreamEvent] = field(default_factory=list)
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.RLock())
    )


class AskStreamReplayBuffer:
    """Thread-safe replay cache for ask stream events."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 600,
        max_runs: int = 256,
        max_events_per_run: int = 4096,
        redis_scan_limit: int = 3000,
        enable_redis: bool = False,
    ) -> None:
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._max_runs = max(32, int(max_runs))
        self._max_events_per_run = max(256, int(max_events_per_run))
        self._redis_scan_limit = max(256, int(redis_scan_limit))
        self._runs: Dict[str, AskStreamRun] = {}
        self._lock = threading.RLock()
        self._redis = self._create_redis_client() if enable_redis else None
        self._redis_unavailable_until = 0.0

    def _create_redis_client(self):
        if redis is None:
            return None
        host = str(_cfg("REDIS_HOST", "redis"))
        port = _cfg_int("REDIS_PORT", 6379)
        db = _cfg_int("REDIS_DB", 0)
        try:
            return redis.Redis(
                host=host,
                port=port,
                db=db,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
                retry_on_timeout=False,
            )
        except Exception:
            return None

    def _redis_meta_key(self, run_id: str) -> str:
        return f"sm:ask_replay:run:{run_id}:meta"

    def _redis_events_key(self, run_id: str) -> str:
        return f"sm:ask_replay:run:{run_id}:events"

    def _redis_has_run(self, run_id: str) -> bool:
        if not self._redis_ready():
            return False
        try:
            assert self._redis is not None
            return bool(self._redis.exists(self._redis_meta_key(run_id)))
        except Exception:
            self._mark_redis_failure()
            return False

    def _redis_ready(self) -> bool:
        if self._redis is None:
            return False
        return time.time() >= self._redis_unavailable_until

    def _mark_redis_failure(self) -> None:
        # 避免每个 token 都触发一次 Redis 连接超时
        self._redis_unavailable_until = time.time() + 15

    def create_run(self, *, run_id: str, session_id: str, user_id: int) -> AskStreamRun:
        now = time.time()
        with self._lock:
            self._cleanup_locked(now)
            run = AskStreamRun(
                run_id=run_id,
                session_id=session_id,
                user_id=int(user_id),
                created_at=now,
                updated_at=now,
            )
            self._runs[run_id] = run
            # 容量保护：优先淘汰最老 run
            if len(self._runs) > self._max_runs:
                stale = sorted(self._runs.values(), key=lambda item: item.updated_at)
                for item in stale[: max(0, len(self._runs) - self._max_runs)]:
                    self._runs.pop(item.run_id, None)
            self._persist_run_meta_redis(run)
            return run

    def append_event(self, *, run_id: str, seq: int, frame: str) -> None:
        run = self.get_run(run_id)
        if not run:
            return
        now = time.time()
        with run.condition:
            run.events.append(AskStreamEvent(seq=int(seq), frame=str(frame), timestamp=now))
            if len(run.events) > self._max_events_per_run:
                overflow = len(run.events) - self._max_events_per_run
                if overflow > 0:
                    run.events = run.events[overflow:]
            run.updated_at = now
            run.condition.notify_all()
        self._append_event_redis(run_id=run_id, seq=int(seq), frame=str(frame), updated_at=now)

    def mark_completed(self, run_id: str) -> None:
        run = self.get_run(run_id)
        if not run:
            return
        with run.condition:
            run.completed = True
            run.updated_at = time.time()
            run.condition.notify_all()
        self._mark_completed_redis(run_id)

    def get_run(self, run_id: str) -> Optional[AskStreamRun]:
        with self._lock:
            run = self._runs.get(run_id)
        if run:
            return run
        return self._load_run_from_redis(run_id)

    def stream_from(
        self,
        *,
        run_id: str,
        since_seq: int,
        wait_timeout_seconds: int = 45,
        poll_seconds: float = 0.3,
    ) -> Iterator[str]:
        if self._redis_ready():
            yield from self._stream_from_redis(
                run_id=run_id,
                since_seq=since_seq,
                wait_timeout_seconds=wait_timeout_seconds,
                poll_seconds=poll_seconds,
            )
            return
        run = self.get_run(run_id)
        if not run:
            return
        yield from self._stream_from_memory(
            run=run,
            since_seq=since_seq,
            wait_timeout_seconds=wait_timeout_seconds,
            poll_seconds=poll_seconds,
        )

    def _stream_from_memory(
        self,
        *,
        run: AskStreamRun,
        since_seq: int,
        wait_timeout_seconds: int,
        poll_seconds: float,
    ) -> Iterator[str]:
        cursor = int(since_seq)
        deadline = time.time() + max(5, int(wait_timeout_seconds))
        while True:
            with run.condition:
                ready = [event for event in run.events if event.seq > cursor]
                if not ready and not run.completed:
                    now = time.time()
                    if now >= deadline:
                        break
                    run.condition.wait(timeout=max(0.05, float(poll_seconds)))
                    continue
                completed = run.completed
            if ready:
                for event in ready:
                    cursor = max(cursor, event.seq)
                    yield event.frame
                deadline = time.time() + max(5, int(wait_timeout_seconds))
                continue
            if completed:
                break

    def _stream_from_redis(
        self,
        *,
        run_id: str,
        since_seq: int,
        wait_timeout_seconds: int,
        poll_seconds: float,
    ) -> Iterator[str]:
        if not self._redis_has_run(run_id):
            with self._lock:
                memory_run = self._runs.get(run_id)
            if memory_run:
                yield from self._stream_from_memory(
                    run=memory_run,
                    since_seq=since_seq,
                    wait_timeout_seconds=wait_timeout_seconds,
                    poll_seconds=poll_seconds,
                )
            return
        cursor = int(since_seq)
        deadline = time.time() + max(5, int(wait_timeout_seconds))
        while True:
            if not self._redis_ready():
                run = self.get_run(run_id)
                if run:
                    yield from self._stream_from_memory(
                        run=run,
                        since_seq=cursor,
                        wait_timeout_seconds=wait_timeout_seconds,
                        poll_seconds=poll_seconds,
                    )
                return
            ready = self._fetch_frames_from_redis(run_id=run_id, since_seq=cursor)
            if ready:
                for seq, frame in ready:
                    cursor = max(cursor, seq)
                    yield frame
                deadline = time.time() + max(5, int(wait_timeout_seconds))
                continue
            completed = self._is_completed_in_redis(run_id)
            if completed:
                break
            if time.time() >= deadline:
                break
            time.sleep(max(0.05, float(poll_seconds)))

    def _persist_run_meta_redis(self, run: AskStreamRun) -> None:
        if not self._redis_ready():
            return
        try:
            assert self._redis is not None
            meta_key = self._redis_meta_key(run.run_id)
            events_key = self._redis_events_key(run.run_id)
            pipe = self._redis.pipeline()
            pipe.hset(
                meta_key,
                mapping={
                    "run_id": run.run_id,
                    "session_id": run.session_id,
                    "user_id": int(run.user_id),
                    "created_at": float(run.created_at),
                    "updated_at": float(run.updated_at),
                    "completed": "0",
                },
            )
            pipe.expire(meta_key, self._ttl_seconds)
            pipe.delete(events_key)
            pipe.expire(events_key, self._ttl_seconds)
            pipe.execute()
        except Exception:
            self._mark_redis_failure()

    def _append_event_redis(self, *, run_id: str, seq: int, frame: str, updated_at: float) -> None:
        if not self._redis_ready():
            return
        try:
            assert self._redis is not None
            meta_key = self._redis_meta_key(run_id)
            events_key = self._redis_events_key(run_id)
            member = f"{int(seq)}|{frame}"
            pipe = self._redis.pipeline()
            pipe.zadd(events_key, {member: int(seq)})
            pipe.zcard(events_key)
            result = pipe.execute()
            count = int(result[-1] or 0)
            if count > self._max_events_per_run:
                overflow = count - self._max_events_per_run
                self._redis.zremrangebyrank(events_key, 0, overflow - 1)
            self._redis.hset(meta_key, mapping={"updated_at": float(updated_at)})
            self._redis.expire(meta_key, self._ttl_seconds)
            self._redis.expire(events_key, self._ttl_seconds)
        except Exception:
            self._mark_redis_failure()

    def _mark_completed_redis(self, run_id: str) -> None:
        if not self._redis_ready():
            return
        try:
            assert self._redis is not None
            meta_key = self._redis_meta_key(run_id)
            events_key = self._redis_events_key(run_id)
            now = time.time()
            pipe = self._redis.pipeline()
            pipe.hset(
                meta_key,
                mapping={
                    "completed": "1",
                    "updated_at": float(now),
                },
            )
            pipe.expire(meta_key, self._ttl_seconds)
            pipe.expire(events_key, self._ttl_seconds)
            pipe.execute()
        except Exception:
            self._mark_redis_failure()

    def _load_run_from_redis(self, run_id: str) -> Optional[AskStreamRun]:
        if not self._redis_ready():
            return None
        try:
            assert self._redis is not None
            meta = self._redis.hgetall(self._redis_meta_key(run_id))
            if not meta:
                return None
            return AskStreamRun(
                run_id=str(meta.get("run_id") or run_id),
                session_id=str(meta.get("session_id") or ""),
                user_id=int(meta.get("user_id") or 0),
                created_at=float(meta.get("created_at") or 0.0),
                updated_at=float(meta.get("updated_at") or 0.0),
                completed=str(meta.get("completed") or "0") == "1",
            )
        except Exception:
            self._mark_redis_failure()
            return None

    def _fetch_frames_from_redis(self, *, run_id: str, since_seq: int) -> List[Tuple[int, str]]:
        if not self._redis_ready():
            return []
        try:
            assert self._redis is not None
            raw = self._redis.zrangebyscore(
                self._redis_events_key(run_id),
                min=int(since_seq) + 1,
                max="+inf",
                start=0,
                num=512,
            )
            out: List[Tuple[int, str]] = []
            for item in raw or []:
                seq_part, sep, frame = str(item).partition("|")
                if not sep:
                    continue
                try:
                    seq = int(seq_part)
                except Exception:
                    continue
                out.append((seq, frame))
            return out
        except Exception:
            self._mark_redis_failure()
            return []

    def _is_completed_in_redis(self, run_id: str) -> bool:
        if not self._redis_ready():
            return False
        try:
            assert self._redis is not None
            completed = self._redis.hget(self._redis_meta_key(run_id), "completed")
            return str(completed or "0") == "1"
        except Exception:
            self._mark_redis_failure()
            return False

    def get_run_owner(self, run_id: str) -> Optional[Tuple[str, int]]:
        run = self.get_run(run_id)
        if not run:
            return None
        return str(run.session_id), int(run.user_id)

    def get_run_completed(self, run_id: str) -> Optional[bool]:
        run = self.get_run(run_id)
        if not run:
            return None
        return bool(run.completed)

    def stats_snapshot(self) -> Dict[str, object]:
        now = time.time()
        with self._lock:
            runs = list(self._runs.values())
        memory_runs = len(runs)
        memory_completed = sum(1 for run in runs if run.completed)
        memory_events = sum(len(run.events) for run in runs)
        oldest_age = (
            int(now - min((run.created_at for run in runs), default=now))
            if runs
            else 0
        )
        snapshot: Dict[str, object] = {
            "config": {
                "ttl_seconds": self._ttl_seconds,
                "max_runs": self._max_runs,
                "max_events_per_run": self._max_events_per_run,
                "redis_scan_limit": self._redis_scan_limit,
            },
            "memory": {
                "runs": memory_runs,
                "completed_runs": memory_completed,
                "active_runs": max(memory_runs - memory_completed, 0),
                "events": memory_events,
                "oldest_run_age_seconds": oldest_age,
            },
            "redis": {
                "enabled": self._redis is not None,
                "ready": self._redis_ready(),
                "backoff_seconds": max(
                    0,
                    int(self._redis_unavailable_until - time.time()),
                ),
            },
        }
        if self._redis_ready():
            snapshot["redis"].update(self._scan_redis_stats())
        return snapshot

    def _scan_redis_stats(self) -> Dict[str, object]:
        if not self._redis_ready():
            return {}
        try:
            assert self._redis is not None
            cursor = 0
            scanned = 0
            run_count = 0
            completed_count = 0
            scan_truncated = False
            while True:
                cursor, keys = self._redis.scan(
                    cursor=cursor,
                    match="sm:ask_replay:run:*:meta",
                    count=200,
                )
                key_list = list(keys or [])
                if key_list:
                    run_count += len(key_list)
                    pipe = self._redis.pipeline()
                    for key in key_list:
                        pipe.hget(key, "completed")
                    flags = pipe.execute()
                    completed_count += sum(
                        1 for flag in (flags or []) if str(flag or "0") == "1"
                    )
                scanned += len(key_list)
                if scanned >= self._redis_scan_limit:
                    scan_truncated = True
                    break
                if int(cursor) == 0:
                    break
            return {
                "runs": run_count,
                "completed_runs": completed_count,
                "active_runs": max(run_count - completed_count, 0),
                "scan_truncated": scan_truncated,
            }
        except Exception:
            self._mark_redis_failure()
            return {"ready": False, "error": "redis_scan_failed"}

    def _cleanup_locked(self, now: float) -> None:
        expired: List[str] = []
        for run_id, run in self._runs.items():
            if now - float(run.updated_at) > self._ttl_seconds:
                expired.append(run_id)
        for run_id in expired:
            self._runs.pop(run_id, None)


_ask_stream_replay_buffer: Optional[AskStreamReplayBuffer] = None


def get_ask_stream_replay_buffer() -> AskStreamReplayBuffer:
    """Return singleton replay buffer."""
    global _ask_stream_replay_buffer
    if _ask_stream_replay_buffer is None:
        _ask_stream_replay_buffer = AskStreamReplayBuffer(
            ttl_seconds=_cfg_int("SM_ASK_REPLAY_TTL_SECS", 600),
            max_runs=_cfg_int("SM_ASK_REPLAY_MAX_RUNS", 256),
            max_events_per_run=_cfg_int("SM_ASK_REPLAY_MAX_EVENTS_PER_RUN", 4096),
            redis_scan_limit=_cfg_int("SM_ASK_REPLAY_REDIS_SCAN_LIMIT", 3000),
            enable_redis=_cfg_bool("SM_ASK_REPLAY_REDIS_ENABLED", True),
        )
    return _ask_stream_replay_buffer
