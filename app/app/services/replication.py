"""repctl wrappers: register clusters, inject configs, and run failover actions."""
from __future__ import annotations

import base64
import json
import os
import shutil
import time
import subprocess
from pathlib import Path

import yaml

from ..config import DATA_DIR, ROOT, match_owner, tool_env
from .jobs import Job
from .k8s import ClusterError, run, run_json
from .tools import which

# repctl reads $HOME/.repctl/clusters and has no option to move it, so the console keeps
# that store inside its own root and runs repctl with HOME set there. Whoever calls it,
# the UI, osmctl or a shell, ends up in the same place.
REPCTL_HOME = ROOT / ".repctl"
CLUSTER_DIR = REPCTL_HOME / "clusters"
CONTROLLER_NS = "dell-replication-controller"
LEGACY_HOME = DATA_DIR / "repctl"          # where an earlier build kept them


def repctl_bin() -> str:
    path = which("repctl")
    if not path:
        raise ClusterError("repctl is not installed yet; install it on the Install page")
    return path


def _private(path: Path) -> None:
    """Owner-only, and owned by whoever owns the data directory.

    The console and the command line tool may run as different users, so a directory
    one created must stay usable by the other.
    """
    try:
        os.chmod(path, 0o700)
    except PermissionError:
        pass
    match_owner(path)


def migrate_legacy_store() -> list[str]:
    """Move configs an earlier build hid under the data directory into ~/.repctl."""
    moved = []
    old = LEGACY_HOME / ".repctl" / "clusters"
    if not old.exists():
        return moved
    CLUSTER_DIR.mkdir(parents=True, exist_ok=True)
    for item in old.iterdir():
        target = CLUSTER_DIR / item.name
        if not target.exists():
            shutil.move(str(item), str(target))
            moved.append(item.name)
    return moved


def repctl_env() -> dict:
    """Environment for repctl: the real home, so the shell sees the same state."""
    env = tool_env()
    CLUSTER_DIR.mkdir(parents=True, exist_ok=True)
    _private(REPCTL_HOME)
    migrate_legacy_store()
    env["HOME"] = str(ROOT)                # repctl derives its store from HOME
    return env


def run_repctl(job: Job, args: list[str], timeout: int | None = None) -> int:
    # repctl writes repctl.log into the current directory, so run it somewhere writable
    work_dir = DATA_DIR / "logs"
    work_dir.mkdir(parents=True, exist_ok=True)
    return job.run([repctl_bin()] + args, env=repctl_env(), cwd=str(work_dir))


