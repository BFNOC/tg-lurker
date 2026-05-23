from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from telethon.tl.types import InputPeerUser, InputUser, User

from bot import Bot
from config import Config
from database import Database
from dedup import DedupEngine
from web import create_app, templates


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


def test_ad_bio_entries_merge_same_sender_across_groups(tmp_path):
    """同一 sender_id 在多个群出现时，广告 Bio 页只展示一条合并记录。"""
    async def run(db: Database):
        await db.upsert_sender_observation(1001, 9001, "seller", "Seller", -10, "群A", 1, "看主页", 100)
        await db.upsert_sender_observation(1001, 9001, "seller", "Seller", -20, "群B", 2, "简介有店铺", 200)
        assert await db.queue_bio_fetch(1001, "测试", force=True)
        await db.complete_bio_fetch(1001, "发卡 shop: t.me/demo", username="seller", display_name="Seller")

        entries = await db.get_ad_bio_entries()

        assert len(entries) == 1
        assert entries[0]["sender_id"] == 1001
        assert entries[0]["group_count"] == 2
        assert "群A" in entries[0]["group_names"]
        assert "群B" in entries[0]["group_names"]
        assert entries[0]["bio_text"] == "发卡 shop: t.me/demo"

    asyncio.run(_with_db(tmp_path, run))


def test_ad_bio_queue_respects_cache_and_allows_force(tmp_path):
    """Bio 已缓存且未过期时不重复入队，手动强制重新排队仍可用。"""
    async def run(db: Database):
        await db.upsert_sender_observation(2002, 9002, "seller2", "Seller2", -10, "群A", 1, "看简介", 100)
        assert await db.queue_bio_fetch(2002, "首次", force=True)
        task = await db.claim_next_bio_fetch_task()
        assert task and task["sender_id"] == 2002
        await db.complete_bio_fetch(2002, "缓存中的 Bio")

        assert not await db.queue_bio_fetch(2002, "缓存期内")
        assert await db.queue_bio_fetch(2002, "手动强制", force=True)

        stats = await db.get_bio_queue_stats()
        assert stats["pending"] == 1

    asyncio.run(_with_db(tmp_path, run))


def test_linkify_bio_renders_telegram_entries(tmp_path):
    """Bio 原文展示时将 URL、t.me 和 @username 转成可点击链接。"""
    db_path = str(tmp_path / "messages.db")
    db = Database(db_path)
    asyncio.run(db.connect())
    try:
        create_app(_make_config(db_path), db)
        rendered = str(templates.env.filters["linkify_bio"]("联系 @seller01，入口 t.me/shop01"))
    finally:
        asyncio.run(db.close())

    assert 'href="https://t.me/seller01"' in rendered
    assert 'href="https://t.me/shop01"' in rendered


def test_ad_bios_page_renders_cached_bio(tmp_path):
    """广告 Bio 页面展示缓存原文和合并后的来源群。"""
    db_path = str(tmp_path / "messages.db")
    db = Database(db_path)
    asyncio.run(db.connect())
    try:
        asyncio.run(db.upsert_sender_observation(3003, 9003, "seller3", "Seller3", -10, "群A", 1, "看主页", 100))
        asyncio.run(db.queue_bio_fetch(3003, "测试", force=True))
        asyncio.run(db.complete_bio_fetch(3003, "主页 t.me/seller3", username="seller3", display_name="Seller3"))
        app = create_app(_make_config(db_path), db)
        with TestClient(app) as client:
            assert client.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
            response = client.get("/ad-bios")
    finally:
        asyncio.run(db.close())

    assert response.status_code == 200
    assert "Seller3" in response.text
    assert "主页" in response.text
    assert "群A" in response.text


def test_bio_candidate_uses_precise_card_site_keyword(tmp_path):
    """卡网自取才触发 Bio 队列，单独讨论卡网不触发。"""
    bot = Bot(_make_config(str(tmp_path / "messages.db")), Database(":memory:"), DedupEngine())

    assert not bot._bio_candidate_reason("这个卡网打不开了")[0]
    assert bot._bio_candidate_reason("卡网自取，看简介")[0]


def test_bio_fetch_rejects_non_user_entity_before_request(tmp_path):
    """队列里混入频道/群实体时，不调用 users.getFullUser。"""
    class FakeClient:
        async def get_input_entity(self, sender_id):
            return object()

        async def __call__(self, request):
            raise AssertionError("non-user entity should not reach GetFullUserRequest")

    async def run():
        bot = Bot(_make_config(str(tmp_path / "messages.db")), Database(":memory:"), DedupEngine())
        bot._client = FakeClient()
        with pytest.raises(ValueError, match="not a Telegram user"):
            await bot._fetch_and_store_bio({"sender_id": -100123, "username": None, "access_hash": None})

    asyncio.run(run())


