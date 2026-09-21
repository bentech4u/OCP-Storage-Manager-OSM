"""OCP Storage Manager: a small control panel for PowerScale CSI and CSM Replication."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import urllib.parse

from . import security, store
from .config import APP_NAME, APP_TAGLINE, ensure_dirs
from .routers import api, install, operations, setup
from .services import oidc
from .templating import templates

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
ensure_dirs()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.middleware("http")
async def require_login(request: Request, call_next):
    if security.is_public(request.url.path):
        return await call_next(request)
    session = security.read_session(request)
    if not session:
        if request.headers.get("HX-Request"):
            resp = JSONResponse({"detail": "session expired"}, status_code=401)
            resp.headers["HX-Redirect"] = "/login"
            return resp
        return security.login_redirect(request)
    request.state.user = session.get("u", "admin")
    request.state.via = session.get("via", "password")
    request.state.role = session.get("role", "admin")
    if request.method not in ("GET", "HEAD") and not security.is_admin(session):
        message = "This account is read-only."
        if request.headers.get("HX-Request"):
            return HTMLResponse(f'<div class="banner bad"><span>⚠</span><div>{message}</div></div>',
                                status_code=403)
        return JSONResponse({"detail": message}, status_code=403)
    return await call_next(request)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


def _sign_in_options(state: dict) -> list[dict]:
    """What the list box on the login page offers.

    The directory is always listed, even before it is configured, so it is obvious the
    console supports it. An unconfigured entry is greyed out rather than hidden.
    """
    cfg = state["setup"]["oidc"]
    local_off = state["setup"].get("local_login", "always") == "off" and cfg["enabled"]
    label = "Microsoft Entra ID" if cfg.get("provider", "entra") == "entra" else "Single sign-on"
    options = [
        {"value": "local", "label": "Local account", "disabled": local_off,
         "note": "switched off" if local_off else ""},
        {"value": "entra", "label": label, "disabled": not cfg["enabled"],
         "note": "" if cfg["enabled"] else "not configured yet"},
    ]
    return options


def _login_page(request: Request, error: str = "", next: str = "/", chosen: str = "local",
                notice: str = "", username: str = "", offer_redirect: bool = False):
    state = store.load()
    cfg = state["setup"]["oidc"]
    # a route the operator cannot reach is no advice at all: when the password form cannot
    # work for this account, offer the provider's page whatever the configured method
    show_redirect = bool(cfg.get("redirect_url")) and cfg["enabled"] and (
        offer_redirect or cfg.get("method", "both") == "both")
    return templates.TemplateResponse(request, "login.html", {
        "username": username, "show_redirect": show_redirect,
        "first_run": not security.password_is_set(),
        "next": next, "error": error, "notice": notice,
        "app_name": APP_NAME, "tagline": APP_TAGLINE,
        "oidc": state["setup"]["oidc"], "options": _sign_in_options(state), "chosen": chosen,
        "oidc_ready": state["setup"]["oidc"]["enabled"],
        "oidc_method": state["setup"]["oidc"].get("method", "both"),
        "redirect_available": bool(cfg.get("redirect_url")),
    })


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, first_run: int = 0, next: str = "/", error: str = ""):
    return _login_page(request, error=error, next=next)


@app.post("/login")
async def login_submit(request: Request, password: str = Form(""), confirm: str = Form(""),
                       username: str = Form(""), source: str = Form("local"),
                       next: str = Form("/")):
    state = store.load()

    # first start: no password exists yet, so this call sets it
    if not state["setup"].get("admin_password_hash") and source == "local":
        if len(password) < 8:
            return _login_page(request, error="Use at least 8 characters.", next=next)
        if password != confirm:
            return _login_page(request, error="The two passwords do not match.", next=next)
        store.update(lambda s: s["setup"].update(
            {"admin_password_hash": security.hash_password(password)}))
        token = security.make_session(state["setup"].get("admin_user", "admin"), "password")
    elif source == "entra":
        cfg = state["setup"]["oidc"]
        if cfg.get("method") == "redirect":
            return RedirectResponse(f"/auth/oidc/login?next={urllib.parse.quote(next or '/')}",
                                    status_code=303)
        if not cfg.get("enabled"):
            return _login_page(request, error="Single sign-on is not enabled.", next=next,
                               chosen="local")
        try:
            claims = oidc.password_login(cfg, username.strip(), password)
        except oidc.OidcError as exc:
            message = str(exc)
            needs_browser = any(hint in message for hint in
                                ("multi-factor", "conditional access"))
            return _login_page(request, error=message, next=next, chosen="entra",
                               username=username, offer_redirect=needs_browser)
        role = oidc.role_for(cfg, claims)
        if not role:
            groups = ", ".join(oidc.groups_of(claims)) or "none"
            return _login_page(request, next=next, chosen="entra", username=username, error=(
                "that account signed in, but none of its groups are allowed here. It has: "
                f"{groups}"))
        token = security.make_session(oidc.account_name(claims), "entra", role)
    else:
        policy = state["setup"].get("local_login", "always")
        if policy == "off" and state["setup"]["oidc"]["enabled"]:
            return _login_page(request, chosen="entra", next=next, error=(
                "local sign-in is switched off. Use single sign-on, or run "
                "osmctl admin local-login always on the installer host."))
        if policy == "installer-host" and (request.client.host if request.client else "") not in (
                "127.0.0.1", "::1"):
            return _login_page(request, chosen="entra", next=next, error=(
                "local sign-in is limited to the installer host."))
        expected = state["setup"].get("admin_user", "admin")
        if username and username.strip() != expected:
            return _login_page(request, error="Wrong user or password.", next=next,
                               username=username)
        if not security.verify_password(password, state["setup"]["admin_password_hash"] or ""):
            return _login_page(request, error="Wrong user or password.", next=next,
                               username=username)
        token = security.make_session(expected, "password")

    response = RedirectResponse(next or "/", status_code=303)
    security.set_session_cookie(response, token, secure=request.url.scheme == "https")
    return response


@app.get("/auth/oidc/login")
async def oidc_redirect(request: Request, next: str = "/"):
    """Send the browser to the provider's own sign-in page."""
    cfg = store.load()["setup"]["oidc"]
    if not cfg.get("enabled"):
        return RedirectResponse("/login?error=Single+sign-on+is+not+enabled", status_code=303)
    redirect_uri = cfg.get("redirect_url") or str(request.url_for("oidc_callback"))
    state = security.make_session(f"state:{next}", "oidc-state")
    try:
        url = oidc.auth_url(cfg, state, redirect_uri)
    except oidc.OidcError as exc:
        return RedirectResponse(f"/login?error={urllib.parse.quote(str(exc))}", status_code=303)
    return RedirectResponse(url, status_code=303)


