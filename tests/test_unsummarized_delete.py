from __future__ import annotations

import asyncio
from contextlib import contextmanager

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


@contextmanager
def _client(tmp_path):
    db_path = str(tmp_path / "messages.db")
    db = Database(db_path)
    asyncio.run(db.connect())
    app = create_app(_make_config(db_path), db)
    try:
        with TestClient(app) as client:
            yield client, db
    finally:
        asyncio.run(db.close())


def _login(client: TestClient) -> None:
    response = client.post("/login", data={"password": "secret"}, follow_redirects=False)
    assert response.status_code == 303


async def _insert_message(db: Database, biz_date: str, message_id: int, group_id: int = -100) -> None:
    await db.insert_message(
        group_id,
        f"群{abs(group_id)}",
        message_id,
        sender_id=message_id,
        sender_name=f"用户{message_id}",
        text=f"消息 {message_id}",
        timestamp=1_800_000_000 + message_id,
        biz_date=biz_date,
    )


def _csrf(client: TestClient) -> str:
    return client.cookies.get(CSRF_COOKIE, "")


def test_dashboard_renders_unsummarized_delete_controls(tmp_path):
    """仪表盘未摘要提示提供单日删除和全部删除入口。"""
    with _client(tmp_path) as (client, db):
        asyncio.run(_insert_message(db, "2026-06-20", 1))
        _login(client)

        response = client.get("/dashboard")

    assert response.status_code == 200
    assert "/messages/unsummarized/delete" in response.text
    assert "删除全部" in response.text
    assert 'name="biz_date" value="2026-06-20"' in response.text


def test_delete_single_unsummarized_date_refreshes_card(tmp_path):
    """删除单个未摘要日期后，只移除该日期的原始消息并刷新卡片。"""
    with _client(tmp_path) as (client, db):
        asyncio.run(_insert_message(db, "2026-06-20", 1))
        asyncio.run(_insert_message(db, "2026-06-20", 2))
        asyncio.run(_insert_message(db, "2026-06-19", 3))
        _login(client)

        response = client.post(
            "/messages/unsummarized/delete",
            data={"csrf_token": _csrf(client), "biz_date": "2026-06-20"},
        )

        deleted_count = asyncio.run(db.get_message_count_by_date("2026-06-20"))
        remaining_count = asyncio.run(db.get_message_count_by_date("2026-06-19"))

    assert response.status_code == 200
    assert "已删除 2 条原始消息" in response.text
    assert "2026-06-20" not in response.text
    assert "2026-06-19" in response.text
    assert deleted_count == 0
    assert remaining_count == 1


def test_delete_rejects_date_not_in_unsummarized_whitelist(tmp_path):
    """已存在自动摘要的日期不允许通过未摘要删除接口删除。"""
    with _client(tmp_path) as (client, db):
        asyncio.run(_insert_message(db, "2026-06-18", 1))
        asyncio.run(db.insert_summary("2026-06-18", -100, "群100", 1, "已摘要"))
        _login(client)

        response = client.post(
            "/messages/unsummarized/delete",
            data={"csrf_token": _csrf(client), "biz_date": "2026-06-18"},
        )

        message_count = asyncio.run(db.get_message_count_by_date("2026-06-18"))

    assert response.status_code == 400
    assert message_count == 1


def test_delete_db_guard_skips_date_that_gets_auto_summary_later(tmp_path):
    """DB 删除语句本身也会保护已生成自动摘要的日期。"""
    with _client(tmp_path) as (_, db):
        asyncio.run(_insert_message(db, "2026-06-18", 1))
        before = asyncio.run(db.get_unsummarized_dates())
        asyncio.run(db.insert_summary("2026-06-18", -100, "群100", 1, "刚生成的摘要"))

        deleted = asyncio.run(db.delete_unsummarized_messages_by_dates(["2026-06-18"]))
        message_count = asyncio.run(db.get_message_count_by_date("2026-06-18"))

    assert before == [{"biz_date": "2026-06-18", "msg_count": 1}]
    assert deleted == 0
    assert message_count == 1


def test_delete_all_unsummarized_dates_keeps_summarized_messages(tmp_path):
    """清空全部未摘要日期时，不删除已经有自动摘要的日期。"""
    with _client(tmp_path) as (client, db):
        asyncio.run(_insert_message(db, "2026-06-20", 1))
        asyncio.run(_insert_message(db, "2026-06-19", 2))
        asyncio.run(_insert_message(db, "2026-06-18", 3))
        asyncio.run(db.insert_summary("2026-06-18", -100, "群100", 1, "已摘要"))
        _login(client)

        response = client.post(
            "/messages/unsummarized/delete",
            data={"csrf_token": _csrf(client), "scope": "all"},
        )

        deleted_a = asyncio.run(db.get_message_count_by_date("2026-06-20"))
        deleted_b = asyncio.run(db.get_message_count_by_date("2026-06-19"))
        kept = asyncio.run(db.get_message_count_by_date("2026-06-18"))

    assert response.status_code == 200
    assert response.text == ""
    assert deleted_a == 0
    assert deleted_b == 0
    assert kept == 1


def test_delete_requires_login(tmp_path):
    """未登录用户不能调用删除接口。"""
    with _client(tmp_path) as (client, db):
        asyncio.run(_insert_message(db, "2026-06-20", 1))

        response = client.post(
            "/messages/unsummarized/delete",
            data={"csrf_token": _csrf(client), "biz_date": "2026-06-20"},
            follow_redirects=False,
        )
        message_count = asyncio.run(db.get_message_count_by_date("2026-06-20"))

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert message_count == 1


def test_delete_requires_csrf(tmp_path):
    """删除接口必须校验 CSRF。"""
    with _client(tmp_path) as (client, db):
        asyncio.run(_insert_message(db, "2026-06-20", 1))
        _login(client)

        response = client.post("/messages/unsummarized/delete", data={"biz_date": "2026-06-20"})
        message_count = asyncio.run(db.get_message_count_by_date("2026-06-20"))

    assert response.status_code == 403
    assert message_count == 1
