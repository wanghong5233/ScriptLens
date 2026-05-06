"""Async run manager for Doc Studio tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import logging
import time
import uuid

logger = logging.getLogger(__name__)


@dataclass
class AsyncRunState:
    """State of an async run."""

    run_id: str
    workspace_id: str
    user_id: int
    status: str
    created_at: float
    updated_at: float
    run_dir: Path
    events: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    cancel_reason: Optional[str] = None
    next_sequence: int = 0
    # Snapshot captured before run execution. Used by re-edit restore flow.
    before_snapshot: Optional[Dict[str, Any]] = field(default=None)
    pending_interaction: Optional[Dict[str, Any]] = field(default=None)

    def snapshot(self) -> Dict[str, Any]:
        """Return a serializable snapshot."""

        return {
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
            "cancel_reason": self.cancel_reason,
            "events_count": len(self.events),
            "next_sequence": self.next_sequence,
            "pending_interaction": self.pending_interaction,
        }


class AsyncRunManager:
    """Manage async runs for Doc Studio."""

    def __init__(self) -> None:
        self._runs: Dict[str, AsyncRunState] = {}
        self._interaction_waiters: Dict[str, Dict[str, asyncio.Future]] = {}

    def create_run(
        self,
        *,
        run_id: str,
        workspace_id: str,
        user_id: int,
        run_dir: Path,
    ) -> AsyncRunState:
        """Create a new run and persist its initial state."""

        run_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        state = AsyncRunState(
            run_id=run_id,
            workspace_id=workspace_id,
            user_id=user_id,
            status="queued",
            created_at=now,
            updated_at=now,
            run_dir=run_dir,
        )
        self._runs[run_id] = state
        self._persist_status(state)
        return state

    def get_run(self, run_id: str) -> Optional[AsyncRunState]:
        """Get run state from memory."""

        return self._runs.get(run_id)

    def load_run(self, run_dir: Path, run_id: str) -> Optional[Dict[str, Any]]:
        """Load run snapshot from disk."""

        status_path = run_dir / f"{run_id}.json"
        if not status_path.exists():
            return None
        try:
            return json.loads(status_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load async run snapshot: %s", exc)
            return None

    def update_status(self, run_id: str, status: str) -> None:
        """Update run status."""

        state = self._runs.get(run_id)
        if not state:
            return
        if state.status == "cancelled" and status != "cancelled":
            return
        state.status = status
        state.updated_at = time.time()
        self._persist_status(state)

    def append_event(self, run_id: str, event_type: str, data: Dict[str, Any]) -> None:
        """Append an event and persist it."""

        state = self._runs.get(run_id)
        if not state:
            return
        sequence = int(state.next_sequence)
        event_id = f"{run_id}:{sequence}"
        event = {
            "id": event_id,
            "sequence": sequence,
            "event": event_type,
            "timestamp": time.time(),
            "data": data,
        }
        state.events.append(event)
        state.next_sequence += 1
        state.updated_at = event["timestamp"]
        # delta 事件频率高，逐条落盘会显著放大 I/O 开销并拖慢流式响应。
        # 这里将其视为瞬时事件：保留内存广播，但不逐条持久化。
        if event_type != "delta":
            self._persist_event(state, event)
            self._persist_status(state)

    def set_result(self, run_id: str, result: Dict[str, Any]) -> None:
        """Mark run as succeeded and save result."""

        state = self._runs.get(run_id)
        if not state:
            return
        if state.status == "cancelled":
            return
        state.result = result
        state.status = "succeeded"
        state.updated_at = time.time()
        self.append_event(run_id, "result", {"result": result})

    def set_error(self, run_id: str, error: str) -> None:
        """Mark run as failed and save error."""

        state = self._runs.get(run_id)
        if not state:
            return
        if state.status == "cancelled":
            return
        state.error = error
        state.status = "failed"
        state.updated_at = time.time()
        self.append_event(run_id, "run_error", {"error": error})

    def cancel_run(self, run_id: str, reason: str = "cancelled_by_user") -> bool:
        """Cancel a running/queued run."""

        state = self._runs.get(run_id)
        if not state:
            return False
        if state.status in {"succeeded", "failed", "cancelled"}:
            return False
        state.status = "cancelled"
        state.cancel_reason = reason
        state.updated_at = time.time()
        state.pending_interaction = None
        waiters = self._interaction_waiters.pop(run_id, {})
        for future in waiters.values():
            if not future.done():
                future.set_result({"decision": "cancelled", "reason": reason})
        self.append_event(run_id, "cancelled", {"reason": reason})
        return True

    def begin_interaction(
        self,
        run_id: str,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Create an interaction request and switch run to waiting state."""

        state = self._runs.get(run_id)
        if not state:
            return None
        interaction_id = uuid.uuid4().hex
        now = time.time()
        safe_payload = dict(payload or {})
        safe_payload["interaction_id"] = interaction_id
        safe_payload["requested_at"] = now
        waiters = self._interaction_waiters.setdefault(run_id, {})
        loop = asyncio.get_running_loop()
        waiters[interaction_id] = loop.create_future()
        state.pending_interaction = safe_payload
        state.status = "awaiting_user_interaction"
        state.updated_at = now
        self._persist_status(state)
        return safe_payload

    async def wait_for_interaction(
        self,
        run_id: str,
        interaction_id: str,
        timeout_seconds: int = 900,
    ) -> Dict[str, Any]:
        """Wait for user decision on an interaction request."""

        state = self._runs.get(run_id)
        if not state:
            return {"decision": "missing_run", "reason": "run_not_found"}

        waiters = self._interaction_waiters.get(run_id) or {}
        future = waiters.get(interaction_id)
        if not future:
            return {"decision": "expired", "reason": "interaction_not_found"}

        try:
            result = await asyncio.wait_for(future, timeout=max(1, int(timeout_seconds)))
        except asyncio.TimeoutError:
            waiters.pop(interaction_id, None)
            state.pending_interaction = None
            if state.status == "awaiting_user_interaction":
                state.status = "running"
            state.updated_at = time.time()
            self._persist_status(state)
            return {"decision": "timeout", "reason": "interaction_timeout"}
        finally:
            if run_id in self._interaction_waiters and interaction_id in self._interaction_waiters[run_id]:
                self._interaction_waiters[run_id].pop(interaction_id, None)
                if not self._interaction_waiters[run_id]:
                    self._interaction_waiters.pop(run_id, None)

        state.pending_interaction = None
        if state.status == "awaiting_user_interaction":
            state.status = "running"
        state.updated_at = time.time()
        self._persist_status(state)
        return dict(result or {})

    def resolve_interaction(
        self,
        run_id: str,
        interaction_id: str,
        decision: str,
        note: Optional[str] = None,
    ) -> bool:
        """Resolve a pending interaction by user decision."""

        state = self._runs.get(run_id)
        if not state:
            return False
        waiters = self._interaction_waiters.get(run_id) or {}
        future = waiters.get(interaction_id)
        if not future:
            return False
        payload = {
            "decision": str(decision or "").strip().lower() or "reject",
            "note": str(note or "").strip(),
            "resolved_at": time.time(),
        }
        if not future.done():
            future.set_result(payload)
        return True

    # Backward-compatible wrappers (remove after callers are migrated).
    def begin_confirmation(self, run_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self.begin_interaction(run_id, payload)

    async def wait_for_confirmation(
        self,
        run_id: str,
        confirmation_id: str,
        timeout_seconds: int = 900,
    ) -> Dict[str, Any]:
        return await self.wait_for_interaction(run_id, confirmation_id, timeout_seconds=timeout_seconds)

    def resolve_confirmation(
        self,
        run_id: str,
        confirmation_id: str,
        decision: str,
        note: Optional[str] = None,
    ) -> bool:
        return self.resolve_interaction(run_id, confirmation_id, decision=decision, note=note)

    def save_before_snapshot(self, run_id: str, snapshot: Dict[str, Any]) -> None:
        """Persist a workspace file snapshot taken before agent execution.

        Args:
            run_id: The async run ID.
            snapshot: Dict with key ``files`` mapping relative path → text content.
        """
        state = self._runs.get(run_id)
        if not state:
            return
        state.before_snapshot = snapshot
        snapshot_path = state.run_dir / f"{run_id}.before.json"
        try:
            snapshot_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to persist before_snapshot for run %s: %s", run_id, exc)

    def get_before_snapshot(self, run_id: str, run_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
        """Load before_snapshot from memory or disk.

        Args:
            run_id: The async run ID.
            run_dir: Directory where run artefacts are stored.

        Returns:
            Snapshot dict, or ``None`` if not found.
        """
        state = self._runs.get(run_id)
        if state and state.before_snapshot:
            return state.before_snapshot
        search_dir = (state.run_dir if state else None) or run_dir
        if not search_dir:
            return None
        snapshot_path = search_dir / f"{run_id}.before.json"
        if not snapshot_path.exists():
            return None
        try:
            return json.loads(snapshot_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load before_snapshot for run %s: %s", run_id, exc)
            return None

    def is_cancelled(self, run_id: str) -> bool:
        """Check whether a run has been cancelled."""

        state = self._runs.get(run_id)
        if not state:
            return False
        return state.status == "cancelled"

    def list_events(self, run_id: str) -> List[Dict[str, Any]]:
        """Return current events list."""

        state = self._runs.get(run_id)
        if not state:
            return []
        return list(state.events)

    def _persist_status(self, state: AsyncRunState) -> None:
        status_path = state.run_dir / f"{state.run_id}.json"
        status_path.write_text(
            json.dumps(state.snapshot(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def _persist_event(self, state: AsyncRunState, event: Dict[str, Any]) -> None:
        events_path = state.run_dir / f"{state.run_id}.events.jsonl"
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str))
            fh.write("\n")


_async_run_manager: Optional[AsyncRunManager] = None


def get_async_run_manager() -> AsyncRunManager:
    """Get global async run manager instance."""

    global _async_run_manager
    if _async_run_manager is None:
        _async_run_manager = AsyncRunManager()
    return _async_run_manager
