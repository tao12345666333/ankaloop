"""SQLite+FTS5 transcript search for persisted AnkaLoop sessions."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .constants import CONFIG_DIR

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'agent',
    chat_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


@dataclass
class TranscriptSearchResult:
    """A matching transcript message."""

    timestamp: str
    session_id: str
    source: str
    chat_id: str | None
    role: str
    content: str
    snippet: str


class TranscriptStore:
    """Persistent transcript store with FTS5 search."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (CONFIG_DIR / "sessions" / "transcripts.db")
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA_SQL)
        return self._conn

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        source: str = "agent",
        chat_id: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Persist one user or assistant transcript message."""
        stripped = content.strip()
        if not stripped:
            return 0
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO messages "
            "(timestamp, session_id, source, chat_id, role, content, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                session_id,
                source,
                chat_id,
                role,
                stripped,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0

    def append_turn(
        self,
        *,
        session_id: str,
        user: str,
        assistant: str,
        source: str = "agent",
        chat_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Persist a completed user/assistant turn."""
        self.append_message(
            session_id=session_id,
            role="user",
            content=user,
            source=source,
            chat_id=chat_id,
            metadata=metadata,
        )
        self.append_message(
            session_id=session_id,
            role="assistant",
            content=assistant,
            source=source,
            chat_id=chat_id,
            metadata=metadata,
        )

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        session_id: str | None = None,
        source: str | None = None,
    ) -> list[TranscriptSearchResult]:
        """Search persisted transcripts."""
        fts_query = self._to_fts_query(query)
        if not fts_query:
            return []
        max_results = max(1, min(max_results, 50))
        filters: list[str] = ["messages_fts MATCH ?"]
        params: list[object] = [fts_query]
        if session_id:
            filters.append("m.session_id = ?")
            params.append(session_id)
        if source:
            filters.append("m.source = ?")
            params.append(source)
        params.append(max_results)
        sql = (
            "SELECT m.timestamp, m.session_id, m.source, m.chat_id, m.role, m.content, "
            "snippet(messages_fts, 0, '[', ']', '…', 24) AS snippet "
            "FROM messages_fts "
            "JOIN messages m ON m.id = messages_fts.rowid "
            f"WHERE {' AND '.join(filters)} "
            "ORDER BY bm25(messages_fts), m.id DESC LIMIT ?"
        )
        try:
            rows = self._get_conn().execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.debug(f"Transcript FTS search failed: {e}")
            return []
        return [
            TranscriptSearchResult(
                timestamp=row["timestamp"],
                session_id=row["session_id"],
                source=row["source"],
                chat_id=row["chat_id"],
                role=row["role"],
                content=row["content"],
                snippet=row["snippet"] or row["content"],
            )
            for row in rows
        ]

    @staticmethod
    def _to_fts_query(query: str) -> str:
        terms = re.findall(r"[\w-]+", query, flags=re.UNICODE)
        return " OR ".join(f'"{term}"' for term in terms[:12])


_default_store: TranscriptStore | None = None


def get_transcript_store(db_path: Path | None = None) -> TranscriptStore:
    """Return the process-global transcript store."""
    global _default_store
    if db_path is not None:
        return TranscriptStore(db_path)
    if _default_store is None:
        _default_store = TranscriptStore()
    return _default_store
