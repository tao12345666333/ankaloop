"""
Memory system for AnkaLoop.

Provides persistent cross-session memory for the agent using a two-layer approach:
- MEMORY.md: Long-term facts, preferences, and knowledge (curated, compact)
- HISTORY.md: Append-only searchable log of past activities and learnings

Memory is stored at:
- User-level: ~/.config/ankaloop/memory/
- Project-level: .ankaloop/memory/ (project-specific knowledge)

This enables the agent to remember important context across sessions,
supporting self-evolution by accumulating knowledge over time.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .constants import CONFIG_DIR, PROJECT_DIR_NAME

logger = logging.getLogger(__name__)

DEFAULT_SOUL = """You are AnkaLoop, a long-running autonomous coding agent.

Act with continuity across sessions. Preserve important user preferences, project facts,
and decisions in memory when they are likely to matter later. Use your identity and soul
as stable guidance, but follow the user's explicit instructions first."""


@dataclass
class MemoryEntry:
    """A single entry in the history log."""

    timestamp: str
    session_id: str
    content: str
    tags: list[str] = field(default_factory=list)

    @staticmethod
    def _normalize_tags(tags: list[str] | str | None) -> list[str]:
        """Coerce tags to a list of strings.

        Callers (notably LLM tool invocations) may pass a bare string or
        None instead of a list. Iterating a bare string would otherwise
        split it into individual characters when joined.
        """
        if not tags:
            return []
        if isinstance(tags, str):
            return [tags]
        return [str(t) for t in tags]

    def to_markdown(self) -> str:
        """Convert entry to markdown format for appending to HISTORY.md."""
        tags_list = self._normalize_tags(self.tags)
        tags_str = f" [{', '.join(tags_list)}]" if tags_list else ""
        return f"### [{self.timestamp}] (session: {self.session_id}){tags_str}\n\n{self.content}\n"


@dataclass
class MemorySearchResult:
    """Result from searching memory."""

    line_number: int
    content: str
    source: str  # "memory" or "history"


