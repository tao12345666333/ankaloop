"""HTTP memory backend.

``HttpMemoryManager`` implements the same method set as ``MemoryManager`` but
maps each method to one HTTP call against a remote memory service, so multiple
processes and environments can share a single long-term memory. The remote is
the single writer; this client performs no merging or caching.

Failure semantics:
- Reads degrade to empty results when the remote is unreachable, so the current
  turn can still complete.
- Writes are staged in a local JSONL outbox and replayed in order once the
  remote recovers; while staged writes remain, new writes are appended to the
  outbox too, so the remote always sees a strict FIFO order.

The HTTP API contract is documented in docs/memory.md.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .constants import CONFIG_DIR
from .memory import MemoryManager, MemorySearchResult

logger = logging.getLogger(__name__)

MAX_OUTBOX_ENTRIES = 1000
_FLUSH_COOLDOWN_SECONDS = 30.0


class MemoryBackendError(Exception):
    """The remote memory backend is unreachable or returned an error."""


class HttpMemoryManager(MemoryManager):
    """MemoryManager backed by a remote HTTP memory service.

    Scope semantics match the local manager: ``scope`` is passed through to the
    remote, which decides how to key it (e.g. a project scope may key on a repo
    URL rather than a local clone path). ``project_root`` is sent along as a
    hint for the same reason.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        project_root: Path | None = None,
        outbox_path: Path | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # Deliberately does not call super().__init__(): no local stores exist
        # for this backend. All public methods are overridden.
        self.base_url = base_url.rstrip("/")
        self.project_root = project_root
        self.outbox_path = outbox_path or (CONFIG_DIR / "memory-outbox.jsonl")
        self._outbox_lock = threading.Lock()
        # Sentinel so the first flush attempt is never skipped by the cooldown.
        self._last_flush_attempt = -_FLUSH_COOLDOWN_SECONDS
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(timeout=timeout_seconds, headers=headers, transport=transport)

    # --- HTTP plumbing ---

    def _scoped(self, params: dict[str, Any], scope: str | None = None) -> dict[str, Any]:
        """Attach scope and project_root pass-through fields to params/body."""
        if scope is not None:
            params["scope"] = scope
        if self.project_root is not None:
            params["project_root"] = str(self.project_root)
        return params

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform one HTTP call and return the parsed JSON object.

        Raises:
            MemoryBackendError: On transport errors, non-2xx status, or a
                malformed JSON response.
        """
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(method, url, params=params, json=json_body)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as e:
            raise MemoryBackendError(f"{method} {path} failed: {e}") from e
        except (json.JSONDecodeError, ValueError) as e:
            raise MemoryBackendError(f"{method} {path} returned invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise MemoryBackendError(f"{method} {path} returned non-object JSON")
        return data

    def _read(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET the path, degrading to an empty object when the remote fails."""
        try:
            return self._request("GET", path, params=params)
        except MemoryBackendError as e:
            logger.warning(f"Memory backend read failed, using empty result: {e}")
            return {}

    # --- Write outbox (stage failed writes, replay in order) ---

    def _read_outbox(self) -> list[dict[str, Any]]:
        if not self.outbox_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            for line in self.outbox_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict) and "op" in entry and "args" in entry:
                        entries.append(entry)
                except json.JSONDecodeError:
                    logger.warning(f"Dropping corrupt outbox line: {line[:80]}")
        except OSError as e:
            logger.warning(f"Could not read memory outbox: {e}")
        return entries

    def _write_outbox(self, entries: list[dict[str, Any]]) -> None:
        try:
            self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
            if not entries:
                self.outbox_path.unlink(missing_ok=True)
                return
            tmp = self.outbox_path.with_suffix(self.outbox_path.suffix + ".tmp")
            tmp.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
            os.replace(tmp, self.outbox_path)
        except OSError as e:
            logger.error(f"Could not write memory outbox: {e}")

    def _stage(self, op: str, args: dict[str, Any]) -> None:
        """Stage a failed write for later replay."""
        with self._outbox_lock:
            entries = self._read_outbox()
            entries.append(
                {
                    "op": op,
                    "args": args,
                    "queued_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            if len(entries) > MAX_OUTBOX_ENTRIES:
                dropped = len(entries) - MAX_OUTBOX_ENTRIES
                logger.warning(f"Memory outbox full; dropping {dropped} oldest staged write(s)")
                entries = entries[-MAX_OUTBOX_ENTRIES:]
            self._write_outbox(entries)

    def _dispatch(self, op: str, args: dict[str, Any]) -> None:
        """Replay one staged write. Raises MemoryBackendError on failure."""
        if op == "write_long_term":
            self._request("PUT", "/v1/long-term", json_body=self._scoped(dict(args), args.get("scope")))
        elif op == "append_history":
            self._request("POST", "/v1/history", json_body=self._scoped(dict(args), args.get("scope")))
        elif op == "write_soul":
            self._request("PUT", "/v1/soul", json_body=dict(args))
        elif op == "write_identity":
            self._request("PUT", "/v1/identity", json_body=dict(args))
        elif op == "upsert_fact":
            self._request(
                "PUT",
                f"/v1/facts/{quote(str(args['key']), safe='')}",
                json_body=self._scoped({k: v for k, v in args.items() if k != "key"}, args.get("scope")),
            )
        elif op == "delete_fact":
            self._request(
                "DELETE",
                f"/v1/facts/{quote(str(args['key']), safe='')}",
                params=self._scoped({}, args.get("scope")),
            )
        else:
            logger.warning(f"Skipping unknown staged memory op: {op!r}")

    def _flush_outbox(self) -> bool:
        """Replay staged writes in order; stop at the first failure.

        Returns True when no staged writes remain.
        """
        with self._outbox_lock:
            entries = self._read_outbox()
            if not entries:
                return True
            now = time.monotonic()
            if now - self._last_flush_attempt < _FLUSH_COOLDOWN_SECONDS:
                return False
            self._last_flush_attempt = now
            remaining: list[dict[str, Any]] = []
            flushed = 0
            for i, entry in enumerate(entries):
                try:
                    self._dispatch(entry["op"], entry["args"])
                    flushed += 1
                except MemoryBackendError as e:
                    logger.warning(f"Memory outbox replay paused: {e}")
                    remaining = entries[i:]
                    break
            self._write_outbox(remaining)
            if flushed:
                logger.info(f"Flushed {flushed} staged memory write(s) to remote backend")
            return not remaining

    def _write(self, op: str, args: dict[str, Any]) -> bool:
        """Flush staged writes, then perform this write; stage it on failure.

        While older writes are still stuck in the outbox, this write is staged
        too so the remote always sees a strict FIFO order.

        Returns True when the write reached the remote now, False when it was
        staged locally for later replay.
        """
        if not self._flush_outbox():
            self._stage(op, args)
            return False
        try:
            self._dispatch(op, args)
            return True
        except MemoryBackendError as e:
            logger.warning(f"Memory backend write failed, staged for retry: {e}")
            self._stage(op, args)
            return False

    # --- Context injection ---

    def get_memory_context(self) -> str:
        """Get combined memory context from the remote backend."""
        data = self._read("/v1/context/memory", params=self._scoped({}))
        return str(data.get("context") or "")

    def get_persona_context(self) -> str:
        """Get the global persona context from the remote backend."""
        data = self._read("/v1/context/persona")
        return str(data.get("context") or "")

    # --- Persona (global-only, same as local manager) ---

    def read_soul(self, scope: str = "user", include_default: bool = False) -> str:
        """Read soul from the remote backend (empty when unreachable)."""
        self._ensure_global_persona_scope(scope)
        data = self._read("/v1/soul", params={"include_default": str(include_default).lower()})
        return str(data.get("content") or "")

    def write_soul(self, content: str, scope: str = "user") -> None:
        """Write soul to the remote backend (staged locally when unreachable)."""
        self._ensure_global_persona_scope(scope)
        self._write("write_soul", {"content": content})

    def read_identity(self, scope: str = "user") -> str:
        """Read identity from the remote backend (empty when unreachable)."""
        self._ensure_global_persona_scope(scope)
        data = self._read("/v1/identity")
        return str(data.get("content") or "")

    def write_identity(self, content: str, scope: str = "user") -> None:
        """Write identity to the remote backend (staged locally when unreachable)."""
        self._ensure_global_persona_scope(scope)
        self._write("write_identity", {"content": content})

    # --- Long-term memory and history ---

    def read_long_term(self, scope: str = "user") -> str:
        """Read long-term memory from the remote backend (empty when unreachable)."""
        data = self._read("/v1/long-term", params=self._scoped({}, scope))
        return str(data.get("content") or "")

    def write_long_term(self, content: str, scope: str = "user") -> bool:
        """Write long-term memory to the remote backend.

        Returns False (and stages the write locally) when the remote is
        unreachable.
        """
        return self._write("write_long_term", {"content": content, "scope": scope})

    def append_history(
        self,
        content: str,
        session_id: str = "unknown",
        tags: list[str] | None = None,
        scope: str = "user",
    ) -> None:
        """Append to the remote history log (staged locally when unreachable)."""
        self._write(
            "append_history",
            {
                "content": content,
                "session_id": session_id,
                "tags": [str(t) for t in tags] if tags else [],
                "scope": scope,
            },
        )

    def search(self, query: str, max_results: int = 20) -> list[MemorySearchResult]:
        """Search the remote backend (empty when unreachable)."""
        data = self._read(
            "/v1/search",
            params=self._scoped({"q": query, "max_results": max_results}),
        )
        results: list[MemorySearchResult] = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            results.append(
                MemorySearchResult(
                    line_number=int(item.get("line_number", 0)),
                    content=str(item.get("content", "")),
                    source=str(item.get("source", "memory")),
                )
            )
        return results

    # --- Facts ---

    def upsert_fact(
        self,
        key: str,
        value: str,
        category: str = "general",
        source: str = "agent",
        confidence: float = 1.0,
        scope: str = "user",
    ) -> None:
        """Insert or update a fact on the remote backend (staged when unreachable)."""
        self._write(
            "upsert_fact",
            {
                "key": key,
                "value": value,
                "category": category,
                "source": source,
                "confidence": confidence,
                "scope": scope,
            },
        )

    def get_fact(self, key: str, scope: str = "user") -> dict | None:
        """Get a fact from the remote backend (None when unreachable)."""
        data = self._read(f"/v1/facts/{quote(key, safe='')}", params=self._scoped({}, scope))
        fact = data.get("fact")
        return fact if isinstance(fact, dict) else None

    def search_facts(
        self,
        query: str,
        max_results: int = 20,
        scope: str = "user",
    ) -> list[dict]:
        """Search facts on the remote backend (empty when unreachable)."""
        data = self._read(
            "/v1/facts/search",
            params=self._scoped({"q": query, "max_results": max_results}, scope),
        )
        return [f for f in data.get("facts") or [] if isinstance(f, dict)]

    def list_facts(
        self,
        category: str | None = None,
        limit: int = 100,
        scope: str = "user",
    ) -> list[dict]:
        """List facts from the remote backend (empty when unreachable)."""
        params: dict[str, Any] = {"limit": limit}
        if category is not None:
            params["category"] = category
        data = self._read("/v1/facts", params=self._scoped(params, scope))
        return [f for f in data.get("facts") or [] if isinstance(f, dict)]

    def delete_fact(self, key: str, scope: str = "user") -> bool:
        """Delete a fact on the remote backend (staged when unreachable)."""
        return self._write("delete_fact", {"key": key, "scope": scope})

    # --- Stats ---

    def get_stats(self) -> dict[str, dict[str, int | bool]]:
        """Get memory statistics from the remote backend (empty when unreachable)."""
        data = self._read("/v1/stats", params=self._scoped({}))
        user = data.get("user")
        project = data.get("project")
        return {
            "user": user if isinstance(user, dict) else {},
            "project": project if isinstance(project, dict) else {},
        }
