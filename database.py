from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiosqlite
from pathlib import Path

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(
    r"https?://[^\s<>'\"]+|(?<![\w.])t\.me/[A-Za-z0-9_/?=&.%#-]+|(?<![\w.])@[A-Za-z0-9_]{5,32}",
    re.IGNORECASE,
)
_URL_TRAILING_PUNCTUATION = ".,!?;:)]}，。！？；：）】》、"
_URL_MAX_TEXT_LENGTH = 20000
_URL_CONTEXT_RADIUS = 120
_URL_DEDUPE_DELETE_BATCH_SIZE = 900
_URL_SUMMARY_UNIQUE_INDEX = "idx_url_entries_summary_url_unique"

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
    biz_period TEXT NOT NULL DEFAULT 'daily',
    group_id INTEGER NOT NULL,
    group_name TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    last_accessed_at INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS context_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    ref_message_id INTEGER NOT NULL,
    FOREIGN KEY (summary_id) REFERENCES summaries(id) ON DELETE CASCADE,
    UNIQUE(summary_id, ref_message_id)
);

CREATE TABLE IF NOT EXISTS context_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sender_name TEXT,
    text TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    FOREIGN KEY (window_id) REFERENCES context_windows(id) ON DELETE CASCADE,
    UNIQUE(window_id, message_id)
);

