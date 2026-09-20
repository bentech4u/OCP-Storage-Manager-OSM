"""Setup: console admin password, OIDC / Microsoft Entra ID, alerting."""
from __future__ import annotations

import json
import shutil
import smtplib
import urllib.request
from email.message import EmailMessage

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import security, store
from ..config import (BIN_DIR, DATA_DIR, HELM_CHART_DIR, KUBECONFIG_DIR, LOG_DIR, ROOT,
                      SECRET_FILE, STATE_FILE, ensure_dirs)
from ..services import inventory, replication, tools
from ..templating import templates

router = APIRouter()


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, notice: str = "", error: str = ""):
    state = store.load()
    return templates.TemplateResponse(request, "setup.html", {
        "page": "setup", "setup": state["setup"], "notice": notice, "error": error,
        "tools": tools.status(),
        "user": getattr(request.state, "user", "admin"),
        "password_set": bool(state["setup"].get("admin_password_hash")),
        "paths": {
            "Application root": str(ROOT),
            "Data directory": str(DATA_DIR),
            "Kubeconfigs": str(KUBECONFIG_DIR),
            "repctl store": str(replication.store_dir()),
            "Job logs": str(LOG_DIR),
            "Charts": str(HELM_CHART_DIR),
            "Tools": str(BIN_DIR),
        },
    })


@router.post("/setup/password", response_class=HTMLResponse)
async def change_password(request: Request, new: str = Form(...), confirm: str = Form(...),
                          current: str = Form("")):
    state = store.load()
    existing = state["setup"].get("admin_password_hash")
    # A console with no password yet, for instance right after a full reset, sets one here
    # rather than demanding a current password that does not exist.
    if existing and not security.verify_password(current, existing):
        return await setup_page(request, error="The current password is wrong.")
    if len(new) < 8:
        return await setup_page(request, error="The new password needs at least 8 characters.")
    if new != confirm:
        return await setup_page(request, error="The new passwords do not match.")
    store.update(lambda s: s["setup"].update({"admin_password_hash": security.hash_password(new)}))
    return await setup_page(request,
                            notice="Password set." if not existing else "Password changed.")


@router.post("/setup/oidc", response_class=HTMLResponse)
async def save_oidc(request: Request, enabled: str = Form(""), provider: str = Form("entra"),
                    tenant_id: str = Form(""), issuer: str = Form(""), client_id: str = Form(""),
                    client_secret: str = Form(""), redirect_url: str = Form(""),
                    allowed_groups: str = Form("")):
    if provider == "entra" and tenant_id and not issuer:
        issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
    cfg = {
        "enabled": enabled == "on", "provider": provider, "tenant_id": tenant_id.strip(),
        "issuer": issuer.strip(), "client_id": client_id.strip(),
        "client_secret": client_secret.strip(), "redirect_url": redirect_url.strip(),
        "allowed_groups": [g.strip() for g in allowed_groups.split(",") if g.strip()],
    }
    store.update(lambda s: s["setup"].update({"oidc": cfg}))
    notice = "Single sign-on settings saved."
    if cfg["enabled"]:
        notice += " Sign-in with the provider appears on the login page."
    return await setup_page(request, notice=notice)


@router.post("/setup/oidc/test", response_class=HTMLResponse)
async def test_oidc(request: Request):
    cfg = store.load()["setup"]["oidc"]
    if not cfg.get("issuer"):
        return HTMLResponse('<div class="banner bad">Set the issuer or tenant first.</div>')
    url = cfg["issuer"].rstrip("/") + "/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            doc = json.load(resp)
        ok = bool(doc.get("authorization_endpoint"))
        return HTMLResponse(
            f'<div class="banner {"ok" if ok else "warn"}"><span>{"✓" if ok else "⚠"}</span>'
            f'<div>Discovery document reached at {url}.<div class="small muted">'
            f'authorization endpoint: {doc.get("authorization_endpoint", "missing")}</div></div></div>')
    except Exception as exc:                               # noqa: BLE001
        return HTMLResponse(f'<div class="banner bad"><span>⚠</span><div>Could not read {url}: {exc}</div></div>')


