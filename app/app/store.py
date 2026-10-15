"""Tiny JSON-file state store, good enough for a single-node installer UI."""
from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from .config import DATA_DIR, STATE_FILE, ensure_dirs, match_owner

_lock = threading.RLock()
LOCK_FILE = DATA_DIR / "state.lock"
_depth = 0


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
            "check_seconds": 120,
            "renotify_hours": 6,
        },
    },
    "clusters": {},      # id -> kubernetes cluster record
    "arrays": {},        # id -> PowerScale array record
    "installs": {},      # cluster_id -> what we installed there
    "jobs": [],          # recent job summaries
}


@contextmanager
def _file_lock():
    """Hold the state across processes, not just across threads.

    osmctl runs in its own process and writes the same file as the console and the
    watcher. Without this, two read-modify-write cycles overlap and the later save
    silently drops the earlier change: a password set from the shell, or an array
    added in the browser, simply disappears.

    update() calls save() while already holding it, so it counts depth: a second
    flock handle in the same process would wait on the first one forever. Callers
    always hold the thread lock, which is what makes the counter safe.
    """
    global _depth
    if _depth:
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
        return
    ensure_dirs()
    handle = None
    try:
        handle = open(LOCK_FILE, "a+")
        match_owner(LOCK_FILE)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        handle = None                     # a read-only data dir must not stop the console
    _depth = 1
    try:
        yield
    finally:
        _depth = 0
        if handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


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
    with _lock, _file_lock():
        return _read()


def save(state: dict) -> None:
    ensure_dirs()
    with _lock, _file_lock():
        # a name of this process's own: two writers sharing one temp file delete it
        # from under each other if the lock ever fails to apply
        tmp = STATE_FILE.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state, indent=2))
        os.chmod(tmp, 0o600)
        match_owner(tmp)
        tmp.replace(STATE_FILE)


def update(fn) -> dict:
    """Read-modify-write under the lock. fn receives and mutates the state."""
    with _lock, _file_lock():
        state = _read()
        fn(state)
        save(state)
        return state


def redact(value: str | None, keep: int = 0) -> str:
    if not value:
        return ""
    return "*" * 8 + (value[-keep:] if keep else "")
