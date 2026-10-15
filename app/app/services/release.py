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

MANIFEST = "osm-bundle.json"
VERSION_FILE = Path("app") / "VERSION"
BACKUP_DIR = DATA_DIR / "backups"

# Never written by an update, whatever a bundle claims to contain.
PROTECTED = ("data", "secrets", "bin", ".repctl", "app/.venv", "app/tmp")


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


def differences(bundle: Path) -> dict:
    """Which files the bundle would add, change or leave alone."""
    manifest = inspect(bundle)
    added, changed, same = [], [], []
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
        (same if current == digest else changed).append(name)
    return {"manifest": manifest, "added": sorted(added), "changed": sorted(changed),
            "same": sorted(same)}


# --- applying ----------------------------------------------------------------------
def apply(bundle: Path, restart: bool = True, health_timeout: int = 60) -> dict:
    """Verify, back up, write, restart, and undo the lot if it does not come back."""
    bundle = Path(bundle)
    if not bundle.exists():
        raise ReleaseError(f"{bundle} does not exist")
    plan = differences(bundle)
    manifest = plan["manifest"]

    with tarfile.open(bundle, "r:gz") as tar:
        payloads: dict[str, bytes] = {}
        for name, digest in manifest["files"].items():
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

    before = (ROOT / "app" / "requirements.txt")
    requirements_before = _sha(before.read_bytes()) if before.exists() else ""

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
              "unchanged": len(plan["same"]), "backup": str(backup), "restarted": False,
              "healthy": None, "rolled_back": False,
              "requirements_changed": bool(requirements_before)
              and requirements_before != manifest.get("requirements", "")}

    if restart:
        from . import maintenance
        maintenance.service("restart")
        result["restarted"] = True
        result["healthy"] = _healthy(health_timeout)
        if not result["healthy"]:
            restore(backup)
            maintenance.service("restart")
            result["rolled_back"] = True
            result["healthy_after_rollback"] = _healthy(health_timeout)
    return result


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


def backups(limit: int = 10) -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("code-*.tar.gz"), reverse=True)[:limit]


def _healthy(timeout: int) -> bool:
    """Ask the console itself, on whatever address the unit starts it with.

    Only a 200 from /healthz counts: a process that bound the port and then failed
    to serve is exactly the state this is meant to catch.
    """
    import ssl
    from . import maintenance
    port, tls = maintenance.console_endpoint()
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
