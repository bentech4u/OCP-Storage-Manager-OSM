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
from .arrays import ArrayError, client_for
from .k8s import ClusterError, probe
from .tools import status as tool_status

SERVICE = "ocpstorage-console"
BACKUP_MEMBERS = ["data/state.json", "data/kubeconfigs", "data/audit", ".repctl", "secrets"]


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
    proc = subprocess.run(["systemctl", action, SERVICE], capture_output=True, text=True,
                          timeout=120)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def service_logs(lines: int = 50, follow: bool = False) -> int:
    cmd = ["journalctl", "-u", SERVICE, "-n", str(lines)]
    if follow:
        cmd.append("-f")
    return subprocess.run(cmd).returncode


def service_environment() -> dict:
    """The environment systemd starts the console with, so checks use the real settings."""
    proc = subprocess.run(["systemctl", "show", SERVICE, "-p", "Environment", "--value"],
                          capture_output=True, text=True, timeout=30)
    found = {}
    for item in (proc.stdout or "").split():
        if "=" in item:
            key, _, value = item.partition("=")
            found[key] = value
    return found


def console_endpoint() -> tuple[int, bool]:
    """Port the console listens on, and whether it speaks https."""
    env = service_environment()
    port = env.get("OSM_PORT") or os.environ.get("OSM_PORT") or "8800"
    tls = bool(env.get("OSM_TLS_CERT") or os.environ.get("OSM_TLS_CERT"))
    try:
        return int(port), tls
    except ValueError:
        return 8800, tls


def listening(port: int | None = None, tls: bool | None = None) -> bool:
    """Ask the console for its health, over whichever port and scheme it is using."""
    if port is None or tls is None:
        found_port, found_tls = console_endpoint()
        port = found_port if port is None else port
        tls = found_tls if tls is None else tls
    import ssl as _ssl
    import urllib.error
    import urllib.request
    scheme = "https" if tls else "http"
    ctx = None
    if tls:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE          # the console may hold a self-signed pair
    try:
        with urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/healthz", timeout=5,
                                    context=ctx):
            return True
    except Exception:                              # noqa: BLE001
        pass
    with socket.socket() as sock:                  # a plain connection still tells us something
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
    stray = [f for f in DATA_DIR.rglob("*") if f.is_file() and f.stat().st_uid != uid]
    try:
        import pwd
        owner = pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        owner = str(uid)
    detail = "consistent"
    if stray:
        names = ", ".join(str(f.relative_to(DATA_DIR)) for f in stray[:3])
        detail = f"{len(stray)} file(s) not owned by {owner}: {names}"
    add("permissions", f"everything in data owned by {owner}", not stray, detail,
        f"chown -R {owner}:{owner} {DATA_DIR}")

    state = store.load()
    add("console", "administrator password set",
        bool(state["setup"].get("admin_password_hash")), "",
        "osmctl admin passwd")
    add("console", "service running", service("is-active")[1] == "active",
        service("is-active")[1], f"systemctl start {SERVICE}")
    port, tls = console_endpoint()
    add("console", f"answering on {port}", listening(port, tls),
        ("https" if tls else "http") + f"://127.0.0.1:{port}/healthz",
        f"journalctl -u {SERVICE}")

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
            client = client_for(array)
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

    from .tools import SYSTEM_BIN, WRAPPER_MARK
    shell_repctl = SYSTEM_BIN / "repctl"
    shared = False
    if shell_repctl.exists():
        try:
            shared = WRAPPER_MARK in shell_repctl.read_text()
        except OSError:
            shared = False
    add("repctl", "a shell sees the same clusters", shared,
        f"{shell_repctl} {'points at the console store' if shared else 'is missing or bypasses it'}",
        "re-run the repctl install, or: osmctl admin link-repctl")

    from . import audit as audit_service
    chain = audit_service.verify()
    add("audit", "the record is intact", chain["ok"],
        chain["problem"] or f"{chain['checked']} entries",
        "investigate: an entry was altered or removed")

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
