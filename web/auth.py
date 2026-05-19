from __future__ import annotations

import hmac
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer

router = APIRouter()

SESSION_MAX_AGE = 86400  # 24h
COOKIE_NAME = "tg_lurker_session"
CSRF_COOKIE = "tg_lurker_csrf"
CSRF_FIELD = "csrf_token"


def _get_serializer(request: Request) -> URLSafeTimedSerializer:
    """Builds a timed serializer using the app's web password as the signing key."""
    secret = request.app.state.config.web_password
    return URLSafeTimedSerializer(secret)


def is_authenticated(request: Request) -> bool:
    """Checks whether the request carries a valid, unexpired session cookie."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return False
    s = _get_serializer(request)
    try:
        s.loads(cookie, max_age=SESSION_MAX_AGE)
        return True
    except Exception:
        return False


def get_csrf_token(request: Request) -> str:
    """Returns the existing CSRF token from the cookie, or generates a new one."""
    token = request.cookies.get(CSRF_COOKIE)
    if not token:
        token = secrets.token_hex(16)
    return token


def set_csrf_cookie(response, token: str) -> None:
    """Attaches the CSRF token as a non-httponly cookie so client-side JS can read it."""
    response.set_cookie(CSRF_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=False, samesite="lax")


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
        s = _get_serializer(request)
        token = s.dumps({"auth": True})
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
        csrf = secrets.token_hex(16)
        set_csrf_cookie(response, csrf)
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
