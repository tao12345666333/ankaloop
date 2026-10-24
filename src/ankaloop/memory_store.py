"""
SQLite+FTS5 backed persistent searchable memory store.

Provides a two-tier memory architecture:
- Episodic memory (events): Append-only log of conversations, tool results, observations
- Declarative memory (facts): Key-value store for extracted facts, preferences, patterns

Uses FTS5 full-text search for efficient querying across both tiers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .memory import MemorySearchResult

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
-- Episodic memory: append-only event log
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT 'unknown',
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'agent'
);

-- FTS5 index for events
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    content,
    tags,
    content=events,
    content_rowid=id
);

-- Triggers to keep events_fts in sync
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
    INSERT INTO events_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;

-- Declarative memory: key-value fact store
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    source TEXT NOT NULL DEFAULT 'agent',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- FTS5 index for facts
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    key,
    value,
    category,
    content=facts,
    content_rowid=id
);

-- Triggers to keep facts_fts in sync
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, key, value, category)
    VALUES (new.id, new.key, new.value, new.category);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, key, value, category)
    VALUES ('delete', old.id, old.key, old.value, old.category);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, key, value, category)
    VALUES ('delete', old.id, old.key, old.value, old.category);
    INSERT INTO facts_fts(rowid, key, value, category)
    VALUES (new.id, new.key, new.value, new.category);
END;
"""


