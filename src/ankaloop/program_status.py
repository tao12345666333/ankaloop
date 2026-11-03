"""Report AnkaLoop's activity to a terminal that understands OSC 7501 status.

Herdr (https://herdr.dev) - like a few other terminal hosts - builds its
"Agents" panel from the *Program Status Protocol*: an OSC 7501 escape sequence
whose body is ``key=value`` pairs joined by ``:`` and terminated by ST::

    ESC ] 7501 ; state=working:app=ankaloop ST

A program that never emits it is indistinguishable from a plain shell, so
AnkaLoop stays invisible in the panel. This module emits the sequence for the
interactive CLI.

Notes on the implementation:

- Everything is written to ``/dev/tty``, never to ``sys.stdout``: stdout is
  frequently redirected or captured (rich, pipes, tests) and would either lose
  the sequence or mix it into piped output. Without a controlling terminal the
  report is a silent no-op.
- Reporting must never break a session: every failure path (no tty, Windows,
  EIO/ENXIO on a closed pty, disabled via env) is swallowed.
- ``ANKALOOP_PROGRAM_STATUS`` set to ``0``/``false``/``no``/``off``/``disable``/
  ``disabled`` turns reporting off; it is evaluated once at import time.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Final, Literal

logger = logging.getLogger(__name__)

ST: Final = "\x1b\\"  # String Terminator (ESC \)
STATES: Final = frozenset({"idle", "working", "done", "blocked", "error", "clear"})
MAX_MESSAGE_BYTES: Final = 2048
# The proposal caps a whole report (ESC ] 7501 ; ... ST) at 4096 bytes.
MAX_SEQUENCE_BYTES: Final = 4096

_DISABLED_VALUES: Final = frozenset({"0", "false", "no", "off", "disable", "disabled"})
_STATUS_ENV: Final = "ANKALOOP_PROGRAM_STATUS"

# A decoded msg/title must not hold a control character (U+0000-U+001F, U+007F,
# U+0080-U+009F), not even a newline or a tab: each is meant to be a single
# line, and a strict terminal discards the whole report when its base64 decodes
# to one. These are replaced rather than escaped, so free text stays readable.
_CONTROL_CHARS: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")

State = Literal["idle", "working", "done", "blocked", "error", "clear"]


def _env_enabled() -> bool:
    """Return False when ``ANKALOOP_PROGRAM_STATUS`` explicitly opts out."""
    return os.environ.get(_STATUS_ENV, "").strip().lower() not in _DISABLED_VALUES


ENABLED: Final = _env_enabled()


def encode_message(msg: str) -> str:
    """Encode a human-readable message as a single-line base64 token.

    The body of an OSC sequence cannot contain ``:``, ``;`` or the ST
    terminator, so the message travels base64-encoded. Control characters
    (newlines and tabs from a traceback or an exception) are replaced before
    encoding: the proposal requires the decoded text to be a single line free
    of control characters, and a strict terminal drops the whole report
    otherwise. Whitespace is stripped from the encoded form so the report stays
    one token, and the UTF-8 input is truncated to ``MAX_MESSAGE_BYTES`` so a
    long message never bloats the terminal stream.
    """
    if not msg:
        return ""
    cleaned = _CONTROL_CHARS.sub(" ", msg)
    encoded = cleaned.encode("utf-8")[:MAX_MESSAGE_BYTES].decode("utf-8", errors="ignore")
    raw = base64.b64encode(encoded.encode("utf-8")).decode("ascii")
    return "".join(raw.split())


def clamp_progress(progress: int | float | None) -> int | None:
    """Clamp ``progress`` to an integer percentage in ``[0, 100]``."""
    if progress is None:
        return None
    try:
        value = int(progress)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, value))


def build_sequence(
    state: State,
    msg: str | None = None,
    progress: int | float | None = None,
) -> str:
    """Build the OSC 7501 sequence for ``state``.

    The body is ``state=<state>:app=ankaloop`` followed by ``:msg=<base64>``
    when ``msg`` is set and ``:progress=<0-100>`` when ``progress`` is set.
    ``state=clear`` carries the state alone.
    """
    if state not in STATES:
        raise ValueError(f"unknown program status state: {state!r}")

    if state == "clear":
        return f"\x1b]7501;state=clear{ST}"

    parts = [f"state={state}", "app=ankaloop"]
    if msg:
        encoded = encode_message(msg)
        if encoded:
            parts.append(f"msg={encoded}")
    progress_value = clamp_progress(progress)
    if progress_value is not None:
        parts.append(f"progress={progress_value}")
    sequence = "\x1b]7501;" + ":".join(parts) + ST
    if len(sequence.encode("ascii", errors="ignore")) > MAX_SEQUENCE_BYTES:
        # Only msg can grow a report; drop it rather than send a rejected one.
        logger.debug("Program status sequence too long, dropping the message")
        parts = [p for p in parts if not p.startswith("msg=")]
        return "\x1b]7501;" + ":".join(parts) + ST
    return sequence


def _write(sequence: str) -> bool:
    """Write ``sequence`` straight to the controlling terminal.

    Returns True when it was written. Never raises: any failure degrades to a
    silent no-op.
    """
    if not sequence:
        return False
    try:
        with open("/dev/tty", "w", encoding="ascii", errors="ignore") as tty:
            tty.write(sequence)
            tty.flush()
        return True
    except (OSError, ValueError):
        # No controlling terminal (non-tty, Windows, closed pty, permission).
        return False
    except Exception:  # pragma: no cover - defensive: status must never break a session
        logger.debug("Failed to report program status", exc_info=True)
        return False


def report(state: State, msg: str | None = None, progress: int | float | None = None) -> bool:
    """Report ``state`` to the terminal, returning True when it was written.

    A no-op returning False when reporting is disabled, when ``ANKALOOP_PROGRAM_STATUS``
    opts out, or when there is no terminal to write to. Never raises.
    """
    if not ENABLED:
        return False
    try:
        sequence = build_sequence(state, msg=msg, progress=progress)
    except (ValueError, TypeError):
        logger.debug("Invalid program status report", exc_info=True)
        return False
    return _write(sequence)


def clear() -> bool:
    """Remove AnkaLoop's status from the terminal (``state=clear``)."""
    return report("clear")
