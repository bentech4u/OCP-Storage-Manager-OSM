"""Detect and install the command line tools the UI drives."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import urllib.request
from pathlib import Path

from ..config import (BIN_DIR, DEFAULT_REPCTL_VERSION, REPCTL_ASSET, REPCTL_REPO,
                      ensure_dirs, tool_env)
from .jobs import Job

TOOLS = {
    "oc": ["version", "--client=true", "-o", "json"],
    "kubectl": ["version", "--client=true", "-o", "json"],
    "helm": ["version", "--short"],
    "repctl": ["--help"],
}


def which(name: str) -> str | None:
    return shutil.which(name, path=tool_env()["PATH"])


def _version_of(name: str, path: str) -> str:
    try:
        out = subprocess.run([path] + TOOLS[name], capture_output=True, text=True,
                             timeout=20, env=tool_env())
        text = (out.stdout or out.stderr).strip()
    except Exception:                                   # noqa: BLE001
        return "unknown"
    if name in ("oc", "kubectl"):
        try:
            data = json.loads(text)
            return data.get("clientVersion", {}).get("gitVersion", "unknown")
        except json.JSONDecodeError:
            return text.splitlines()[0][:80] if text else "unknown"
    if name == "repctl":
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=20, env=tool_env())
            v = (out.stdout or out.stderr).strip().splitlines()
            if v and "unknown" not in v[0].lower():
                return v[0][:80]
        except Exception:                               # noqa: BLE001
            pass
        return "installed"
    return text.splitlines()[0][:80] if text else "unknown"


def status() -> dict:
    """What is available on the host right now."""
    result = {}
    for name in TOOLS:
        path = which(name)
        result[name] = {
            "name": name,
            "found": bool(path),
            "path": path or "",
            "version": _version_of(name, path) if path else "",
            "managed": bool(path) and str(BIN_DIR) in (path or ""),
        }
    return result


def internet() -> bool:
    try:
        req = urllib.request.Request("https://api.github.com/rate_limit", method="GET")
        with urllib.request.urlopen(req, timeout=8):
            return True
    except Exception:                                   # noqa: BLE001
        return False


def repctl_releases(limit: int = 15) -> list[dict]:
    """Releases of dell/csm-replication, marking which ship a prebuilt linux binary."""
    url = f"https://api.github.com/repos/{REPCTL_REPO}/releases?per_page={limit}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
    except Exception as exc:                            # noqa: BLE001
        raise RuntimeError(f"cannot reach GitHub: {exc}") from exc
    out = []
    for rel in data:
        asset = next((a for a in rel.get("assets", [])
                      if a["name"].lower() in (REPCTL_ASSET, "repctl")), None)
        out.append({
            "tag": rel.get("tag_name", ""),
            "published": (rel.get("published_at") or "")[:10],
            "has_binary": bool(asset),
            "url": asset["browser_download_url"] if asset else "",
            "size": asset["size"] if asset else 0,
        })
    return out


SYSTEM_BIN = Path("/usr/local/bin")


def _link_into_path(target: Path, name: str = "repctl") -> str:
    """Also expose the tool on the system path, so a plain shell can run it."""
    link = SYSTEM_BIN / name
    try:
        SYSTEM_BIN.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            if link.is_symlink() and link.resolve() == target.resolve():
                return str(link)
            if not link.is_symlink():          # never replace a real binary
                return ""
            link.unlink()
        link.symlink_to(target)
        return str(link)
    except OSError:
        return ""


def _install_binary(src_bytes: bytes, name: str = "repctl") -> Path:
    ensure_dirs()
    target = BIN_DIR / name
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(src_bytes)
    tmp.chmod(tmp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    tmp.replace(target)
    return target


def install_repctl_from_url(job: Job, url: str) -> Path:
    job.log(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "osm"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = resp.read()
    job.log(f"downloaded {len(payload)} bytes")
    path = _install_binary(payload)
    job.log(f"installed {path}")
    _announce_link(job, path)
    _verify(job, path)
    return path


def install_repctl_from_upload(job: Job, payload: bytes, filename: str) -> Path:
    job.log(f"installing uploaded file {filename} ({len(payload)} bytes)")
    path = _install_binary(payload)
    job.log(f"installed {path}")
    _announce_link(job, path)
    _verify(job, path)
    return path


def _announce_link(job: Job, path: Path) -> None:
    link = _link_into_path(path)
    if link:
        job.log(f"linked {link} so you can run repctl from any shell")
    else:
        job.log(f"could not link into {SYSTEM_BIN}; run it as {path} or add that directory to PATH")


def _verify(job: Job, path: Path) -> None:
    head = path.read_bytes()[:4]
    if head[:4] != b"\x7fELF":
        raise RuntimeError("that file is not a Linux binary (no ELF header)")
    rc = job.run([str(path), "--help"])
    if rc != 0:
        raise RuntimeError(f"repctl --help exited {rc}; wrong architecture or build?")
    job.log("repctl runs on this host")


def build_repctl_from_source(job: Job, version: str) -> Path:
    """Fallback for tags with no published binary: build in a golang container."""
    engine = which("podman") or which("docker")
    if not engine:
        raise RuntimeError("no podman or docker to build with, and no prebuilt binary for "
                           f"{version}. Upload a binary instead.")
    out_dir = BIN_DIR / "build"
    out_dir.mkdir(parents=True, exist_ok=True)
    script = (
        "set -e; git clone --depth 1 -b %s https://github.com/%s /src; "
        "cd /src/repctl; CGO_ENABLED=0 go build -o /out/repctl .; ls -l /out/repctl"
        % (version, REPCTL_REPO)
    )
    rc = job.run([engine, "run", "--rm", "-v", f"{out_dir}:/out:Z",
                  "docker.io/library/golang:1.25", "sh", "-c", script])
    if rc != 0:
        raise RuntimeError(f"build failed with exit code {rc}")
    built = out_dir / "repctl"
    path = _install_binary(built.read_bytes())
    job.log(f"installed {path}")
    _announce_link(job, path)
    _verify(job, path)
    return path


def default_repctl_choice() -> dict:
    """What the install page should preselect."""
    return {"version": DEFAULT_REPCTL_VERSION,
            "url": f"https://github.com/{REPCTL_REPO}/releases/download/"
                   f"{DEFAULT_REPCTL_VERSION}/{REPCTL_ASSET}"}
