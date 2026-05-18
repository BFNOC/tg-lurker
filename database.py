from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
from pathlib import Path

logger = logging.getLogger(__name__)

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

CREATE INDEX IF NOT EXISTS idx_messages_biz_date ON messages(biz_date);
CREATE INDEX IF NOT EXISTS idx_messages_group_date ON messages(group_id, biz_date);
CREATE INDEX IF NOT EXISTS idx_messages_group_msgid ON messages(group_id, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_group_timestamp ON messages(group_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_summaries_biz_date ON summaries(biz_date);
CREATE INDEX IF NOT EXISTS idx_summaries_group_date ON summaries(group_id, biz_date);
CREATE INDEX IF NOT EXISTS idx_context_windows_summary ON context_windows(summary_id);
CREATE INDEX IF NOT EXISTS idx_context_messages_window ON context_messages(window_id);
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
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        await self._migrate()

    async def _migrate(self) -> None:
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
        await self.conn.commit()

    async def _ensure_column(self, table: str, column: str, ddl: str) -> None:
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

    async def get_messages_since(self, group_id: int, since_ts: int, before_ts: int | None = None) -> list[dict]:
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
        try:
            cursor = await self.conn.execute(
                """INSERT INTO summaries
                   (biz_date, biz_period, group_id, group_name, message_count, summary_text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (biz_date, biz_period, group_id, group_name, message_count, summary_text),
            )
            await self.conn.commit()
            return cursor.lastrowid
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

    async def summary_exists(self, biz_date: str, group_id: int, biz_period: str) -> bool:
        cursor = await self.conn.execute(
            """SELECT 1 FROM summaries
               WHERE biz_date = ? AND group_id = ? AND biz_period = ?
               LIMIT 1""",
            (biz_date, group_id, biz_period),
        )
        return await cursor.fetchone() is not None

    async def get_last_summary_ts(self, group_id: int) -> int | None:
        cursor = await self.conn.execute(
            "SELECT MAX(created_at) FROM summaries WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else None

    async def get_summaries_by_date(
        self, biz_date: str, group_id: int | None = None
    ) -> list[dict]:
        if group_id is not None:
            cursor = await self.conn.execute(
                """SELECT id, biz_date, biz_period, group_id, group_name, message_count, summary_text, created_at
                   FROM summaries WHERE biz_date = ? AND group_id = ?
                   ORDER BY biz_period, group_id""",
                (biz_date, group_id),
            )
        else:
            cursor = await self.conn.execute(
                """SELECT id, biz_date, biz_period, group_id, group_name, message_count, summary_text, created_at
                   FROM summaries WHERE biz_date = ?
                   ORDER BY biz_period, group_id""",
                (biz_date,),
            )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "biz_date": r[1],
                "biz_period": r[2],
                "group_id": r[3],
                "group_name": r[4],
                "message_count": r[5],
                "summary_text": r[6],
                "created_at": r[7],
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

    async def get_summary_with_context(self, summary_id: int) -> dict | None:
        """读取单条摘要及其全部上下文窗口和上下文消息，用于导出。"""
        cursor = await self.conn.execute(
            """SELECT id, biz_date, biz_period, group_id, group_name, message_count, summary_text, created_at
               FROM summaries WHERE id = ?""",
            (summary_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        summary = {
            "id": row[0],
            "biz_date": row[1],
            "biz_period": row[2],
            "group_id": row[3],
            "group_name": row[4],
            "message_count": row[5],
            "summary_text": row[6],
            "created_at": row[7],
            "windows": [],
        }

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

    async def get_custom_cron_groups(self) -> list[dict]:
        cursor = await self.conn.execute(
            """SELECT group_id, group_name, summary_cron
               FROM monitored_groups
               WHERE is_active = 1 AND summary_cron IS NOT NULL AND TRIM(summary_cron) != ''
               ORDER BY group_name"""
        )
        rows = await cursor.fetchall()
        return [{"group_id": r[0], "group_name": r[1], "summary_cron": r[2]} for r in rows]

    async def get_default_cron_groups(self) -> list[dict]:
        cursor = await self.conn.execute(
            """SELECT group_id, group_name
               FROM monitored_groups
               WHERE is_active = 1 AND (summary_cron IS NULL OR TRIM(summary_cron) = '')
               ORDER BY group_name"""
        )
        rows = await cursor.fetchall()
        return [{"group_id": r[0], "group_name": r[1]} for r in rows]

    async def get_context_message_ids_for_group_date(self, biz_date: str, group_id: int) -> set[int]:
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
        await self.conn.execute(
            "UPDATE monitored_groups SET is_active = ? WHERE group_id = ?",
            (1 if is_active else 0, group_id),
        )
        await self.conn.commit()

    async def list_all_groups(self) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT group_id, group_name, is_active, summary_cron FROM monitored_groups ORDER BY group_name"
        )
        rows = await cursor.fetchall()
        return [
            {"group_id": r[0], "group_name": r[1], "is_active": bool(r[2]), "summary_cron": r[3]}
            for r in rows
        ]

    async def list_groups_with_activity(self) -> list[dict]:
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

    # --- context windows ---

    async def insert_context_window(self, summary_id: int, group_id: int, ref_message_id: int, covered_refs: list[int] | None = None) -> int:
        import json
        covered_json = json.dumps(covered_refs) if covered_refs else None
        cursor = await self.conn.execute(
            "INSERT INTO context_windows (summary_id, group_id, ref_message_id, covered_refs) VALUES (?, ?, ?, ?)",
            (summary_id, group_id, ref_message_id, covered_json),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def insert_context_messages(self, window_id: int, messages: list[dict]) -> None:
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
        import time
        await self.conn.execute(
            "UPDATE summaries SET last_accessed_at = ? WHERE id = ?",
            (int(time.time()), summary_id),
        )
        await self.conn.commit()

    async def get_messages_around(self, group_id: int, center_message_id: int, radius: int) -> list[dict]:
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
        else:
            placeholders = ",".join("?" * len(keep_message_ids))
            if before_timestamp is not None:
                cursor = await self.conn.execute(
                    f"DELETE FROM messages WHERE biz_date = ? AND group_id = ? AND timestamp <= ? AND message_id NOT IN ({placeholders})",
                    (biz_date, group_id, before_timestamp, *keep_message_ids),
                )
            else:
                cursor = await self.conn.execute(
                    f"DELETE FROM messages WHERE biz_date = ? AND group_id = ? AND message_id NOT IN ({placeholders})",
                    (biz_date, group_id, *keep_message_ids),
                )
        await self.conn.commit()
        return cursor.rowcount

    async def get_context_storage_count(self) -> int:
        cursor = await self.conn.execute("SELECT COUNT(*) FROM context_messages")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def cleanup_lru_contexts(self, max_rows: int) -> int:
        total = await self.get_context_storage_count()
        if total <= max_rows:
            return 0
        deleted = 0
        while total > max_rows:
            cursor = await self.conn.execute(
                """SELECT id FROM summaries
                   WHERE id IN (SELECT DISTINCT summary_id FROM context_windows)
                   ORDER BY last_accessed_at ASC LIMIT 1"""
            )
            row = await cursor.fetchone()
            if not row:
                break
            summary_id = row[0]
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
