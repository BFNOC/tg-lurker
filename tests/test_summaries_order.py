from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from config import Config
from database import Database
from web import create_app
from web.auth import CSRF_COOKIE


def _make_config(db_path: str) -> Config:
    return Config(
        api_id=1,
        api_hash="hash",
        owner_id=1,
        llm_base_url="https://example.test/v1",
        llm_api_key="sk-test",
        db_path=db_path,
        session_path="./data/test.session",
        web_password="secret",
    )


async def _with_db(tmp_path, fn):
    db = Database(str(tmp_path / "messages.db"))
    await db.connect()
    try:
        await fn(db)
    finally:
        await db.close()


async def _insert_summary_row(
    db: Database,
    *,
    biz_period: str,
    group_id: int,
    group_name: str,
    summary_text: str,
    created_at: int,
) -> None:
    await db.conn.execute(
        """INSERT INTO summaries
           (biz_date, biz_period, group_id, group_name, message_count, summary_text, created_at)
           VALUES ('2026-05-24', ?, ?, ?, 1, ?, ?)""",
        (biz_period, group_id, group_name, summary_text, created_at),
    )


async def _seed_grouped_summaries(db: Database) -> None:
    await _insert_summary_row(
        db,
        biz_period="daily",
        group_id=-100,
        group_name="群A",
        summary_text="A older",
        created_at=1000,
    )
    await _insert_summary_row(
        db,
        biz_period="manual_09",
        group_id=-100,
        group_name="群A",
        summary_text="A newer",
        created_at=3000,
    )
    await _insert_summary_row(
        db,
        biz_period="daily",
        group_id=-200,
        group_name="群B",
        summary_text="B older",
        created_at=2000,
    )
    await _insert_summary_row(
        db,
        biz_period="manual_10",
        group_id=-200,
        group_name="群B",
        summary_text="B newer",
        created_at=2500,
    )
    await db.conn.commit()


def test_summaries_order_groups_by_latest_group_then_time(tmp_path):
    """摘要列表先按群组块聚合，再在组内按生成时间倒序。"""
    async def run(db: Database):
        await _seed_grouped_summaries(db)

        rows = await db.get_summaries_by_date("2026-05-24")
        scoped_rows = await db.get_summaries_by_date("2026-05-24", group_id=-100)

        assert [row["summary_text"] for row in rows] == [
            "A newer",
            "A older",
            "B newer",
            "B older",
        ]
        assert [row["summary_text"] for row in scoped_rows] == ["A newer", "A older"]

    asyncio.run(_with_db(tmp_path, run))


def test_summaries_order_keeps_renamed_group_contiguous_on_tie(tmp_path):
    """同一 group_id 当天改名时，排序仍保持同组摘要连续。"""
    async def run(db: Database):
        await _insert_summary_row(
            db,
            biz_period="daily",
            group_id=-100,
            group_name="Z old name",
            summary_text="A older renamed",
            created_at=1000,
        )
        await _insert_summary_row(
            db,
            biz_period="manual_09",
            group_id=-100,
            group_name="B latest name",
            summary_text="A latest renamed",
            created_at=3000,
        )
        await _insert_summary_row(
            db,
            biz_period="daily",
            group_id=-200,
            group_name="M other group",
            summary_text="B latest tied",
            created_at=3000,
        )
        await db.conn.commit()

        rows = await db.get_summaries_by_date("2026-05-24")

        assert [row["summary_text"] for row in rows] == [
            "A latest renamed",
            "A older renamed",
            "B latest tied",
        ]
        assert [row["group_id"] for row in rows] == [-100, -100, -200]
        assert rows[0]["group_sort_name"] == "B latest name"

    asyncio.run(_with_db(tmp_path, run))


def test_summaries_page_renders_group_dividers_in_order(tmp_path):
    """摘要页使用群组分隔线，避免同组摘要被时段分隔打散。"""
    db_path = str(tmp_path / "messages.db")
    db = Database(db_path)
    asyncio.run(db.connect())
    try:
        asyncio.run(_seed_grouped_summaries(db))
        app = create_app(_make_config(db_path), db)
        with TestClient(app) as client:
            assert client.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
            response = client.get("/summaries?date=2026-05-24")
    finally:
        asyncio.run(db.close())

    assert response.status_code == 200
    assert "summary-group-divider" in response.text
    assert "summary-period-divider" not in response.text
    assert response.text.find("群A") < response.text.find("A newer") < response.text.find("A older")
    assert response.text.find("A older") < response.text.find("群B") < response.text.find("B newer")
    assert response.text.find("B newer") < response.text.find("B older")


def test_summary_favorite_refresh_preserves_context_window_metadata(tmp_path):
    """收藏局部刷新后的 data-windows 仍使用摘要页 ctx 按钮需要的窗口元数据。"""
    db_path = str(tmp_path / "messages.db")
    db = Database(db_path)
    asyncio.run(db.connect())
    try:
        async def seed():
            summary_id = await db.insert_summary(
                "2026-05-24",
                -100,
                "群A",
                1,
                "重点 [m:10]",
            )
            window_id = await db.insert_context_window(summary_id, -100, 10, covered_refs=[10, 11])
            await db.insert_context_messages(
                window_id,
                [
                    {"group_id": -100, "message_id": 10, "sender_name": "A", "text": "ref", "timestamp": 100},
                    {"group_id": -100, "message_id": 11, "sender_name": "A", "text": "next", "timestamp": 101},
                ],
            )
            return summary_id

        summary_id = asyncio.run(seed())
        app = create_app(_make_config(db_path), db)
        with TestClient(app) as client:
            assert client.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
            response = client.post(
                f"/summaries/{summary_id}/favorite",
                data={"csrf_token": client.cookies.get(CSRF_COOKIE), "custom_text": "重点备注"},
            )
    finally:
        asyncio.run(db.close())

    assert response.status_code == 200
    assert "summary-period-title" in response.text
    assert "summary-group-name" not in response.text
    assert "fav-note-label" in response.text
    assert "&quot;id&quot;" in response.text
    assert "&quot;covered_refs&quot;" in response.text
    assert "&quot;messages&quot;" not in response.text
