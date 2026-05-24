from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from config import Config
from database import Database
from web import create_app


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


def test_extract_urls_normalizes_supported_forms():
    """URL 抽取支持 http(s)、t.me 和 Telegram username，并清理尾部标点。"""
    entries = Database.extract_urls(
        "官网 https://Example.COM/path). 入口 t.me/shop01，客服 @seller01，邮箱 x@example.com"
    )

    assert [e["url"] for e in entries] == [
        "https://example.com/path",
        "https://t.me/shop01",
        "https://t.me/seller01",
    ]
    assert [e["domain"] for e in entries] == ["example.com", "t.me", "t.me"]


def test_extract_urls_ignores_malformed_urls():
    """畸形 URL 不应中断摘要/Bio 入库流程。"""
    entries = Database.extract_urls("坏链接 https://[invalid-ipv6 但后面还有 https://ok.example/a")

    assert [e["url"] for e in entries] == ["https://ok.example/a"]


def test_insert_summary_collects_url_entries(tmp_path):
    """摘要写入后，同步收集摘要文本里的 URL。"""
    async def run(db: Database):
        summary_id = await db.insert_summary(
            "2026-05-24",
            -100,
            "测试群",
            3,
            "重要链接：https://Example.com/deal [m:1]",
        )
        assert summary_id is not None

        entries = await db.get_url_entries(source_type="summary")

        assert len(entries) == 1
        assert entries[0]["url"] == "https://example.com/deal"
        assert entries[0]["source_type"] == "summary"
        assert entries[0]["source_id"] == summary_id
        assert entries[0]["group_name"] == "测试群"

    asyncio.run(_with_db(tmp_path, run))


def test_complete_bio_fetch_collects_url_entries(tmp_path):
    """Bio 抓取结果保存后，同步收集 Bio 里的 URL。"""
    async def run(db: Database):
        await db.upsert_sender_observation(1001, 9001, "seller", "Seller", -10, "群A", 1, "看主页", 100)
        await db.complete_bio_fetch(1001, "发卡入口 t.me/demo，客服 @seller01")

        entries = await db.get_url_entries(source_type="bio")

        assert [e["url"] for e in entries] == ["https://t.me/seller01", "https://t.me/demo"]
        assert all(e["source_type"] == "bio" for e in entries)
        assert all(e["sender_id"] == 1001 for e in entries)
        assert all(e["sender_name"] == "Seller" for e in entries)
        assert all(e["group_name"] == "群A" for e in entries)

    asyncio.run(_with_db(tmp_path, run))


def test_url_entry_search_treats_like_wildcards_literally(tmp_path):
    """搜索百分号和下划线时按普通字符匹配，不展开为 LIKE 通配符。"""
    async def run(db: Database):
        await db.insert_summary("2026-05-24", -100, "测试群", 1, "A https://example.test/a")
        await db.insert_summary("2026-05-24", -101, "测试群2", 1, "B https://example.test/percent%25")

        assert await db.count_url_entries(query="%") == 1
        percent_rows = await db.get_url_entries(query="%")
        assert [row["url"] for row in percent_rows] == ["https://example.test/percent%25"]
        assert await db.count_url_entries(query="_") == 0

    asyncio.run(_with_db(tmp_path, run))


def test_sync_url_entries_for_source_is_idempotent(tmp_path):
    """同一来源重复同步时，链接库保持一条当前记录。"""
    async def run(db: Database):
        summary_id = await db.insert_summary(
            "2026-05-24",
            -100,
            "测试群",
            1,
            "链接 https://example.test/a",
        )
        assert summary_id is not None

        await db.sync_url_entries_for_source(
            "summary",
            summary_id,
            "链接 https://example.test/a",
            source_label="测试群",
            biz_date="2026-05-24",
            group_id=-100,
            group_name="测试群",
        )

        assert await db.count_url_entries() == 1
        entries = await db.get_url_entries()
        assert entries[0]["url"] == "https://example.test/a"

    asyncio.run(_with_db(tmp_path, run))


def test_backfill_url_entries_reads_existing_sources(tmp_path):
    """历史摘要和 Bio 可通过幂等回填补齐到链接库。"""
    async def run(db: Database):
        await db.conn.execute(
            """INSERT INTO summaries
               (biz_date, biz_period, group_id, group_name, message_count, summary_text, created_at)
               VALUES ('2026-05-24', 'daily', -100, '旧群', 1, '旧摘要 https://old.example/a', 100)"""
        )
        await db.conn.execute(
            """INSERT INTO sender_profiles
               (sender_id, username, display_name, bio_text, fetched_at, first_seen_at, last_seen_at, fetch_status)
               VALUES (2002, 'old_seller', 'Old Seller', '旧 Bio t.me/oldshop', 120, 100, 120, 'fetched')"""
        )
        await db.conn.commit()

        count = await db.backfill_url_entries()
        entries = await db.get_url_entries()

        assert count == 2
        assert {e["url"] for e in entries} == {"https://old.example/a", "https://t.me/oldshop"}

    asyncio.run(_with_db(tmp_path, run))


def test_urls_page_renders_blank_links(tmp_path):
    """链接库页面展示收集到的 URL，并使用新标签页安全打开外链。"""
    db_path = str(tmp_path / "messages.db")
    db = Database(db_path)
    asyncio.run(db.connect())
    try:
        asyncio.run(db.insert_summary("2026-05-24", -100, "测试群", 1, "看 https://example.test/item"))
        app = create_app(_make_config(db_path), db)
        with TestClient(app) as client:
            assert client.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
            response = client.get("/urls")
    finally:
        asyncio.run(db.close())

    assert response.status_code == 200
    assert "https://example.test/item" in response.text
    assert 'target="_blank"' in response.text
    assert 'rel="noopener noreferrer"' in response.text
