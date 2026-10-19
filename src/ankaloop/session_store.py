"""Safe JSON persistence for AnkaLoop sessions.

Recovery contract for recreated serving processes (supervisor restart,
sandbox pause/resume, snapshot restore): the store is the source of truth.
A process that finds its in-memory revision behind — or otherwise diverged
from — the persisted revision must adopt the latest persisted state, never
an older caller-supplied snapshot. Turn commits are append-only, so a stale
commit is rebased onto the persisted state and retried once; a destructive
operation such as a session reset still surfaces `SessionConflictError`.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .session_state import InvalidSessionStateError, SessionState

SESSION_SCHEMA_VERSION = 2
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
_TIMELINE_FIELDS = {
    "attempt",
    "cached_input_tokens",
    "context_tokens",
    "context_window",
    "delay_seconds",
    "duration_ms",
    "error_kind",
    "input_tokens",
    "input_limit",
    "model",
    "output_reserve",
    "output_tokens",
    "partial_output",
    "priority",
    "result_length",
    "settled",
    "status_code",
    "step",
    "success",
    "task_id",
    "tool_id",
    "tool_name",
    "total_tokens",
    "turn_id",
    "turn_status",
    "usage_from_api",
}


class SessionStoreError(Exception):
    """Base error for session persistence."""


class InvalidSessionIdError(SessionStoreError, ValueError):
    """Raised when a session ID is unsafe for use as a file name."""


class SessionLoadError(SessionStoreError):
    """Raised when persisted session data cannot be loaded safely."""


class SessionSaveError(SessionStoreError):
    """Raised when persisted session data cannot be saved atomically."""


class SessionConflictError(SessionSaveError):
    """Raised when another live owner committed the session first."""


def sanitize_timeline_data(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only non-content metadata approved for durable timeline records."""
    return {
        key: value
        for key, value in data.items()
        if key in _TIMELINE_FIELDS and isinstance(value, (str, int, float, bool, type(None)))
    }


