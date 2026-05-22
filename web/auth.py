from __future__ import annotations

import hmac
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer

router = APIRouter()

SESSION_DAYS_SETTING = "web_session_days"
DEFAULT_SESSION_DAYS = 30
SESSION_MIN_DAYS = 1
SESSION_MAX_DAYS = 365
SECONDS_PER_DAY = 86400
COOKIE_NAME = "tg_lurker_session"
CSRF_COOKIE = "tg_lurker_csrf"
CSRF_FIELD = "csrf_token"


def normalize_session_days(value, default_days: int = DEFAULT_SESSION_DAYS) -> int:
    """将登录有效期规范化为可接受的天数范围。"""
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        days = default_days
    return max(SESSION_MIN_DAYS, min(SESSION_MAX_DAYS, days))


async def ensure_session_days_loaded(request: Request) -> None:
    """从 settings 表加载 Web 登录有效期，并缓存到 app.state。"""
    if getattr(request.app.state, "web_session_days_loaded", False):
        return
    config_days = normalize_session_days(getattr(request.app.state.config, "web_session_days", DEFAULT_SESSION_DAYS))
    raw_days = await request.app.state.db.get_setting(SESSION_DAYS_SETTING, str(config_days))
    request.app.state.web_session_days = normalize_session_days(raw_days, config_days)
    request.app.state.web_session_days_loaded = True


def get_session_max_age(request: Request) -> int:
    """返回当前 Web session cookie 的有效秒数。"""
    days = normalize_session_days(getattr(request.app.state, "web_session_days", DEFAULT_SESSION_DAYS))
    return days * SECONDS_PER_DAY


def _get_serializer(request: Request) -> URLSafeTimedSerializer:
    """Builds a timed serializer using the app's web password as the signing key."""
    secret = request.app.state.config.web_password
    return URLSafeTimedSerializer(secret)


def create_session_token(request: Request) -> str:
    """签发新的 Web session token。"""
    return _get_serializer(request).dumps({"auth": True})


def is_authenticated(request: Request) -> bool:
    """Checks whether the request carries a valid, unexpired session cookie."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return False
    s = _get_serializer(request)
    try:
        s.loads(cookie, max_age=get_session_max_age(request))
        return True
    except Exception:
        return False


def get_csrf_token(request: Request) -> str:
    """Returns the existing CSRF token from the cookie, or generates a new one."""
    token = request.cookies.get(CSRF_COOKIE)
    if not token:
        token = secrets.token_hex(16)
    return token


def set_session_cookie(response, token: str, max_age: int) -> None:
    """写入签名后的 Web session cookie。"""
    response.set_cookie(COOKIE_NAME, token, max_age=max_age, httponly=True, samesite="lax")


def set_csrf_cookie(response, token: str, max_age: int) -> None:
    """写入 CSRF cookie，前端需要读取该值后随表单提交。"""
    response.set_cookie(CSRF_COOKIE, token, max_age=max_age, httponly=False, samesite="lax")


def refresh_auth_cookies(response, request: Request) -> None:
    """保存设置后按最新有效期刷新当前登录与 CSRF cookie。"""
    max_age = get_session_max_age(request)
    session_cookie = request.cookies.get(COOKIE_NAME)
    if session_cookie:
        set_session_cookie(response, create_session_token(request), max_age)
    set_csrf_cookie(response, get_csrf_token(request), max_age)


async def verify_csrf(request: Request) -> bool:
    """Validates the double-submit CSRF cookie against the form field."""
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    form = await request.form()
    form_token = form.get(CSRF_FIELD, "")
    return cookie_token != "" and cookie_token == form_token


@router.get("/login")
async def login_page(request: Request):
    """Renders the login form."""
    from web import templates
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/login")
async def login_submit(request: Request):
    """Validates credentials and sets session + CSRF cookies on success."""
    from web import templates
    form = await request.form()
    password = form.get("password", "")

    if hmac.compare_digest(password.encode("utf-8"), request.app.state.config.web_password.encode("utf-8")):
        token = create_session_token(request)
        response = RedirectResponse("/", status_code=303)
        max_age = get_session_max_age(request)
        set_session_cookie(response, token, max_age)
        csrf = secrets.token_hex(16)
        set_csrf_cookie(response, csrf, max_age)
        return response

    return templates.TemplateResponse(
        request, "login.html", {"error": "密码错误"}
    )


@router.get("/logout")
async def logout(request: Request):
    """Clears session and CSRF cookies, then redirects to the login page."""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE)
    return response
