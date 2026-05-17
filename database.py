from __future__ import annotations

import aiosqlite
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    group_name TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    sender_id INTEGER,
    sender_name TEXT,
    text TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    biz_date TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    UNIQUE(group_id, message_id)
);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_date TEXT NOT NULL,
    group_id INTEGER NOT NULL,
    group_name TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    UNIQUE(biz_date, group_id)
);

CREATE TABLE IF NOT EXISTS monitored_groups (
    group_id INTEGER PRIMARY KEY,
    group_name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    added_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocked_senders (
    sender_id INTEGER PRIMARY KEY,
    sender_name TEXT,
    reason TEXT NOT NULL DEFAULT 'ad',
    blocked_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    group_name TEXT NOT NULL,
    sender_id INTEGER,
    sender_name TEXT,
    keywords TEXT NOT NULL,
    message_text TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_biz_date ON messages(biz_date);
CREATE INDEX IF NOT EXISTS idx_messages_group_date ON messages(group_id, biz_date);
CREATE INDEX IF NOT EXISTS idx_summaries_biz_date ON summaries(biz_date);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);
"""


class Database:
    def __init__(self, db_path: str):
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    # --- messages ---

    async def insert_message(
        self,
        group_id: int,
        group_name: str,
        message_id: int,
        sender_id: int | None,
        sender_name: str | None,
        text: str,
        timestamp: int,
        biz_date: str,
    ) -> bool:
        import sqlite3
        try:
            cursor = await self.conn.execute(
                """INSERT OR IGNORE INTO messages
                   (group_id, group_name, message_id, sender_id, sender_name, text, timestamp, biz_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (group_id, group_name, message_id, sender_id, sender_name, text, timestamp, biz_date),
            )
            await self.conn.commit()
            return cursor.rowcount > 0
        except sqlite3.IntegrityError:
            return False

    async def get_messages_by_date(
        self, biz_date: str, group_id: int | None = None,
        limit: int = 0, offset: int = 0,
    ) -> list[dict]:
        params: list = []
        sql = """SELECT group_id, group_name, message_id, sender_id, sender_name, text, timestamp
                 FROM messages WHERE biz_date = ?"""
        params.append(biz_date)

        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)

        sql += " ORDER BY message_id DESC"

        if limit > 0:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [
            {
                "group_id": r[0],
                "group_name": r[1],
                "message_id": r[2],
                "sender_id": r[3],
                "sender_name": r[4],
                "text": r[5],
                "timestamp": r[6],
            }
            for r in rows
        ]

    async def get_message_count_by_date(self, biz_date: str, group_id: int | None = None) -> int:
        if group_id is not None:
            cursor = await self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE biz_date = ? AND group_id = ?",
                (biz_date, group_id),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE biz_date = ?", (biz_date,)
            )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def delete_messages_by_date(self, biz_date: str, before_timestamp: int | None = None) -> int:
        if before_timestamp is not None:
            cursor = await self.conn.execute(
                "DELETE FROM messages WHERE biz_date = ? AND timestamp <= ?",
                (biz_date, before_timestamp),
            )
        else:
            cursor = await self.conn.execute(
                "DELETE FROM messages WHERE biz_date = ?", (biz_date,)
            )
        await self.conn.commit()
        return cursor.rowcount

    async def get_last_message_id(self, group_id: int) -> int | None:
        cursor = await self.conn.execute(
            "SELECT MAX(message_id) FROM messages WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else None

    async def get_message_texts_by_date(self, biz_date: str) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT text FROM messages WHERE biz_date = ?", (biz_date,)
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def get_today_message_count(self, biz_date: str) -> int:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE biz_date = ?", (biz_date,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_unsummarized_dates(self) -> list[dict]:
        cursor = await self.conn.execute(
            """SELECT m.biz_date, COUNT(*) as msg_count
               FROM messages m
               LEFT JOIN summaries s ON m.biz_date = s.biz_date
               WHERE s.id IS NULL
               GROUP BY m.biz_date
               ORDER BY m.biz_date DESC"""
        )
        rows = await cursor.fetchall()
        return [{"biz_date": r[0], "msg_count": r[1]} for r in rows]

    # --- summaries ---

    async def insert_summary(
        self,
        biz_date: str,
        group_id: int,
        group_name: str,
        message_count: int,
        summary_text: str,
    ) -> None:
        await self.conn.execute(
            """INSERT OR REPLACE INTO summaries
               (biz_date, group_id, group_name, message_count, summary_text)
               VALUES (?, ?, ?, ?, ?)""",
            (biz_date, group_id, group_name, message_count, summary_text),
        )
        await self.conn.commit()

    async def get_summaries_by_date(
        self, biz_date: str, group_id: int | None = None
    ) -> list[dict]:
        if group_id is not None:
            cursor = await self.conn.execute(
                """SELECT biz_date, group_id, group_name, message_count, summary_text, created_at
                   FROM summaries WHERE biz_date = ? AND group_id = ?""",
                (biz_date, group_id),
            )
        else:
            cursor = await self.conn.execute(
                """SELECT biz_date, group_id, group_name, message_count, summary_text, created_at
                   FROM summaries WHERE biz_date = ?
                   ORDER BY group_id""",
                (biz_date,),
            )
        rows = await cursor.fetchall()
        return [
            {
                "biz_date": r[0],
                "group_id": r[1],
                "group_name": r[2],
                "message_count": r[3],
                "summary_text": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

    async def get_available_dates(self, limit: int = 30) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT DISTINCT biz_date FROM summaries ORDER BY biz_date DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def delete_expired_summaries(self, cutoff_date: str) -> int:
        cursor = await self.conn.execute(
            "DELETE FROM summaries WHERE biz_date < ?", (cutoff_date,)
        )
        await self.conn.commit()
        return cursor.rowcount

    # --- monitored_groups ---

    async def upsert_group(self, group_id: int, group_name: str) -> None:
        await self.conn.execute(
            """INSERT INTO monitored_groups (group_id, group_name)
               VALUES (?, ?)
               ON CONFLICT(group_id) DO UPDATE SET group_name = excluded.group_name""",
            (group_id, group_name),
        )
        await self.conn.commit()

    async def get_active_groups(self) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT group_id, group_name FROM monitored_groups WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
        return [{"group_id": r[0], "group_name": r[1]} for r in rows]

    async def toggle_group(self, group_id: int, is_active: bool) -> None:
        await self.conn.execute(
            "UPDATE monitored_groups SET is_active = ? WHERE group_id = ?",
            (1 if is_active else 0, group_id),
        )
        await self.conn.commit()

    async def list_all_groups(self) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT group_id, group_name, is_active FROM monitored_groups ORDER BY group_name"
        )
        rows = await cursor.fetchall()
        return [
            {"group_id": r[0], "group_name": r[1], "is_active": bool(r[2])}
            for r in rows
        ]

    # --- settings ---

    async def get_setting(self, key: str, default: str = "") -> str:
        cursor = await self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self.conn.commit()

    # --- blocked_senders ---

    async def block_sender(self, sender_id: int, sender_name: str | None = None, reason: str = "ad") -> None:
        await self.conn.execute(
            """INSERT OR REPLACE INTO blocked_senders (sender_id, sender_name, reason)
               VALUES (?, ?, ?)""",
            (sender_id, sender_name, reason),
        )
        await self.conn.commit()

    async def unblock_sender(self, sender_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM blocked_senders WHERE sender_id = ?", (sender_id,)
        )
        await self.conn.commit()

    async def is_sender_blocked(self, sender_id: int) -> bool:
        cursor = await self.conn.execute(
            "SELECT 1 FROM blocked_senders WHERE sender_id = ?", (sender_id,)
        )
        return await cursor.fetchone() is not None

    async def get_blocked_senders(self) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT sender_id, sender_name, reason, blocked_at FROM blocked_senders ORDER BY blocked_at DESC"
        )
        rows = await cursor.fetchall()
        return [
            {"sender_id": r[0], "sender_name": r[1], "reason": r[2], "blocked_at": r[3]}
            for r in rows
        ]

    # --- alerts ---

    async def insert_alert(
        self,
        group_id: int,
        group_name: str,
        sender_id: int | None,
        sender_name: str,
        keywords: str,
        message_text: str,
    ) -> None:
        await self.conn.execute(
            """INSERT INTO alerts (group_id, group_name, sender_id, sender_name, keywords, message_text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (group_id, group_name, sender_id, sender_name, keywords, message_text),
        )
        await self.conn.commit()

    async def get_alerts(self, limit: int = 50, offset: int = 0) -> list[dict]:
        cursor = await self.conn.execute(
            """SELECT id, group_name, sender_name, keywords, message_text, created_at
               FROM alerts ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "group_name": r[1],
                "sender_name": r[2],
                "keywords": r[3],
                "message_text": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

    async def get_alert_count(self) -> int:
        cursor = await self.conn.execute("SELECT COUNT(*) FROM alerts")
        row = await cursor.fetchone()
        return row[0] if row else 0
