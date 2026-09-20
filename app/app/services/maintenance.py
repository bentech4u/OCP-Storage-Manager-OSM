"""Console housekeeping: health checks, backups and locked-out recovery."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .. import security, store
from ..config import (BIN_DIR, DATA_DIR, HELM_CHART_DIR, KUBECONFIG_DIR, LOG_DIR, ROOT,
                      SECRET_FILE, STATE_FILE, ensure_dirs)
from . import replication
from .arrays import ArrayError, OneFS
from .k8s import ClusterError, probe
from .tools import status as tool_status

SERVICE = "ocpstorage-console"
BACKUP_MEMBERS = ["data/state.json", "data/kubeconfigs", ".repctl", "secrets"]


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# --- password and sessions -----------------------------------------------------
def set_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("use at least 8 characters")
    store.update(lambda s: s["setup"].update(
        {"admin_password_hash": security.hash_password(password)}))


def clear_password() -> None:
    """Drop the password so the next visit runs first-time setup again."""
    store.update(lambda s: s["setup"].update({"admin_password_hash": None}))


def revoke_sessions() -> str:
    """Rotate the signing key, which signs everyone out immediately."""
    ensure_dirs()
    if SECRET_FILE.exists():
        SECRET_FILE.unlink()
    return str(SECRET_FILE)


# --- service -------------------------------------------------------------------
def service(action: str) -> tuple[int, str]:
    if action not in ("start", "stop", "restart", "status", "is-active"):
        raise ValueError(f"unknown action {action}")
    proc = subprocess.run(["systemctl", action, SERVICE], capture_output=True, text=True)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def service_logs(lines: int = 50, follow: bool = False) -> int:
    cmd = ["journalctl", "-u", SERVICE, "-n", str(lines)]
    if follow:
        cmd.append("-f")
    return subprocess.run(cmd).returncode


def listening(port: int = 8800) -> bool:
    with socket.socket() as sock:
        sock.settimeout(3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# --- doctor --------------------------------------------------------------------
def doctor() -> list[dict]:
    """Everything that has bitten us at least once, checked in one pass."""
    checks: list[dict] = []

    def add(area: str, what: str, ok: bool, detail: str = "", fix: str = "") -> None:
        checks.append({"area": area, "what": what, "ok": ok, "detail": detail, "fix": fix})

    for name, tool in tool_status().items():
        add("tools", name, tool["found"], tool["version"] or tool["path"] or "not installed",
            "osmctl tools install-repctl" if name == "repctl" else f"install {name} on this host")

    for path, label in ((ROOT, "root"), (DATA_DIR, "data"), (KUBECONFIG_DIR, "kubeconfigs"),
                        (LOG_DIR, "logs"), (BIN_DIR, "tools"), (HELM_CHART_DIR, "charts")):
        exists = path.exists()
        writable = exists and os.access(path, os.W_OK)
        add("paths", label, exists and (writable or label == "charts"),
            f"{path}{'' if exists else ' missing'}{'' if writable or not exists else ' not writable'}",
            f"chown -R ocpstorage:ocpstorage {ROOT}")

    for path in (DATA_DIR, KUBECONFIG_DIR, replication.REPCTL_HOME):
        if path.exists():
            mode = oct(path.stat().st_mode & 0o777)
            add("permissions", f"{path.name} is private", mode in ("0o700", "0o750"), mode,
                f"chmod 700 {path}")

    # Running osmctl as root used to leave files the service user could not read.
    uid = DATA_DIR.stat().st_uid if DATA_DIR.exists() else os.getuid()
    stray = [str(f) for f in DATA_DIR.rglob("*") if f.is_file() and f.stat().st_uid != uid]
    add("permissions", "everything in data owned by the service user", not stray,
        f"{len(stray)} file(s) with another owner" if stray else "consistent",
        f"chown -R {uid}:{uid} {DATA_DIR}")

    state = store.load()
    add("console", "administrator password set",
        bool(state["setup"].get("admin_password_hash")), "",
        "osmctl admin passwd")
    add("console", "service running", service("is-active")[1] == "active",
        service("is-active")[1], f"systemctl start {SERVICE}")
    add("console", "answering on 8800", listening(), "", f"journalctl -u {SERVICE}")

    for cluster in state["clusters"].values():
        kubeconfig = Path(cluster.get("kubeconfig", ""))
        add("clusters", f"{cluster['id']} kubeconfig present", kubeconfig.exists(),
            str(kubeconfig), "re-add the cluster or export the service account again")
        if kubeconfig.exists():
            try:
                info = probe(kubeconfig)
                add("clusters", f"{cluster['id']} reachable", True,
                    f"{info.get('user','')} on {info.get('server','')}")
                add("clusters", f"{cluster['id']} cluster-admin", info.get("cluster_admin", False),
                    "", "repctl needs it to install custom resource definitions")
            except ClusterError as exc:
                add("clusters", f"{cluster['id']} reachable", False, str(exc)[:120])

    for array in state["arrays"].values():
        try:
            client = OneFS(array["endpoint"], int(array.get("port", 8080)),
                           array["username"], array["password"])
            info = client.inspect()
            add("arrays", f"{array['name']} reachable", True,
                f"OneFS {info.get('onefs_version','')}")
            add("arrays", f"{array['name']} privileges", not info.get("missing_privileges"),
                ", ".join(info.get("missing_privileges", [])) or "complete")
            licences = info.get("licenses", {})
            add("arrays", f"{array['name']} licences",
                all(v in ("Licensed", "Evaluation", "Activated") for v in licences.values()),
                ", ".join(f"{k.lower()} {v}" for k, v in licences.items()))
        except (ArrayError, Exception) as exc:                       # noqa: BLE001
            add("arrays", f"{array['name']} reachable", False, str(exc)[:120])

    listing = replication.list_clusters()
    known = {c["id"] for c in state["clusters"].values()}
    registered = {r["id"] for r in listing["rows"]}
    add("repctl", "store readable", not listing["error"],
        listing.get("error") or listing["path"])
    add("repctl", "every cluster registered", known <= registered and bool(known),
        f"registered: {', '.join(sorted(registered)) or 'none'}", "osmctl repctl register")
    return checks


# --- backup and restore ---------------------------------------------------------
def backup(target: Path | None = None) -> Path:
    """Tar up the state that cannot be recreated: records, kubeconfigs, certificates."""
    target = Path(target) if target else ROOT / f"backup-{now_stamp()}.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "w:gz") as tar:
        for member in BACKUP_MEMBERS:
            path = ROOT / member
            if path.exists():
                tar.add(path, arcname=member)
    os.chmod(target, 0o600)
    return target


def backup_contents(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getnames()[:40]


def restore(archive: Path, force: bool = False) -> list[str]:
    archive = Path(archive)
    if not archive.exists():
        raise FileNotFoundError(str(archive))
    if STATE_FILE.exists() and not force:
        raise RuntimeError("this console already has state; pass force to overwrite it")
    restored = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.split("/")[0] not in {m.split("/")[0] for m in BACKUP_MEMBERS}:
                continue                                  # ignore anything unexpected
            tar.extract(member, path=ROOT, filter="data")
            restored.append(member.name)
    ensure_dirs()
    return restored


# --- jobs ----------------------------------------------------------------------
def job_logs(limit: int = 20) -> list[dict]:
    if not LOG_DIR.exists():
        return []
    files = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for f in files:
        parts = f.stem.split("-")
        out.append({"file": f.name, "kind": parts[3] if len(parts) > 3 else "",
                    "when": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "size": f.stat().st_size, "path": str(f)})
    return out