class MemoryStore:
    """Two-layer memory system for persistent agent knowledge.

    Layer 1 - Long-term Memory (MEMORY.md):
        Curated facts, preferences, patterns, and key knowledge.
        The agent can read and rewrite this file to keep it organized.
        Should stay compact (<5000 words).

    Layer 2 - History Log (HISTORY.md):
        Append-only log of past activities, decisions, and learnings.
        Searchable via grep-style queries.
        Grows over time.

    Layer 3 - SQLite+FTS5 Backend (memory.db):
        Episodic events and declarative facts with full-text search.
    """

    def __init__(self, memory_dir: Path):
        """Initialize memory store.

        Args:
            memory_dir: Directory to store memory files
        """
        self.memory_dir = memory_dir
        self.memory_file = memory_dir / "MEMORY.md"
        self.history_file = memory_dir / "HISTORY.md"
        self.soul_file = memory_dir.parent / "SOUL.md"
        self.identity_file = memory_dir.parent / "IDENTITY.md"
        self._init_sqlite()

    def _init_sqlite(self) -> None:
        """Lazily initialize the SQLite memory store."""
        from .memory_store import SQLiteMemoryStore

        self._sqlite = SQLiteMemoryStore(self.memory_dir / "memory.db")

    @staticmethod
    def get_user_memory_dir() -> Path:
        """Get user-level memory directory."""
        return CONFIG_DIR / "memory"

    @staticmethod
    def get_project_memory_dir(project_root: Path | None = None) -> Path:
        """Get project-level memory directory."""
        root = project_root or Path.cwd()
        return root / PROJECT_DIR_NAME / "memory"

    def _ensure_dir(self) -> None:
        """Ensure memory directory exists."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    # --- Long-term Memory (MEMORY.md) ---

    def read_long_term(self) -> str:
        """Read long-term memory content.

        Returns:
            Content of MEMORY.md, or empty string if not found
        """
        if self.memory_file.exists():
            try:
                return self.memory_file.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning(f"Could not read long-term memory: {e}")
        return ""

    def write_long_term(self, content: str) -> bool:
        """Write (overwrite) long-term memory.

        The agent should curate this to keep it organized and compact.

        Args:
            content: New content for MEMORY.md

        Returns:
            Whether the content was written successfully.
        """
        self._ensure_dir()
        try:
            self.memory_file.write_text(content, encoding="utf-8")
            logger.info(f"Updated long-term memory ({len(content)} chars)")
            return True
        except Exception as e:
            logger.error(f"Could not write long-term memory: {e}")
            return False

    # --- Soul and Identity ---

    def read_soul(self, include_default: bool = False) -> str:
        """Read the persistent soul/persona text.

        Args:
            include_default: Return the built-in default when no SOUL.md exists.

        Returns:
            SOUL.md content, default soul, or an empty string.
        """
        if self.soul_file.exists():
            try:
                return self.soul_file.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning(f"Could not read soul: {e}")
        return DEFAULT_SOUL if include_default else ""

    def write_soul(self, content: str) -> None:
        """Write the persistent soul/persona text."""
        self._ensure_dir()
        try:
            self.soul_file.parent.mkdir(parents=True, exist_ok=True)
            self.soul_file.write_text(content, encoding="utf-8")
            logger.info(f"Updated soul ({len(content)} chars)")
        except Exception as e:
            logger.error(f"Could not write soul: {e}")

    def read_identity(self) -> str:
        """Read the persistent identity profile."""
        if self.identity_file.exists():
            try:
                return self.identity_file.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning(f"Could not read identity: {e}")
        return ""

    def write_identity(self, content: str) -> None:
        """Write the persistent identity profile."""
        self._ensure_dir()
        try:
            self.identity_file.parent.mkdir(parents=True, exist_ok=True)
            self.identity_file.write_text(content, encoding="utf-8")
            logger.info(f"Updated identity ({len(content)} chars)")
        except Exception as e:
            logger.error(f"Could not write identity: {e}")

    def has_custom_persona(self) -> bool:
        """Return whether this store has a non-empty custom soul or identity."""
        return bool(self.read_soul().strip() or self.read_identity().strip())

    def get_persona_context(self, label: str, include_default_soul: bool = False) -> str:
        """Generate persona context for system prompt injection."""
        parts: list[str] = []
        soul = self.read_soul(include_default=include_default_soul).strip()
        identity = self.read_identity().strip()

        if soul:
            parts.append(f"## {label} Soul\n{soul}")
        if identity:
            parts.append(f"## {label} Identity\n{identity}")

        if not parts:
            return ""

        return "<persona>\n" + "\n\n".join(parts) + "\n</persona>"

    # --- History Log (HISTORY.md) ---

    def append_history(
        self,
        content: str,
        session_id: str = "unknown",
        tags: list[str] | None = None,
    ) -> None:
        """Append an entry to the history log.

        Args:
            content: The content to log
            session_id: Current session ID
            tags: Optional tags for categorization
        """
        self._ensure_dir()
        entry = MemoryEntry(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            session_id=session_id,
            content=content.strip(),
            tags=MemoryEntry._normalize_tags(tags),
        )
        try:
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(entry.to_markdown() + "\n")
            logger.debug(f"Appended to history log: {content[:80]}...")
        except Exception as e:
            logger.error(f"Could not append to history: {e}")

        # Also write to SQLite events table
        try:
            self._sqlite.append_event(
                content=content.strip(),
                session_id=session_id,
                tags=MemoryEntry._normalize_tags(tags),
                source="history",
            )
        except Exception as e:
            logger.warning(f"Could not append to SQLite events: {e}")

    def read_history(self, max_lines: int = 500) -> str:
        """Read history log content.

        Args:
            max_lines: Maximum number of lines to return (from the end)

        Returns:
            Content of HISTORY.md (tail if too large), or empty string
        """
        if not self.history_file.exists():
            return ""
        try:
            content = self.history_file.read_text(encoding="utf-8")
            lines = content.split("\n")
            if len(lines) > max_lines:
                return "\n".join(lines[-max_lines:])
            return content
        except Exception as e:
            logger.warning(f"Could not read history: {e}")
            return ""

    def search_history(self, query: str, max_results: int = 20) -> list[MemorySearchResult]:
        """Search history log for matching entries.

        Uses SQLite FTS5 when available, falls back to regex grep.

        Args:
            query: Search query (case-insensitive substring match)
            max_results: Maximum results to return

        Returns:
            List of matching results with line numbers
        """
        # Try SQLite FTS5 first
        try:
            fts_results = self._sqlite.search_events(query, max_results=max_results)
            if fts_results:
                return fts_results
        except Exception as e:
            logger.debug(f"SQLite search_events failed, falling back: {e}")

        # Fallback to regex grep on markdown file
        results: list[MemorySearchResult] = []
        if not self.history_file.exists():
            return results

        try:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            lines = self.history_file.read_text(encoding="utf-8").split("\n")

            for i, line in enumerate(lines, 1):
                if pattern.search(line):
                    results.append(
                        MemorySearchResult(
                            line_number=i,
                            content=line.strip(),
                            source="history",
                        )
                    )
                    if len(results) >= max_results:
                        break
        except Exception as e:
            logger.warning(f"Could not search history: {e}")

        return results

    def search_memory(self, query: str, max_results: int = 20) -> list[MemorySearchResult]:
        """Search both long-term memory, history, and SQLite stores.

        Args:
            query: Search query
            max_results: Maximum results to return

        Returns:
            Combined results from all memory layers
        """
        results: list[MemorySearchResult] = []

        # Search long-term memory first (MEMORY.md)
        if self.memory_file.exists():
            try:
                pattern = re.compile(re.escape(query), re.IGNORECASE)
                lines = self.memory_file.read_text(encoding="utf-8").split("\n")
                for i, line in enumerate(lines, 1):
                    if pattern.search(line):
                        results.append(
                            MemorySearchResult(
                                line_number=i,
                                content=line.strip(),
                                source="memory",
                            )
                        )
                        if len(results) >= max_results:
                            return results
            except Exception as e:
                logger.warning(f"Could not search memory: {e}")

        # Search SQLite (facts + events via FTS5)
        remaining = max_results - len(results)
        if remaining > 0:
            try:
                sqlite_results = self._sqlite.search(query, max_results=remaining)
                results.extend(sqlite_results)
            except Exception as e:
                logger.debug(f"SQLite search failed, falling back: {e}")
                # Fallback to history grep
                remaining = max_results - len(results)
                if remaining > 0:
                    results.extend(self.search_history(query, max_results=remaining))

        return results

    # --- Context Generation ---

    def get_memory_context(self) -> str:
        """Generate memory context for injection into agent prompts.

        Returns long-term memory, durable facts, and recent episodic memory
        formatted for the system prompt.

        Returns:
            Formatted memory context string, or empty string
        """
        long_term = self.read_long_term().strip()
        facts: list[dict] = []
        recent = ""
        db_file = self.memory_dir / "memory.db"
        if db_file.exists():
            facts = self.list_facts(limit=20)
            recent = self._get_recent_events_context(limit=8)
        elif self.history_file.exists():
            recent = self.read_history(max_lines=80).strip()

        if not long_term and not facts and not recent:
            return ""

        parts = [
            "The following is persistent memory from previous sessions. "
            "Use it for continuity and update it when the user asks you to remember something."
        ]
        if long_term:
            parts.append(f"## Long-term Memory\n{long_term}")
        if facts:
            fact_lines = [f"- [{f['category']}] {f['key']}: {f['value']}" for f in facts[:20]]
            parts.append("## Durable Facts\n" + "\n".join(fact_lines))
        if recent:
            parts.append("## Recent Episodic Memory\n" + recent)

        return "<memory>\n" + "\n\n".join(parts) + "\n</memory>"

    def _get_recent_events_context(self, limit: int = 8) -> str:
        """Return recent episodic memory as compact prompt text."""
        try:
            events = self._sqlite.get_recent_events(limit=limit)
        except Exception as e:
            logger.debug(f"Could not read recent SQLite events: {e}")
            events = []

        lines: list[str] = []
        for event in reversed(events):
            content = str(event.get("content", "")).strip()
            if not content:
                continue
            timestamp = event.get("timestamp", "")
            session_id = event.get("session_id", "unknown")
            lines.append(f"- [{timestamp} session:{session_id}] {content}")

        if lines:
            return "\n".join(lines)

        history = self.read_history(max_lines=80).strip()
        return history

    # --- Facts (delegated to SQLite) ---

    def upsert_fact(
        self,
        key: str,
        value: str,
        category: str = "general",
        source: str = "agent",
        confidence: float = 1.0,
    ) -> None:
        """Insert or update a declarative fact.

        Args:
            key: Unique key for the fact.
            value: Fact value/content.
            category: Category for grouping.
            source: Source of the fact.
            confidence: Confidence score (0.0-1.0).
        """
        self._sqlite.upsert_fact(key, value, category, source, confidence)

    def get_fact(self, key: str) -> dict | None:
        """Get a specific fact by key.

        Args:
            key: The fact key.

        Returns:
            Fact dict or None.
        """
        return self._sqlite.get_fact(key)

    def search_facts(self, query: str, max_results: int = 20) -> list[dict]:
        """Search facts using FTS5.

        Args:
            query: Search query.
            max_results: Maximum results.

        Returns:
            List of matching fact dicts.
        """
        return self._sqlite.search_facts(query, max_results)

    def list_facts(self, category: str | None = None, limit: int = 100) -> list[dict]:
        """List facts, optionally filtered by category.

        Args:
            category: Optional category filter.
            limit: Maximum number of facts.

        Returns:
            List of fact dicts.
        """
        return self._sqlite.list_facts(category, limit)

    def delete_fact(self, key: str) -> bool:
        """Delete a fact by key.

        Args:
            key: The fact key to delete.

        Returns:
            True if deleted, False if not found.
        """
        return self._sqlite.delete_fact(key)

    # --- Stats ---

    def get_stats(self) -> dict[str, int | bool]:
        """Get memory statistics.

        Returns:
            Dictionary with memory stats
        """
        stats: dict[str, int | bool] = {
            "has_long_term": self.memory_file.exists(),
            "has_history": self.history_file.exists(),
            "has_soul": self.soul_file.exists(),
            "has_identity": self.identity_file.exists(),
            "long_term_size": 0,
            "history_size": 0,
            "soul_size": 0,
            "identity_size": 0,
            "history_entries": 0,
        }

        if self.memory_file.exists():
            stats["long_term_size"] = self.memory_file.stat().st_size

        if self.history_file.exists():
            stats["history_size"] = self.history_file.stat().st_size
            # Count entries by counting "### [" markers
            try:
                content = self.history_file.read_text(encoding="utf-8")
                stats["history_entries"] = content.count("### [")
            except Exception:
                pass

        if self.soul_file.exists():
            stats["soul_size"] = self.soul_file.stat().st_size
        if self.identity_file.exists():
            stats["identity_size"] = self.identity_file.stat().st_size

        # SQLite stats
        try:
            sqlite_stats = self._sqlite.get_stats()
            stats["sqlite_event_count"] = sqlite_stats["event_count"]
            stats["sqlite_fact_count"] = sqlite_stats["fact_count"]
            stats["sqlite_db_size"] = sqlite_stats["db_size_bytes"]
        except Exception as e:
            logger.debug(f"Could not get SQLite stats: {e}")

        return stats


class MemoryManager:
    """Manages both user-level and project-level memory stores.

    Provides a unified interface that merges context from both stores,
    with project-level memory taking precedence for search results.
    """

    def __init__(self, project_root: Path | None = None):
        """Initialize memory manager.

        Args:
            project_root: Project root directory (defaults to cwd)
        """
        self.user_store = MemoryStore(MemoryStore.get_user_memory_dir())
        self.project_store = MemoryStore(MemoryStore.get_project_memory_dir(project_root))

    def _store_for_scope(self, scope: str) -> MemoryStore:
        """Return the memory store for a scope.

        Args:
            scope: "user" or "project"

        Returns:
            The selected memory store. Non-project scopes default to user store.
        """
        return self.project_store if scope == "project" else self.user_store

    def _ensure_global_persona_scope(self, scope: str) -> None:
        """Reject project-scoped persona operations."""
        if scope != "user":
            raise ValueError("Agent identity and soul are global-only; use scope='user'.")

    def get_memory_context(self) -> str:
        """Get combined memory context from both stores.

        Returns:
            Combined memory context string
        """
        parts = []

        # User-level memory
        user_ctx = self.user_store.get_memory_context()
        if user_ctx:
            parts.append(user_ctx)

        # Project-level memory
        project_ctx = self.project_store.get_memory_context()
        if project_ctx:
            parts.append(project_ctx)

        return "\n\n".join(parts)

    def get_persona_context(self) -> str:
        """Get the effective global persona context.

        Persona is intentionally global-only: long-running agents should keep a
        single durable identity across Telegram, CLI, and project sessions.
        Project memory is still merged separately, but project SOUL.md and
        IDENTITY.md are not injected to avoid conflicting identities.
        """
        return self.user_store.get_persona_context(
            "Global",
            include_default_soul=not self.user_store.has_custom_persona(),
        )

    def read_soul(self, scope: str = "user", include_default: bool = False) -> str:
        """Read soul from the specified scope."""
        self._ensure_global_persona_scope(scope)
        return self.user_store.read_soul(include_default=include_default)

    def write_soul(self, content: str, scope: str = "user") -> None:
        """Write soul to the specified scope."""
        self._ensure_global_persona_scope(scope)
        self.user_store.write_soul(content)

    def read_identity(self, scope: str = "user") -> str:
        """Read identity from the specified scope."""
        self._ensure_global_persona_scope(scope)
        return self.user_store.read_identity()

    def write_identity(self, content: str, scope: str = "user") -> None:
        """Write identity to the specified scope."""
        self._ensure_global_persona_scope(scope)
        self.user_store.write_identity(content)

    def read_long_term(self, scope: str = "user") -> str:
        """Read long-term memory from the specified scope.

        Args:
            scope: "user" or "project"

        Returns:
            Memory content
        """
        store = self._store_for_scope(scope)
        return store.read_long_term()

    def write_long_term(self, content: str, scope: str = "user") -> bool:
        """Write long-term memory to the specified scope.

        Args:
            content: Memory content
            scope: "user" or "project"

        Returns:
            Whether the content was written successfully.
        """
        store = self._store_for_scope(scope)
        return store.write_long_term(content)

    def append_history(
        self,
        content: str,
        session_id: str = "unknown",
        tags: list[str] | None = None,
        scope: str = "user",
    ) -> None:
        """Append to history log.

        Args:
            content: Content to log
            session_id: Session ID
            tags: Optional tags
            scope: "user" or "project"
        """
        store = self._store_for_scope(scope)
        store.append_history(content, session_id, MemoryEntry._normalize_tags(tags))

    def search(self, query: str, max_results: int = 20) -> list[MemorySearchResult]:
        """Search across both memory stores.

        Args:
            query: Search query
            max_results: Maximum results

        Returns:
            Combined search results
        """
        results: list[MemorySearchResult] = []
        # Project results first (higher precedence)
        results.extend(self.project_store.search_memory(query, max_results))
        remaining = max_results - len(results)
        if remaining > 0:
            results.extend(self.user_store.search_memory(query, remaining))
        return results

    # --- Facts (delegated to scoped store) ---

    def upsert_fact(
        self,
        key: str,
        value: str,
        category: str = "general",
        source: str = "agent",
        confidence: float = 1.0,
        scope: str = "user",
    ) -> None:
        """Insert or update a declarative fact.

        Args:
            key: Unique key for the fact.
            value: Fact value/content.
            category: Category for grouping.
            source: Source of the fact.
            confidence: Confidence score (0.0-1.0).
            scope: "user" or "project".
        """
        store = self._store_for_scope(scope)
        store.upsert_fact(key, value, category, source, confidence)

    def get_fact(self, key: str, scope: str = "user") -> dict | None:
        """Get a specific fact by key.

        Args:
            key: The fact key.
            scope: "user" or "project".

        Returns:
            Fact dict or None.
        """
        store = self._store_for_scope(scope)
        return store.get_fact(key)

    def search_facts(
        self,
        query: str,
        max_results: int = 20,
        scope: str = "user",
    ) -> list[dict]:
        """Search facts using FTS5.

        Args:
            query: Search query.
            max_results: Maximum results.
            scope: "user" or "project".

        Returns:
            List of matching fact dicts.
        """
        store = self._store_for_scope(scope)
        return store.search_facts(query, max_results)

    def list_facts(
        self,
        category: str | None = None,
        limit: int = 100,
        scope: str = "user",
    ) -> list[dict]:
        """List facts, optionally filtered by category.

        Args:
            category: Optional category filter.
            limit: Maximum number of facts.
            scope: "user" or "project".

        Returns:
            List of fact dicts.
        """
        store = self._store_for_scope(scope)
        return store.list_facts(category, limit)

    def delete_fact(self, key: str, scope: str = "user") -> bool:
        """Delete a fact by key.

        Args:
            key: The fact key to delete.
            scope: "user" or "project".

        Returns:
            True if deleted, False if not found.
        """
        store = self._store_for_scope(scope)
        return store.delete_fact(key)

    def get_stats(self) -> dict[str, dict[str, int | bool]]:
        """Get stats for both stores.

        Returns:
            Dictionary with user and project stats
        """
        return {
            "user": self.user_store.get_stats(),
            "project": self.project_store.get_stats(),
        }


# --- Global Memory Manager ---

_memory_manager: MemoryManager | None = None
_memory_manager_project_root: Path | None = None


def _create_memory_manager(project_root: Path | None) -> MemoryManager:
    """Create a memory manager based on the ``[memory]`` config section.

    With ``backend = "http"`` this returns an ``HttpMemoryManager`` so every
    caller of ``get_memory_manager`` (agent context injection, the memory tool,
    sub-agents) transparently uses the remote backend. Unknown or incomplete
    configuration falls back to the local backend.
    """
    from .config import load_config

    try:
        memory_cfg = load_config().memory
    except Exception as e:
        logger.warning(f"Could not load config; using local memory backend: {e}")
        memory_cfg = None

    if memory_cfg is not None and memory_cfg.backend == "http":
        if not memory_cfg.url:
            logger.error("[memory] backend='http' requires 'url'; falling back to local backend")
        else:
            from .memory_http import HttpMemoryManager

            token = memory_cfg.token or os.environ.get("ANKA_MEMORY_TOKEN")
            return HttpMemoryManager(
                base_url=memory_cfg.url,
                token=token,
                timeout_seconds=memory_cfg.timeout_seconds,
                project_root=project_root,
            )
    elif memory_cfg is not None and memory_cfg.backend != "local":
        logger.warning(f"Unknown [memory] backend {memory_cfg.backend!r}; using local backend")

    return MemoryManager(project_root)


def get_memory_manager(project_root: Path | None = None) -> MemoryManager:
    """Get or create the global memory manager.

    Args:
        project_root: Project root directory

    Returns:
        Global MemoryManager instance
    """
    global _memory_manager, _memory_manager_project_root
    resolved_root = project_root.resolve() if project_root else None
    if _memory_manager is None or (resolved_root is not None and resolved_root != _memory_manager_project_root):
        _memory_manager = _create_memory_manager(resolved_root)
        _memory_manager_project_root = resolved_root
    return _memory_manager


def reset_memory_manager() -> None:
    """Reset the global memory manager (for testing)."""
    global _memory_manager, _memory_manager_project_root
    _memory_manager = None
    _memory_manager_project_root = None