def test_bio_fetch_prefers_username_before_cached_access_hash(tmp_path):
    """优先用 username 重新解析用户，避免旧 access_hash 触发 Invalid object ID。"""
    class FakeDB:
        def __init__(self):
            self.saved = None

        async def complete_bio_fetch(self, **kwargs):
            self.saved = kwargs

    class FakeClient:
        def __init__(self):
            self.lookups = []
            self.requests = []

        async def get_input_entity(self, lookup):
            self.lookups.append(lookup)
            assert lookup == "plus_t1"
            return InputPeerUser(user_id=5005, access_hash=9999)

        async def __call__(self, request):
            self.requests.append(request.id)
            if isinstance(request.id, InputUser) and request.id.access_hash == 1111:
                raise AssertionError("cached access_hash should not be requested before username")
            return SimpleNamespace(
                full_user=SimpleNamespace(about="卡网：http://shop.example.test"),
                users=[User(id=5005, access_hash=9999, first_name="Plus", username="plus_t1")],
            )

    async def run():
        db = FakeDB()
        bot = Bot(_make_config(str(tmp_path / "messages.db")), db, DedupEngine())
        bot._client = FakeClient()
        await bot._fetch_and_store_bio({"sender_id": 5005, "username": "plus_t1", "access_hash": 1111})

        assert bot._client.lookups == ["plus_t1"]
        assert bot._client.requests[0].access_hash == 9999
        assert db.saved["bio_text"] == "卡网：http://shop.example.test"
        assert db.saved["username"] == "plus_t1"
        assert db.saved["access_hash"] == 9999

    asyncio.run(run())


def test_bio_fetch_falls_back_when_username_resolves_to_other_user(tmp_path):
    """username 已被别人占用时，不保存错用户的 Bio，继续尝试缓存 access_hash。"""
    class FakeDB:
        def __init__(self):
            self.saved = None

        async def complete_bio_fetch(self, **kwargs):
            self.saved = kwargs

    class FakeClient:
        def __init__(self):
            self.lookups = []
            self.requests = []

        async def get_input_entity(self, lookup):
            self.lookups.append(lookup)
            assert lookup == "stale_name"
            return InputPeerUser(user_id=9999, access_hash=2222)

        async def get_entity(self, lookup):
            self.lookups.append(f"fresh:{lookup}")
            assert lookup == "stale_name"
            return User(id=9999, access_hash=3333, first_name="Other", username="stale_name")

        async def __call__(self, request):
            self.requests.append(request.id)
            assert isinstance(request.id, InputUser)
            assert request.id.user_id == 5005
            return SimpleNamespace(
                full_user=SimpleNamespace(about="卡网自取：http://shop.example.test"),
                users=[User(id=5005, access_hash=1111, first_name="Plus", username="plus_t1")],
            )

    async def run():
        db = FakeDB()
        bot = Bot(_make_config(str(tmp_path / "messages.db")), db, DedupEngine())
        bot._client = FakeClient()
        await bot._fetch_and_store_bio({"sender_id": 5005, "username": "stale_name", "access_hash": 1111})

        assert bot._client.lookups == ["stale_name", "fresh:stale_name"]
        assert len(bot._client.requests) == 1
        assert db.saved["bio_text"] == "卡网自取：http://shop.example.test"
        assert db.saved["username"] == "plus_t1"

    asyncio.run(run())


def test_bio_fetch_refreshes_username_after_stale_cached_input(tmp_path):
    """username 缓存里的 access_hash 失效时，强制刷新 username 后再抓取。"""
    class FakeDB:
        def __init__(self):
            self.saved = None

        async def complete_bio_fetch(self, **kwargs):
            self.saved = kwargs

    class FakeClient:
        def __init__(self):
            self.lookups = []
            self.requests = []

        async def get_input_entity(self, lookup):
            self.lookups.append(f"cache:{lookup}")
            assert lookup == "plus_t1"
            return InputPeerUser(user_id=5005, access_hash=1111)

        async def get_entity(self, lookup):
            self.lookups.append(f"fresh:{lookup}")
            assert lookup == "plus_t1"
            return User(id=5005, access_hash=9999, first_name="Plus", username="plus_t1")

        async def __call__(self, request):
            self.requests.append(request.id)
            if request.id.access_hash == 1111:
                raise ValueError("Invalid object ID for a user")
            return SimpleNamespace(
                full_user=SimpleNamespace(about="卡网自取：http://shop.example.test"),
                users=[User(id=5005, access_hash=9999, first_name="Plus", username="plus_t1")],
            )

    async def run():
        db = FakeDB()
        bot = Bot(_make_config(str(tmp_path / "messages.db")), db, DedupEngine())
        bot._client = FakeClient()
        await bot._fetch_and_store_bio({"sender_id": 5005, "username": "plus_t1", "access_hash": 1111})

        assert bot._client.lookups == ["cache:plus_t1", "fresh:plus_t1"]
        assert [r.access_hash for r in bot._client.requests] == [1111, 9999]
        assert db.saved["bio_text"] == "卡网自取：http://shop.example.test"
        assert db.saved["access_hash"] == 9999

    asyncio.run(run())
