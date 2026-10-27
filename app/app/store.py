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


PREVIOUS_FILE = STATE_FILE.with_suffix(".prev.json")


def _read() -> dict:
    """Read the state, and never answer 'empty' for a file that is merely damaged.

    A host that loses power mid-write leaves a truncated file. Returning defaults for
    that and then saving over it loses every cluster, array and setting, and logs the
    operator out of an installation that was working the day before. So: keep the last
    good copy, fall back to it, and put the damaged one aside instead of over it.
    """
    if not STATE_FILE.exists():
        if PREVIOUS_FILE.exists():
            try:
                recovered = json.loads(PREVIOUS_FILE.read_text())
                save(recovered)
                return _merge(recovered)
            except (json.JSONDecodeError, OSError):
                pass
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        data = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        damaged = STATE_FILE.with_name(f"state.unreadable-{stamp}.json")
        try:
            STATE_FILE.replace(damaged)
        except OSError:
            damaged = STATE_FILE
        if PREVIOUS_FILE.exists():
            try:
                data = json.loads(PREVIOUS_FILE.read_text())
                print(f"state.json was unreadable ({exc}); kept it as {damaged.name} and "
                      "carried on with the previous copy", flush=True)
                save(data)
                return _merge(data)
            except (json.JSONDecodeError, OSError):
                pass
        print(f"state.json was unreadable ({exc}) and there is no previous copy; it is "
              f"kept as {damaged.name} and this installation starts empty", flush=True)
        return json.loads(json.dumps(DEFAULT_STATE))
    return _merge(data)


def _merge(data: dict) -> dict:
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
        body = json.dumps(state, indent=2)
        with open(tmp, "w") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())        # on the disk, not just in the page cache
        os.chmod(tmp, 0o600)
        match_owner(tmp)
        tmp.replace(STATE_FILE)
        try:
            directory = os.open(str(STATE_FILE.parent), os.O_RDONLY)
            os.fsync(directory)
            os.close(directory)
        except OSError:
            pass
        # the spare copy follows a write that completed, so a torn file later falls
        # back to the last state that was actually good rather than the one before it
        try:
            with open(PREVIOUS_FILE, "w") as spare:
                spare.write(body)
                spare.flush()
                os.fsync(spare.fileno())
            os.chmod(PREVIOUS_FILE, 0o600)
            match_owner(PREVIOUS_FILE)
        except OSError:
            pass


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
