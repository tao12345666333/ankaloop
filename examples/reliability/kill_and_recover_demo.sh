#!/usr/bin/env bash
# kill_and_recover_demo.sh -- reproducible "Schrödinger's tool call" demo
#
# Kills a Python process between T1 (intent) and T2 (outcome), then runs
# the boot-time recovery scan to show how a journal answers
# "did that tool actually execute?"  Requires only python3 (>=3.11).
#
# Usage:   bash kill_and_recover_demo.sh [workdir]
# Slides:  PyCon China 2026 -- "When the Agent Dies at 3 A.M."
set -euo pipefail
DEMO_DIR="${1:-$(mktemp -d /tmp/kill-recover-demo.XXXXXX)}"
HERE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$DEMO_DIR"

# --- Act 1: a worker that will be SIGKILLed mid-tool -------------------
cat > "$DEMO_DIR/worker.py" <<'PY'
import os, sys, signal
from pathlib import Path
sys.path.insert(0, os.environ["JOURNAL_LIB"])
from minimal_journal import MinimalJournal

journal = MinimalJournal(Path(sys.argv[1]))
journal.turn_started("turn-1", "deploy to prod")

with journal.operation("turn-1", "call_1", "send_email", {"to": "ops@x.io"}) as op:
    # Side effect starts here in real life.  We simulate "response never
    # arrived" by dying between the effect and its settlement:
    os.kill(os.getpid(), signal.SIGKILL)   # docker kill / OOM / power loss
PY

echo "== 1. starting worker (it will be SIGKILLed mid-tool) =="
JOURNAL_LIB="$HERE" python3 "$DEMO_DIR/worker.py" "$DEMO_DIR" || true

echo
echo "== 2. journal on disk after the crash =="
cat "$DEMO_DIR/journal.jsonl"

echo
echo "== 3. boot-time recovery scan (what a restart actually sees) =="
JOURNAL_LIB="$HERE" python3 - "$DEMO_DIR" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, __import__("os").environ["JOURNAL_LIB"])
from minimal_journal import MinimalJournal
for d in MinimalJournal(Path(sys.argv[1])).resolve():
    marker = "OK " if d.status == "completed" else "!! "
    print(f"  {marker}{d.operation_id:16} {d.tool_name:12} "
          f"status={d.status:14} recovery={d.recovery_mode}")
PY

echo
echo "=> send_email MAY have executed. Fail closed: never auto-retry;"
echo "   surface it with the original arguments and let a human judge."
