"""An append-only record of who did what.

Each entry is one line of JSON in a monthly file, and carries the hash of the entry
before it. Changing or removing a line breaks the chain from that point on, which
`verify` reports, so the record is tamper-evident without needing a database.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config import DATA_DIR, ensure_dirs, match_owner

AUDIT_DIR = DATA_DIR / "audit"
GENESIS = "0" * 64
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_for(stamp: str) -> Path:
    return AUDIT_DIR / f"audit-{stamp[:7]}.jsonl"          # one file per month


def _files() -> list[Path]:
    if not AUDIT_DIR.exists():
        return []
    return sorted(AUDIT_DIR.glob("audit-*.jsonl"))


def _digest(entry: dict) -> str:
    body = {k: entry[k] for k in sorted(entry) if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


def _last_hash() -> str:
    files = _files()
    if not files:
        return GENESIS
    last = None
    with files[-1].open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if not last:
        return GENESIS
    try:
        return json.loads(last).get("hash", GENESIS)
    except json.JSONDecodeError:
        return GENESIS


def record(action: str, actor: str = "", via: str = "", target: str = "",
           outcome: str = "", detail: str = "", job: str = "", **extra) -> dict:
    """Append one entry. Never raises: an audit failure must not break the action."""
    try:
        ensure_dirs()
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(AUDIT_DIR, 0o700)
        except PermissionError:
            pass
        match_owner(AUDIT_DIR)
        stamp = _now()
        with _lock:
            entry = {
                "at": stamp, "action": action, "actor": actor or "system", "via": via or "",
                "target": target, "outcome": outcome, "detail": detail[:500], "job": job,
                "prev": _last_hash(),
            }
            if extra:
                entry["extra"] = {k: str(v)[:200] for k, v in extra.items()}
            entry["hash"] = _digest(entry)
            path = _file_for(stamp)
            new_file = not path.exists()
            with path.open("a") as fh:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
            if new_file:
                try:
                    os.chmod(path, 0o600)
                except PermissionError:
                    pass
                match_owner(path)
        return entry
    except Exception:                                      # noqa: BLE001
        return {}


def entries(limit: int = 200, actor: str = "", action: str = "", target: str = "",
            since: str = "", until: str = "", text: str = "") -> list[dict]:
    """Most recent first, filtered."""
    found: list[dict] = []
    for path in reversed(_files()):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if actor and actor.lower() not in entry.get("actor", "").lower():
                continue
            if action and action != entry.get("action"):
                continue
            if target and target.lower() not in entry.get("target", "").lower():
                continue
            if since and entry.get("at", "") < since:
                continue
            if until and entry.get("at", "") > until:
                continue
            if text:
                blob = json.dumps(entry).lower()
                if text.lower() not in blob:
                    continue
            found.append(entry)
            if len(found) >= limit:
                return found
    return found


def actions() -> list[str]:
    """Every action name seen, for the filter list."""
    names = set()
    for path in _files():
        try:
            for line in path.read_text().splitlines():
                if line.strip():
                    try:
                        names.add(json.loads(line).get("action", ""))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            continue
    return sorted(n for n in names if n)


def verify() -> dict:
    """Walk the whole chain and report the first entry that does not fit."""
    previous = GENESIS
    checked = 0
    for path in _files():
        try:
            lines = path.read_text().splitlines()
        except OSError as exc:
            return {"ok": False, "checked": checked, "problem": f"cannot read {path}: {exc}"}
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return {"ok": False, "checked": checked,
                        "problem": f"{path.name} line {number} is not readable"}
            if entry.get("prev") != previous:
                return {"ok": False, "checked": checked,
                        "problem": f"{path.name} line {number} does not follow the entry before "
                                   "it, so something was removed or reordered"}
            if entry.get("hash") != _digest(entry):
                return {"ok": False, "checked": checked,
                        "problem": f"{path.name} line {number} was altered after it was written"}
            previous = entry["hash"]
            checked += 1
    return {"ok": True, "checked": checked, "problem": "",
            "files": [p.name for p in _files()]}


def to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["at", "actor", "via", "action", "target", "outcome", "detail", "job", "hash"])
    for e in rows:
        writer.writerow([e.get("at", ""), e.get("actor", ""), e.get("via", ""),
                         e.get("action", ""), e.get("target", ""), e.get("outcome", ""),
                         e.get("detail", ""), e.get("job", ""), e.get("hash", "")[:16]])
    return out.getvalue()
