"""Application configuration and paths."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

APP_NAME = "OCP Storage Manager"
APP_TAGLINE = "PowerScale CSI and replication for OpenShift"

BASE_DIR = Path(__file__).resolve().parent            # .../app/app
PROJECT_DIR = BASE_DIR.parent                         # .../app
# Everything the console needs lives under one root, so it can be owned by a service user.
ROOT = Path(os.environ.get("OSM_HOME", PROJECT_DIR.parent))

DATA_DIR = Path(os.environ.get("OSM_DATA", ROOT / "data"))
KUBECONFIG_DIR = DATA_DIR / "kubeconfigs"
BIN_DIR = ROOT / "bin"                                # helm, repctl and anything we install
LOG_DIR = DATA_DIR / "logs"
STATE_FILE = DATA_DIR / "state.json"
SECRET_FILE = DATA_DIR / "session.key"

# Tools are looked up here first, then on the host PATH.
EXTRA_PATH = [
    str(BIN_DIR),
    "/opt/ocpdeploy/bin/4.22.13",
    "/usr/local/bin",
    "/usr/bin",
]

REPCTL_REPO = "dell/csm-replication"
REPCTL_ASSET = "repctl-linux-amd64"
DEFAULT_REPCTL_VERSION = "v1.13.0"        # newest tag that ships a prebuilt binary

HELM_CHART_DIR = ROOT / "charts"
DRIVER_CHART = HELM_CHART_DIR / "csi-isilon"
REPLICATION_CHART = HELM_CHART_DIR / "csm-replication"
REPLICATION_CRDS = REPLICATION_CHART / "crds" / "replicationcrds.all.yaml"
DEFAULT_VALUES = ROOT / "values" / "my-isilon-settings.yaml"
SECRETS_DIR = ROOT / "secrets"

DRIVER_NAMESPACE_DEFAULT = "isilon"
REPLICATION_NAMESPACE = "dell-replication-controller"
OPERATOR_NAMESPACE = "dell-csm-operator"

SESSION_COOKIE = "osm_session"
SESSION_MAX_AGE = 8 * 60 * 60


def ensure_dirs() -> None:
    for d in (DATA_DIR, KUBECONFIG_DIR, BIN_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)


def session_secret() -> str:
    """Stable per-installation session key, generated on first start."""
    ensure_dirs()
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    key = secrets.token_urlsafe(48)
    SECRET_FILE.write_text(key)
    os.chmod(SECRET_FILE, 0o600)
    return key


def owner_of_data() -> tuple[int, int]:
    """Whoever owns the data directory owns everything the console writes."""
    try:
        info = DATA_DIR.stat()
        return info.st_uid, info.st_gid
    except OSError:
        return os.getuid(), os.getgid()


def match_owner(path) -> None:
    """Keep files readable by the service user even when root ran the command.

    osmctl is often run by an administrator while the console itself runs as its own
    user, so anything written as root is handed back to that user.
    """
    if os.geteuid() != 0:
        return
    uid, gid = owner_of_data()
    if uid == 0:
        return
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def tool_env() -> dict:
    """Environment for subprocesses: our bin dir first, then the host PATH."""
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(EXTRA_PATH + [env.get("PATH", "")])
    env.pop("KUBECONFIG", None)
    return env