@app.get("/auth/oidc/callback", name="oidc_callback")
async def oidc_callback(request: Request, code: str = "", state: str = "", error: str = "",
                        error_description: str = ""):
    if error:
        message = error_description or error
        return RedirectResponse(f"/login?error={urllib.parse.quote(message[:200])}",
                                status_code=303)
    cfg = store.load()["setup"]["oidc"]
    parsed = security.read_session_value(state)
    target = "/"
    if parsed and str(parsed.get("u", "")).startswith("state:"):
        target = parsed["u"].split("state:", 1)[1] or "/"
    redirect_uri = cfg.get("redirect_url") or str(request.url_for("oidc_callback"))
    try:
        claims = oidc.exchange_code(cfg, code, redirect_uri)
    except oidc.OidcError as exc:
        return RedirectResponse(f"/login?error={urllib.parse.quote(str(exc))}", status_code=303)
    role = oidc.role_for(cfg, claims)
    if not role:
        return RedirectResponse("/login?error=" + urllib.parse.quote(
            "that account is not in a group allowed here"), status_code=303)
    response = RedirectResponse(target, status_code=303)
    security.set_session_cookie(response, security.make_session(
        oidc.account_name(claims), "entra", role), secure=request.url.scheme == "https")
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    security.clear_session_cookie(response)
    return response


app.include_router(operations.router)
app.include_router(install.router)
app.include_router(setup.router)
app.include_router(api.router)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    from .routers.home import dashboard
    return await dashboard(request)
