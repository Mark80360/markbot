"""SQLite-backed session store for web chat persistence."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


_DATA_DIR = Path.home() / ".markbot"
_DB_PATH = _DATA_DIR / "web_chat.db"


class WebSessionStore:
    def __init__(self, db_path: str | Path = _DB_PATH):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self._db_path))
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    def _init_db(self):
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS web_sessions (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL,
                last_active REAL NOT NULL,
                message_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS web_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL REFERENCES web_sessions(id) ON DELETE CASCADE,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                timestamp   REAL NOT NULL,
                metadata    TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON web_messages(session_id, id);
            CREATE VIRTUAL TABLE IF NOT EXISTS web_messages_fts
                USING fts5(content, session_id, content=web_messages, content_rowid=id);
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON web_messages BEGIN
                INSERT INTO web_messages_fts(rowid, content, session_id)
                VALUES (new.id, new.content, new.session_id);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON web_messages BEGIN
                INSERT INTO web_messages_fts(web_messages_fts, rowid, content, session_id)
                VALUES ('delete', old.id, old.content, old.session_id);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON web_messages BEGIN
                INSERT INTO web_messages_fts(web_messages_fts, rowid, content, session_id)
                VALUES ('delete', old.id, old.content, old.session_id);
                INSERT INTO web_messages_fts(rowid, content, session_id)
                VALUES (new.id, new.content, new.session_id);
            END;
        """)
        conn.commit()
        conn.close()

    def create_session(self, session_id: str, title: str = "新对话") -> dict[str, Any]:
        now = time.time()
        conn = self._conn
        conn.execute(
            "INSERT INTO web_sessions (id, title, created_at, last_active) VALUES (?, ?, ?, ?)",
            (session_id, title, now, now),
        )
        conn.commit()
        return {"id": session_id, "title": title, "created_at": now, "last_active": now, "message_count": 0}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id, title, created_at, last_active, message_count FROM web_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        msgs = [
            {"role": r["role"], "content": r["content"], "timestamp": r["timestamp"],
             "metadata": json.loads(r["metadata"]) if r["metadata"] else {}}
            for r in self._conn.execute(
                "SELECT role, content, timestamp, metadata FROM web_messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        ]
        return dict(row) | {"messages": msgs}

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, title, created_at, last_active, message_count FROM web_sessions "
            "ORDER BY last_active DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM web_sessions WHERE id = ?", (session_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def update_title(self, session_id: str, title: str) -> bool:
        cur = self._conn.execute(
            "UPDATE web_sessions SET title = ? WHERE id = ?", (title, session_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def add_message(self, session_id: str, role: str, content: str, metadata: dict | None = None) -> int:
        now = time.time()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        cur = self._conn.execute(
            "INSERT INTO web_messages (session_id, role, content, timestamp, metadata) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, now, meta_json),
        )
        self._conn.execute(
            "UPDATE web_sessions SET last_active = ?, message_count = message_count + 1 WHERE id = ?",
            (now, session_id),
        )
        self._conn.commit()
        return cur.lastrowid or 0

    def search_sessions(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        if not query.strip():
            return self.list_sessions(limit=limit)
        try:
            rows = self._conn.execute(
                "SELECT DISTINCT s.id, s.title, s.created_at, s.last_active, s.message_count "
                "FROM web_sessions s "
                "JOIN web_messages_fts fts ON s.id = fts.session_id "
                "WHERE web_messages_fts MATCH ? "
                "ORDER BY s.last_active DESC LIMIT ?",
                (query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return self.list_sessions(limit=limit)

    def delete_empty_sessions(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM web_sessions WHERE message_count = 0"
        )
        self._conn.commit()
        return cur.rowcount
