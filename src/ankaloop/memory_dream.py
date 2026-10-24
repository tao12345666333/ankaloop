"""Background memory consolidation for long-running AnkaLoop sessions."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_config
from .llm import create_llm_client
from .memory import MemoryStore, get_memory_manager

logger = logging.getLogger(__name__)

DREAM_MIN_INTERVAL_SECONDS = 4 * 60 * 60
DREAM_MIN_NEW_EVENTS = 10
DREAM_MAX_EVENTS = 80
DREAM_LOCK_STALE_SECONDS = 60 * 60

DREAM_PROMPT = """\
You are consolidating AnkaLoop memory for a long-running Telegram assistant.

Update the existing project MEMORY.md using the recent session events below.

Keep:
- Durable user preferences and expectations.
- Stable project facts, decisions, constraints, and rationale.
- Repeated plans, ongoing goals, blockers, and follow-ups that are still useful.

Discard:
- Greetings, transient task chatter, duplicate status updates, and raw tool noise.
- Completed-work logs that are only useful as transcript history.
- Relative dates unless you rewrite them as absolute dates from the event timestamps.

Return the complete updated MEMORY.md as concise Markdown with headings.
If nothing durable should be changed, reply exactly: NO_REPLY.
"""


@dataclass
class DreamRunResult:
    """Result of one memory dream pass."""

    ran: bool
    updated: bool
    reason: str


class MemoryDreamer:
    """Consolidate recent episodic memory into project long-term memory."""

    def __init__(
        self,
        project_root: Path,
        *,
        min_interval_seconds: int = DREAM_MIN_INTERVAL_SECONDS,
        min_new_events: int = DREAM_MIN_NEW_EVENTS,
        client: Any | None = None,
        model: str | None = None,
    ) -> None:
        self.project_root = project_root
        self.min_interval_seconds = min_interval_seconds
        self.min_new_events = min_new_events
        self._client = client
        self._model = model
        self.memory_dir = MemoryStore.get_project_memory_dir(project_root)
        self.state_file = self.memory_dir / "dream_state.json"
        self.lock_file = self.memory_dir / "dream.lock"
        self._lock_token: str | None = None

    def run_once(self) -> DreamRunResult:
        """Run one gated consolidation pass."""
        state = self._load_state()
        last_event_id = int(state.get("last_event_id", 0))
        batch = self._pending_events(last_event_id)
        if not batch:
            return DreamRunResult(ran=False, updated=False, reason="no_events")

        if len(batch) < self.min_new_events:
            return DreamRunResult(ran=False, updated=False, reason="not_enough_new_events")

        last_attempt_at = max(
            float(state.get("last_run_at", 0)),
            float(state.get("last_attempt_at", 0)),
        )
        if time.time() - last_attempt_at < self.min_interval_seconds:
            return DreamRunResult(ran=False, updated=False, reason="too_soon")

        if not self._acquire_lock():
            return DreamRunResult(ran=False, updated=False, reason="locked")

        try:
            pending_memory = state.get("pending_memory")
            if isinstance(pending_memory, str) and pending_memory:
                pending_event_id = int(state.get("pending_event_id", last_event_id))
                manager = get_memory_manager(self.project_root)
                if not manager.write_long_term(pending_memory, scope="project"):
                    state["last_attempt_at"] = time.time()
                    self._save_state(state)
                    return DreamRunResult(ran=True, updated=False, reason="write_failed")
                self._save_state(
                    {
                        "last_run_at": time.time(),
                        "last_event_id": pending_event_id,
                    }
                )
                return DreamRunResult(ran=True, updated=True, reason="updated")

            manager = get_memory_manager(self.project_root)
            updated_any = False
            # Consolidate every unconsolidated event, batch by batch, so a backlog that
            # grew past one batch (long dream intervals, many concurrent sessions) is
            # never skipped: the cursor only advances over consolidated batches.
            while batch:
                batch_last_event_id = max(int(event.get("id", 0)) for event in batch)
                updated_memory = self._consolidate(batch)
                if updated_memory:
                    if not manager.write_long_term(updated_memory, scope="project"):
                        self._save_state(
                            {
                                **state,
                                "last_attempt_at": time.time(),
                                "pending_memory": updated_memory,
                                "pending_event_id": batch_last_event_id,
                            }
                        )
                        return DreamRunResult(ran=True, updated=updated_any, reason="write_failed")
                    self._save_state({"last_run_at": time.time(), "last_event_id": batch_last_event_id})
                    updated_any = True
                else:
                    self._save_state({"last_run_at": time.time(), "last_event_id": batch_last_event_id})
                batch = self._pending_events(batch_last_event_id)
            return DreamRunResult(ran=True, updated=updated_any, reason="updated" if updated_any else "no_reply")
        except Exception as e:
            logger.debug(f"Memory dream failed (non-critical): {e}")
            return DreamRunResult(ran=True, updated=False, reason="error")
        finally:
            self._release_lock()

    def _pending_events(self, after_id: int) -> list[dict[str, Any]]:
        """Fetch the next batch of unconsolidated events (id > ``after_id``), oldest first."""
        manager = get_memory_manager(self.project_root)
        try:
            return manager.project_store._sqlite.get_events_after(after_id, limit=DREAM_MAX_EVENTS)
        except Exception as e:
            logger.debug(f"Could not load dream events: {e}")
            return []

    def _consolidate(self, events: list[dict[str, Any]]) -> str:
        manager = get_memory_manager(self.project_root)
        existing = manager.read_long_term(scope="project").strip()
        event_text = self._format_events(events)
        client = self._client or self._make_client()
        model_override = {"model": self._model} if self._model else {}
        response = client.chat(
            messages=[
                {"role": "system", "content": DREAM_PROMPT},
                {
                    "role": "user",
                    "content": f"Existing MEMORY.md:\n\n{existing or '(empty)'}\n\nRecent events:\n\n{event_text}",
                },
            ],
            **model_override,
        )
        content = (response.content or "").strip()
        if not content or content == "NO_REPLY":
            return ""
        if "#" not in content:
            logger.debug("Memory dream rejected non-Markdown response")
            return ""
        return content

    @staticmethod
    def _format_events(events: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for event in events:
            content = str(event.get("content", "")).strip()
            if not content:
                continue
            lines.append(f"- [{event.get('timestamp', '')} session:{event.get('session_id', 'unknown')}] {content}")
        return "\n".join(lines)

    def _make_client(self) -> Any:
        cfg = load_config()
        return create_llm_client(cfg.chat)

    def _load_state(self) -> dict[str, Any]:
        try:
            if self.state_file.exists():
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
                return state if isinstance(state, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            logger.debug(f"Could not read memory dream state: {e}")
        return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _acquire_lock(self) -> bool:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        token = f"{time.time()} {uuid.uuid4().hex}"
        while True:
            try:
                with self.lock_file.open("x", encoding="utf-8") as f:
                    f.write(token)
            except FileExistsError:
                text = self._read_lock_text(self.lock_file)
                if text == token:
                    # A losing stealer moved our just-created lock aside and put
                    # it back; the file still carries our token, so it is ours.
                    self._lock_token = token
                    return True
                written = self._lock_written_at(text)
                if written is None or time.time() - written < DREAM_LOCK_STALE_SECONDS:
                    # Live (or unreadable) lock: the normal "locked" outcome, and
                    # the file is left untouched for its owner to release.
                    return False
                if not self._steal_stale_lock():
                    return False
                continue
            if self._read_lock_text(self.lock_file) != token:
                # A concurrent takeover moved our just-created lock away before
                # the token landed; only the takeover winner may keep the lock.
                continue
            self._lock_token = token
            return True

    def _steal_stale_lock(self) -> bool:
        """Atomically take over a stale lock file.

        Renaming the lock to a unique per-process name selects a single winner
        among concurrent takeover attempts, so two processes can never both
        conclude that the same stale lock is theirs to take over. The moved
        file's timestamp is re-checked after the rename; a lock that was
        replaced with a live one in the meantime is put back untouched.
        """
        stolen = self.lock_file.with_name(f"{self.lock_file.name}.{uuid.uuid4().hex}.stolen")
        try:
            os.rename(self.lock_file, stolen)
        except FileNotFoundError:
            return True  # the lock vanished while we raced for it; retry the create
        try:
            written = self._lock_written_at(self._read_lock_text(stolen))
            if written is None or time.time() - written < DREAM_LOCK_STALE_SECONDS:
                # A live lock was created between our staleness check and the
                # rename; put it back so its owner can release it.
                if not self.lock_file.exists():
                    os.replace(stolen, self.lock_file)
                return False
        finally:
            stolen.unlink(missing_ok=True)
        return True

    @staticmethod
    def _read_lock_text(path: Path) -> str | None:
        """Lock file text, or ``None`` when it cannot be read."""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    @staticmethod
    def _lock_written_at(text: str | None) -> float | None:
        """Timestamp recorded in lock file text, or ``None`` when unreadable."""
        if text is None:
            return None
        if not text.strip():
            return 0.0  # empty lock file is treated as maximally stale
        try:
            return float(text.split(maxsplit=1)[0])
        except ValueError:
            return None

    def _release_lock(self) -> None:
        token, self._lock_token = self._lock_token, None
        if not token:
            return
        try:
            current = self.lock_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            logger.debug(f"Could not read memory dream lock: {e}")
            return
        if current != token:
            # The lock file now belongs to another acquisition; leave it alone.
            return
        try:
            self.lock_file.unlink()
        except FileNotFoundError:
            return
        except OSError as e:
            logger.debug(f"Could not release memory dream lock: {e}")
