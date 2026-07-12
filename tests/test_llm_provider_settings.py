from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

from fastapi.testclient import TestClient

from config import Config
from database import Database
from web import routes
from web import create_app
from web.auth import CSRF_COOKIE


def _config(db_path: str) -> Config:
    return Config(
        api_id=1,
        api_hash="hash",
        owner_id=1,
        llm_base_url="https://legacy.example/v1",
        llm_api_key="legacy-secret-key",
        db_path=db_path,
        session_path="./data/test.session",
        web_password="secret",
    )


@contextmanager
def _client(tmp_path):
    db = Database(str(tmp_path / "messages.db"))
    asyncio.run(db.connect())
    try:
        with TestClient(create_app(_config(str(tmp_path / "messages.db")), db)) as client:
            assert client.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
            yield client, db
    finally:
        asyncio.run(db.close())


def _provider_form(client, providers: list[dict]) -> dict[str, list[str] | str]:
    model_lists = ["\n".join(provider["models"] if "models" in provider else [provider["model"]]) for provider in providers]
    return {
        "csrf_token": client.cookies.get(CSRF_COOKIE),
        "llm_provider_id": [p["id"] for p in providers],
        "llm_provider_base_url": [p["base_url"] for p in providers],
        "llm_provider_api_key": [p["api_key"] for p in providers],
        "llm_provider_models": model_lists,
        "llm_provider_api_format": [p.get("api_format", "chat") for p in providers],
    }


def test_settings_page_masks_legacy_key_and_persists_provider_order(tmp_path):
    with _client(tmp_path) as (client, db):
        page = client.get("/settings")
        assert page.status_code == 200
        assert "legacy-secret-key" not in page.text
        assert "••••••••-key" in page.text
        assert 'name="llm_provider_models"' in page.text
        assert 'name="llm_provider_model"' not in page.text
        assert "llm-test-connection" in page.text

        providers = [
            {"id": "primary", "base_url": "https://one.example/v1", "api_key": "first-secret", "model": "model-one"},
            {"id": "backup", "base_url": "https://two.example/v1", "api_key": "second-secret", "model": "model-two", "api_format": "responses"},
        ]
        response = client.post("/settings", data=_provider_form(client, providers))
        assert response.status_code == 200
        saved = json.loads(asyncio.run(db.get_setting("llm_providers")))

    assert [provider["id"] for provider in saved] == ["primary", "backup"]
    assert saved[1]["api_format"] == "responses"
    assert saved[0]["models"] == ["model-one"]
    assert saved[0]["model"] == "model-one"


def test_settings_keeps_masked_key_when_reordering(tmp_path):
    existing = [
        {"id": "first", "base_url": "https://one", "api_key": "first-secret", "model": "one", "api_format": "chat"},
        {"id": "second", "base_url": "https://two", "api_key": "second-secret", "model": "two", "api_format": "chat"},
    ]
    with _client(tmp_path) as (client, db):
        asyncio.run(db.set_setting("llm_providers", json.dumps(existing)))
        response = client.post("/settings", data=_provider_form(client, [
            {"id": "second", "base_url": "https://two", "api_key": "••••••••cret", "model": "two"},
            {"id": "first", "base_url": "https://one", "api_key": "••••••••cret", "model": "one"},
        ]))
        assert response.status_code == 200
        saved = json.loads(asyncio.run(db.get_setting("llm_providers")))

    assert [(p["id"], p["api_key"]) for p in saved] == [
        ("second", "second-secret"),
        ("first", "first-secret"),
    ]


def test_settings_persists_model_list_in_submitted_order(tmp_path):
    with _client(tmp_path) as (client, db):
        response = client.post("/settings", data=_provider_form(client, [{
            "id": "primary",
            "base_url": "https://one.example/v1",
            "api_key": "first-secret",
            "models": ["model-primary", "model-backup", "model-last"],
        }]))
        assert response.status_code == 200
        saved = json.loads(asyncio.run(db.get_setting("llm_providers")))

    assert saved == [{
        "id": "primary",
        "base_url": "https://one.example/v1",
        "api_key": "first-secret",
        "models": ["model-primary", "model-backup", "model-last"],
        "model": "model-primary",
        "api_format": "chat",
    }]