@router.post("/setup/alerts", response_class=HTMLResponse)
async def save_alerts(request: Request, enabled: str = Form(""), smtp_host: str = Form(""),
                      smtp_port: int = Form(25), smtp_user: str = Form(""),
                      smtp_password: str = Form(""), smtp_tls: str = Form(""),
                      mail_from: str = Form(""), mail_to: str = Form(""),
                      webhook_url: str = Form("")):
    cfg = {
        "enabled": enabled == "on", "smtp_host": smtp_host.strip(), "smtp_port": int(smtp_port),
        "smtp_user": smtp_user.strip(), "smtp_password": smtp_password,
        "smtp_tls": smtp_tls == "on", "mail_from": mail_from.strip(),
        "mail_to": [m.strip() for m in mail_to.split(",") if m.strip()],
        "webhook_url": webhook_url.strip(),
        "events": ["replication_failure", "driver_unhealthy", "failover"],
    }
    store.update(lambda s: s["setup"].update({"alerts": cfg}))
    return await setup_page(request, notice="Alert settings saved.")


@router.post("/setup/reset", response_class=HTMLResponse)
async def reset_console(request: Request, scope: str = Form("inventory"),
                        confirm: str = Form("")):
    """Wipe what the console knows, so a new site starts from nothing.

    Nothing is uninstalled from any cluster or array; only this console's own records,
    stored kubeconfigs and repctl state are removed.
    """
    if confirm.strip().lower() != "reset":
        return HTMLResponse('<div class="banner bad"><span>⚠</span>'
                            "<div>Type reset in the box to confirm.</div></div>")
    removed = []
    if scope == "everything":
        for path in (KUBECONFIG_DIR, replication.REPCTL_HOME):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                removed.append(str(path))
        for path in (STATE_FILE, SECRET_FILE):
            if path.exists():
                path.unlink()                  # a new session key invalidates old cookies
                removed.append(str(path))
        ensure_dirs()
        inventory.invalidate()
        response = HTMLResponse(
            '<div class="banner ok"><span>✓</span><div>Everything was cleared, including the '
            'administrator password. Reload the page to set up the console from scratch.</div></div>')
        security.clear_session_cookie(response)
        return response

    def wipe(state: dict) -> None:
        state["clusters"] = {}
        state["arrays"] = {}
        state["installs"] = {}
        state["jobs"] = []

    store.update(wipe)
    for path in (KUBECONFIG_DIR, replication.REPCTL_HOME):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
    ensure_dirs()
    inventory.invalidate()
    return HTMLResponse(
        '<div class="banner ok"><span>✓</span><div>Clusters, arrays and stored kubeconfigs were '
        'removed. Your password and settings were kept. Nothing was uninstalled from any cluster.'
        "</div></div>")


@router.post("/setup/alerts/test", response_class=HTMLResponse)
async def test_alert(request: Request):
    cfg = store.load()["setup"]["alerts"]
    results = []
    if cfg.get("smtp_host") and cfg.get("mail_to"):
        try:
            msg = EmailMessage()
            msg["Subject"] = "OCP Storage Manager test alert"
            msg["From"] = cfg.get("mail_from") or "osm@localhost"
            msg["To"] = ", ".join(cfg["mail_to"])
            msg.set_content("This is a test alert from the OSM console.")
            with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 25)), timeout=15) as smtp:
                if cfg.get("smtp_tls"):
                    smtp.starttls()
                if cfg.get("smtp_user"):
                    smtp.login(cfg["smtp_user"], cfg.get("smtp_password", ""))
                smtp.send_message(msg)
            results.append(("ok", f"Mail sent to {', '.join(cfg['mail_to'])}."))
        except Exception as exc:                            # noqa: BLE001
            results.append(("bad", f"SMTP failed: {exc}"))
    if cfg.get("webhook_url"):
        try:
            payload = json.dumps({"text": "OCP Storage Manager test alert"}).encode()
            req = urllib.request.Request(cfg["webhook_url"], data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                results.append(("ok", f"Webhook answered HTTP {resp.status}."))
        except Exception as exc:                            # noqa: BLE001
            results.append(("bad", f"Webhook failed: {exc}"))
    if not results:
        results.append(("warn", "Nothing to test: set an SMTP server or a webhook first."))
    html = "".join(f'<div class="banner {lvl}"><span>{"✓" if lvl == "ok" else "⚠"}</span>'
                   f"<div>{msg}</div></div>" for lvl, msg in results)
    return HTMLResponse(html)
