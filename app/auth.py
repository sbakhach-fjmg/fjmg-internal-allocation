"""Shared-password sign-in with an HMAC-signed cookie. APP_PASSWORD empty = open (laptop only)."""
from __future__ import annotations
import hashlib
import hmac
from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from app.config import APP_PASSWORD, APP_SECRET

COOKIE = "fj_used_session"
OPEN_PATHS = ("/login", "/static/", "/healthz")


def _token() -> str:
    return hmac.new((APP_SECRET or "dev").encode(), b"fj-used-sales-session", hashlib.sha256).hexdigest()


def is_authed(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _token())


def password_ok(pw: str) -> bool:
    return bool(APP_PASSWORD) and hmac.compare_digest(pw, APP_PASSWORD)


def set_session(resp):
    resp.set_cookie(COOKIE, _token(), max_age=30 * 24 * 3600, httponly=True, samesite="lax", secure=bool(APP_SECRET))
    return resp


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith(OPEN_PATHS) or is_authed(request):
            return await call_next(request)
        return RedirectResponse(url=f"/login?next={path}", status_code=303)