CREATE TABLE IF NOT EXISTS monitored_groups (
    group_id INTEGER PRIMARY KEY,
    group_name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    summary_cron TEXT DEFAULT NULL,
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

CREATE TABLE IF NOT EXISTS sender_profiles (
    sender_id INTEGER PRIMARY KEY,
    access_hash INTEGER,
    username TEXT,
    display_name TEXT,
    bio_text TEXT,
    bio_hash TEXT,
    fetched_at INTEGER,
    next_fetch_at INTEGER NOT NULL DEFAULT 0,
    fetch_status TEXT NOT NULL DEFAULT 'new',
    last_error TEXT,
    first_seen_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    last_seen_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    last_group_id INTEGER,
    last_group_name TEXT,
    last_message_id INTEGER,
    last_message_text TEXT,
    is_ad_candidate INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sender_group_seen (
    sender_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    group_name TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    last_message_id INTEGER,
    PRIMARY KEY(sender_id, group_id)
);

CREATE TABLE IF NOT EXISTS bio_fetch_queue (
    sender_id INTEGER PRIMARY KEY,
    priority INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_run_at INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS url_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    source_label TEXT,
    source_context TEXT,
    biz_date TEXT,
    group_id INTEGER,
    group_name TEXT,
    sender_id INTEGER,
    sender_name TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    UNIQUE(url, source_type, source_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_biz_date ON messages(biz_date);
CREATE INDEX IF NOT EXISTS idx_messages_group_date ON messages(group_id, biz_date);
CREATE INDEX IF NOT EXISTS idx_messages_group_msgid ON messages(group_id, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_group_timestamp ON messages(group_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_summaries_biz_date ON summaries(biz_date);
CREATE INDEX IF NOT EXISTS idx_summaries_group_date ON summaries(group_id, biz_date);
CREATE INDEX IF NOT EXISTS idx_context_windows_summary ON context_windows(summary_id);
CREATE INDEX IF NOT EXISTS idx_context_messages_window ON context_messages(window_id);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);
CREATE INDEX IF NOT EXISTS idx_sender_profiles_candidate ON sender_profiles(is_ad_candidate, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_sender_profiles_fetch ON sender_profiles(fetch_status, next_fetch_at);
CREATE INDEX IF NOT EXISTS idx_sender_group_seen_group ON sender_group_seen(group_id, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_bio_fetch_queue_next ON bio_fetch_queue(status, next_run_at, priority);
CREATE INDEX IF NOT EXISTS idx_url_entries_created ON url_entries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_url_entries_source ON url_entries(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_url_entries_domain ON url_entries(domain);
CREATE INDEX IF NOT EXISTS idx_url_entries_url ON url_entries(url);

CREATE TRIGGER IF NOT EXISTS trg_url_entries_summary_delete
AFTER DELETE ON summaries
BEGIN
    DELETE FROM url_entries WHERE source_type = 'summary' AND source_id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_url_entries_sender_delete
AFTER DELETE ON sender_profiles
BEGIN
    DELETE FROM url_entries WHERE source_type = 'bio' AND source_id = OLD.sender_id;
END;

CREATE TABLE IF NOT EXISTS summary_favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_date TEXT NOT NULL,
    biz_period TEXT NOT NULL DEFAULT 'daily',
    group_id INTEGER NOT NULL,
    custom_text TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    UNIQUE(biz_date, group_id, biz_period)
);

CREATE INDEX IF NOT EXISTS idx_summary_favorites_identity ON summary_favorites(biz_date, group_id, biz_period);
CREATE INDEX IF NOT EXISTS idx_summary_favorites_created ON summary_favorites(created_at);
"""


class Database:
    """Async SQLite wrapper for tg-lurker storage."""

    def __init__(self, db_path: str):
        """Initializes database with path to SQLite file."""
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._temp_table_counter = 0

    async def connect(self) -> None:
        """Opens connection, applies schema, and runs migrations."""
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        await self._migrate()

    async def _migrate(self) -> None:
        """Applies incremental schema changes for new columns and indexes."""
        await self._ensure_column(
            "monitored_groups",
            "summary_cron",
            "ALTER TABLE monitored_groups ADD COLUMN summary_cron TEXT DEFAULT NULL",
        )
        await self._ensure_column(
            "summaries",
            "last_accessed_at",
            "ALTER TABLE summaries ADD COLUMN last_accessed_at INTEGER NOT NULL DEFAULT 0",
        )
        await self._ensure_column(
            "summaries",
            "biz_period",
            "ALTER TABLE summaries ADD COLUMN biz_period TEXT NOT NULL DEFAULT 'daily'",
        )
        await self._rebuild_summaries_if_needed()
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_biz_date ON summaries(biz_date)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_group_date ON summaries(group_id, biz_date)"
        )
        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_biz_period ON summaries(biz_date, group_id, biz_period)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_accessed ON summaries(last_accessed_at)"
        )
        await self._ensure_column(
            "context_windows",
            "covered_refs",
            "ALTER TABLE context_windows ADD COLUMN covered_refs TEXT",
        )
        await self._migrate_favorites()
        await self._migrate_url_entries()
        await self._backfill_url_entries_once()
        await self.conn.commit()

    async def _migrate_favorites(self) -> None:
        """Creates summary_favorites table and indexes if they don't exist."""
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS summary_favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                biz_date TEXT NOT NULL,
                biz_period TEXT NOT NULL DEFAULT 'daily',
                group_id INTEGER NOT NULL,
                custom_text TEXT,
                created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                UNIQUE(biz_date, group_id, biz_period)
            )"""
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summary_favorites_identity ON summary_favorites(biz_date, group_id, biz_period)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summary_favorites_created ON summary_favorites(created_at)"
        )

    async def _migrate_url_entries(self) -> None:
        """Creates the collected URL table and indexes if they don't exist."""
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS url_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                domain TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                source_label TEXT,
                source_context TEXT,
                biz_date TEXT,
                group_id INTEGER,
                group_name TEXT,
                sender_id INTEGER,
                sender_name TEXT,
                created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                UNIQUE(url, source_type, source_id)
            )"""
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_url_entries_created ON url_entries(created_at DESC)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_url_entries_source ON url_entries(source_type, source_id)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_url_entries_domain ON url_entries(domain)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_url_entries_url ON url_entries(url)"
        )
        cursor = await self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (_URL_SUMMARY_UNIQUE_INDEX,),
        )
        if await cursor.fetchone() is None:
            await self._dedupe_summary_url_entries()
        await self.conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_URL_SUMMARY_UNIQUE_INDEX} "
            "ON url_entries(url) WHERE source_type = 'summary'"
        )
        await self.conn.execute(
            """CREATE TRIGGER IF NOT EXISTS trg_url_entries_summary_delete
               AFTER DELETE ON summaries
               BEGIN
                   DELETE FROM url_entries WHERE source_type = 'summary' AND source_id = OLD.id;
               END"""
        )
        await self.conn.execute(
            """CREATE TRIGGER IF NOT EXISTS trg_url_entries_sender_delete
               AFTER DELETE ON sender_profiles
               BEGIN
                   DELETE FROM url_entries WHERE source_type = 'bio' AND source_id = OLD.sender_id;
               END"""
        )

    async def _dedupe_summary_url_entries(self) -> None:
        """Keeps one summary URL entry per normalized URL before enforcing uniqueness."""
        await self.conn.execute(
            """DELETE FROM url_entries
               WHERE source_type = 'summary'
                 AND id NOT IN (
                     SELECT MAX(id)
                     FROM url_entries
                     WHERE source_type = 'summary'
                     GROUP BY url
                 )"""
        )

    async def _backfill_url_entries_once(self) -> None:
        """Backfills URLs from existing summaries and Bio rows once per database."""
        flag = await self.get_setting("url_entries_backfilled", "")
        if flag == "1":
            return
        await self.conn.execute("SAVEPOINT url_entries_backfill")
        try:
            count = await self.backfill_url_entries(commit=False)
            await self.conn.execute("RELEASE SAVEPOINT url_entries_backfill")
        except Exception:
            await self.conn.execute("ROLLBACK TO SAVEPOINT url_entries_backfill")
            await self.conn.execute("RELEASE SAVEPOINT url_entries_backfill")
            logger.exception("Failed to backfill URL entries; startup will continue")
            return
        await self.set_setting("url_entries_backfilled", "1")
        if count:
            logger.info("Backfilled %s URL entries", count)

    async def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        """Adds column to table if it doesn't already exist."""
        cursor = await self.conn.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        if any(row[1] == column for row in rows):
            return
        await self.conn.execute(ddl)
        await self.conn.commit()

    async def _rebuild_summaries_if_needed(self) -> None:
        """移除旧版 UNIQUE(biz_date, group_id)，允许同日多时段摘要并保留原 id。"""
        cursor = await self.conn.execute("PRAGMA index_list(summaries)")
        indexes = await cursor.fetchall()
        has_old_unique = False
        for index in indexes:
            index_name = index[1]
            is_unique = bool(index[2])
            if not is_unique:
                continue
            info_cursor = await self.conn.execute(f"PRAGMA index_info({index_name})")
            columns = [row[2] for row in await info_cursor.fetchall()]
            if columns == ["biz_date", "group_id"]:
                has_old_unique = True
                break

        if not has_old_unique:
            return

        await self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            await self.conn.execute("BEGIN")
            await self.conn.execute(
                """CREATE TABLE summaries_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    biz_date TEXT NOT NULL,
                    biz_period TEXT NOT NULL DEFAULT 'daily',
                    group_id INTEGER NOT NULL,
                    group_name TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    summary_text TEXT NOT NULL,
                    last_accessed_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                )"""
            )
            await self.conn.execute(
                """INSERT INTO summaries_new
                   (id, biz_date, biz_period, group_id, group_name, message_count,
                    summary_text, last_accessed_at, created_at)
                   SELECT id, biz_date, biz_period, group_id, group_name, message_count,
                          summary_text, last_accessed_at, created_at
                   FROM summaries"""
            )
            await self.conn.execute("DROP TABLE summaries")
            await self.conn.execute("ALTER TABLE summaries_new RENAME TO summaries")
            await self.conn.execute("COMMIT")
        except Exception:
            await self.conn.execute("ROLLBACK")
            raise
        finally:
            await self.conn.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        """Closes the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Returns active connection, raising if not connected."""
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
        """Inserts a message, ignoring duplicates by (group_id, message_id). Returns True if inserted."""
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
        """Fetches messages for a business date, optionally filtered by group, ordered by message_id DESC."""
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

    async def get_messages_since(self, group_id: int, since_ts: int, before_ts: int | None = None) -> list[dict]:
        """Fetches messages in a group after a timestamp, optionally bounded by an upper timestamp."""
        params: list[int] = [group_id, since_ts]
        upper = ""
        if before_ts is not None:
            upper = " AND timestamp <= ?"
            params.append(before_ts)
        cursor = await self.conn.execute(
            f"""SELECT group_id, group_name, message_id, sender_id, sender_name, text, timestamp
               FROM messages
               WHERE group_id = ? AND timestamp > ?{upper}
               ORDER BY timestamp ASC, message_id ASC""",
            params,
        )
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
        """Returns total message count for a business date, optionally filtered by group."""
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
        """Deletes messages for a business date, optionally only those at or before a timestamp. Returns count deleted."""
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
        """Returns the highest message_id for a group, or None if no messages exist."""
        cursor = await self.conn.execute(
            "SELECT MAX(message_id) FROM messages WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else None

    async def get_message_texts_by_date(self, biz_date: str) -> list[str]:
        """Returns raw text of all messages for a business date."""
        cursor = await self.conn.execute(
            "SELECT text FROM messages WHERE biz_date = ?", (biz_date,)
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def get_today_message_count(self, biz_date: str) -> int:
        """Returns total message count across all groups for a business date."""
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE biz_date = ?", (biz_date,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_today_stats_by_group(self, biz_date: str) -> list[dict]:
        """按群组统计当天消息数，群名取当天最新一条消息里的值。"""
        cursor = await self.conn.execute(
            """SELECT m.group_id,
                      (
                          SELECT m2.group_name
                          FROM messages m2
                          WHERE m2.biz_date = ? AND m2.group_id = m.group_id
                          ORDER BY m2.timestamp DESC, m2.id DESC
                          LIMIT 1
                      ) AS group_name,
                      COUNT(*) AS count
               FROM messages m
               WHERE m.biz_date = ?
               GROUP BY m.group_id
               ORDER BY count DESC""",
            (biz_date, biz_date),
        )
        rows = await cursor.fetchall()
        return [{"group_id": r[0], "group_name": r[1], "count": r[2]} for r in rows]

    async def get_today_hourly_distribution(
        self, biz_date: str, tz_name: str, group_id: int | None = None
    ) -> list[dict]:
        """按业务时区统计当天 0-23 点消息分布，避免依赖 SQLite/宿主机时区。"""
        params: list = [biz_date]
        sql = "SELECT timestamp FROM messages WHERE biz_date = ?"
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)

        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        tz = ZoneInfo(tz_name)
        counts = Counter(datetime.fromtimestamp(r[0], tz).hour for r in rows)
        return [{"hour": hour, "count": counts.get(hour, 0)} for hour in range(24)]

    async def get_today_top_senders(
        self, biz_date: str, group_id: int | None = None, limit: int = 10
    ) -> list[dict]:
        """按 sender_id 和 sender_name 统计当天发言人，避免同名用户被合并。"""
        params: list = [biz_date]
        sql = """SELECT sender_id, sender_name, COUNT(*) AS count
                 FROM messages
                 WHERE biz_date = ? AND sender_id IS NOT NULL"""
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        sql += " GROUP BY sender_id, sender_name ORDER BY count DESC LIMIT ?"
        params.append(limit)

        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [{"sender_id": r[0], "sender_name": r[1], "count": r[2]} for r in rows]

    async def get_unsummarized_dates(self) -> list[dict]:
        """Returns dates with messages and no auto summary, using date-level semantics."""
        cursor = await self.conn.execute(
            """SELECT m.biz_date, COUNT(*) as msg_count
               FROM messages m
               LEFT JOIN summaries s ON m.biz_date = s.biz_date
                                      AND s.biz_period NOT LIKE 'manual_%'
               WHERE s.id IS NULL
               GROUP BY m.biz_date
               ORDER BY m.biz_date DESC"""
        )
        rows = await cursor.fetchall()
        return [{"biz_date": r[0], "msg_count": r[1]} for r in rows]

    async def delete_unsummarized_messages_by_dates(self, biz_dates: list[str]) -> int:
        """Deletes date-level unsummarized messages, preserving any date with an auto summary."""
        if not biz_dates:
            return 0

        placeholders = ",".join("?" for _ in biz_dates)
        cursor = await self.conn.execute(
            f"""DELETE FROM messages
               WHERE biz_date IN ({placeholders})
                 AND NOT EXISTS (
                     SELECT 1 FROM summaries s
                     WHERE s.biz_date = messages.biz_date
                       AND s.biz_period NOT LIKE 'manual_%'
                 )""",
            tuple(biz_dates),
        )
        await self.conn.commit()
        return cursor.rowcount

    # --- summaries ---

    async def insert_summary(
        self,
        biz_date: str,
        group_id: int,
        group_name: str,
        message_count: int,
        summary_text: str,
        biz_period: str = "daily",
    ) -> int | None:
        """Inserts a summary row, returning its id. Returns None on duplicate (biz_date, group_id, biz_period)."""
        try:
            cursor = await self.conn.execute(
                """INSERT INTO summaries
                   (biz_date, biz_period, group_id, group_name, message_count, summary_text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (biz_date, biz_period, group_id, group_name, message_count, summary_text),
            )
            summary_id = cursor.lastrowid
            await self.sync_url_entries_for_source(
                "summary",
                summary_id,
                summary_text,
                source_label=group_name,
                biz_date=biz_date,
                group_id=group_id,
                group_name=group_name,
                commit=False,
            )
            await self.conn.commit()
            return summary_id
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                await self.conn.rollback()
                logger.warning(
                    "Skipped duplicate summary biz_date=%s group_id=%s biz_period=%s",
                    biz_date,
                    group_id,
                    biz_period,
                )
                return None
            raise
        except Exception:
            await self.conn.rollback()
            raise

    async def summary_exists(self, biz_date: str, group_id: int, biz_period: str) -> bool:
        """Checks whether a summary exists for the given date, group, and period."""
        cursor = await self.conn.execute(
            """SELECT 1 FROM summaries
               WHERE biz_date = ? AND group_id = ? AND biz_period = ?
               LIMIT 1""",
            (biz_date, group_id, biz_period),
        )
        return await cursor.fetchone() is not None

    async def get_last_summary_ts(self, group_id: int) -> int | None:
        """Returns the creation timestamp of the most recent summary for a group."""
        cursor = await self.conn.execute(
            "SELECT MAX(created_at) FROM summaries WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else None

    async def get_summaries_by_date(
        self, biz_date: str, group_id: int | None = None
    ) -> list[dict]:
        """Fetches summaries for a business date, optionally filtered by group."""
        select_summary = """SELECT s.id, s.biz_date, s.biz_period, s.group_id, s.group_name,
                                   s.message_count, s.summary_text, s.created_at,
                                   CASE WHEN f.id IS NOT NULL THEN 1 ELSE 0 END AS is_favorite,
                                   f.custom_text AS favorite_custom_text,
                                   f.created_at AS favorited_at"""
        select_group_order = """,
                                   gm.group_latest_created_at,
                                   gm.group_sort_name"""
        from_clause = """ FROM summaries s
                          LEFT JOIN summary_favorites f
                              ON f.biz_date = s.biz_date
                              AND f.group_id = s.group_id
                              AND f.biz_period = s.biz_period"""
        if group_id is not None:
            cursor = await self.conn.execute(
                select_summary
                + from_clause
                + " WHERE s.biz_date = ? AND s.group_id = ? ORDER BY s.created_at DESC, s.id DESC",
                (biz_date, group_id),
            )
        else:
            cursor = await self.conn.execute(
                """WITH group_meta AS (
                       SELECT group_id,
                              created_at AS group_latest_created_at,
                              group_name AS group_sort_name
                       FROM (
                           SELECT group_id, group_name, created_at,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY group_id
                                      ORDER BY created_at DESC, id DESC
                                  ) AS rn
                           FROM summaries
                           WHERE biz_date = ?
                       )
                       WHERE rn = 1
                   )
                   """
                + select_summary
                + select_group_order
                + from_clause
                + " JOIN group_meta gm ON gm.group_id = s.group_id"
                + """ WHERE s.biz_date = ?
                       ORDER BY group_latest_created_at DESC,
                                group_sort_name COLLATE NOCASE ASC,
                                s.group_id ASC,
                                s.created_at DESC,
                                s.id DESC""",
                (biz_date, biz_date),
            )
        rows = await cursor.fetchall()
        summaries = []
        for r in rows:
            summary = {
                "id": r[0],
                "biz_date": r[1],
                "biz_period": r[2],
                "group_id": r[3],
                "group_name": r[4],
                "message_count": r[5],
                "summary_text": r[6],
                "created_at": r[7],
                "is_favorite": bool(r[8]),
                "favorite_custom_text": r[9],
                "favorited_at": r[10],
            }
            if len(r) > 11:
                summary["group_latest_created_at"] = r[11]
                summary["group_sort_name"] = r[12]
            summaries.append(summary)
        return summaries

    async def get_available_dates(self, limit: int = 30) -> list[str]:
        """Returns distinct business dates that have summaries, most recent first."""
        cursor = await self.conn.execute(
            "SELECT DISTINCT biz_date FROM summaries ORDER BY biz_date DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def get_historical_daily_counts(self, limit: int = 30) -> list[dict]:
        """从历史摘要表统计每日消息总数。"""
        cursor = await self.conn.execute(
            """SELECT biz_date, SUM(message_count) AS total
               FROM summaries
               WHERE biz_period NOT LIKE 'manual_%'
               GROUP BY biz_date
               ORDER BY biz_date DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [{"biz_date": r[0], "total": r[1] or 0} for r in rows]

    async def get_summary(self, summary_id: int) -> dict | None:
        """Returns one summary row with favorite metadata but without context messages."""
        cursor = await self.conn.execute(
            """SELECT s.id, s.biz_date, s.biz_period, s.group_id, s.group_name,
                      s.message_count, s.summary_text, s.created_at,
                      CASE WHEN f.id IS NOT NULL THEN 1 ELSE 0 END AS is_favorite,
                      f.custom_text AS favorite_custom_text,
                      f.created_at AS favorited_at
               FROM summaries s
               LEFT JOIN summary_favorites f
                   ON f.biz_date = s.biz_date
                   AND f.group_id = s.group_id
                   AND f.biz_period = s.biz_period
               WHERE s.id = ?""",
            (summary_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        return {
            "id": row[0],
            "biz_date": row[1],
            "biz_period": row[2],
            "group_id": row[3],
            "group_name": row[4],
            "message_count": row[5],
            "summary_text": row[6],
            "created_at": row[7],
            "is_favorite": bool(row[8]),
            "favorite_custom_text": row[9],
            "favorited_at": row[10],
        }

    async def get_summary_with_context(self, summary_id: int) -> dict | None:
        """读取单条摘要及其全部上下文窗口和上下文消息，用于导出。"""
        summary = await self.get_summary(summary_id)
        if not summary:
            return None

        summary["windows"] = []
        windows_cursor = await self.conn.execute(
            """SELECT id, ref_message_id
               FROM context_windows
               WHERE summary_id = ?
               ORDER BY ref_message_id ASC""",
            (summary_id,),
        )
        windows = await windows_cursor.fetchall()
        for window in windows:
            messages = await self.get_context_messages(window[0])
            summary["windows"].append({
                "ref_message_id": window[1],
                "messages": messages,
            })

        return summary

    async def get_summaries_by_date_for_export(
        self, biz_date: str, group_id: int | None = None
    ) -> list[dict]:
        """读取某日摘要及上下文；批量导出复用单条导出的完整结构。"""
        summaries = await self.get_summaries_by_date(biz_date, group_id)
        result = []
        for summary in summaries:
            full_summary = await self.get_summary_with_context(summary["id"])
            if full_summary:
                result.append(full_summary)
        return result

    async def delete_expired_summaries(self, cutoff_date: str) -> int:
        """Deletes summaries older than cutoff_date, skipping favorited ones. Returns count deleted."""
        cursor = await self.conn.execute(
            """DELETE FROM summaries WHERE biz_date < ?
               AND NOT EXISTS (
                   SELECT 1 FROM summary_favorites f
                   WHERE f.biz_date = summaries.biz_date
                   AND f.group_id = summaries.group_id
                   AND f.biz_period = summaries.biz_period
               )""",
            (cutoff_date,),
        )
        await self.conn.commit()
        return cursor.rowcount

    async def delete_summary(self, summary_id: int) -> bool:
        """Deletes a summary and its associated favorite record atomically. Returns True if deleted."""
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.conn.execute(
                "SELECT biz_date, group_id, biz_period FROM summaries WHERE id = ?",
                (summary_id,),
            )
            row = await cursor.fetchone()
            if not row:
                await self.conn.rollback()
                return False
            biz_date, group_id, biz_period = row[0], row[1], row[2]
            await self.conn.execute(
                """DELETE FROM summary_favorites
                   WHERE biz_date = ? AND group_id = ? AND biz_period = ?""",
                (biz_date, group_id, biz_period),
            )
            await self.conn.execute("DELETE FROM summaries WHERE id = ?", (summary_id,))
            await self.conn.commit()
            return True
        except Exception:
            await self.conn.rollback()
            raise

    # --- summary_favorites ---

    async def get_summary_identity(self, summary_id: int) -> tuple[str, int, str] | None:
        """Returns (biz_date, group_id, biz_period) for a summary, or None if not found."""
        cursor = await self.conn.execute(
            "SELECT biz_date, group_id, biz_period FROM summaries WHERE id = ?",
            (summary_id,),
        )
        row = await cursor.fetchone()
        return (row[0], row[1], row[2]) if row else None

    async def upsert_summary_favorite(
        self, biz_date: str, group_id: int, biz_period: str, custom_text: str | None = None
    ) -> int:
        """Inserts or updates a favorite. Returns the favorite id."""
        import time
        now = int(time.time())
        await self.conn.execute(
            """INSERT INTO summary_favorites (biz_date, group_id, biz_period, custom_text, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(biz_date, group_id, biz_period)
               DO UPDATE SET custom_text = excluded.custom_text, updated_at = excluded.updated_at""",
            (biz_date, group_id, biz_period, custom_text, now, now),
        )
        await self.conn.commit()
        cursor = await self.conn.execute(
            """SELECT id FROM summary_favorites
               WHERE biz_date = ? AND group_id = ? AND biz_period = ?""",
            (biz_date, group_id, biz_period),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def delete_summary_favorite(
        self, biz_date: str, group_id: int, biz_period: str
    ) -> bool:
        """Removes a favorite record. Returns True if a row was deleted."""
        cursor = await self.conn.execute(
            """DELETE FROM summary_favorites
               WHERE biz_date = ? AND group_id = ? AND biz_period = ?""",
            (biz_date, group_id, biz_period),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_favorite_by_natural_key(
        self, biz_date: str, group_id: int, biz_period: str
    ) -> dict | None:
        """Returns favorite record by natural key, or None."""
        cursor = await self.conn.execute(
            """SELECT id, biz_date, biz_period, group_id, custom_text, created_at, updated_at
               FROM summary_favorites
               WHERE biz_date = ? AND group_id = ? AND biz_period = ?""",
            (biz_date, group_id, biz_period),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "biz_date": row[1],
            "biz_period": row[2],
            "group_id": row[3],
            "custom_text": row[4],
            "created_at": row[5],
            "updated_at": row[6],
        }

    async def get_all_favorites(self) -> list[dict]:
        """Returns all favorites with summary data, ordered by favorite created_at DESC."""
        cursor = await self.conn.execute(
            """SELECT f.id, f.biz_date, f.biz_period, f.group_id, f.custom_text,
                      f.created_at AS favorited_at,
                      s.id AS summary_id, s.group_name, s.message_count,
                      s.summary_text, s.created_at AS summary_created_at
               FROM summary_favorites f
               LEFT JOIN summaries s
                   ON s.biz_date = f.biz_date
                   AND s.group_id = f.group_id
                   AND s.biz_period = f.biz_period
               ORDER BY f.created_at DESC"""
        )
        rows = await cursor.fetchall()
        return [
            {
                "favorite_id": r[0],
                "biz_date": r[1],
                "biz_period": r[2],
                "group_id": r[3],
                "custom_text": r[4],
                "favorited_at": r[5],
                "summary_id": r[6],
                "group_name": r[7],
                "message_count": r[8],
                "summary_text": r[9],
                "summary_created_at": r[10],
            }
            for r in rows
        ]

    async def is_summary_favorite(
        self, biz_date: str, group_id: int, biz_period: str
    ) -> bool:
        """Checks whether a summary is favorited."""
        cursor = await self.conn.execute(
            """SELECT 1 FROM summary_favorites
               WHERE biz_date = ? AND group_id = ? AND biz_period = ?
               LIMIT 1""",
            (biz_date, group_id, biz_period),
        )
        return await cursor.fetchone() is not None

    # --- monitored_groups ---

    async def upsert_group(self, group_id: int, group_name: str) -> None:
        """Inserts or updates a monitored group's name."""
        await self.conn.execute(
            """INSERT INTO monitored_groups (group_id, group_name)
               VALUES (?, ?)
               ON CONFLICT(group_id) DO UPDATE SET group_name = excluded.group_name""",
            (group_id, group_name),
        )
        await self.conn.commit()

    async def get_active_groups(self) -> list[dict]:
        """Returns all active monitored groups."""
        cursor = await self.conn.execute(
            "SELECT group_id, group_name FROM monitored_groups WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
        return [{"group_id": r[0], "group_name": r[1]} for r in rows]

    async def get_all_groups(self) -> list[dict]:
        """Returns all monitored groups (active and inactive)."""
        cursor = await self.conn.execute(
            "SELECT group_id, group_name FROM monitored_groups"
        )
        rows = await cursor.fetchall()
        return [{"group_id": r[0], "group_name": r[1]} for r in rows]

    async def get_custom_cron_groups(self) -> list[dict]:
        """Returns active groups that have a custom summary cron schedule."""
        cursor = await self.conn.execute(
            """SELECT group_id, group_name, summary_cron
               FROM monitored_groups
               WHERE is_active = 1 AND summary_cron IS NOT NULL AND TRIM(summary_cron) != ''
               ORDER BY group_name"""
        )
        rows = await cursor.fetchall()
        return [{"group_id": r[0], "group_name": r[1], "summary_cron": r[2]} for r in rows]

    async def get_default_cron_groups(self) -> list[dict]:
        """Returns active groups that use the default (global) summary cron."""
        cursor = await self.conn.execute(
            """SELECT group_id, group_name
               FROM monitored_groups
               WHERE is_active = 1 AND (summary_cron IS NULL OR TRIM(summary_cron) = '')
               ORDER BY group_name"""
        )
        rows = await cursor.fetchall()
        return [{"group_id": r[0], "group_name": r[1]} for r in rows]

    async def get_context_message_ids_for_group_date(self, biz_date: str, group_id: int) -> set[int]:
        """Returns message IDs already stored in context windows for a group and date."""
        cursor = await self.conn.execute(
            """SELECT DISTINCT cm.message_id
               FROM context_messages cm
               JOIN context_windows cw ON cw.id = cm.window_id
               JOIN summaries s ON s.id = cw.summary_id
               WHERE s.biz_date = ? AND cw.group_id = ?""",
            (biz_date, group_id),
        )
        rows = await cursor.fetchall()
        return {r[0] for r in rows}

    async def toggle_group(self, group_id: int, is_active: bool) -> None:
        """Enables or disables monitoring for a group."""
        await self.conn.execute(
            "UPDATE monitored_groups SET is_active = ? WHERE group_id = ?",
            (1 if is_active else 0, group_id),
        )
        await self.conn.commit()

    async def list_all_groups(self) -> list[dict]:
        """Returns all monitored groups with their active status and custom cron."""
        cursor = await self.conn.execute(
            "SELECT group_id, group_name, is_active, summary_cron FROM monitored_groups ORDER BY group_name"
        )
        rows = await cursor.fetchall()
        return [
            {"group_id": r[0], "group_name": r[1], "is_active": bool(r[2]), "summary_cron": r[3]}
            for r in rows
        ]

    async def list_groups_with_activity(self) -> list[dict]:
        """Returns all groups joined with summary activity stats (avg daily messages, last summary time)."""
        cursor = await self.conn.execute(
            """SELECT g.group_id,
                      g.group_name,
                      g.is_active,
                      g.summary_cron,
                      COALESCE(AVG(d.daily_count), 0) AS avg_daily_messages,
                      COUNT(d.biz_date) AS summary_days,
                      MAX(d.last_summary_at) AS last_summary_at
               FROM monitored_groups g
               LEFT JOIN (
                   SELECT group_id,
                          biz_date,
                          SUM(message_count) AS daily_count,
                          MAX(created_at) AS last_summary_at
                   FROM summaries
                   WHERE biz_period NOT LIKE 'manual_%'
                   GROUP BY group_id, biz_date
               ) d ON d.group_id = g.group_id
               GROUP BY g.group_id, g.group_name, g.is_active, g.summary_cron
               ORDER BY g.group_name"""
        )
        rows = await cursor.fetchall()
        return [
            {
                "group_id": r[0],
                "group_name": r[1],
                "is_active": bool(r[2]),
                "summary_cron": r[3],
                "avg_daily_messages": float(r[4] or 0),
                "summary_days": r[5],
                "last_summary_at": r[6],
            }
            for r in rows
        ]

    async def update_group_summary_cron(self, group_id: int, summary_cron: str | None) -> None:
        """Sets a custom summary cron for a group. None or empty string resets to default."""
        value = summary_cron.strip() if summary_cron else None
        if value == "":
            value = None
        await self.conn.execute(
            "UPDATE monitored_groups SET summary_cron = ? WHERE group_id = ?",
            (value, group_id),
        )
        await self.conn.commit()

    # --- settings ---

    async def get_setting(self, key: str, default: str = "") -> str:
        """Returns a setting value by key, or default if not found."""
        cursor = await self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        """Inserts or replaces a setting key-value pair."""
        await self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self.conn.commit()

    # --- url entries ---

    @staticmethod
    def _url_context(text: str, start: int, end: int) -> str:
        """Returns a compact text fragment around a matched URL."""
        left = max(0, start - _URL_CONTEXT_RADIUS)
        right = min(len(text), end + _URL_CONTEXT_RADIUS)
        context = text[left:right].strip()
        if left > 0:
            context = "..." + context
        if right < len(text):
            context = context + "..."
        return context[:500]

    @staticmethod
    def _normalize_url(raw: str) -> tuple[str, str] | None:
        """Normalizes supported URL forms into clickable http(s) URLs and returns (url, domain)."""
        value = raw.strip().rstrip(_URL_TRAILING_PUNCTUATION)
        if not value:
            return None
        lower = value.lower()
        if value.startswith("@"):
            value = f"https://t.me/{value[1:]}"
        elif lower.startswith("t.me/"):
            value = f"https://{value}"

        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
                return None
            domain = parsed.netloc.lower()
            if not domain:
                return None
            normalized = urlunsplit((
                parsed.scheme.lower(),
                domain,
                parsed.path,
                parsed.query,
                parsed.fragment,
            ))
        except ValueError:
            return None
        return normalized, domain

    @classmethod
    def extract_urls(cls, text: str | None) -> list[dict]:
        """Extracts supported URLs from text, preserving first-seen order and context."""
        if not text:
            return []
        value = str(text)[:_URL_MAX_TEXT_LENGTH]
        entries: list[dict] = []
        seen: set[str] = set()
        for match in _URL_PATTERN.finditer(value):
            raw = match.group(0)
            normalized = cls._normalize_url(raw)
            if normalized is None:
                continue
            url, domain = normalized
            if url in seen:
                continue
            seen.add(url)
            entries.append({
                "url": url,
                "domain": domain,
                "raw_url": raw.strip().rstrip(_URL_TRAILING_PUNCTUATION),
                "source_context": cls._url_context(value, match.start(), match.end()),
            })
        return entries

    async def sync_url_entries_for_source(
        self,
        source_type: str,
        source_id: int,
        text: str | None,
        source_label: str | None = None,
        biz_date: str | None = None,
        group_id: int | None = None,
        group_name: str | None = None,
        sender_id: int | None = None,
        sender_name: str | None = None,
        created_at: int | None = None,
        commit: bool = True,
    ) -> int:
        """Replaces URL entries for one source and returns the number of extracted URLs."""
        if source_type not in ("summary", "bio"):
            raise ValueError(f"Unsupported URL source_type: {source_type}")

        try:
            await self.conn.execute(
                "DELETE FROM url_entries WHERE source_type = ? AND source_id = ?",
                (source_type, source_id),
            )
            entries = self.extract_urls(text)
            if entries:
                now = int(time.time())
                row_created_at = created_at or now
                if source_type == "summary":
                    urls = [entry["url"] for entry in entries]
                    for i in range(0, len(urls), _URL_DEDUPE_DELETE_BATCH_SIZE):
                        chunk = urls[i : i + _URL_DEDUPE_DELETE_BATCH_SIZE]
                        placeholders = ", ".join("?" for _ in chunk)
                        await self.conn.execute(
                            f"""DELETE FROM url_entries
                                WHERE source_type = 'summary'
                                  AND source_id < ?
                                  AND url IN ({placeholders})""",
                            [source_id, *chunk],
                        )
                await self.conn.executemany(
                    """INSERT OR IGNORE INTO url_entries
                       (url, domain, source_type, source_id, source_label, source_context,
                        biz_date, group_id, group_name, sender_id, sender_name, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            entry["url"],
                            entry["domain"],
                            source_type,
                            source_id,
                            source_label,
                            entry["source_context"],
                            biz_date,
                            group_id,
                            group_name,
                            sender_id,
                            sender_name,
                            row_created_at,
                            now,
                        )
                        for entry in entries
                    ],
                )
            if commit:
                await self.conn.commit()
            return len(entries)
        except Exception:
            if commit:
                await self.conn.rollback()
            raise

    async def backfill_url_entries(self, commit: bool = True) -> int:
        """Backfills URL entries from historical summaries and fetched Bio rows."""
        try:
            total = 0
            cursor = await self.conn.execute(
                """SELECT id, biz_date, group_id, group_name, summary_text, created_at
                   FROM summaries
                   ORDER BY id ASC"""
            )
            for row in await cursor.fetchall():
                total += await self.sync_url_entries_for_source(
                    "summary",
                    row[0],
                    row[4],
                    source_label=row[3],
                    biz_date=row[1],
                    group_id=row[2],
                    group_name=row[3],
                    created_at=row[5],
                    commit=False,
                )

            cursor = await self.conn.execute(
                """SELECT sender_id, username, display_name, bio_text, fetched_at,
                          last_group_id, last_group_name
                   FROM sender_profiles
                   WHERE bio_text IS NOT NULL AND TRIM(bio_text) != ''
                   ORDER BY sender_id ASC"""
            )
            for row in await cursor.fetchall():
                label = row[2] or (f"@{row[1]}" if row[1] else str(row[0]))
                total += await self.sync_url_entries_for_source(
                    "bio",
                    row[0],
                    row[3],
                    source_label=label,
                    group_id=row[5],
                    group_name=row[6],
                    sender_id=row[0],
                    sender_name=label,
                    created_at=row[4],
                    commit=False,
                )
            if commit:
                await self.conn.commit()
            return total
        except Exception:
            if commit:
                await self.conn.rollback()
            raise

    def _url_entries_filters(
        self,
        query: str = "",
        source_type: str = "all",
        domain: str = "all",
    ) -> tuple[str, list]:
        """Builds WHERE conditions for the URL library page."""
        clauses: list[str] = []
        params: list = []
        if source_type in ("summary", "bio"):
            clauses.append("source_type = ?")
            params.append(source_type)
        if domain and domain != "all":
            clauses.append("domain = ?")
            params.append(domain)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            clauses.append(
                """(url LIKE ? ESCAPE '\\'
                    OR domain LIKE ? ESCAPE '\\'
                    OR source_label LIKE ? ESCAPE '\\'
                    OR source_context LIKE ? ESCAPE '\\'
                    OR group_name LIKE ? ESCAPE '\\'
                    OR sender_name LIKE ? ESCAPE '\\')"""
            )
            params.extend([like, like, like, like, like, like])
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    async def get_url_entries(
        self,
        query: str = "",
        source_type: str = "all",
        limit: int = 50,
        offset: int = 0,
        domain: str = "all",
    ) -> list[dict]:
        """Returns collected URL entries ordered by newest first."""
        where, params = self._url_entries_filters(query, source_type, domain)
        cursor = await self.conn.execute(
            f"""SELECT id, url, domain, source_type, source_id, source_label,
                      source_context, biz_date, group_id, group_name, sender_id,
                      sender_name, created_at
               FROM url_entries
               {where}
               ORDER BY created_at DESC, id DESC
               LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "url": r[1],
                "domain": r[2],
                "source_type": r[3],
                "source_id": r[4],
                "source_label": r[5],
                "source_context": r[6],
                "biz_date": r[7],
                "group_id": r[8],
                "group_name": r[9],
                "sender_id": r[10],
                "sender_name": r[11],
                "created_at": r[12],
            }
            for r in rows
        ]

    async def count_url_entries(
        self,
        query: str = "",
        source_type: str = "all",
        domain: str = "all",
    ) -> int:
        """Returns total URL entry count for the given filters."""
        where, params = self._url_entries_filters(query, source_type, domain)
        cursor = await self.conn.execute(
            f"SELECT COUNT(*) FROM url_entries{where}",
            params,
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_url_domain_counts(
        self,
        query: str = "",
        source_type: str = "all",
        limit: int = 12,
    ) -> list[dict]:
        """Returns the most common URL domains for the current non-domain filters."""
        where, params = self._url_entries_filters(query, source_type)
        cursor = await self.conn.execute(
            f"""SELECT domain, COUNT(*) AS entry_count
               FROM url_entries
               {where}
               GROUP BY domain
               ORDER BY entry_count DESC, domain ASC
               LIMIT ?""",
            [*params, limit],
        )
        rows = await cursor.fetchall()
        return [{"domain": r[0], "count": r[1]} for r in rows]

    async def get_url_counts_for_domains(
        self,
        domains: list[str],
        query: str = "",
        source_type: str = "all",
    ) -> dict[str, int]:
        """Returns counts for specific domains under the current non-domain filters."""
        if not domains:
            return {}
        where, params = self._url_entries_filters(query, source_type)
        placeholders = ", ".join("?" for _ in domains)
        domain_clause = f"domain IN ({placeholders})"
        where = f"{where} AND {domain_clause}" if where else f" WHERE {domain_clause}"
        cursor = await self.conn.execute(
            f"""SELECT domain, COUNT(*) AS entry_count
               FROM url_entries
               {where}
               GROUP BY domain""",
            [*params, *domains],
        )
        rows = await cursor.fetchall()
        return {r[0]: r[1] for r in rows}

    # --- blocked_senders ---

    async def block_sender(self, sender_id: int, sender_name: str | None = None, reason: str = "ad") -> None:
        """Blocks a sender, replacing any existing block entry."""
        await self.conn.execute(
            """INSERT OR REPLACE INTO blocked_senders (sender_id, sender_name, reason)
               VALUES (?, ?, ?)""",
            (sender_id, sender_name, reason),
        )
        await self.conn.commit()

    async def unblock_sender(self, sender_id: int) -> None:
        """Removes a sender from the blocklist."""
        await self.conn.execute(
            "DELETE FROM blocked_senders WHERE sender_id = ?", (sender_id,)
        )
        await self.conn.commit()

    async def is_sender_blocked(self, sender_id: int) -> bool:
        """Checks whether a sender is in the blocklist."""
        cursor = await self.conn.execute(
            "SELECT 1 FROM blocked_senders WHERE sender_id = ?", (sender_id,)
        )
        return await cursor.fetchone() is not None

    async def get_blocked_senders(self) -> list[dict]:
        """Returns all blocked senders ordered by most recently blocked."""
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
        """Records a keyword-triggered alert."""
        await self.conn.execute(
            """INSERT INTO alerts (group_id, group_name, sender_id, sender_name, keywords, message_text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (group_id, group_name, sender_id, sender_name, keywords, message_text),
        )
        await self.conn.commit()

    async def get_alerts(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Fetches alerts in reverse chronological order with pagination."""
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
        """Returns total number of alerts."""
        cursor = await self.conn.execute("SELECT COUNT(*) FROM alerts")
        row = await cursor.fetchone()
        return row[0] if row else 0

    # --- sender profiles / Bio queue ---

    @staticmethod
    def _bio_hash(text: str | None) -> str:
        """返回 Bio 原文的稳定摘要，用于判断是否发生变化。"""
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _message_preview(text: str | None, max_len: int = 500) -> str:
        """截断最近发言，避免画像表被长消息撑大。"""
        value = (text or "").strip()
        return value[:max_len]

    async def upsert_sender_observation(
        self,
        sender_id: int | None,
        access_hash: int | None,
        username: str | None,
        display_name: str | None,
        group_id: int,
        group_name: str,
        message_id: int,
        message_text: str,
        timestamp: int,
    ) -> None:
        """记录发送者最近一次出现，并按 sender_id 合并多个群的出现情况。"""
        if sender_id is None:
            return

        preview = self._message_preview(message_text)
        await self.conn.execute(
            """INSERT INTO sender_profiles
               (sender_id, access_hash, username, display_name, first_seen_at, last_seen_at,
                last_group_id, last_group_name, last_message_id, last_message_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sender_id) DO UPDATE SET
                   access_hash = COALESCE(excluded.access_hash, sender_profiles.access_hash),
                   username = COALESCE(excluded.username, sender_profiles.username),
                   display_name = COALESCE(excluded.display_name, sender_profiles.display_name),
                   last_seen_at = excluded.last_seen_at,
                   last_group_id = excluded.last_group_id,
                   last_group_name = excluded.last_group_name,
                   last_message_id = excluded.last_message_id,
                   last_message_text = excluded.last_message_text""",
            (
                sender_id,
                access_hash,
                username,
                display_name,
                timestamp,
                timestamp,
                group_id,
                group_name,
                message_id,
                preview,
            ),
        )
        await self.conn.execute(
            """INSERT INTO sender_group_seen
               (sender_id, group_id, group_name, message_count, first_seen_at, last_seen_at, last_message_id)
               VALUES (?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(sender_id, group_id) DO UPDATE SET
                   group_name = excluded.group_name,
                   message_count = sender_group_seen.message_count + 1,
                   last_seen_at = excluded.last_seen_at,
                   last_message_id = excluded.last_message_id""",
            (sender_id, group_id, group_name, timestamp, timestamp, message_id),
        )
        await self.conn.commit()

    async def queue_bio_fetch(
        self,
        sender_id: int | None,
        reason: str,
        priority: int = 0,
        force: bool = False,
    ) -> bool:
        """将发送者加入 Bio 抓取队列；未过缓存期时默认不重复入队。"""
        if sender_id is None:
            return False

        now = int(time.time())
        await self.conn.execute(
            """INSERT INTO sender_profiles (sender_id, first_seen_at, last_seen_at, is_ad_candidate)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(sender_id) DO UPDATE SET is_ad_candidate = 1""",
            (sender_id, now, now),
        )

        cursor = await self.conn.execute(
            "SELECT next_fetch_at FROM sender_profiles WHERE sender_id = ?",
            (sender_id,),
        )
        row = await cursor.fetchone()
        if not force and row and row[0] and row[0] > now:
            await self.conn.commit()
            return False

        cursor = await self.conn.execute(
            "SELECT status FROM bio_fetch_queue WHERE sender_id = ?",
            (sender_id,),
        )
        existing = await cursor.fetchone()
        if not force and existing and existing[0] in ("pending", "running"):
            await self.conn.commit()
            return False

        await self.conn.execute(
            """INSERT INTO bio_fetch_queue
               (sender_id, priority, reason, status, next_run_at, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?, ?)
               ON CONFLICT(sender_id) DO UPDATE SET
                   priority = MAX(bio_fetch_queue.priority, excluded.priority),
                   reason = excluded.reason,
                   status = 'pending',
                   next_run_at = excluded.next_run_at,
                   updated_at = excluded.updated_at""",
            (sender_id, priority, reason[:200], now, now, now),
        )
        await self.conn.commit()
        return True

    async def reset_running_bio_tasks(self) -> None:
        """启动时恢复上次异常退出遗留的 running 任务。"""
        now = int(time.time())
        await self.conn.execute(
            "UPDATE bio_fetch_queue SET status = 'pending', updated_at = ? WHERE status = 'running'",
            (now,),
        )
        await self.conn.commit()

    async def claim_next_bio_fetch_task(self) -> dict | None:
        """领取下一个到期的 Bio 抓取任务，并标记为 running。"""
        now = int(time.time())
        cursor = await self.conn.execute(
            """SELECT q.sender_id, q.reason, q.retry_count, p.access_hash, p.username,
                      p.display_name, p.is_ad_candidate
               FROM bio_fetch_queue q
               LEFT JOIN sender_profiles p ON p.sender_id = q.sender_id
               WHERE q.status = 'pending' AND q.next_run_at <= ?
               ORDER BY q.priority DESC, q.updated_at ASC
               LIMIT 1""",
            (now,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        await self.conn.execute(
            "UPDATE bio_fetch_queue SET status = 'running', updated_at = ? WHERE sender_id = ?",
            (now, row[0]),
        )
        await self.conn.commit()
        return {
            "sender_id": row[0],
            "reason": row[1],
            "retry_count": row[2],
            "access_hash": row[3],
            "username": row[4],
            "display_name": row[5],
            "is_ad_candidate": bool(row[6]),
        }

    async def complete_bio_fetch(
        self,
        sender_id: int,
        bio_text: str | None,
        username: str | None = None,
        display_name: str | None = None,
        access_hash: int | None = None,
    ) -> None:
        """保存 Bio 抓取结果，并根据账号类型设置下一次允许抓取时间。"""
        now = int(time.time())
        cursor = await self.conn.execute(
            """SELECT is_ad_candidate, username, display_name, last_group_id, last_group_name
               FROM sender_profiles WHERE sender_id = ?""",
            (sender_id,),
        )
        row = await cursor.fetchone()
        is_ad_candidate = bool(row[0]) if row else True
        existing_username = row[1] if row else None
        existing_display_name = row[2] if row else None
        source_group_id = row[3] if row else None
        source_group_name = row[4] if row else None
        resolved_username = username or existing_username
        resolved_display_name = display_name or existing_display_name
        cache_days = 7 if is_ad_candidate else 30
        bio = (bio_text or "").strip()
        status = "fetched" if bio else "empty"
        source_label = resolved_display_name or (f"@{resolved_username}" if resolved_username else str(sender_id))
        await self.conn.execute(
            """INSERT INTO sender_profiles
               (sender_id, access_hash, username, display_name, bio_text, bio_hash,
                fetched_at, next_fetch_at, fetch_status, last_error, first_seen_at, last_seen_at,
                is_ad_candidate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
               ON CONFLICT(sender_id) DO UPDATE SET
                   access_hash = COALESCE(excluded.access_hash, sender_profiles.access_hash),
                   username = COALESCE(excluded.username, sender_profiles.username),
                   display_name = COALESCE(excluded.display_name, sender_profiles.display_name),
                   bio_text = excluded.bio_text,
                   bio_hash = excluded.bio_hash,
                   fetched_at = excluded.fetched_at,
                   next_fetch_at = excluded.next_fetch_at,
                   fetch_status = excluded.fetch_status,
                   last_error = NULL""",
            (
                sender_id,
                access_hash,
                username,
                display_name,
                bio,
                self._bio_hash(bio),
                now,
                now + cache_days * 86400,
                status,
                now,
                now,
                1 if is_ad_candidate else 0,
            ),
        )
        await self.conn.execute(
            "UPDATE bio_fetch_queue SET status = 'done', updated_at = ? WHERE sender_id = ?",
            (now, sender_id),
        )
        await self.sync_url_entries_for_source(
            "bio",
            sender_id,
            bio,
            source_label=source_label,
            group_id=source_group_id,
            group_name=source_group_name,
            sender_id=sender_id,
            sender_name=source_label,
            created_at=now,
            commit=False,
        )
        await self.conn.commit()

    async def fail_bio_fetch(self, sender_id: int, error: str, retry_after: int | None = None) -> None:
        """记录 Bio 抓取失败；限流时保留队列任务并推迟执行。"""
        now = int(time.time())
        msg = error[:300]
        if retry_after and retry_after > 0:
            next_run_at = now + retry_after
            await self.conn.execute(
                """UPDATE bio_fetch_queue
                   SET status = 'pending', retry_count = retry_count + 1,
                       next_run_at = ?, updated_at = ?
                   WHERE sender_id = ?""",
                (next_run_at, now, sender_id),
            )
            fetch_status = "rate_limited"
            next_fetch_at = next_run_at
        else:
            await self.conn.execute(
                "UPDATE bio_fetch_queue SET status = 'failed', retry_count = retry_count + 1, updated_at = ? WHERE sender_id = ?",
                (now, sender_id),
            )
            fetch_status = "failed"
            next_fetch_at = now + 7 * 86400

        await self.conn.execute(
            """UPDATE sender_profiles
               SET fetch_status = ?, last_error = ?, next_fetch_at = ?
               WHERE sender_id = ?""",
            (fetch_status, msg, next_fetch_at, sender_id),
        )
        await self.conn.commit()

    def _ad_bio_filters(
        self, query: str = "", group_id: int | None = None, status: str = "all"
    ) -> tuple[str, list]:
        """构造广告 Bio 页面复用的 WHERE 子句。"""
        clauses = ["p.is_ad_candidate = 1"]
        params: list = []
        if query:
            like = f"%{query}%"
            clauses.append(
                """(p.username LIKE ? COLLATE NOCASE
                    OR p.display_name LIKE ? COLLATE NOCASE
                    OR p.bio_text LIKE ? COLLATE NOCASE
                    OR p.last_message_text LIKE ? COLLATE NOCASE)"""
            )
            params.extend([like, like, like, like])
        if group_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM sender_group_seen s WHERE s.sender_id = p.sender_id AND s.group_id = ?)"
            )
            params.append(group_id)
        if status == "pending":
            clauses.append("q.status IN ('pending', 'running')")
        elif status == "fetched":
            clauses.append("p.fetch_status IN ('fetched', 'empty')")
        elif status == "failed":
            clauses.append("(p.fetch_status IN ('failed', 'rate_limited') OR q.status = 'failed')")
        return " AND ".join(clauses), params

    async def get_ad_bio_entries(
        self,
        query: str = "",
        group_id: int | None = None,
        status: str = "all",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """按 sender_id 合并返回广告 Bio 采集页数据。"""
        where, params = self._ad_bio_filters(query, group_id, status)
        cursor = await self.conn.execute(
            f"""SELECT p.sender_id, p.username, p.display_name, p.bio_text, p.fetched_at,
                      p.fetch_status, p.last_error, p.last_seen_at, p.last_group_name,
                      p.last_message_id, p.last_message_text, q.status, q.reason,
                      (SELECT GROUP_CONCAT(group_name, ' / ') FROM (
                          SELECT group_name
                          FROM sender_group_seen s
                          WHERE s.sender_id = p.sender_id
                          ORDER BY s.last_seen_at DESC
                          LIMIT 5
                      )) AS group_names,
                      (SELECT COUNT(*) FROM sender_group_seen s WHERE s.sender_id = p.sender_id) AS group_count
               FROM sender_profiles p
               LEFT JOIN bio_fetch_queue q ON q.sender_id = p.sender_id
               WHERE {where}
               ORDER BY COALESCE(p.fetched_at, 0) DESC, p.last_seen_at DESC
               LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return [
            {
                "sender_id": r[0],
                "username": r[1],
                "display_name": r[2],
                "bio_text": r[3],
                "fetched_at": r[4],
                "fetch_status": r[5],
                "last_error": r[6],
                "last_seen_at": r[7],
                "last_group_name": r[8],
                "last_message_id": r[9],
                "last_message_text": r[10],
                "queue_status": r[11],
                "queue_reason": r[12],
                "group_names": r[13] or "",
                "group_count": r[14] or 0,
            }
            for r in rows
        ]

    async def count_ad_bio_entries(
        self, query: str = "", group_id: int | None = None, status: str = "all"
    ) -> int:
        """返回广告 Bio 采集页筛选后的总数。"""
        where, params = self._ad_bio_filters(query, group_id, status)
        cursor = await self.conn.execute(
            f"""SELECT COUNT(*)
               FROM sender_profiles p
               LEFT JOIN bio_fetch_queue q ON q.sender_id = p.sender_id
               WHERE {where}""",
            params,
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_ad_bio_source_groups(self) -> list[dict]:
        """返回广告候选发送者出现过的群组，用于页面筛选。"""
        cursor = await self.conn.execute(
            """SELECT s.group_id, s.group_name, COUNT(DISTINCT s.sender_id) AS sender_count
               FROM sender_group_seen s
               JOIN sender_profiles p ON p.sender_id = s.sender_id
               WHERE p.is_ad_candidate = 1
               GROUP BY s.group_id, s.group_name
               ORDER BY sender_count DESC, s.group_name ASC"""
        )
        rows = await cursor.fetchall()
        return [{"group_id": r[0], "group_name": r[1], "sender_count": r[2]} for r in rows]

    async def get_bio_queue_stats(self) -> dict:
        """返回 Bio 抓取队列状态统计。"""
        cursor = await self.conn.execute(
            "SELECT status, COUNT(*) FROM bio_fetch_queue GROUP BY status"
        )
        rows = await cursor.fetchall()
        stats = {r[0]: r[1] for r in rows}
        return {
            "pending": stats.get("pending", 0),
            "running": stats.get("running", 0),
            "done": stats.get("done", 0),
            "failed": stats.get("failed", 0),
        }

    # --- context windows ---

    async def insert_context_window(self, summary_id: int, group_id: int, ref_message_id: int, covered_refs: list[int] | None = None) -> int:
        """Creates a context window linked to a summary. Returns the new window id."""
        import json
        covered_json = json.dumps(covered_refs) if covered_refs else None
        cursor = await self.conn.execute(
            "INSERT INTO context_windows (summary_id, group_id, ref_message_id, covered_refs) VALUES (?, ?, ?, ?)",
            (summary_id, group_id, ref_message_id, covered_json),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def insert_context_messages(self, window_id: int, messages: list[dict]) -> None:
        """Bulk-inserts messages into a context window, ignoring duplicates."""
        await self.conn.executemany(
            """INSERT OR IGNORE INTO context_messages
               (window_id, group_id, message_id, sender_name, text, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (window_id, m["group_id"], m["message_id"], m.get("sender_name"), m["text"], m["timestamp"])
                for m in messages
            ],
        )
        await self.conn.commit()

    async def get_context_windows_by_summary(self, summary_id: int) -> list[dict]:
        """Returns context windows for a summary with decoded covered_refs."""
        import json
        cursor = await self.conn.execute(
            "SELECT id, group_id, ref_message_id, covered_refs FROM context_windows WHERE summary_id = ?",
            (summary_id,),
        )
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            try:
                covered = json.loads(r[3]) if r[3] else [r[2]]
            except (json.JSONDecodeError, TypeError):
                covered = [r[2]]
            results.append({"id": r[0], "group_id": r[1], "ref_message_id": r[2], "covered_refs": covered})
        return results

    async def get_context_messages(self, window_id: int) -> list[dict]:
        """Returns messages in a context window ordered by message_id."""
        cursor = await self.conn.execute(
            """SELECT message_id, sender_name, text, timestamp
               FROM context_messages WHERE window_id = ?
               ORDER BY message_id ASC""",
            (window_id,),
        )
        rows = await cursor.fetchall()
        return [
            {"message_id": r[0], "sender_name": r[1], "text": r[2], "timestamp": r[3]}
            for r in rows
        ]

    async def touch_summary(self, summary_id: int) -> None:
        """Updates last_accessed_at to current time for LRU eviction tracking."""
        import time
        await self.conn.execute(
            "UPDATE summaries SET last_accessed_at = ? WHERE id = ?",
            (int(time.time()), summary_id),
        )
        await self.conn.commit()

    async def get_messages_around(self, group_id: int, center_message_id: int, radius: int) -> list[dict]:
        """Returns messages surrounding a center message within a radius, ordered by message_id."""
        before_cursor = await self.conn.execute(
            """SELECT group_id, message_id, sender_name, text, timestamp
               FROM messages
               WHERE group_id = ? AND message_id < ?
               ORDER BY message_id DESC LIMIT ?""",
            (group_id, center_message_id, radius),
        )
        before = await before_cursor.fetchall()

        after_cursor = await self.conn.execute(
            """SELECT group_id, message_id, sender_name, text, timestamp
               FROM messages
               WHERE group_id = ? AND message_id >= ?
               ORDER BY message_id ASC LIMIT ?""",
            (group_id, center_message_id, radius + 1),
        )
        after = await after_cursor.fetchall()

        rows = list(reversed(before)) + list(after)
        return [
            {"group_id": r[0], "message_id": r[1], "sender_name": r[2], "text": r[3], "timestamp": r[4]}
            for r in rows
        ]

    async def delete_messages_except_context(
        self, biz_date: str, before_timestamp: int | None, keep_message_ids: set[int], group_id: int
    ) -> int:
        """Deletes messages for a group/date, preserving those in keep_message_ids. Returns count deleted."""
        if not keep_message_ids:
            if before_timestamp is not None:
                cursor = await self.conn.execute(
                    "DELETE FROM messages WHERE biz_date = ? AND group_id = ? AND timestamp <= ?",
                    (biz_date, group_id, before_timestamp),
                )
            else:
                cursor = await self.conn.execute(
                    "DELETE FROM messages WHERE biz_date = ? AND group_id = ?",
                    (biz_date, group_id),
                )
            await self.conn.commit()
            return cursor.rowcount

        self._temp_table_counter += 1
        table_name = f"_keep_ids_{self._temp_table_counter}"
        await self.conn.execute(f"CREATE TEMP TABLE {table_name} (message_id INTEGER PRIMARY KEY)")
        try:
            batch_size = 900
            keep_list = [(int(mid),) for mid in keep_message_ids]
            for i in range(0, len(keep_list), batch_size):
                await self.conn.executemany(
                    f"INSERT OR IGNORE INTO {table_name} (message_id) VALUES (?)",
                    keep_list[i:i + batch_size],
                )

            if before_timestamp is not None:
                cursor = await self.conn.execute(
                    f"""DELETE FROM messages WHERE biz_date = ? AND group_id = ? AND timestamp <= ?
                       AND message_id NOT IN (SELECT message_id FROM {table_name})""",
                    (biz_date, group_id, before_timestamp),
                )
            else:
                cursor = await self.conn.execute(
                    f"""DELETE FROM messages WHERE biz_date = ? AND group_id = ?
                       AND message_id NOT IN (SELECT message_id FROM {table_name})""",
                    (biz_date, group_id),
                )
            await self.conn.commit()
            return cursor.rowcount
        finally:
            await self.conn.execute(f"DROP TABLE IF EXISTS {table_name}")

    async def get_context_storage_count(self) -> int:
        """Returns total number of context messages across all windows."""
        cursor = await self.conn.execute("SELECT COUNT(*) FROM context_messages")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def cleanup_lru_contexts(self, max_rows: int) -> int:
        """Evicts oldest-accessed context windows, skipping favorited summaries. Returns count deleted."""
        total = await self.get_context_storage_count()
        if total <= max_rows:
            return 0
        deleted = 0
        processed_ids: set[int] = set()
        while total > max_rows:
            cursor = await self.conn.execute(
                """SELECT id FROM summaries
                   WHERE id IN (SELECT DISTINCT summary_id FROM context_windows)
                   AND NOT EXISTS (
                       SELECT 1 FROM summary_favorites f
                       WHERE f.biz_date = summaries.biz_date
                       AND f.group_id = summaries.group_id
                       AND f.biz_period = summaries.biz_period
                   )
                   ORDER BY last_accessed_at ASC LIMIT 1"""
            )
            row = await cursor.fetchone()
            if not row:
                break
            summary_id = row[0]
            if summary_id in processed_ids:
                break
            processed_ids.add(summary_id)
            count_cursor = await self.conn.execute(
                """SELECT COUNT(*) FROM context_messages
                   WHERE window_id IN (SELECT id FROM context_windows WHERE summary_id = ?)""",
                (summary_id,),
            )
            count_row = await count_cursor.fetchone()
            batch_size = count_row[0] if count_row else 0
            await self.conn.execute(
                "DELETE FROM context_windows WHERE summary_id = ?", (summary_id,)
            )
            await self.conn.commit()
            total -= batch_size
            deleted += batch_size
        return deleted
