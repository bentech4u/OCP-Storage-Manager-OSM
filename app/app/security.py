"""Admin password, session cookies and optional OIDC login."""
from __future__ import annotations

from typing import Optional

from fastapi import Request
from fastapi.responses import RedirectResponse
import bcrypt
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import store
from .config import SESSION_COOKIE, SESSION_MAX_AGE, session_secret

PUBLIC_PATHS = ("/login", "/auth/", "/static/", "/healthz", "/favicon.ico")

# How each sign-in reads in the interface.
METHOD_LABELS = {"password": "local", "entra": "Microsoft Entra ID", "oidc": "single sign-on",
                 "maintenance": "maintenance"}


def _encode(password: str) -> bytes:
    # bcrypt itself refuses anything over 72 bytes, so cut there rather than fail.
    return password.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_encode(password), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_encode(password), hashed.encode())
    except (ValueError, TypeError):
        return False


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(session_secret(), salt="osm-session")


def make_session(username: str, via: str = "password", role: str = "admin") -> str:
    """The cookie carries who signed in, how, and what they may do."""
    return _serializer().dumps({"u": username, "via": via, "role": role})


def read_session(request: Request) -> Optional[dict]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return _serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    except Exception:
        return None


def read_session_value(token: str) -> Optional[dict]:
    """Read a signed value that did not come from a cookie, such as an OIDC state."""
    if not token:
        return None
    try:
        return _serializer().loads(token, max_age=900)
    except Exception:                                        # noqa: BLE001
        return None


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax"
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def is_admin(session: dict | None) -> bool:
    return bool(session) and session.get("role", "admin") != "viewer"


def password_is_set() -> bool:
    return bool(store.load()["setup"].get("admin_password_hash"))


def is_public(path: str) -> bool:
    return any(path.startswith(p) for p in PUBLIC_PATHS)


def login_redirect(request: Request) -> RedirectResponse:
    nxt = request.url.path
    target = "/login" if password_is_set() else "/login?first_run=1"
    if nxt and nxt != "/":
        target += ("&" if "?" in target else "?") + f"next={nxt}"
    return RedirectResponse(target, status_code=303)