def test_settings_rejects_provider_without_a_model(tmp_path):
    with _client(tmp_path) as (client, _db):
        response = client.post("/settings", data=_provider_form(client, [{
            "id": "primary",
            "base_url": "https://one.example/v1",
            "api_key": "first-secret",
            "models": ["", "   "],
        }]))

    assert response.status_code == 422


def test_invalid_provider_does_not_partially_save_other_settings(tmp_path):
    with _client(tmp_path) as (client, db):
        asyncio.run(db.set_setting("system_prompt", "existing prompt"))
        form = _provider_form(client, [{
            "id": "primary",
            "base_url": "https://one.example/v1",
            "api_key": "first-secret",
            "models": [],
        }])
        form["system_prompt"] = "should not persist"
        response = client.post("/settings", data=form)

        assert response.status_code == 422
        assert asyncio.run(db.get_setting("system_prompt")) == "existing prompt"


def test_llm_connection_test_uses_masked_saved_key_without_persisting(tmp_path, monkeypatch):
    captured = {}

    async def fake_test(_config, base_url, api_key, model, api_format):
        captured.update(base_url=base_url, api_key=api_key, model=model, api_format=api_format)

    monkeypatch.setattr(routes, "_test_llm_provider_connection", fake_test)
    saved_provider = [{
        "id": "primary",
        "base_url": "https://saved.example/v1",
        "api_key": "saved-secret-key",
        "models": ["saved-model"],
        "model": "saved-model",
        "api_format": "chat",
    }]
    with _client(tmp_path) as (client, db):
        asyncio.run(db.set_setting("llm_providers", json.dumps(saved_provider)))
        response = client.post("/settings/test-llm-provider", data={
            "csrf_token": client.cookies.get(CSRF_COOKIE),
            "provider_id": "primary",
            "base_url": "https://unsaved.example/v1",
            "api_key": "••••••••-key",
            "model": "selected-model",
            "api_format": "responses",
        })
        persisted = asyncio.run(db.get_setting("llm_providers"))

    assert response.json() == {"ok": True, "message": "连接成功：selected-model 可用。"}
    assert captured == {
        "base_url": "https://unsaved.example/v1",
        "api_key": "saved-secret-key",
        "model": "selected-model",
        "api_format": "responses",
    }
    assert json.loads(persisted) == saved_provider


def test_llm_connection_test_rejects_invalid_fields(tmp_path):
    with _client(tmp_path) as (client, _db):
        response = client.post("/settings/test-llm-provider", data={
            "csrf_token": client.cookies.get(CSRF_COOKIE),
            "provider_id": "invalid id!",
            "base_url": "not-a-url",
            "api_key": "key",
            "model": "",
            "api_format": "unknown",
        })

    assert response.status_code == 422
    assert response.json()["ok"] is False


def test_llm_connection_test_rejects_unresolvable_masked_key(tmp_path):
    with _client(tmp_path) as (client, _db):
        response = client.post("/settings/test-llm-provider", data={
            "csrf_token": client.cookies.get(CSRF_COOKIE),
            "provider_id": "new-provider",
            "base_url": "https://example.com/v1",
            "api_key": "••••••••-key",
            "model": "test-model",
            "api_format": "chat",
        })

    assert response.status_code == 422
    assert response.json() == {"ok": False, "message": "请重新填写 API Key"}


def test_llm_connection_test_does_not_leak_key_on_failure(tmp_path, monkeypatch):
    async def fake_test(*_args):
        raise RuntimeError("never-expose-this-key")

    monkeypatch.setattr(routes, "_test_llm_provider_connection", fake_test)
    with _client(tmp_path) as (client, _db):
        response = client.post("/settings/test-llm-provider", data={
            "csrf_token": client.cookies.get(CSRF_COOKIE),
            "provider_id": "primary",
            "base_url": "https://example.com/v1",
            "api_key": "never-expose-this-key",
            "model": "test-model",
            "api_format": "chat",
        })

    assert response.json() == {"ok": False, "message": "连接失败：RuntimeError"}
    assert "never-expose-this-key" not in response.text
