"""Durable tool-boundary journal for crash detection in AnkaLoop turns.

Inspired by the Maka/pi tool-boundary protocol: before a tool with side
effects executes, a durable *intent* is committed (T1 analog); after its
result is appended to the conversation, a durable *outcome* is committed
(T2 analog).  A crash between the two leaves an open operation whose side
effect may have happened -- recovery treats it as indeterminate and never
auto-retries.  This module intentionally records evidence only: the
canonical session snapshot remains the whole-turn commit, and the journal
answers "what was in flight when the process died?".

The journal is an append-only JSONL file next to the session snapshot
(``<session-id>.journal.jsonl``).  Trimming only ever cuts at
``turn_started`` boundaries so an open operation can never be orphaned
from its intent and no false crash evidence can be manufactured.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .session_store import _exclusive_file_lock, validate_session_id

logger = logging.getLogger(__name__)

JOURNAL_PROTOCOL = "ankaloop.tool-journal.v1"
"""Protocol marker written with every ``turn_started`` event."""

REPLAY_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "grep",
        "session_search",
        "think",
        "todo",
    }
)
"""Built-in tools whose re-execution has no world side effects.

Everything else (including MCP tools with unknown semantics) defaults to
``never_auto_retry`` because an unknown tool may mutate the world.
"""

TERMINAL_TURN_EVENTS = frozenset({"turn_committed", "turn_failed", "turn_cancelled"})

# Event kinds written by the runtime.
TURN_STARTED = "turn_started"
TOOL_INTENT = "tool_intent"
TOOL_OUTCOME = "tool_outcome"
TURN_COMMITTED = "turn_committed"
TURN_FAILED = "turn_failed"
TURN_CANCELLED = "turn_cancelled"
RECOVERY_ACKNOWLEDGED = "recovery_acknowledged"
"""Operator acknowledgement of reviewed interrupted turns / open operations."""


class ToolJournalError(RuntimeError):
    """Raised when a journal write fails at a boundary that gates execution."""


def canonical_args_hash(tool_name: str, arguments: Any) -> str:
    """Return a stable hash binding a tool call to its canonical arguments."""
    payload = json.dumps(
        {"tool": tool_name, "args": arguments},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_recovery_mode(tool_name: str) -> str:
    """Return the conservative replay classification for one tool."""
    return "replay_safe" if tool_name in REPLAY_SAFE_TOOLS else "never_auto_retry"


@dataclass(frozen=True)
class ToolOperationDecision:
    """The fail-closed recovery decision for one journaled tool operation."""

    operation_id: str
    tool_name: str
    status: str
    """completed | indeterminate | acknowledged | aborted_unsettled | corruption"""
    reason: str
    turn_id: str | None = None
    tool_call_id: str | None = None
    recovery_mode: str | None = None
    settled: bool = False


@dataclass(frozen=True)
class InterruptedTurn:
    """A turn that started but never reached a terminal journal event."""

    turn_id: str
    terminal: bool
    """False means the process likely died mid-turn (no terminal event)."""
    open_operations: tuple[str, ...] = field(default_factory=tuple)
    acknowledged: bool = False
    """True once an operator explicitly acknowledged this interruption."""


@dataclass(frozen=True)
class JournalRecovery:
    """The resolved view of a journal used for crash detection."""

    decisions: tuple[ToolOperationDecision, ...]
    interrupted_turns: tuple[InterruptedTurn, ...]
    has_corruption: bool

    @property
    def requires_attention(self) -> bool:
        """Whether any open operation or interrupted turn needs inspection."""
        return (
            any(not turn.acknowledged for turn in self.interrupted_turns)
            or self.has_corruption
            or any(decision.status == "indeterminate" for decision in self.decisions)
        )

    def summary(self) -> dict[str, Any]:
        """Return a compact serializable summary for logs and APIs."""
        return {
            "interrupted_turns": [
                {
                    "turn_id": turn.turn_id,
                    "terminal": turn.terminal,
                    "acknowledged": turn.acknowledged,
                    "open_operations": list(turn.open_operations),
                }
                for turn in self.interrupted_turns
            ],
            "open_operations": [
                {
                    "operation_id": d.operation_id,
                    "tool_name": d.tool_name,
                    "status": d.status,
                    "reason": d.reason,
                    "recovery_mode": d.recovery_mode,
                }
                for d in self.decisions
                if d.status != "completed"
            ],
            "has_corruption": self.has_corruption,
        }


class ToolJournal:
    """Append-only, fsynced JSONL journal for one session's tool boundaries.

    Writes are serialized with a per-instance lock plus the same
    cross-process file lock convention as the session store.  ``enabled``
    is false for ephemeral agents, turning every method into a no-op so
    call sites need no conditional logic.
    """

    def __init__(self, root: Path, session_id: str, *, enabled: bool = True, max_events: int = 2000):
        self.root = root.expanduser()
        self.session_id = validate_session_id(session_id)
        self.enabled = enabled
        self.max_events = max_events
        self.path = self.root / f"{self.session_id}.journal.jsonl"
        self.lock_path = self.root / f".{self.session_id}.journal.lock"
        self._lock = threading.RLock()

    # -- Event constructors -------------------------------------------------

    def turn_started(self, turn_id: str, prompt: str) -> dict[str, Any]:
        """Record the durable start of a turn (protocol marker event)."""
        return self._append(
            {
                "kind": TURN_STARTED,
                "protocol": JOURNAL_PROTOCOL,
                "turn_id": turn_id,
                "prompt": prompt,
            }
        )

    def tool_intent(
        self,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: Any,
    ) -> dict[str, Any]:
        """Record the T1 boundary: preflight passed, execution is imminent.

        This write is durable *before* the tool runs.  Its failure must
        gate execution, so it raises :class:`ToolJournalError`.
        """
        return self._append(
            {
                "kind": TOOL_INTENT,
                "turn_id": turn_id,
                "operation_id": f"{turn_id}:{tool_call_id}",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "canonical_args_hash": canonical_args_hash(tool_name, arguments),
                "recovery_mode": classify_recovery_mode(tool_name),
            }
        )

    def tool_outcome(
        self,
        turn_id: str,
        tool_call_id: str,
        *,
        success: bool,
        duration_ms: float | None = None,
        error: str | None = None,
        args_hash: str | None = None,
    ) -> dict[str, Any]:
        """Record the T2 boundary: the result is part of the conversation.

        ``args_hash`` must be the intent's ``canonical_args_hash``; a
        mismatch between the two is corruption, because the outcome cannot
        belong to that intent's execution.
        """
        event: dict[str, Any] = {
            "kind": TOOL_OUTCOME,
            "turn_id": turn_id,
            "operation_id": f"{turn_id}:{tool_call_id}",
            "tool_call_id": tool_call_id,
            "success": success,
        }
        if args_hash is not None:
            event["canonical_args_hash"] = args_hash
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        if error is not None:
            event["error"] = error[:500]
        return self._append(event)

    def turn_committed(self, turn_id: str, revision: int) -> dict[str, Any]:
        """Record that the turn's whole-state commit succeeded."""
        return self._append({"kind": TURN_COMMITTED, "turn_id": turn_id, "revision": revision})

    def turn_failed(self, turn_id: str, error: str | None = None) -> dict[str, Any]:
        """Record that the turn ended without committing (cancelled/failed)."""
        event: dict[str, Any] = {"kind": TURN_FAILED, "turn_id": turn_id}
        if error is not None:
            event["error"] = str(error)[:500]
        return self._append(event)

    def turn_cancelled(self, turn_id: str) -> dict[str, Any]:
        """Record an explicit cancellation terminal event."""
        return self._append({"kind": TURN_CANCELLED, "turn_id": turn_id})

    # -- Reading ------------------------------------------------------------

    def read(self) -> list[dict[str, Any]]:
        """Return all retained journal events in chronological order."""
        if not self.enabled:
            return []
        with self._lock:
            if not self.path.exists():
                return []
            with self.lock_path.open("a+b") as lock_handle, _exclusive_file_lock(lock_handle):
                return _read_journal_unlocked(self.path)

    def resolve(self) -> JournalRecovery:
        """Scan retained events and produce the fail-closed decision table."""
        return resolve_journal(self.read())

    def mark_recovery_acknowledged(self) -> dict[str, Any]:
        """Durably acknowledge every open operation as reviewed by an operator.

        Fail-closed recovery never auto-retries, so open crash suspects
        would resurface on every startup without an explicit human
        decision.  This appends one ``recovery_acknowledged`` event naming
        the interrupted turns and open operations; the next ``resolve``
        reports them as ``acknowledged`` instead of ``indeterminate``.
        Anything opened *after* the acknowledgement is still a crash
        suspect.  The read and the append are not one atomic transaction;
        a concurrent operation missed here simply stays flagged, which is
        the safe direction.
        """
        with self._lock:
            recovery = self.resolve()
            turn_ids = sorted(turn.turn_id for turn in recovery.interrupted_turns)
            operation_ids = sorted(
                decision.operation_id for decision in recovery.decisions if decision.status == "indeterminate"
            )
            if turn_ids or operation_ids:
                self._append(
                    {
                        "kind": RECOVERY_ACKNOWLEDGED,
                        "turns": turn_ids,
                        "operations": operation_ids,
                    }
                )
            return {"turns": turn_ids, "operations": operation_ids}

    def delete(self) -> None:
        """Delete the durable journal for this session."""
        if not self.enabled:
            return
        with self._lock:
            if not self.root.exists():
                return
            with self.lock_path.open("a+b") as lock_handle, _exclusive_file_lock(lock_handle):
                self.path.unlink(missing_ok=True)

    # -- Internals ----------------------------------------------------------

    def _append(self, event: dict[str, Any]) -> dict[str, Any]:
        record = {
            **event,
            "ts": datetime.now().isoformat(),
        }
        if not self.enabled:
            return record
        with self._lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with self.lock_path.open("a+b") as lock_handle, _exclusive_file_lock(lock_handle):
                    events = _read_journal_unlocked(self.path)
                    events.append(record)
                    _write_journal_unlocked(self.path, self.session_id, _trim_events(events, self.max_events))
            except ToolJournalError:
                raise
            except OSError as exc:
                raise ToolJournalError(f"Tool journal write failed: {exc}") from exc
        return record


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync so the rename itself is durable.

    Without it a crash after ``os.replace`` can revert the journal to its
    pre-append state, silently dropping T1 intent evidence for a tool
    that already executed.  Filesystems that reject directory fsync fall
    back to OS-level rename guarantees.
    """
    with contextlib.suppress(OSError):
        dir_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _read_journal_unlocked(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _write_journal_unlocked(path: Path, session_id: str, events: Iterable[dict[str, Any]]) -> None:
    payload = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{session_id}.journal.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def _trim_events(events: list[dict[str, Any]], max_events: int) -> list[dict[str, Any]]:
    """Drop old events without ever splitting a turn.

    Cutting anywhere inside a turn can orphan an intent from its outcome
    (a manufactured false crash) or an outcome from its intent (a
    manufactured corruption), so the cut is clamped to the first
    ``turn_started`` boundary at or after the target drop point.  A cut
    at a turn boundary drops whole turns, so nothing can be manufactured;
    the newest turn is never a cut target, so an open operation can never
    be orphaned from its intent either.  When no safe boundary exists
    (one giant turn, or no ``turn_started`` at all), everything is kept:
    unbounded growth beats manufactured crash evidence.
    """
    if len(events) <= max_events:
        return events
    retained = max(1, int(max_events * 0.9))
    drop_until = len(events) - retained
    if drop_until <= 0:
        return events
    cut_candidates = [
        index for index, event in enumerate(events) if event.get("kind") == TURN_STARTED and index >= drop_until
    ]
    if not cut_candidates:
        return events
    return events[cut_candidates[0] :]


def resolve_journal(events: list[dict[str, Any]]) -> JournalRecovery:
    """Pure decision table over journal events (Maka-style, fail-closed).

    - intent + outcome -> completed
    - intent without outcome, turn has no terminal event -> indeterminate
      (crash suspected: the side effect may have happened), unless the
      operation was explicitly acknowledged -> acknowledged
    - intent without outcome, turn reached a terminal event ->
      aborted_unsettled (turn was closed knowingly, never auto-retried;
      recorded evidence, but not a crash suspect)
    - duplicated operation identity, orphaned outcome, or an outcome whose
      args hash differs from its intent -> corruption
    """
    intents: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    turn_terminal: dict[str, str] = {}
    turn_ids: set[str] = set()
    acknowledged_turns: set[str] = set()
    acknowledged_operations: set[str] = set()
    corruption_reasons: list[str] = []

    for event in events:
        kind = event.get("kind")
        if kind == TURN_STARTED:
            turn_ids.add(str(event.get("turn_id")))
            continue
        if kind in TERMINAL_TURN_EVENTS:
            turn_id = str(event.get("turn_id"))
            turn_terminal[turn_id] = str(kind)
            continue
        if kind == RECOVERY_ACKNOWLEDGED:
            turns = event.get("turns")
            operations = event.get("operations")
            if isinstance(turns, list):
                acknowledged_turns.update(str(turn_id) for turn_id in turns)
            if isinstance(operations, list):
                acknowledged_operations.update(str(operation_id) for operation_id in operations)
            continue
        if kind == TOOL_INTENT:
            operation_id = str(event.get("operation_id"))
            if operation_id in intents:
                corruption_reasons.append(f"duplicate intent for {operation_id}")
                continue
            intents[operation_id] = event
            continue
        if kind == TOOL_OUTCOME:
            operation_id = str(event.get("operation_id"))
            if operation_id in outcomes:
                corruption_reasons.append(f"duplicate outcome for {operation_id}")
                continue
            outcomes[operation_id] = event
            continue

    decisions: list[ToolOperationDecision] = []
    for operation_id, outcome in outcomes.items():
        intent = intents.get(operation_id)
        if intent is None:
            decisions.append(
                ToolOperationDecision(
                    operation_id=operation_id,
                    tool_name="unknown",
                    status="corruption",
                    reason="orphaned outcome without intent",
                    turn_id=outcome.get("turn_id"),
                )
            )
            continue
        intent_hash = intent.get("canonical_args_hash")
        outcome_hash = outcome.get("canonical_args_hash")
        if intent_hash and outcome_hash and intent_hash != outcome_hash:
            decisions.append(
                ToolOperationDecision(
                    operation_id=operation_id,
                    tool_name=str(intent.get("tool_name") or "unknown"),
                    status="corruption",
                    reason="outcome args hash differs from intent",
                    turn_id=intent.get("turn_id"),
                    tool_call_id=intent.get("tool_call_id"),
                )
            )
            continue
        decisions.append(
            ToolOperationDecision(
                operation_id=operation_id,
                tool_name=str(intent.get("tool_name") or "unknown"),
                status="completed",
                reason="intent settled by matching outcome",
                turn_id=intent.get("turn_id"),
                tool_call_id=intent.get("tool_call_id"),
                recovery_mode=intent.get("recovery_mode"),
                settled=True,
            )
        )

    open_operations_by_turn: dict[str, list[str]] = {}
    for operation_id, intent in intents.items():
        if operation_id in outcomes:
            continue
        turn_id = str(intent.get("turn_id"))
        open_operations_by_turn.setdefault(turn_id, []).append(operation_id)
        if turn_id in turn_terminal:
            status, reason = "aborted_unsettled", "turn ended while operation was open"
        elif operation_id in acknowledged_operations:
            status, reason = "acknowledged", "crash suspect acknowledged by operator"
        else:
            status, reason = "indeterminate", "crash suspected between intent and outcome"
        decisions.append(
            ToolOperationDecision(
                operation_id=operation_id,
                tool_name=str(intent.get("tool_name") or "unknown"),
                status=status,
                reason=reason,
                turn_id=turn_id,
                tool_call_id=intent.get("tool_call_id"),
                recovery_mode=intent.get("recovery_mode"),
            )
        )

    interrupted: list[InterruptedTurn] = []
    for turn_id in sorted(turn_ids):
        if turn_id not in turn_terminal:
            interrupted.append(
                InterruptedTurn(
                    turn_id=turn_id,
                    terminal=False,
                    open_operations=tuple(sorted(open_operations_by_turn.get(turn_id, []))),
                    acknowledged=turn_id in acknowledged_turns,
                )
            )

    if corruption_reasons:
        decisions.extend(
            ToolOperationDecision(
                operation_id="journal",
                tool_name="journal",
                status="corruption",
                reason=reason,
            )
            for reason in corruption_reasons
        )

    return JournalRecovery(
        decisions=tuple(decisions),
        interrupted_turns=tuple(interrupted),
        has_corruption=bool(corruption_reasons) or any(d.status == "corruption" for d in decisions),
    )
