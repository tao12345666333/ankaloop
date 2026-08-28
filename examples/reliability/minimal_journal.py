#!/usr/bin/env python3
"""minimal_journal.py -- the smallest durable tool journal that is still honest.

A dependency-free distillation of AnkaLoop's tool journal
(src/ankaloop/tool_journal.py).  It answers the question every agent
crash leaves behind: *did that tool call actually execute?*

Protocol (WAL-style, intent-before-effect):
  T1  tool_intent   -- durably written BEFORE the tool runs; a write
                       failure blocks execution (never run an
                       unjournaled side effect)
  T2  tool_outcome  -- written AFTER the result is applied (best
                       effort: the world already moved)
  crash between T1 and T2 => indeterminate: the side effect MAY have
  happened, so recovery must NEVER auto-retry.  Fail closed.

Drop-in usage:

    from minimal_journal import MinimalJournal

    journal = MinimalJournal(Path("/var/lib/myagent"))
    journal.turn_started("turn-1", "deploy prod")

    with journal.operation("turn-1", "call_1", "send_email", {...}) as op:
        result = send_email(...)          # your side-effecting tool
        op.settle(True)                   # T2, happy path

    # ... process dies here ...  on next boot:
    for d in journal.resolve():
        print(d.status, d.tool_name)      # completed | indeterminate

Run this file directly to watch a simulated crash + recovery scan.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

KIND_TURN_STARTED = "turn_started"
KIND_INTENT = "tool_intent"
KIND_OUTCOME = "tool_outcome"
KIND_TURN_DONE = "turn_committed"

# Tools whose re-execution cannot change the world.  Everything else
# (including every unknown/MCP tool) is never_auto_retry by default.
REPLAY_SAFE = frozenset({"read_file", "grep", "search", "think"})


@dataclass(frozen=True)
class Decision:
    """Fail-closed verdict for one journaled operation."""

    operation_id: str
    tool_name: str
    status: str  # completed | indeterminate
    recovery_mode: str  # replay_safe | never_auto_retry


class MinimalJournal:
    """Append-only, fsynced JSONL journal + recovery scanner.

    Each append rewrites the whole (trimmed) file through a temp file +
    os.replace, mirroring AnkaLoop's atomic-snapshot discipline at toy
    scale.  Writes are serialized by the interpreter lock of a single
    process; multi-process writers should add an fcntl lock the same
    way AnkaLoop's session store does.
    """

    def __init__(self, root: Path, max_events: int = 500) -> None:
        self.root = root
        self.path = root / "journal.jsonl"
        self.max_events = max_events
        self.root.mkdir(parents=True, exist_ok=True)

    # -- T1 / T2 boundaries -------------------------------------------------

    @contextmanager
    def operation(
        self,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Iterator[Operation]:
        """Wrap one side-effecting tool call in the T1/T2 protocol.

        T1 is written on entry; if that write fails the error propagates
        and the tool never runs.  If the body crashes before settle(),
        the intent stays open -- exactly what a real crash looks like.
        """
        op = Operation(self, turn_id, tool_call_id, tool_name, arguments)
        op._t1()
        try:
            yield op
        finally:
            pass  # unsettled intent is intentionally left open

    def turn_started(self, turn_id: str, prompt: str) -> None:
        self._append({"kind": KIND_TURN_STARTED, "turn_id": turn_id, "prompt": prompt})

    def turn_committed(self, turn_id: str) -> None:
        self._append({"kind": KIND_TURN_DONE, "turn_id": turn_id})

    # -- Recovery -----------------------------------------------------------

    def resolve(self) -> tuple[Decision, ...]:
        """Fail-closed decision table over the journal (see module docstring)."""
        intents: dict[str, dict[str, Any]] = {}
        outcomes: set[str] = set()
        for event in self._read():
            if event.get("kind") == KIND_INTENT:
                intents[event["operation_id"]] = event
            elif event.get("kind") == KIND_OUTCOME:
                outcomes.add(event["operation_id"])
        decisions = []
        for operation_id, intent in intents.items():
            settled = operation_id in outcomes
            tool_name = str(intent.get("tool_name", "unknown"))
            decisions.append(
                Decision(
                    operation_id=operation_id,
                    tool_name=tool_name,
                    status="completed" if settled else "indeterminate",
                    recovery_mode="replay_safe" if tool_name in REPLAY_SAFE else "never_auto_retry",
                )
            )
        return tuple(decisions)

    # -- Internals ----------------------------------------------------------

    def _append(self, event: dict[str, Any]) -> None:
        events = self._read()
        record = {**event, "ts": datetime.now().isoformat()}
        events.append(record)
        self._write(events[-self.max_events :])

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line is skipped, never trusted
            if isinstance(parsed, dict):
                events.append(parsed)
        return events

    def _write(self, events: list[dict[str, Any]]) -> None:
        payload = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".journal.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as h:
                h.write(payload)
                h.flush()
                os.fsync(h.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


@dataclass
class Operation:
    journal: MinimalJournal
    turn_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] | None
    settled: bool = False

    @property
    def operation_id(self) -> str:
        return f"{self.turn_id}:{self.tool_call_id}"

    def _t1(self) -> None:
        self.journal._append(
            {
                "kind": KIND_INTENT,
                "turn_id": self.turn_id,
                "operation_id": self.operation_id,
                "tool_call_id": self.tool_call_id,
                "tool_name": self.tool_name,
                "arguments": self.arguments,
            }
        )

    def settle(self, success: bool, error: str | None = None) -> None:
        """T2: record that the effect applied. Call exactly once."""
        if self.settled:
            return
        event: dict[str, Any] = {
            "kind": KIND_OUTCOME,
            "turn_id": self.turn_id,
            "operation_id": self.operation_id,
            "success": success,
        }
        if error is not None:
            event["error"] = str(error)[:500]
        self.journal._append(event)
        self.settled = True


# -- Crash demo: python3 minimal_journal.py ---------------------------------
if __name__ == "__main__":
    import shutil
    import sys

    demo_root = Path("/tmp/minimal-journal-demo")
    shutil.rmtree(demo_root, ignore_errors=True)
    journal = MinimalJournal(demo_root)

    print("== turn 1: one clean operation, one crashed mid-flight ==")
    journal.turn_started("turn-1", "deploy to prod")
    with journal.operation("turn-1", "call_1", "read_file", {"path": "/etc/hosts"}) as op:
        op.settle(success=True)  # happy path: T1 + T2
    with journal.operation("turn-1", "call_2", "send_email", {"to": "ops@x.io"}) as op:
        # NO settle(): simulate the process dying mid-tool -- the T1
        # intent is durable but T2 never lands.
        pass

    print("== boot-time recovery scan ==")
    verdicts = journal.resolve()
    for d in verdicts:
        marker = "OK " if d.status == "completed" else "!! "
        print(f"  {marker}{d.operation_id:16} {d.tool_name:12} status={d.status:14} recovery={d.recovery_mode}")
    indeterminate = [d for d in verdicts if d.status == "indeterminate"]
    print(f"\n  open operations: {len(indeterminate)}")
    if indeterminate:
        print("  -> send_email MAY have run. Do NOT auto-retry; ask a human or")
        print("     check the mail server log before deciding.  This is")
        print("     fail-closed recovery: 'probably did not run' is not a")
        print("     reason to run it again.")
        sys.exit(1)