def capture(args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    work_dir = DATA_DIR / "logs"
    work_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.run([repctl_bin()] + args, capture_output=True, text=True,
                          timeout=timeout, env=repctl_env(), cwd=str(work_dir))


def help_text(sub: list[str] | None = None) -> str:
    """The installed binary's own help, so the UI never guesses at syntax."""
    try:
        proc = capture((sub or []) + ["--help"])
        return (proc.stdout or proc.stderr).strip()
    except Exception as exc:                        # noqa: BLE001
        return f"repctl not available: {exc}"


def add_clusters(job: Job, clusters: list[dict]) -> int:
    """repctl cluster add: copies each kubeconfig into repctl's own store."""
    args = ["cluster", "add",
            "-f", ",".join(c["kubeconfig"] for c in clusters),
            "-n", ",".join(c["id"] for c in clusters), "--force"]
    job.log("registering clusters with repctl: " + ", ".join(c["id"] for c in clusters))
    return run_repctl(job, args)


def store_dir() -> Path:
    """Where repctl keeps the kubeconfigs it was given."""
    return CLUSTER_DIR


def list_clusters() -> dict:
    """What repctl itself reports, not what this console remembers."""
    out: dict = {"rows": [], "raw": "", "error": "", "path": str(store_dir()),
                 "files": []}
    try:
        out["files"] = sorted(p.name for p in store_dir().iterdir()) if store_dir().exists() else []
    except OSError:
        pass
    try:
        proc = capture(["cluster", "get"])
    except Exception as exc:                            # noqa: BLE001
        out["error"] = str(exc)
        return out
    text = (proc.stdout or "") + (proc.stderr or "")
    out["raw"] = text.strip()
    if proc.returncode != 0 or "FATAL" in text:
        if "failed to find any valid config" in text:
            out["error"] = "repctl has no clusters registered yet"
        else:
            out["error"] = text.strip().splitlines()[-1][:200] if text.strip() else "repctl failed"
        return out
    for line in text.splitlines():
        stripped = line.strip()
        # repctl draws a box and a header before the rows, and logs with a timestamp
        if not stripped or stripped[0] in "+|[" or stripped.lower().startswith("clusterid"):
            continue
        parts = stripped.split()
        out["rows"].append({
            "id": parts[0],
            "version": parts[1] if len(parts) > 2 else "",
            "url": parts[-1] if len(parts) > 1 else "",
            "detail": " ".join(parts[1:])[:120],
        })
    return out


SA_TOKEN_SECRET = "replication-secret"
CONTROLLER_SA = "dell-replication-controller-sa"


def ensure_sa_token(job: Job, kubeconfig: str, cluster_id: str) -> bool:
    """Make sure the controller's service account has a usable, listed token secret.

    repctl reads the token from the secrets the service account lists as mountable.
    The Helm chart ships such a secret; the CSM Operator does not, and Kubernetes has
    not created them automatically since 1.24, so it is created here when missing.
    """
    have = run(kubeconfig, ["-n", CONTROLLER_NS, "get", "secret", SA_TOKEN_SECRET]).returncode == 0
    if not have:
        manifest = yaml.safe_dump({
            "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/service-account-token",
            "metadata": {"name": SA_TOKEN_SECRET, "namespace": CONTROLLER_NS,
                         "annotations": {"kubernetes.io/service-account.name": CONTROLLER_SA}},
        })
        job.log(f"  {cluster_id}: creating the token secret {SA_TOKEN_SECRET}")
        proc = run(kubeconfig, ["apply", "-f", "-"], stdin_text=manifest, timeout=60)
        if proc.returncode != 0:
            job.log(f"  {cluster_id}: could not create it: "
                    f"{(proc.stderr or proc.stdout).strip()[:160]}")
            return False
    # repctl looks at the account's mountable secrets, so the secret has to be listed there
    patch = json.dumps({"secrets": [{"name": SA_TOKEN_SECRET}]})
    run(kubeconfig, ["-n", CONTROLLER_NS, "patch", "sa", CONTROLLER_SA, "-p", patch], timeout=60)
    for _ in range(15):
        try:
            secret = run_json(kubeconfig, ["-n", CONTROLLER_NS, "get", "secret", SA_TOKEN_SECRET])
        except ClusterError:
            secret = {}
        if (secret.get("data") or {}).get("token"):
            return True
        time.sleep(2)
    job.log(f"  {cluster_id}: the token secret never filled in")
    return False


def sa_kubeconfig(job: Job, kubeconfig: str, cluster_id: str) -> str | None:
    """Build a kubeconfig for the controller's own service account.

    repctl's --use-sa asks kubectl to describe the account and greps for a
    "Mountable secrets" line, which Kubernetes 1.35 no longer prints, so its own
    generation fails. Reading the token secret directly gives the same result, and the
    file is then handed to repctl as a custom configuration.
    """
    if not ensure_sa_token(job, kubeconfig, cluster_id):
        return None
    try:
        secret = run_json(kubeconfig, ["-n", CONTROLLER_NS, "get", "secret", SA_TOKEN_SECRET])
    except ClusterError as exc:
        job.log(f"  {cluster_id}: cannot read the token secret: {exc}")
        return None
    data = secret.get("data", {})
    if not data.get("token"):
        return None
    token = base64.b64decode(data["token"]).decode()
    server = run(kubeconfig, ["config", "view", "--minify", "--raw", "-o",
                              "jsonpath={.clusters[0].cluster.server}"]).stdout.strip()
    cluster_entry: dict = {"server": server}
    if data.get("ca.crt"):
        cluster_entry["certificate-authority-data"] = data["ca.crt"]
    else:
        cluster_entry["insecure-skip-tls-verify"] = True
    cfg = {
        "apiVersion": "v1", "kind": "Config", "preferences": {},
        "clusters": [{"name": cluster_id, "cluster": cluster_entry}],
        "users": [{"name": CONTROLLER_SA, "user": {"token": token}}],
        "contexts": [{"name": cluster_id, "context": {"cluster": cluster_id,
                                                      "user": CONTROLLER_SA,
                                                      "namespace": CONTROLLER_NS}}],
        "current-context": cluster_id,
    }
    out_dir = DATA_DIR / "repctl-sa"
    out_dir.mkdir(parents=True, exist_ok=True)
    _private(out_dir)
    path = out_dir / cluster_id
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    os.chmod(path, 0o600)
    match_owner(path)
    job.log(f"  {cluster_id}: built a kubeconfig for {CONTROLLER_SA}")
    return str(path)


def inject(job: Job, cluster_ids: list[str], use_sa: bool = True,
           kubeconfigs: dict[str, str] | None = None) -> int:
    """Push each cluster's configuration into the other's replication namespace.

    repctl injects for every cluster it manages, narrowed by the global --clusters flag.
    """
    args = ["--clusters", ",".join(cluster_ids), "cluster", "inject"]
    if use_sa:
        paths = []
        for cid in cluster_ids:
            kubeconfig = (kubeconfigs or {}).get(cid)
            built = sa_kubeconfig(job, kubeconfig, cid) if kubeconfig else None
            if built:
                paths.append(built)
        if len(paths) == len(cluster_ids):
            args += ["--custom-configs", ",".join(paths)]
        else:
            job.log("  could not build service account configurations for every cluster, so the "
                    "admin ones are injected instead, which Dell calls the less secure option")
    job.log("injecting cluster configs so each replication controller can reach its peer")
    rc = run_repctl(job, args)
    if rc != 0 and use_sa:
        job.log("  that failed; retrying with the admin configurations")
        rc = run_repctl(job, ["--clusters", ",".join(cluster_ids), "cluster", "inject"])
    return rc


def configure_controller(job: Job, kubeconfig: str, cluster_id: str,
                         targets: list[dict]) -> None:
    """Set the controller's own id and its peers, keeping what repctl wrote.

    repctl's injection fills each target's address and secret reference. Rewriting the
    config map from scratch would drop those, and the controller then logs
    'Secret "" not found', so the existing entries are merged rather than replaced.
    """
    current = controller_config(kubeconfig)
    existing = {t.get("clusterId"): dict(t) for t in (current.get("targets") or [])
                if isinstance(t, dict)}
    merged = []
    for wanted in targets:
        cid = wanted["clusterId"]
        entry = existing.get(cid, {})
        entry["clusterId"] = cid
        if wanted.get("address"):
            entry.setdefault("address", wanted["address"])
        if wanted.get("secretRef"):
            entry.setdefault("secretRef", wanted["secretRef"])
        merged.append(entry)
    config = {"clusterId": cluster_id, "targets": merged}
    if current.get("CSI_LOG_LEVEL"):
        config["CSI_LOG_LEVEL"] = current["CSI_LOG_LEVEL"]
    cm = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "dell-replication-controller-config", "namespace": CONTROLLER_NS},
        "data": {"config.yaml": yaml.safe_dump(config, sort_keys=False)},
    }
    job.log(f"  {cluster_id}: clusterId={cluster_id} targets="
            f"{[(t['clusterId'], t.get('secretRef', '-')) for t in merged]}")
    proc = run(kubeconfig, ["apply", "-f", "-"], stdin_text=yaml.safe_dump(cm), timeout=60)
    if proc.returncode != 0:
        raise ClusterError("could not write the replication controller config: "
                           + (proc.stderr or proc.stdout).strip()[:200])
    run(kubeconfig, ["-n", CONTROLLER_NS, "rollout", "restart", "deployment",
                     "dell-replication-controller-manager"], timeout=60)


