from __future__ import annotations

import asyncio
from contextlib import contextmanager

from fastapi.testclient import TestClient

from config import Config
from database import Database
from web import create_app
from web.auth import COOKIE_NAME, CSRF_COOKIE, SECONDS_PER_DAY, SESSION_DAYS_SETTING


def _make_config(db_path: str, web_session_days: int = 30) -> Config:
    return Config(
        api_id=1,
        api_hash="hash",
        owner_id=1,
        llm_base_url="https://example.test/v1",
        llm_api_key="sk-test",
        db_path=db_path,
        session_path="./data/test.session",
        web_password="secret",
        web_session_days=web_session_days,
    )


@contextmanager
def _client(tmp_path, stored_session_days: int | None = None, config_session_days: int = 30):
    db_path = str(tmp_path / "messages.db")
    db = Database(db_path)
    asyncio.run(db.connect())
    if stored_session_days is not None:
        asyncio.run(db.set_setting(SESSION_DAYS_SETTING, str(stored_session_days)))
    app = create_app(_make_config(db_path, config_session_days), db)
    try:
        with TestClient(app) as client:
            yield client, db, app
    finally:
        asyncio.run(db.close())


def _cookie_header(response, cookie_name: str) -> str:
    cookies = response.headers.get_list("set-cookie")
    return next(cookie for cookie in cookies if cookie.startswith(f"{cookie_name}="))


def _assert_cookie_max_age(response, cookie_name: str, days: int) -> None:
    header = _cookie_header(response, cookie_name)
    assert f"Max-Age={days * SECONDS_PER_DAY}" in header


def _login(client: TestClient):
    return client.post("/login", data={"password": "secret"}, follow_redirects=False)


def _post_settings(client: TestClient, data: dict):
    payload = {"csrf_token": client.cookies.get(CSRF_COOKIE)}
    payload.update(data)
    return client.post("/settings", data=payload)


def test_login_cookie_defaults_to_30_days(tmp_path):
    """未配置时，Web 登录 cookie 默认保留 30 天。"""
    with _client(tmp_path) as (client, _, _):
        response = _login(client)

    assert response.status_code == 303
    _assert_cookie_max_age(response, COOKIE_NAME, 30)
    _assert_cookie_max_age(response, CSRF_COOKIE, 30)


def test_persisted_setting_controls_login_cookie(tmp_path):
    """重启后从 settings 表读取已保存的 Web 登录有效期。"""
    with _client(tmp_path, stored_session_days=45) as (client, _, app):
        response = _login(client)

    assert response.status_code == 303
    assert app.state.web_session_days == 45
    _assert_cookie_max_age(response, COOKIE_NAME, 45)
    _assert_cookie_max_age(response, CSRF_COOKIE, 45)


def test_settings_updates_session_days_and_refreshes_cookies(tmp_path):
    """设置页保存后，当前 cookie 与后续登录都使用新的有效期。"""
    with _client(tmp_path) as (client, db, app):
        login_response = _login(client)
        assert login_response.status_code == 303

        response = _post_settings(client, {SESSION_DAYS_SETTING: "45"})

        assert response.status_code == 200
        assert asyncio.run(db.get_setting(SESSION_DAYS_SETTING)) == "45"
        assert app.state.web_session_days == 45
        _assert_cookie_max_age(response, COOKIE_NAME, 45)
        _assert_cookie_max_age(response, CSRF_COOKIE, 45)

        client.get("/logout", follow_redirects=False)
        relogin_response = _login(client)

    assert relogin_response.status_code == 303
    _assert_cookie_max_age(relogin_response, COOKIE_NAME, 45)
    _assert_cookie_max_age(relogin_response, CSRF_COOKIE, 45)


def test_settings_clamps_invalid_session_days(tmp_path):
    """设置页提交异常值时，服务端按 1 到 365 天范围规范化。"""
    with _client(tmp_path) as (client, db, app):
        assert _login(client).status_code == 303

        low_response = _post_settings(client, {SESSION_DAYS_SETTING: "0"})
        assert low_response.status_code == 200
        assert asyncio.run(db.get_setting(SESSION_DAYS_SETTING)) == "1"
        assert app.state.web_session_days == 1
        _assert_cookie_max_age(low_response, COOKIE_NAME, 1)
        _assert_cookie_max_age(low_response, CSRF_COOKIE, 1)

        high_response = _post_settings(client, {SESSION_DAYS_SETTING: "999"})
        assert high_response.status_code == 200
        assert asyncio.run(db.get_setting(SESSION_DAYS_SETTING)) == "365"
        assert app.state.web_session_days == 365
        _assert_cookie_max_age(high_response, COOKIE_NAME, 365)
        _assert_cookie_max_age(high_response, CSRF_COOKIE, 365)

        invalid_response = _post_settings(client, {SESSION_DAYS_SETTING: "abc"})
        assert asyncio.run(db.get_setting(SESSION_DAYS_SETTING)) == "30"
        assert app.state.web_session_days == 30

    assert invalid_response.status_code == 200
    _assert_cookie_max_age(invalid_response, COOKIE_NAME, 30)
    _assert_cookie_max_age(invalid_response, CSRF_COOKIE, 30)


def test_missing_session_days_field_preserves_existing_setting(tmp_path):
    """程序化保存设置但缺少登录有效期字段时，不重置已有设置。"""
    with _client(tmp_path, stored_session_days=45) as (client, db, app):
        assert _login(client).status_code == 303

        response = _post_settings(client, {})
        assert asyncio.run(db.get_setting(SESSION_DAYS_SETTING)) == "45"
        assert app.state.web_session_days == 45

    assert response.status_code == 200
    _assert_cookie_max_age(response, COOKIE_NAME, 45)
    _assert_cookie_max_age(response, CSRF_COOKIE, 45)