class SQLiteMemoryStore:
    """SQLite+FTS5 backed persistent memory store."""

    def __init__(self, db_path: Path):
        """Initialize the SQLite memory store.

        Args:
            db_path: Path to the SQLite database file.
        """
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create DB connection, initializing schema if needed."""
        if self._conn is not None:
            return self._conn

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema(self._conn)
        return self._conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables and FTS indexes."""
        conn.executescript(_SCHEMA_SQL)

    # --- Events (episodic) ---

    def append_event(
        self,
        content: str,
        session_id: str = "unknown",
        tags: list[str] | None = None,
        source: str = "agent",
    ) -> int:
        """Append an event to the episodic memory log.

        Args:
            content: Event content text.
            session_id: Current session identifier.
            tags: Optional list of tags for categorization.
            source: Source of the event.

        Returns:
            The row ID of the inserted event.
        """
        conn = self._get_conn()
        now = datetime.now().isoformat(timespec="seconds")
        tags_json = json.dumps(tags or [])
        cursor = conn.execute(
            "INSERT INTO events (timestamp, session_id, content, tags, source) VALUES (?, ?, ?, ?, ?)",
            (now, session_id, content, tags_json, source),
        )
        conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def search_events(self, query: str, max_results: int = 20) -> list[MemorySearchResult]:
        """Search episodic events using FTS5 full-text search.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return.

        Returns:
            List of matching MemorySearchResult objects.
        """
        rows = self._run_fts_query(
            table="events",
            fts_table="events_fts",
            select_columns="f.id, f.content, f.timestamp, f.tags",
            query=query,
            max_results=max_results,
        )
        return [self._event_row_to_search_result(row) for row in rows]

    def get_recent_events(self, limit: int = 50) -> list[dict]:
        """Get the most recent events.

        Args:
            limit: Maximum number of events to return.

        Returns:
            List of event dicts ordered newest-first.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, timestamp, session_id, content, tags, source FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_events_after(self, after_id: int = 0, limit: int = 50) -> list[dict]:
        """Get events with an id greater than ``after_id``, ordered oldest-first.

        Args:
            after_id: Exclusive lower bound on event ids (a consolidation cursor).
            limit: Maximum number of events to return.

        Returns:
            List of event dicts ordered oldest-first.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, timestamp, session_id, content, tags, source FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- Facts (declarative) ---

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
            category: Category for grouping facts.
            source: Source of the fact.
            confidence: Confidence score (0.0-1.0).
        """
        conn = self._get_conn()
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO facts (key, value, category, source, confidence, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, category=excluded.category, "
            "source=excluded.source, confidence=excluded.confidence, "
            "updated_at=excluded.updated_at",
            (key, value, category, source, confidence, now, now),
        )
        conn.commit()

    def get_fact(self, key: str) -> dict | None:
        """Get a specific fact by key.

        Args:
            key: The fact key to look up.

        Returns:
            Fact dict or None if not found.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, key, value, category, source, confidence, created_at, updated_at FROM facts WHERE key = ?",
            (key,),
        ).fetchone()
        return dict(row) if row else None

    def search_facts(self, query: str, max_results: int = 20) -> list[dict]:
        """Search facts using FTS5 full-text search.

        Args:
            query: Search query string.
            max_results: Maximum number of results.

        Returns:
            List of matching fact dicts.
        """
        rows = self._run_fts_query(
            table="facts",
            fts_table="facts_fts",
            select_columns="f.id, f.key, f.value, f.category, f.source, f.confidence, f.created_at, f.updated_at",
            query=query,
            max_results=max_results,
        )
        return [dict(row) for row in rows]

    def _run_fts_query(
        self,
        *,
        table: str,
        fts_table: str,
        select_columns: str,
        query: str,
        max_results: int,
    ) -> list[sqlite3.Row]:
        """Execute a standard FTS query with shared error handling."""
        safe_query = _sanitize_fts_query(query)
        if not safe_query:
            return []

        conn = self._get_conn()
        sql = (
            f"SELECT {select_columns} "
            f"FROM {fts_table} ft "
            f"JOIN {table} f ON f.id = ft.rowid "
            f"WHERE {fts_table} MATCH ? "
            "ORDER BY rank "
            "LIMIT ?"
        )

        try:
            return conn.execute(sql, (safe_query, max_results)).fetchall()
        except sqlite3.OperationalError:
            logger.debug(f"FTS query failed for {table}: {safe_query!r}")
            return []

    @staticmethod
    def _event_row_to_search_result(row: sqlite3.Row) -> MemorySearchResult:
        """Convert an events table row to MemorySearchResult."""
        return MemorySearchResult(
            line_number=row["id"],
            content=row["content"],
            source="events",
        )

    def list_facts(self, category: str | None = None, limit: int = 100) -> list[dict]:
        """List facts, optionally filtered by category.

        Args:
            category: Optional category filter.
            limit: Maximum number of facts to return.

        Returns:
            List of fact dicts.
        """
        conn = self._get_conn()
        if category:
            rows = conn.execute(
                "SELECT id, key, value, category, source, confidence, "
                "created_at, updated_at FROM facts "
                "WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, key, value, category, source, confidence, "
                "created_at, updated_at FROM facts "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_fact(self, key: str) -> bool:
        """Delete a fact by key.

        Args:
            key: The fact key to delete.

        Returns:
            True if a fact was deleted, False if not found.
        """
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM facts WHERE key = ?", (key,))
        conn.commit()
        return cursor.rowcount > 0

    # --- Combined search ---

    def search(self, query: str, max_results: int = 20) -> list[MemorySearchResult]:
        """Search across both events and facts.

        Facts are returned first, then events.

        Args:
            query: Search query string.
            max_results: Maximum total results.

        Returns:
            Combined list of MemorySearchResult objects.
        """
        results: list[MemorySearchResult] = []

        # Facts first (declarative knowledge has higher priority)
        fact_results = self.search_facts(query, max_results=max_results)
        for fact in fact_results:
            results.append(
                MemorySearchResult(
                    line_number=fact["id"],
                    content=f"[{fact['category']}] {fact['key']}: {fact['value']}",
                    source="facts",
                )
            )

        # Then events
        remaining = max_results - len(results)
        if remaining > 0:
            results.extend(self.search_events(query, max_results=remaining))

        return results

    # --- Stats ---

    def get_stats(self) -> dict:
        """Get statistics about the SQLite memory store.

        Returns:
            Dictionary with event and fact counts and DB size.
        """
        conn = self._get_conn()
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        fact_count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        db_size = self._db_path.stat().st_size if self._db_path.exists() else 0
        return {
            "event_count": event_count,
            "fact_count": fact_count,
            "db_size_bytes": db_size,
        }

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a user query for safe use with FTS5 MATCH.

    Wraps each token in double quotes to prevent FTS5 syntax injection.

    Args:
        query: Raw user query string.

    Returns:
        Sanitized FTS5 query string.
    """
    tokens = query.split()
    if not tokens:
        return ""
    # Quote each token to avoid FTS5 syntax errors
    return " ".join(f'"{t}"' for t in tokens)