def controller_config(kubeconfig: str) -> dict:
    """Read back what the replication controller is actually configured with."""
    try:
        cm = run_json(kubeconfig, ["-n", CONTROLLER_NS, "get", "cm",
                                   "dell-replication-controller-config"], timeout=60)
    except ClusterError:
        return {}
    try:
        return yaml.safe_load(cm.get("data", {}).get("config.yaml", "")) or {}
    except yaml.YAMLError:
        return {}


def replication_groups(kubeconfig: str) -> list[dict]:
    from .k8s import summarize_rg
    try:
        items = run_json(kubeconfig, ["get", "dellcsireplicationgroups"], timeout=60).get("items", [])
    except ClusterError:
        return []
    return [summarize_rg(rg) for rg in items]


# What the PowerScale driver actually implements. Its ExecuteAction switch accepts
# FAILOVER_REMOTE, UNPLANNED_FAILOVER_LOCAL, FAILBACK_LOCAL,
# ACTION_FAILBACK_DISCARD_CHANGES_LOCAL, REPROTECT_LOCAL, SYNC, SUSPEND and RESUME.
# swap and establish exist in repctl but are rejected by this driver, so they are not offered.
ACTIONS = {
    "failover": {
        "verb": ["failover"], "flag": "--target", "needs_target": True,
        "label": "Failover",
        "help": "Move service to the other site. Planned by default; tick unplanned when the "
                "current source is unreachable. Quiesce writers first, then reprotect afterwards.",
        "danger": True,
    },
    "failback": {
        "verb": ["failback"], "flag": "--target", "needs_target": True,
        "label": "Failback",
        "help": "Undo a failover, before reprotect, returning service to the site that had it. "
                "Once a pair has been reprotected, move service with Failover instead.",
        "danger": True,
    },
    "reprotect": {
        "verb": ["reprotect"], "flag": "--at", "needs_target": True,
        "label": "Reprotect",
        "help": "Re-establish protection after a failover, run at the named cluster.",
        "danger": False,
    },
    "suspend": {
        "verb": ["exec", "-a", "suspend"], "flag": None, "needs_target": False,
        "label": "Suspend", "help": "Pause replication without breaking the pair.", "danger": False,
    },
    "resume": {
        "verb": ["exec", "-a", "resume"], "flag": None, "needs_target": False,
        "label": "Resume", "help": "Resume a suspended pair.", "danger": False,
    },
    "sync": {
        "verb": ["exec", "-a", "sync"], "flag": None, "needs_target": False,
        "label": "Sync now", "help": "Trigger an immediate synchronisation.", "danger": False,
    },
}

