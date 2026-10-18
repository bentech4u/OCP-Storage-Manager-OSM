"""Keep a copy of every manifest the console applies.

Operations here are carried out by applying YAML to a cluster. Writing that YAML down
as it goes gives three things the job log alone does not: a record of exactly what was
applied and when, something to hand to a change request, and a starting point for
putting the same objects under version control.

Secret values are replaced with a placeholder in the saved copy. The files sit beside
the state in a private directory, and nothing is gained by holding a second copy of an
array password; the shape of the object is what is worth keeping. A manifest with real
values can still be produced on demand from the Install page.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..config import DATA_DIR, match_owner

DIR = DATA_DIR / "manifests"
KEEP = 400                                   # oldest are removed beyond this
REDACTED = "<kept in the console, not written here>"


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "object").lower()).strip("-")
    return cleaned[:48] or "object"


def redact(text: str) -> tuple[str, bool]:
    """Blank the data of a Secret, leaving everything else as it was applied."""
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return text, False
    touched = False
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "Secret":
            continue
        for field in ("data", "stringData"):
            if isinstance(doc.get(field), dict):
                doc[field] = {k: REDACTED for k in doc[field]}
                touched = True
    if not touched:
        return text, False
    return yaml.safe_dump_all([d for d in docs if d is not None], sort_keys=False), True


def save(text: str, what: str, job: str = "", cluster: str = "") -> Path | None:
    """Write one manifest down. Never raises: a record is not worth a failed install."""
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(DIR, 0o700)
        except PermissionError:
            pass
        match_owner(DIR)
        body, hidden = redact(text)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{_slug(cluster) + '-' if cluster else ''}{_slug(what)}.yaml"
        header = [f"# {what}", f"# applied by OCP Storage Manager at {stamp} UTC"]
        if cluster:
            header.append(f"# cluster: {cluster}")
        if job:
            header.append(f"# job: {job}")
        if hidden:
            header.append("# secret values are not written to this file")
        path = DIR / name
        path.write_text("\n".join(header) + "\n---\n" + body.lstrip())
        try:
            os.chmod(path, 0o600)
        except PermissionError:
            pass
        match_owner(path)
        _prune()
        return path
    except OSError:
        return None


def _prune() -> None:
    files = sorted(DIR.glob("*.yaml"), reverse=True)
    for old in files[KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def recent(limit: int = 100, cluster: str = "") -> list[dict]:
    if not DIR.exists():
        return []
    out = []
    for path in sorted(DIR.glob("*.yaml"), reverse=True):
        try:
            head = path.read_text().splitlines()
        except OSError:
            continue
        what = head[0][2:] if head and head[0].startswith("# ") else path.stem
        where = next((l[11:] for l in head[:5] if l.startswith("# cluster: ")), "")
        if cluster and where != cluster:
            continue
        out.append({"name": path.name, "what": what, "cluster": where,
                    "when": path.name[:15].replace("-", " at ", 1),
                    "size": path.stat().st_size})
        if len(out) >= limit:
            break
    return out


def read(name: str) -> str:
    """One file by name. The name is a file in this directory and nothing else."""
    safe = Path(name).name
    path = DIR / safe
    if not path.exists() or path.parent != DIR:
        raise FileNotFoundError(name)
    return path.read_text()