class SessionTimelineStore:
    """Bounded append-only JSONL timeline for one session."""

    def __init__(self, root: Path, session_id: str, *, max_events: int = 2000):
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.root = root.expanduser()
        self.session_id = validate_session_id(session_id)
        self.max_events = max_events
        self.path = self.root / f"{self.session_id}.timeline.jsonl"
        self.lock_path = self.root / f".{self.session_id}.timeline.lock"
        self._lock = threading.RLock()

    def append(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Append one sanitized event and enforce bounded retention."""
        event = {
            "id": uuid4().hex,
            "type": str(event_type),
            "timestamp": timestamp or datetime.now().isoformat(),
            "session_id": self.session_id,
            "data": sanitize_timeline_data(data or {}),
        }
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+b") as lock_handle, _exclusive_file_lock(lock_handle):
                event_count = len(self._read_unlocked())
                fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                event_count += 1
                if event_count > self.max_events:
                    retained_count = max(1, int(self.max_events * 0.9))
                    retained = self._read_unlocked()[-retained_count:]
                    self._write_unlocked(retained)
        return event

    def read(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return the newest retained events in chronological order."""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            if not self.path.exists():
                return []
            with self.lock_path.open("a+b") as lock_handle, _exclusive_file_lock(lock_handle):
                return self._read_unlocked()[-limit:]

    def delete(self) -> None:
        """Delete this session's durable timeline."""
        with self._lock:
            if not self.root.exists():
                return
            with self.lock_path.open("a+b") as lock_handle, _exclusive_file_lock(lock_handle):
                self.path.unlink(missing_ok=True)

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _write_unlocked(self, events: list[dict[str, Any]]) -> None:
        fd, temp_name = tempfile.mkstemp(
            dir=self.root,
            prefix=f".{self.session_id}.timeline.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            with suppress(OSError):
                os.unlink(temp_name)
            raise


def validate_session_id(session_id: str) -> str:
    """Validate and return a session ID safe for file-backed persistence."""
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.fullmatch(session_id) or ".." in session_id:
        raise InvalidSessionIdError("Session ID must be 1-128 safe filename characters and cannot contain '..'")
    return session_id


def redact_sensitive_data(value: Any) -> Any:
    """Return a JSON-compatible copy with common secret fields redacted."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in _SENSITIVE_KEYS else redact_sensitive_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    return value


@contextmanager
def _exclusive_file_lock(handle: Any):
    """Lock one session lock file across processes on Unix and Windows."""
    if os.name == "nt":
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SessionStore:
    """Versioned JSON session store using atomic replacement."""

    def __init__(self, root: Path, session_id: str):
        self.root = root.expanduser()
        self.session_id = validate_session_id(session_id)
        self.path = self.root / f"{self.session_id}.json"
        self.lock_path = self.root / f".{self.session_id}.lock"
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any] | None:
        """Load a session, returning ``None`` only when it does not exist."""
        with self._lock:
            if not self.path.exists():
                return None
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SessionLoadError(f"Could not load session {self.session_id}: {exc}") from exc

            if not isinstance(data, dict):
                raise SessionLoadError(f"Session {self.session_id} must contain a JSON object")
            version = data.get("schema_version")
            if version != SESSION_SCHEMA_VERSION or type(version) is not int:
                raise SessionLoadError(f"Unsupported session schema version {version!r} for {self.session_id}")
            try:
                state = SessionState.from_snapshot(data, self.session_id)
            except (InvalidSessionStateError, TypeError, ValueError) as exc:
                raise SessionLoadError(f"Invalid session state for {self.session_id}: {exc}") from exc
            return {
                **state.to_snapshot(),
                "schema_version": SESSION_SCHEMA_VERSION,
                "session_id": self.session_id,
            }

    def read_revision(self) -> int:
        """Return the persisted revision, or 0 when the session does not exist.

        Writes replace the file atomically, so an unlocked read observes a
        complete old or new file; serving processes use this cheap probe to
        detect that the store moved past their in-memory revision.
        """
        with self._lock:
            if not self.path.exists():
                return 0
            return self._read_current_revision()

    def save(self, data: dict[str, Any], *, expected_revision: int | None = None) -> int:
        """Atomically save a session and reject stale owner revisions.

        Callers that own a loaded session should pass its current revision.
        The optional form remains useful for one-shot import and test callers.
        """
        with self._lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with self.lock_path.open("a+b") as lock_handle, _exclusive_file_lock(lock_handle):
                    return self._save_locked(data, expected_revision)
            except SessionConflictError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise SessionSaveError(f"Could not save session {self.session_id}: {exc}") from exc

    def _save_locked(self, data: dict[str, Any], expected_revision: int | None) -> int:
        """Write the next revision while the inter-process lock is held."""
        current_revision = self._read_current_revision()
        if expected_revision is not None and current_revision != expected_revision:
            raise SessionConflictError(
                f"Session {self.session_id} changed from revision {expected_revision} to {current_revision}"
            )
        next_revision = current_revision + 1
        payload = redact_sensitive_data(
            {
                **data,
                "revision": next_revision,
                "schema_version": SESSION_SCHEMA_VERSION,
                "session_id": self.session_id,
            }
        )
        fd, temp_name = tempfile.mkstemp(
            dir=self.root,
            prefix=f".{self.session_id}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            with suppress(OSError):
                os.unlink(temp_name)
            raise
        return next_revision

    def _read_current_revision(self) -> int:
        """Read the on-disk revision while the cross-process lock is held."""
        if not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            revision = data.get("revision", 0)
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionSaveError(f"Could not inspect session {self.session_id}: {exc}") from exc
        if type(revision) is not int or revision < 0:
            raise SessionSaveError(f"Session {self.session_id} has an invalid revision")
        return revision

    def delete(self) -> None:
        """Delete the persisted session if present."""
        with self._lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with self.lock_path.open("a+b") as lock_handle, _exclusive_file_lock(lock_handle):
                    self.path.unlink(missing_ok=True)
            except OSError as exc:
                raise SessionStoreError(f"Could not delete session {self.session_id}: {exc}") from exc
