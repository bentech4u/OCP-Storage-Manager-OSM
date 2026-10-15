"""Move a version of the console to a site with no internet.

The installer at the office cannot reach GitHub, so updates arrive as a file. One
command builds that file where the repository is, another applies it there:

    osmctl update pack                      # on the machine that can pull
    osmctl update apply osm-<version>.tar.gz    # on the installer

The bundle holds only what git tracks: the application, the charts, the values and
the unit file. Nothing under data, secrets, bin or .repctl is in it, so applying one
never touches the clusters, the arrays, the kubeconfigs or repctl's store.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from ..config import DATA_DIR, ROOT, inherit_owner, match_owner

SERVICE = os.environ.get("OSM_SERVICE", "ocpstorage-console")
MANIFEST = "osm-bundle.json"
VERSION_FILE = Path("app") / "VERSION"
BACKUP_DIR = DATA_DIR / "backups"
KEEP_BACKUPS = 10          # older ones are removed as new updates are applied

# Never written by an update, whatever a bundle claims to contain: the state, the
# credentials, the tools, repctl's own store and the virtual environment.
PROTECTED = ("data", "secrets", "bin", ".repctl", "app/.venv", "app/tmp")

# Tracked, but belonging to the site once it exists: an update offers a new copy and
# keeps the local one, because this is where an installation is tuned.
SITE_OWNED = ("values",)


class ReleaseError(RuntimeError):
    pass


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _git(args: list[str], cwd: Path = ROOT) -> str:
    proc = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True,
                          timeout=120)
    if proc.returncode != 0:
        raise ReleaseError((proc.stderr or proc.stdout).strip()[:300])
    return proc.stdout


def _safe_member(name: str) -> bool:
    """A bundle may only write inside the tree, and never over live state."""
    if name.startswith("/") or ".." in Path(name).parts:
        return False
    return not any(name == p or name.startswith(p + "/") for p in PROTECTED)


# --- building -------------------------------------------------------------------
def pack(out_dir: Path | None = None, ref: str = "HEAD") -> Path:
    """Build a bundle from what git tracks at `ref`. Runs where the repository is."""
    if not (ROOT / ".git").exists():
        raise ReleaseError(f"{ROOT} is not a git repository, so there is nothing to pack")
    commit = _git(["rev-parse", "--short", ref]).strip()
    described = _git(["describe", "--tags", "--always", ref]).strip()
    names = [n for n in _git(["ls-tree", "-r", "--name-only", ref]).splitlines() if n.strip()]
    files: dict[str, str] = {}
    blobs: dict[str, bytes] = {}
    for name in names:
        if not _safe_member(name):
            continue
        blob = subprocess.run(["git", "show", f"{ref}:{name}"], cwd=str(ROOT),
                              capture_output=True, timeout=120)
        if blob.returncode != 0:
            raise ReleaseError(f"cannot read {name} at {ref}")
        blobs[name] = blob.stdout
        files[name] = _sha(blob.stdout)

    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest = {
        "version": described, "commit": commit, "created": created,
        "files": files, "count": len(files),
        "requirements": files.get("app/requirements.txt", ""),
    }
    body = json.dumps(manifest, indent=2).encode()
    stamp = json.dumps({"version": described, "commit": commit, "created": created},
                       indent=2).encode()

    out_dir = Path(out_dir or ROOT / "dist")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"osm-{described}-{time.strftime('%Y%m%d')}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, payload in blobs.items():
            _add(tar, name, payload)
        _add(tar, str(VERSION_FILE), stamp)
        _add(tar, MANIFEST, body)
    return path


def _add(tar: tarfile.TarFile, name: str, payload: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o755 if name in ("app/osmctl", "app/run.sh") else mode
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(payload))


# --- reading ---------------------------------------------------------------------
def inspect(bundle: Path) -> dict:
    """The manifest, without unpacking anything."""
    with tarfile.open(bundle, "r:gz") as tar:
        member = tar.extractfile(MANIFEST)
        if not member:
            raise ReleaseError(f"{bundle.name} is not an OSM bundle: no {MANIFEST} inside")
        return json.loads(member.read())


def installed() -> dict:
    """What this installation is running, if a bundle put it there."""
    path = ROOT / VERSION_FILE
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _site_owned(name: str) -> bool:
    return any(name == p or name.startswith(p + "/") for p in SITE_OWNED)


def differences(bundle: Path) -> dict:
    """Which files the bundle would add, change, keep or leave alone."""
    manifest = inspect(bundle)
    added, changed, same, kept = [], [], [], []
    for name, digest in manifest["files"].items():
        target = ROOT / name
        if not target.exists():
            added.append(name)
            continue
        try:
            current = _sha(target.read_bytes())
        except OSError:
            changed.append(name)
            continue
        if current == digest:
            same.append(name)
        elif _site_owned(name):
            kept.append(name)                  # this site tuned it; leave it alone
        else:
            changed.append(name)
    return {"manifest": manifest, "added": sorted(added), "changed": sorted(changed),
            "same": sorted(same), "kept": sorted(kept)}


# --- applying ----------------------------------------------------------------------
def apply(bundle: Path, restart: bool = True, health_timeout: int = 60) -> dict:
    """Verify, back up, write, restart, and undo the lot if it does not come back."""
    bundle = Path(bundle)
    if not bundle.exists():
        raise ReleaseError(f"{bundle} does not exist")
    plan = differences(bundle)
    manifest = plan["manifest"]

    skip = set(plan["kept"]) | set(plan["same"])
    with tarfile.open(bundle, "r:gz") as tar:
        payloads: dict[str, bytes] = {}
        for name, digest in manifest["files"].items():
            if name in skip:
                continue
            if not _safe_member(name):
                raise ReleaseError(f"the bundle wants to write {name}, which is not allowed")
            member = tar.extractfile(name)
            if member is None:
                raise ReleaseError(f"{name} is listed in the manifest but missing from the bundle")
            payload = member.read()
            if _sha(payload) != digest:
                raise ReleaseError(f"{name} does not match its checksum; the bundle is damaged")
            payloads[name] = payload
        stamp = tar.extractfile(str(VERSION_FILE))
        version_blob = stamp.read() if stamp else b"{}"

    if not set(manifest["files"]) - set(plan["same"]) - set(plan["kept"]):
        # already identical: record which bundle this tree matches and leave it alone
        with tarfile.open(bundle, "r:gz") as tar:
            stamp = tar.extractfile(str(VERSION_FILE))
            if stamp:
                _write(ROOT / VERSION_FILE, stamp.read())
        return {"version": manifest["version"], "commit": manifest["commit"], "written": 0,
                "added": 0, "changed": 0, "unchanged": len(plan["same"]),
                "kept": plan["kept"], "backup": "", "restarted": False, "healthy": None,
                "rolled_back": False, "integration": {}, "requirements_changed": False,
                "nothing_to_do": True}

    before = (ROOT / "app" / "requirements.txt")
    requirements_before = _sha(before.read_bytes()) if before.exists() else ""
    integration_before = integration_check()

    backup = _backup(list(payloads) + [str(VERSION_FILE)])
    written = 0
    try:
        for name, payload in payloads.items():
            _write(ROOT / name, payload)
            written += 1
        _write(ROOT / VERSION_FILE, version_blob)
    except OSError as exc:
        restore(backup)
        raise ReleaseError(f"writing {name} failed ({exc}); the previous version is back") from exc

    result = {"version": manifest["version"], "commit": manifest["commit"],
              "written": written, "added": len(plan["added"]), "changed": len(plan["changed"]),
              "unchanged": len(plan["same"]), "kept": plan["kept"],
              "backup": str(backup), "restarted": False,
              "healthy": None, "rolled_back": False, "integration": {},
              "requirements_changed": bool(requirements_before)
              and requirements_before != manifest.get("requirements", ""),
              "_before": integration_before}

    if restart:
        _restart()
        result["restarted"] = True
        result["healthy"] = _healthy(health_timeout)
        if not result["healthy"]:
            restore(backup)
            _restart()
            result["rolled_back"] = True
            result["healthy_after_rollback"] = _healthy(health_timeout)
            return result
    result["integration"] = integration_check(before=result.pop("_before", None))
    return result


PROBE = """
import json, sys
sys.path.insert(0, %r)
out = {"clusters": 0, "arrays": 0, "repctl": 0, "reachable_clusters": 0,
       "reachable_arrays": 0, "problems": []}
