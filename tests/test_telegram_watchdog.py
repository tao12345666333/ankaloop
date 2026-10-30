"""Tests for the Telegram bot watchdog loop in ankaloop.cli."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ankaloop.cli import (
    TELEGRAM_WATCHDOG_INITIAL_DELAY,
    TELEGRAM_WATCHDOG_MAX_DELAY,
    _telegram_bot_watchdog,
)


class _SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _config(webhook_mode: bool = False) -> SimpleNamespace:
    return SimpleNamespace(webhook_mode=webhook_mode, webhook_url=None)


class _RunSpy:
    """Patch ankaloop.cli.asyncio.run during a test."""

    def __init__(self) -> None:
        self.count = 0

    def __enter__(self) -> _RunSpy:
        import ankaloop.cli as cli

        self._cli = cli
        self._real = cli.asyncio.run

        def fake_run(_coro: object) -> None:
            # The fake bot methods return a plain object, not a coroutine —
            # do not touch it (calling .close() on a non-coroutine raises and
            # would send every "successful" start into the retry branch).
            self.count += 1

        cli.asyncio.run = fake_run  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        self._cli.asyncio.run = self._real  # type: ignore[assignment]


def test_watchdog_retries_until_success_and_uses_backoff() -> None:
    sleeps = _SleepRecorder()
    attempts: list[bool] = []

    flaky = {"remaining": 2}

    class FakeBot:
        def __init__(self, **kwargs: object) -> None:
            attempts.append(True)
            if flaky["remaining"] > 0:
                flaky["remaining"] -= 1
                raise RuntimeError("transient network error")

        def start_polling(self) -> object:
            return object()

    with _RunSpy() as spy:
        _telegram_bot_watchdog(
            FakeBot,
            token="t",
            allowed_users={1},
            admin_users=set(),
            work_dir=None,
            config=_config(),
            sleep=sleeps,
        )

    assert len(attempts) == 3
    assert spy.count == 1
    assert sleeps.calls == [
        TELEGRAM_WATCHDOG_INITIAL_DELAY,
        TELEGRAM_WATCHDOG_INITIAL_DELAY * 2,
    ]


def test_watchdog_backoff_is_capped_and_loops_forever() -> None:
    sleeps = _SleepRecorder()

    def aborting_sleep(seconds: float) -> None:
        if len(sleeps.calls) >= 12:
            raise StopLoop
        sleeps(seconds)

    class StopLoop(Exception):
        pass

    class AlwaysFailsBot:
        def __init__(self, **kwargs: object) -> None:
            raise RuntimeError("still broken")

    with _RunSpy() as spy, pytest.raises(StopLoop):
        _telegram_bot_watchdog(
            AlwaysFailsBot,
            token="t",
            allowed_users=set(),
            admin_users=set(),
            work_dir=None,
            config=_config(),
            sleep=aborting_sleep,
        )

    # AlwaysFailsBot raises in its constructor, so asyncio.run is never
    # reached; the loop must nevertheless keep retrying indefinitely.
    assert spy.count == 0
    assert all(s <= TELEGRAM_WATCHDOG_MAX_DELAY for s in sleeps.calls)
    assert sleeps.calls[-1] == TELEGRAM_WATCHDOG_MAX_DELAY


def test_watchdog_releases_global_registrations_between_attempts() -> None:
    """A failed attempt must not leak its global registrations.

    start_polling registers EventBus notification handlers and the telegram_*
    tools before touching the network. If a startup attempt fails, the watchdog
    must release them (via bot.stop()) before retrying, or each retry stacks
    another set of registrations: duplicate notifications once the network
    recovers, and tools left pointing at a dead bot that a healthy bot can never
    re-register.
    """
    from ankaloop.event_bus import EventType, get_event_bus

    sleeps = _SleepRecorder()
    bus = get_event_bus()
    baseline = len(bus._handlers)
    attempts: list[bool] = []
    stops: list[int] = []
    flaky = {"remaining": 2}

    class LeakyBot:
        def __init__(self, **kwargs: object) -> None:
            attempts.append(True)
            self._handler_ids: list[str] = []

        def start_polling(self) -> object:
            # Mirror the real bot: register, then fail during startup.
            self._handler_ids.append(get_event_bus().subscribe(EventType.TASK_COMPLETED, lambda event: None))
            if flaky["remaining"] > 0:
                flaky["remaining"] -= 1
                raise RuntimeError("startup failed after registering")

        def stop(self) -> None:
            # Sync on the fake: the watchdog wraps it in asyncio.run, which the
            # _RunSpy fake counts without awaiting.
            stops.append(1)
            for handler_id in self._handler_ids:
                get_event_bus().unsubscribe(handler_id)
            self._handler_ids = []

    with _RunSpy() as spy:
        _telegram_bot_watchdog(
            LeakyBot,
            token="t",
            allowed_users={1},
            admin_users=set(),
            work_dir=None,
            config=_config(),
            sleep=sleeps,
        )

    assert len(attempts) == 3
    # 2 failed attempts each wrap a cleanup stop() + 1 successful start in asyncio.run.
    assert spy.count == 3
    # One stop per failed attempt (the constructor-failure attempts release nothing).
    assert len(stops) == 2
    # Only the successful attempt's registration remains; a leaked fix would
    # leave one handler per attempt (baseline + 3 here).
    assert len(get_event_bus()._handlers) == baseline + 1
    assert sleeps.calls == [5.0, 10.0]


def test_watchdog_does_not_stop_a_bot_that_never_started() -> None:
    """If the factory itself raises, there is no bot to release."""
    stops: list[int] = []

    def factory(**kwargs: object) -> object:
        raise RuntimeError("constructor failed")

    sleeps = _SleepRecorder()

    class StopLoop(Exception):
        pass

    def aborting_sleep(seconds: float) -> None:
        if len(sleeps.calls) >= 1:
            raise StopLoop
        sleeps(seconds)

    with pytest.raises(StopLoop):
        _telegram_bot_watchdog(
            factory,
            token="t",
            allowed_users=set(),
            admin_users=set(),
            work_dir=None,
            config=_config(),
            sleep=aborting_sleep,
        )

    assert stops == []


def test_watchdog_webhook_clean_return_exits_without_retry() -> None:
    sleeps = _SleepRecorder()
    attempts: list[bool] = []
    webhooks_requested: list[str] = []

    class FakeBot:
        def __init__(self, **kwargs: object) -> None:
            attempts.append(True)
            assert kwargs["token"] == "tok"
            assert kwargs["work_dir"] == Path("/tmp/x")

        def start_webhook(self, url: str) -> None:
            webhooks_requested.append(url)

    cfg = SimpleNamespace(webhook_mode=True, webhook_url="https://example.invalid/hook")

    with _RunSpy() as spy:
        _telegram_bot_watchdog(
            FakeBot,
            token="tok",
            allowed_users={7},
            admin_users={7},
            work_dir=Path("/tmp/x"),
            config=cfg,
            sleep=sleeps,
        )

    assert attempts == [True]
    assert spy.count == 1
    assert sleeps.calls == []
    assert webhooks_requested == ["https://example.invalid/hook"]