# exec only runs at the current source site.
SOURCE_ONLY = {"suspend", "resume", "sync"}


def build_args(rg: str, action: str, target_cluster: str | None = None,
               unplanned: bool = False, discard: bool = False) -> list[str]:
    spec = ACTIONS.get(action)
    if not spec:
        raise ClusterError(f"unknown action {action}")
    args = ["--rg", rg] + list(spec["verb"])
    if spec["flag"] and target_cluster:
        args += [spec["flag"], target_cluster]
    if action == "failover" and unplanned:
        args.append("--unplanned")
    if action == "failback" and discard:
        args.append("--discard")
    return args


def rg_state(kubeconfig: str, rg: str) -> dict:
    """The group's current action and link state, as the cluster reports them."""
    try:
        obj = run_json(kubeconfig, ["get", "dellcsireplicationgroup", rg], timeout=60)
    except ClusterError as exc:
        return {"error": str(exc)[:160]}
    status = obj.get("status", {}) or {}
    link = status.get("replicationLinkState", {}) or {}
    pending = (obj.get("spec", {}) or {}).get("action") or ""
    annotation = (obj["metadata"].get("annotations") or {}).get("Action", "")
    current, completed, failure = "", True, ""
    if annotation:
        try:
            parsed = json.loads(annotation)
            current = parsed.get("name", "")
            completed = bool(parsed.get("completed"))
            failure = parsed.get("finalError", "")
        except json.JSONDecodeError:
            pass
    return {"pending": pending, "action": current, "completed": completed, "error": failure,
            "state": status.get("state", ""), "link": link.get("state", ""),
            "is_source": link.get("isSource")}


def busy(kubeconfig: str, rg: str) -> str:
    """Name of an action still running on this group, or an empty string."""
    now = rg_state(kubeconfig, rg)
    if now.get("pending"):
        return now["pending"]
    if now.get("action") and not now.get("completed"):
        return now["action"]
    if "IN_PROGRESS" in (now.get("state") or ""):
        return now["state"]
    return ""


# what each button leaves in the group's action annotation once the driver ran it
EXPECTED_ANNOTATION = {
    "failover": ("FAILOVER_REMOTE", "UNPLANNED_FAILOVER_LOCAL"),
    "failback": ("FAILBACK_LOCAL", "ACTION_FAILBACK_DISCARD_CHANGES_LOCAL"),
    "reprotect": ("REPROTECT_LOCAL",),
    "suspend": ("SUSPEND",), "resume": ("RESUME",), "sync": ("SYNC",),
}