try:
    from app import store
    state = store.load()
    out["clusters"] = len(state["clusters"])
    out["arrays"] = len(state["arrays"])
except Exception as exc:
    out["problems"].append("the state file could not be read: %%s" %% exc)
    print(json.dumps(out)); raise SystemExit(0)
try:
    from app.services import replication
    out["repctl"] = len(replication.list_clusters().get("rows", []))
except Exception as exc:
    out["problems"].append("repctl: %%s" %% exc)
try:
    from app.services import inventory
    data = inventory.snapshot(force=True)
    out["reachable_clusters"] = sum(1 for c in data["clusters"] if c.get("reachable"))
    out["reachable_arrays"] = sum(1 for a in data["arrays"] if a.get("reachable"))
except Exception as exc:
    out["problems"].append("inventory: %%s" %% exc)
print(json.dumps(out))
"""


def integration_check(before: dict | None = None) -> dict:
    """Confirm the installed code still reads everything the old one was using.

    This runs in a new process on purpose. The updater has the previous version of
    every module in memory, so asking in here would report on code that is no longer
    on disk, and would miss a tree that cannot be imported at all.
    """
    import sys
    out = {"clusters": 0, "arrays": 0, "repctl": 0, "reachable_clusters": 0,
           "reachable_arrays": 0, "problems": []}
    try:
        proc = subprocess.run([sys.executable, "-c", PROBE % str(ROOT / "app")],
                              capture_output=True, text=True, timeout=180,
                              cwd=str(ROOT / "app"))
    except Exception as exc:                               # noqa: BLE001
        out["problems"].append(f"could not run the check: {exc}")
        return out
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        out["problems"].append("the installed code did not load: "
                               + (proc.stderr or proc.stdout).strip()[-300:])
        return out
    if before:
        for key in ("clusters", "arrays", "repctl"):
            if before.get(key) != out[key]:
                out["problems"].append(
                    f"{key}: {before.get(key)} before the update, {out[key]} after")
    return out


def _restart() -> None:
    subprocess.run(["systemctl", "restart", SERVICE], capture_output=True, text=True,
                   timeout=120)


def _endpoint() -> tuple[int, bool]:
    """Port and scheme straight from the unit, without importing the application."""
    try:
        proc = subprocess.run(["systemctl", "show", SERVICE, "-p", "Environment", "--value"],
                              capture_output=True, text=True, timeout=30)
        env = dict(item.split("=", 1) for item in proc.stdout.split() if "=" in item)
    except Exception:                                      # noqa: BLE001
        env = {}
    port = env.get("OSM_PORT") or os.environ.get("OSM_PORT") or "8800"
    tls = bool(env.get("OSM_TLS_CERT") or os.environ.get("OSM_TLS_CERT"))
    try:
        return int(port), tls
    except ValueError:
        return 8800, tls


def _write(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    inherit_owner(target.parent)
    tmp = target.with_name(target.name + f".new.{os.getpid()}")
    tmp.write_bytes(payload)
    if target.exists():
        shutil.copymode(target, tmp)
    elif target.name in ("osmctl", "run.sh"):
        tmp.chmod(0o755)
    tmp.replace(target)
    match_owner(target)
    inherit_owner(target)


def _backup(names: list[str]) -> Path:
    """Keep the current copy of everything this update would touch."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    inherit_owner(BACKUP_DIR)
    try:
        os.chmod(BACKUP_DIR, 0o700)
    except PermissionError:
        pass
    path = BACKUP_DIR / f"code-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name in names:
            target = ROOT / name
            if target.exists():
                tar.add(target, arcname=name)
    match_owner(path)
    for old_backup in backups(limit=None)[KEEP_BACKUPS:]:
        try:
            old_backup.unlink()
        except OSError:
            pass
    return path


def restore(backup: Path) -> int:
    """Put a backup taken by apply() back in place."""
    count = 0
    with tarfile.open(backup, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not _safe_member(member.name):
                continue
            payload = tar.extractfile(member)
            if payload is None:
                continue
            _write(ROOT / member.name, payload.read())
            count += 1
    return count


def backups(limit: int | None = 10) -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    found = sorted(BACKUP_DIR.glob("code-*.tar.gz"), reverse=True)
    return found if limit is None else found[:limit]


def _healthy(timeout: int) -> bool:
    """Ask the console itself, on whatever address the unit starts it with.

    Only a 200 from /healthz counts: a process that bound the port and then failed
    to serve is exactly the state this is meant to catch.
    """
    import ssl
    port, tls = _endpoint()
    scheme = "https" if tls else "http"
    url = f"{scheme}://127.0.0.1:{port}/healthz"
    context = None
    if tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5, context=context) as resp:
                if resp.status == 200:
                    return True
        except Exception:                                  # noqa: BLE001
            pass
        time.sleep(2)
    return False
