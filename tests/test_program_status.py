"""Tests for the OSC 7501 program status reporting module."""

from __future__ import annotations

import base64
import importlib

import pytest

from ankaloop import program_status
from ankaloop.program_status import (
    MAX_MESSAGE_BYTES,
    MAX_SEQUENCE_BYTES,
    build_sequence,
    clamp_progress,
    encode_message,
    report,
)

ST = "\x1b\\"


# --- sequence format -------------------------------------------------------


def test_working_sequence_matches_protocol() -> None:
    assert build_sequence("working") == "\x1b]7501;state=working:app=ankaloop" + ST


def test_all_documented_states_are_accepted() -> None:
    for state in ("idle", "working", "done", "blocked", "error"):
        assert build_sequence(state).startswith(f"\x1b]7501;state={state}:app=ankaloop")


def test_clear_state_removes_the_entry() -> None:
    # "clear" carries only the state: app and payload keys would recreate it.
    assert build_sequence("clear") == "\x1b]7501;state=clear" + ST


def test_unknown_state_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_sequence("busy")  # type: ignore[arg-type]


def test_message_and_progress_are_appended() -> None:
    sequence = build_sequence("working", msg="thinking", progress=40)
    encoded = base64.b64encode(b"thinking").decode("ascii")
    assert sequence == f"\x1b]7501;state=working:app=ankaloop:msg={encoded}:progress=40" + ST


def test_empty_message_is_omitted() -> None:
    assert ":msg=" not in build_sequence("working", msg="")
    assert ":progress=" not in build_sequence("working", progress=None)


# --- msg encoding ----------------------------------------------------------


def test_message_is_base64_and_single_line() -> None:
    encoded = encode_message("hello\nworld")
    assert "\n" not in encoded
    assert " " not in encoded
    # The decoded text must stay one line: the protocol forbids control
    # characters, and a strict terminal drops a report whose msg decodes to one.
    assert base64.b64decode(encoded).decode("utf-8") == "hello world"


def test_message_strips_control_characters() -> None:
    decoded = base64.b64decode(encode_message("a\tb\r\nc\x00d\x7f")).decode("utf-8")
    assert decoded == "a b  c d "
    assert not any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in decoded)


def test_exception_message_is_single_line() -> None:
    # The CLI reports str(exc), which routinely spans several lines.
    source = "ValueError: bad\n  at frame 1\n\tin f()"
    decoded = base64.b64decode(encode_message(source)).decode("utf-8")
    assert "\n" not in decoded
    assert "\t" not in decoded


def test_truncated_message_stays_within_the_protocol_size_limit() -> None:
    # 2048 message bytes become ~2.7 KB of base64, comfortably under 4096.
    sequence = build_sequence("error", msg="x" * (MAX_MESSAGE_BYTES * 4))
    assert len(sequence.encode("utf-8")) <= MAX_SEQUENCE_BYTES
    assert ":msg=" in sequence


def test_oversized_sequence_drops_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # The terminal would reject an over-long report wholesale, so the payload
    # is dropped instead of losing the state along with it.
    monkeypatch.setattr(program_status, "MAX_SEQUENCE_BYTES", 32)
    sequence = build_sequence("error", msg="boom")
    assert sequence == "\x1b]7501;state=error:app=ankaloop" + ST


def test_message_is_truncated_to_max_bytes() -> None:
    encoded = encode_message("x" * (MAX_MESSAGE_BYTES * 4))
    assert len(base64.b64decode(encoded)) == MAX_MESSAGE_BYTES


def test_message_truncation_respects_utf8_boundaries() -> None:
    # Two-byte characters: cutting at an odd byte must not produce invalid UTF-8.
    encoded = encode_message("é" * MAX_MESSAGE_BYTES)
    decoded = base64.b64decode(encoded).decode("utf-8")
    assert decoded == "é" * (MAX_MESSAGE_BYTES // 2)


def test_message_cannot_break_out_of_the_sequence() -> None:
    sequence = build_sequence("error", msg="boom\x07\x1b\\state=idle")
    assert sequence.count("state=") == 1
    assert sequence.endswith(ST)
    assert "\x07" not in sequence


# --- progress clamp --------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-5, 0), (0, 0), (42.9, 42), (100, 100), (150, 100)],
)
def test_progress_is_clamped(value: float, expected: int) -> None:
    assert clamp_progress(value) == expected
    assert f":progress={expected}" in build_sequence("working", progress=value)


def test_non_numeric_progress_is_dropped() -> None:
    assert clamp_progress("not-a-number") is None  # type: ignore[arg-type]
    assert clamp_progress(None) is None


# --- terminal writes -------------------------------------------------------


class _FakeTty:
    """Minimal stand-in for the /dev/tty text stream."""

    def __init__(self) -> None:
        self.written: list[str] = []

    def __enter__(self) -> _FakeTty:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def write(self, data: str) -> None:
        self.written.append(data)

    def flush(self) -> None:
        return None


def test_report_writes_the_sequence_to_dev_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    tty = _FakeTty()
    opened: list[tuple[str, str]] = []

    def fake_open(path: str, mode: str = "r", **kwargs: object) -> _FakeTty:
        opened.append((path, mode))
        return tty

    monkeypatch.setattr(program_status, "ENABLED", True)
    monkeypatch.setattr("builtins.open", fake_open)

    assert report("idle") is True
    # Never stdout: the sequence must reach the terminal device itself.
    assert opened == [("/dev/tty", "w")]
    assert tty.written == ["\x1b]7501;state=idle:app=ankaloop" + ST]


def test_report_is_silent_without_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open(path: str, mode: str = "r", **kwargs: object) -> _FakeTty:
        raise OSError(6, "No such device or address")

    monkeypatch.setattr(program_status, "ENABLED", True)
    monkeypatch.setattr("builtins.open", fake_open)

    assert report("working") is False


def test_report_never_raises_on_write_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenTty(_FakeTty):
        def write(self, data: str) -> None:
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(program_status, "ENABLED", True)
    monkeypatch.setattr("builtins.open", lambda *a, **k: BrokenTty())

    assert report("working") is False


def test_report_is_a_noop_when_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_open(*args: object, **kwargs: object) -> None:  # pragma: no cover
        raise AssertionError("/dev/tty must not be touched when disabled")

    monkeypatch.setattr(program_status, "ENABLED", False)
    monkeypatch.setattr("builtins.open", unexpected_open)

    assert report("working") is False


# --- opt-out environment variable -----------------------------------------


@pytest.mark.parametrize("value", ["0", "off", "false", "FALSE", "no", "disabled"])
def test_env_opt_out_disables_reporting(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ANKALOOP_PROGRAM_STATUS", value)
    module = importlib.reload(program_status)
    try:
        assert module.ENABLED is False
        assert module.report("working") is False
    finally:
        monkeypatch.delenv("ANKALOOP_PROGRAM_STATUS", raising=False)
        importlib.reload(program_status)


@pytest.mark.parametrize("value", ["", "1", "on", "true", "yes"])
def test_env_default_and_truthy_values_enable_reporting(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ANKALOOP_PROGRAM_STATUS", value)
    module = importlib.reload(program_status)
    try:
        assert module.ENABLED is True
    finally:
        monkeypatch.delenv("ANKALOOP_PROGRAM_STATUS", raising=False)
        importlib.reload(program_status)