def act(job: Job, rg: str, action: str, target_cluster: str | None = None,
        unplanned: bool = False, discard: bool = False,
        kubeconfig: str | None = None, wait: int = 300,
        kubeconfigs: dict[str, str] | None = None) -> int:
    """Run one repctl action and wait for the clusters to carry it out.

    Which side records the action depends on the action: a failover is written at the
    source, a reprotect at the site being promoted. Both clusters are therefore polled,
    and the action counts as done when one of them reports the matching action finished
    and neither is still working.
    """
    watching = dict(kubeconfigs or {})
    if kubeconfig and not watching:
        watching = {"cluster": kubeconfig}

    for cid, kc in watching.items():
        running = busy(kc, rg)
        if running:
            raise ClusterError(
                f"{rg} is still running {running} on {cid}. Actions are written onto the group "
                "one at a time, and a second one issued now would be dropped. Let it finish.")

    args = build_args(rg, action, target_cluster, unplanned, discard)
    job.log("repctl " + " ".join(args))
    rc = run_repctl(job, args)
    if rc != 0 or not watching:
        return rc

    expected = EXPECTED_ANNOTATION.get(action, ())
    job.log("waiting for the cluster to carry it out")
    last = ""
    for _ in range(max(1, wait // 5)):
        time.sleep(5)
        seen, working, failure = False, False, ""
        lines = []
        for cid, kc in sorted(watching.items()):
            now = rg_state(kc, rg)
            lines.append(f"  {cid}: state={now.get('state','?')} link={now.get('link','?')} "
                         f"source={now.get('is_source')} action={now.get('action','-')}"
                         f"{'' if now.get('completed') else ' (running)'}")
            arrived = now.get("action") in expected if expected else True
            if "IN_PROGRESS" in (now.get("state") or "") or now.get("pending"):
                working = True
            if arrived and now.get("completed"):
                seen = True
                if now.get("error"):
                    failure = now["error"]
        block = "\n".join(lines)
        if block != last:
            job.log(block)
            last = block
        if failure:
            job.log(f"  the cluster reported: {failure}")
            return 1
        if seen and not working:
            job.log("action finished")
            return 0
    job.log("still running after the wait; check the group on the Operations page")
    return 0


def action_preview(rg: str, action: str, target_cluster: str | None = None,
                   unplanned: bool = False, discard: bool = False) -> str:
    return "repctl " + " ".join(build_args(rg, action, target_cluster, unplanned, discard))


def preflight_use_sa(clusters: list[dict]) -> list[dict]:
    """--use-sa only works when each cluster already runs the controller, exposes a
    mountable token secret, and its kubeconfig carries the CA data. It also shells out
    to bash, kubectl and envsubst on this host."""
    checks: list[dict] = []
    for tool in ("bash", "kubectl", "envsubst"):
        checks.append({"what": f"{tool} on PATH", "ok": bool(which(tool)),
                       "detail": which(tool) or "not found",
                       "fix": f"install {tool} on this host"})
    for c in clusters:
        kc = c["kubeconfig"]
        ns_ok = run(kc, ["get", "ns", CONTROLLER_NS]).returncode == 0
        checks.append({"what": f"{c['id']}: namespace {CONTROLLER_NS}", "ok": ns_ok,
                       "detail": "present" if ns_ok else "missing",
                       "fix": "install the replication controller first"})
        sec_ok = run(kc, ["-n", CONTROLLER_NS, "get", "secret", "replication-secret"]).returncode == 0
        checks.append({"what": f"{c['id']}: token secret replication-secret", "ok": sec_ok,
                       "detail": "present" if sec_ok else "missing",
                       "fix": "comes with the controller chart; install it first"})
        try:
            text = Path(kc).read_text()
            ca_ok = "certificate-authority-data" in text
        except OSError:
            ca_ok = False
        checks.append({"what": f"{c['id']}: kubeconfig has CA data", "ok": ca_ok,
                       "detail": "yes" if ca_ok else "missing, repctl --use-sa needs it",
                       "fix": "export a kubeconfig that embeds certificate-authority-data"})
    return checks


def reachability(clusters: list[dict]) -> list[dict]:
    """Each controller must reach the other cluster's API server directly."""
    import socket
    from urllib.parse import urlparse
    out = []
    for c in clusters:
        url = urlparse(c.get("server", ""))
        host, port = url.hostname, url.port or 6443
        ok, detail = False, ""
        if host:
            try:
                with socket.create_connection((host, port), timeout=5):
                    ok = True
                    detail = f"{host}:{port} reachable from this host"
            except OSError as exc:
                detail = f"{host}:{port} {exc}"
        out.append({"cluster": c["id"], "ok": ok, "detail": detail})
    return out
