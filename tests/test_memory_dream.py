import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from ankaloop.memory import get_memory_manager, reset_memory_manager
from ankaloop.memory_dream import DREAM_LOCK_STALE_SECONDS, DREAM_MAX_EVENTS, MemoryDreamer


class _FakeClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=self.content)


class _ScriptedClient:
    """LLM client stub returning a fixed sequence of responses."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=self.contents.pop(0))


def _event_lines(call: dict) -> list[str]:
    content = call["messages"][1]["content"]
    return [line for line in content.splitlines() if line.startswith("- [")]


def test_memory_dreamer_consolidates_recent_events(tmp_path):
    reset_memory_manager()
    manager = get_memory_manager(tmp_path)
    manager.append_history("User is planning Telegram memory improvements", session_id="s1", scope="project")
    manager.append_history("Need proactive heartbeat support", session_id="s1", scope="project")
    client = _FakeClient("# Project Memory\n- User is planning Telegram memory improvements.")
    dreamer = MemoryDreamer(
        tmp_path,
        min_interval_seconds=0,
        min_new_events=2,
        client=client,
        model="test-model",
    )

    result = dreamer.run_once()

    assert result.ran is True
    assert result.updated is True
    assert "Telegram memory improvements" in manager.read_long_term(scope="project")
    assert client.calls
    reset_memory_manager()


def test_memory_dreamer_skips_until_enough_events(tmp_path):
    reset_memory_manager()
    manager = get_memory_manager(tmp_path)
    manager.append_history("Only one event", session_id="s1", scope="project")
    dreamer = MemoryDreamer(
        tmp_path,
        min_interval_seconds=0,
        min_new_events=2,
        client=_FakeClient("# Memory\n- no-op"),
        model="test-model",
    )

    result = dreamer.run_once()

    assert result.ran is False
    assert result.reason == "not_enough_new_events"
    reset_memory_manager()


def test_memory_dreamer_does_not_advance_cursor_when_memory_write_fails(tmp_path):
    reset_memory_manager()
    manager = get_memory_manager(tmp_path)
    manager.append_history("first", session_id="s1", scope="project")
    manager.append_history("second", session_id="s1", scope="project")
    dreamer = MemoryDreamer(
        tmp_path,
        min_interval_seconds=0,
        min_new_events=2,
        client=_FakeClient("# Memory\n- consolidated"),
        model="test-model",
    )

    with patch.object(manager, "write_long_term", return_value=False):
        result = dreamer.run_once()

    assert result.reason == "write_failed"
    state = dreamer._load_state()
    assert state.get("last_event_id", 0) == 0
    assert state["pending_event_id"] > 0
    assert state["pending_memory"] == "# Memory\n- consolidated"

    result = dreamer.run_once()

    assert result.updated is True
    assert len(dreamer._client.calls) == 1
    reset_memory_manager()


def test_memory_dreamer_takes_over_stale_lock(tmp_path):
    dreamer = MemoryDreamer(tmp_path)
    dreamer.memory_dir.mkdir(parents=True, exist_ok=True)
    stale_written = time.time() - DREAM_LOCK_STALE_SECONDS - 1
    dreamer.lock_file.write_text(str(stale_written), encoding="utf-8")

    assert dreamer._acquire_lock() is True
    token = dreamer.lock_file.read_text(encoding="utf-8")
    assert float(token.split()[0]) > stale_written  # replacement carries a fresh timestamp

    dreamer._release_lock()
    assert not dreamer.lock_file.exists()


def test_memory_dreamer_fresh_lock_blocks_second_acquirer(tmp_path):
    dreamer = MemoryDreamer(tmp_path)
    assert dreamer._acquire_lock() is True
    winner_token = dreamer.lock_file.read_text(encoding="utf-8")

    other = MemoryDreamer(tmp_path)
    assert other._acquire_lock() is False
    # the losing acquirer must leave the winner's lock untouched
    assert dreamer.lock_file.read_text(encoding="utf-8") == winner_token
    assert not list(dreamer.memory_dir.glob("*.stolen"))

    dreamer._release_lock()
    assert not dreamer.lock_file.exists()


def test_memory_dreamer_lock_race_has_single_winner(tmp_path):
    """Two dreamers racing on the same stale lock: exactly one wins."""
    dreamer = MemoryDreamer(tmp_path)
    dreamer.memory_dir.mkdir(parents=True, exist_ok=True)
    dreamer.lock_file.write_text(str(time.time() - DREAM_LOCK_STALE_SECONDS - 1), encoding="utf-8")

    barrier = threading.Barrier(2)
    results: list[bool] = []

    def race() -> None:
        racer = MemoryDreamer(tmp_path)
        barrier.wait(timeout=5)
        results.append(racer._acquire_lock())

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert results.count(True) == 1
    # the surviving lock is a fresh one, not the stale lock we planted
    assert float(dreamer.lock_file.read_text(encoding="utf-8").split()[0]) > time.time() - 60
    assert not list(dreamer.memory_dir.glob("*.stolen"))


def test_memory_dreamer_release_leaves_replaced_lock_alone(tmp_path):
    dreamer = MemoryDreamer(tmp_path)
    assert dreamer._acquire_lock() is True

    # simulate the lock file having been replaced by another acquisition
    dreamer.lock_file.write_text("999 another-owner", encoding="utf-8")
    dreamer._release_lock()

    assert dreamer.lock_file.read_text(encoding="utf-8") == "999 another-owner"


def test_memory_dreamer_consolidates_backlog_beyond_single_window(tmp_path):
    reset_memory_manager()
    manager = get_memory_manager(tmp_path)
    for i in range(200):
        manager.append_history(f"event {i}", session_id="s1", scope="project")
    client = _FakeClient("# Project Memory\n- consolidated note.")
    dreamer = MemoryDreamer(
        tmp_path,
        min_interval_seconds=0,
        min_new_events=10,
        client=client,
        model="test-model",
    )

    result = dreamer.run_once()

    assert result.ran is True
    assert result.updated is True
    # one bounded LLM call per batch: 200 events need three DREAM_MAX_EVENTS batches
    assert len(client.calls) == 3
    assert all(len(_event_lines(call)) <= DREAM_MAX_EVENTS for call in client.calls)
    consolidated = {line.rsplit("] ", 1)[-1] for call in client.calls for line in _event_lines(call)}
    assert consolidated == {f"event {i}" for i in range(200)}
    # batches run oldest-first with no gaps
    assert "event 0" in client.calls[0]["messages"][1]["content"]
    assert "event 199" in client.calls[-1]["messages"][1]["content"]
    assert dreamer._load_state()["last_event_id"] == 200

    # the backlog is fully caught up: nothing left to consolidate
    second = dreamer.run_once()
    assert second.ran is False
    assert second.reason == "no_events"
    assert len(client.calls) == 3
    reset_memory_manager()


def test_memory_dreamer_advances_cursor_through_unanswered_batches(tmp_path):
    reset_memory_manager()
    manager = get_memory_manager(tmp_path)
    for i in range(100):
        manager.append_history(f"event {i}", session_id="s1", scope="project")
    client = _ScriptedClient(["NO_REPLY", "# Project Memory\n- consolidated."])
    dreamer = MemoryDreamer(
        tmp_path,
        min_interval_seconds=0,
        min_new_events=10,
        client=client,
        model="test-model",
    )

    result = dreamer.run_once()

    # the first batch answered NO_REPLY, the second updated memory: the cursor
    # advances past both, so neither batch is reprocessed
    assert result.ran is True
    assert result.updated is True
    assert result.reason == "updated"
    assert len(client.calls) == 2
    assert dreamer._load_state()["last_event_id"] == 100
    assert "consolidated" in manager.read_long_term(scope="project")
    reset_memory_manager()
