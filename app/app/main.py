"""OCP Storage Manager: a small control panel for PowerScale CSI and CSM Replication."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import security, store
from .config import APP_NAME, APP_TAGLINE, ensure_dirs
from .routers import api, install, operations, setup
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
    return await call_next(request)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, first_run: int = 0, next: str = "/", error: str = ""):
    return templates.TemplateResponse(request, "login.html", {
        "first_run": not security.password_is_set(),
        "next": next, "error": error, "app_name": APP_NAME, "tagline": APP_TAGLINE,
        "oidc": store.load()["setup"]["oidc"],
    })


@app.post("/login")
async def login_submit(request: Request, password: str = Form(""), confirm: str = Form(""),
                       next: str = Form("/")):
    state = store.load()
    if not state["setup"].get("admin_password_hash"):
        if len(password) < 8:
            return await login_form(request, next=next, error="Use at least 8 characters.")
        if password != confirm:
            return await login_form(request, next=next, error="The two passwords do not match.")
        store.update(lambda s: s["setup"].update(
            {"admin_password_hash": security.hash_password(password)}))
        token = security.make_session("admin")
    else:
        if not security.verify_password(password, state["setup"]["admin_password_hash"]):
            return await login_form(request, next=next, error="Wrong password.")
        token = security.make_session("admin")
    response = RedirectResponse(next or "/", status_code=303)
    security.set_session_cookie(response, token)
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
