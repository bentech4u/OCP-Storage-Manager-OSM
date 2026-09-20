"""Tiny JSON-file state store, good enough for a single-node installer UI."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from .config import STATE_FILE, ensure_dirs, match_owner

_lock = threading.RLock()

DEFAULT_STATE: dict[str, Any] = {
    "version": 1,
    "setup": {
        "admin_password_hash": None,
        "admin_user": "admin",
        "oidc": {
            "enabled": False,
            "provider": "entra",
            "issuer": "",
            "client_id": "",
            "client_secret": "",
            "tenant_id": "",
            "redirect_url": "",
            "allowed_groups": [],
            "viewer_groups": [],
            "scope": "openid profile email",
            "method": "password",          # password form, or redirect to the provider
        },
        "local_login": "always",           # always | installer-host | off
        "alerts": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 25,
            "smtp_user": "",
            "smtp_password": "",
            "smtp_tls": True,
            "mail_from": "",
            "mail_to": [],
            "webhook_url": "",
            "events": ["replication_failure", "driver_unhealthy", "failover"],
        },
    },
    "clusters": {},      # id -> kubernetes cluster record
    "arrays": {},        # id -> PowerScale array record
    "installs": {},      # cluster_id -> what we installed there
    "jobs": [],          # recent job summaries
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read() -> dict:
    if not STATE_FILE.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        data = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT_STATE))
    merged = json.loads(json.dumps(DEFAULT_STATE))
    merged.update(data)
    for key in ("setup",):                       # keep new sub-keys when upgrading
        base = json.loads(json.dumps(DEFAULT_STATE[key]))
        base.update(data.get(key, {}))
        for sub in ("oidc", "alerts"):
            sub_base = json.loads(json.dumps(DEFAULT_STATE[key][sub]))
            sub_base.update(data.get(key, {}).get(sub, {}))
            base[sub] = sub_base
        merged[key] = base
    return merged


def load() -> dict:
    with _lock:
        return _read()


def save(state: dict) -> None:
    ensure_dirs()
    with _lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.chmod(tmp, 0o600)
        match_owner(tmp)
        tmp.replace(STATE_FILE)


def update(fn) -> dict:
    """Read-modify-write under the lock. fn receives and mutates the state."""
    with _lock:
        state = _read()
        fn(state)
        save(state)
        return state


def redact(value: str | None, keep: int = 0) -> str:
    if not value:
        return ""
    return "*" * 8 + (value[-keep:] if keep else "")
