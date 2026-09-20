"""Talk to Kubernetes/OpenShift clusters through the oc (or kubectl) CLI."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

import yaml

from ..config import KUBECONFIG_DIR, ensure_dirs, match_owner, tool_env
from .jobs import Job
from .tools import which

SA_NAMESPACE = "osm"
SA_NAME = "osm-console"
SA_CLUSTER_ROLE = "cluster-admin"


class ClusterError(RuntimeError):
    pass


def cli() -> str:
    path = which("oc") or which("kubectl")
    if not path:
        raise ClusterError("neither oc nor kubectl was found on this host")
    return path


def run(kubeconfig: str | Path, args: list[str], timeout: int = 60,
        stdin_text: str | None = None) -> subprocess.CompletedProcess:
    env = tool_env()
    env["KUBECONFIG"] = str(kubeconfig)
    return subprocess.run([cli()] + args, capture_output=True, text=True,
                          timeout=timeout, env=env, input=stdin_text)


def run_json(kubeconfig: str | Path, args: list[str], timeout: int = 60) -> dict:
    proc = run(kubeconfig, args + ["-o", "json"], timeout=timeout)
    if proc.returncode != 0:
        raise ClusterError((proc.stderr or proc.stdout).strip()[:400])
    return json.loads(proc.stdout or "{}")


def kubeconfig_path(cluster_id: str) -> Path:
    ensure_dirs()
    return KUBECONFIG_DIR / f"{cluster_id}.kubeconfig"


def validate_kubeconfig(content: str) -> dict:
    """Accept anything that really is a kubeconfig.

    Many kubeconfigs, including the ones OpenShift's installer writes, carry no
    "kind: Config" line at all, so the shape is what counts: a clusters list plus
    contexts or users. JSON is valid YAML, so both forms parse here.
    """
    text = content.lstrip("\ufeff")
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ClusterError(f"this file is not valid YAML or JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        first = text.strip().splitlines()[0][:60] if text.strip() else "(empty file)"
        raise ClusterError(f"this file does not look like a kubeconfig; it starts with: {first}")
    kind = str(parsed.get("kind", "")).lower()
    if kind and kind != "config":
        raise ClusterError(f"this file is a {parsed['kind']}, not a kubeconfig")
    if not parsed.get("clusters"):
        keys = ", ".join(list(parsed)[:6]) or "nothing"
        raise ClusterError("a kubeconfig needs a clusters section; this file has: " + keys)
    if not parsed.get("contexts") and not parsed.get("users"):
        raise ClusterError("this kubeconfig has no contexts and no users")
    return parsed


def save_kubeconfig(cluster_id: str, content: str | bytes) -> Path:
    ensure_dirs()
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    validate_kubeconfig(content)
    path = kubeconfig_path(cluster_id)
    path.write_text(content)
    os.chmod(path, 0o600)
    match_owner(path)
    return path


def login_kubeconfig(server: str, username: str, password: str, cluster_id: str,
                     insecure: bool = True) -> Path:
    """Log in with a username and password and keep the resulting kubeconfig.

    This is the kubeadmin route: oc exchanges the credentials for a token, so the
    file we store never holds the password.
    """
    ensure_dirs()
    server = normalize_api_address(server)
    hint = address_hint(server)
    if hint:
        raise ClusterError(f"cannot reach {server}: {hint}")
    path = kubeconfig_path(cluster_id)
    env = tool_env()
    env["KUBECONFIG"] = str(path)
    cmd = [cli(), "login", server, "-u", username, "-p", password]
    if insecure:
        cmd.append("--insecure-skip-tls-verify=true")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env)
    if proc.returncode != 0:
        raw = (proc.stderr or proc.stdout).strip()
        raise ClusterError(_login_error(raw))
    os.chmod(path, 0o600)
    return path


def normalize_api_address(raw: str) -> str:
    """Accept what people actually paste and turn it into an API address."""
    text = (raw or "").strip().rstrip("/")
    if not text:
        return ""
    if text.startswith("http://"):
        text = "https://" + text[len("http://"):]
    if not text.startswith("https://"):
        text = "https://" + text
    from urllib.parse import urlparse
    parsed = urlparse(text)
    host, port = parsed.hostname or "", parsed.port
    # The OpenShift web console address is not the API address; derive the API one.
    if host.startswith("console-openshift-console.apps."):
        host = "api." + host[len("console-openshift-console.apps."):]
        port = port or 6443
    elif host.startswith("apps."):
        host = "api." + host[len("apps."):]
        port = port or 6443
    return f"https://{host}:{port or 6443}"


def address_hint(address: str) -> str:
    """Why a login address failed, and what to try instead."""
    import socket
    from urllib.parse import urlparse
    parsed = urlparse(address)
    host, port = parsed.hostname or address, parsed.port or 6443
    try:
        socket.gethostbyname(host)
    except OSError:
        tried = [host]
        suggestions = []
        if not host.startswith("api."):
            candidate = "api." + host
            try:
                socket.gethostbyname(candidate)
                suggestions.append(f"https://{candidate}:{port}")
            except OSError:
                tried.append(candidate)
        detail = f"the name {host} does not resolve from this host"
        if suggestions:
            return detail + f". {suggestions[0]} does resolve, try that address."
        return (detail + ". Check the spelling, use the cluster's API address such as "
                "https://api.<cluster>.<domain>:6443, or give the IP address of an API "
                "endpoint instead.")
    try:
        with socket.create_connection((host, port), timeout=6):
            return ""
    except OSError as exc:
        return f"{host} resolves but nothing answered on port {port}: {exc}"


def _login_error(raw: str) -> str:
    """Turn the login failures people actually hit into plain sentences."""
    text = raw.lower()
    if "401" in text or "unauthorized" in text or "unexpected response: 500" in text:
        return ("the server rejected that user and password. For OpenShift, kubeadmin uses the "
                "password from the installer's kubeadmin-password file, and that account is often "
                "removed after another administrator is created.")
    if "certificate signed by unknown authority" in text or "x509" in text:
        return ("the cluster's certificate is not trusted here. Tick the option to accept it, or "
                "upload a kubeconfig that embeds the authority certificate.")
    if "no such host" in text or "could not resolve" in text or "name or service not known" in text:
        return "that API address does not resolve from this host; check DNS or use the IP address."
    if "tls" in text and "handshake" in text:
        return ("the address answered but not with the Kubernetes API; make sure this is the API "
                "address, usually https://api.<cluster>.<domain>:6443, not the web console.")
    if "connection refused" in text or "timeout" in text or "timed out" in text or "i/o timeout" in text:
        return "nothing answered at that API address; check the address, the port and any firewall."
    if "server rejected our request" in text or "forbidden" in text:
        return "the login worked but the account is not allowed to read the cluster."
    lines = [l for l in raw.splitlines() if l.strip()]
    return lines[-1][:300] if lines else "login failed"


def probe(kubeconfig: str | Path) -> dict:
    """Identity, version and node summary. Raises ClusterError when unreachable."""
    info: dict = {"kubeconfig": str(kubeconfig)}
    proc = run(kubeconfig, ["whoami", "--show-server"], timeout=30)
    if proc.returncode != 0:
        cfg = run(kubeconfig, ["config", "view", "--minify",
                               "-o", "jsonpath={.clusters[0].cluster.server}"], timeout=30)
        info["server"] = cfg.stdout.strip()
    else:
        info["server"] = proc.stdout.strip()
    who = run(kubeconfig, ["whoami"], timeout=30)
    info["user"] = who.stdout.strip() if who.returncode == 0 else ""

    ver = run(kubeconfig, ["version", "-o", "json"], timeout=40)
    if ver.returncode != 0:
        raise ClusterError((ver.stderr or ver.stdout).strip()[:400] or "cluster not reachable")
    try:
        vdata = json.loads(ver.stdout)
    except json.JSONDecodeError:
        vdata = {}
    info["kubernetes_version"] = (vdata.get("serverVersion") or {}).get("gitVersion", "")
    info["openshift_version"] = (vdata.get("openshiftVersion") or "")
    info["is_openshift"] = bool(info["openshift_version"])
    if not info["is_openshift"]:
        crd = run(kubeconfig, ["get", "crd", "securitycontextconstraints.security.openshift.io"],
                  timeout=30)
        info["is_openshift"] = crd.returncode == 0

    try:
        nodes = run_json(kubeconfig, ["get", "nodes"], timeout=60).get("items", [])
    except ClusterError:
        nodes = []
    ready, workers = 0, 0
    for n in nodes:
        conds = {c["type"]: c["status"] for c in n.get("status", {}).get("conditions", [])}
        if conds.get("Ready") == "True":
            ready += 1
        labels = n.get("metadata", {}).get("labels", {})
        if "node-role.kubernetes.io/worker" in labels:
            workers += 1
    info.update({"nodes": len(nodes), "nodes_ready": ready, "workers": workers})

    auth = run(kubeconfig, ["auth", "can-i", "*", "*", "--all-namespaces"], timeout=30)
    info["cluster_admin"] = auth.stdout.strip().startswith("yes")
    return info


def can_i(kubeconfig: str | Path, verb: str, resource: str) -> bool:
    proc = run(kubeconfig, ["auth", "can-i", verb, resource, "--all-namespaces"], timeout=30)
    return proc.stdout.strip().startswith("yes")


# --- RBAC checks that matter for repctl and the driver install -----------------
REQUIRED_RBAC = [
    ("create", "customresourcedefinitions", "install the replication and driver CRDs"),
    ("create", "clusterroles", "driver and controller RBAC"),
    ("create", "clusterrolebindings", "bind the driver service accounts"),
    ("create", "namespaces", "create the driver and controller namespaces"),
    ("create", "secrets", "array credentials and the injected cluster configs"),
    ("create", "serviceaccounts", "driver and controller service accounts"),
    ("create", "deployments", "controller deployment"),
    ("create", "daemonsets", "node plugin"),
    ("create", "storageclasses", "replicated and plain storage classes"),
    ("create", "dellcsireplicationgroups", "replication groups"),
    ("update", "persistentvolumes", "failover reassigns PVs"),
]


def rbac_report(kubeconfig: str | Path) -> list[dict]:
    out = []
    for verb, resource, why in REQUIRED_RBAC:
        ok = can_i(kubeconfig, verb, resource)
        out.append({"verb": verb, "resource": resource, "why": why, "ok": ok})
    return out


# --- service account kubeconfig ------------------------------------------------
SA_MANIFEST = """
apiVersion: v1
kind: Namespace
metadata:
  name: {ns}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {sa}
  namespace: {ns}
---
apiVersion: v1
kind: Secret
metadata:
  name: {sa}-token
  namespace: {ns}
  annotations:
    kubernetes.io/service-account.name: {sa}
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {sa}-{role}
subjects:
  - kind: ServiceAccount
    name: {sa}
    namespace: {ns}
roleRef:
  kind: ClusterRole
  name: {role}
  apiGroup: rbac.authorization.k8s.io
"""


def create_sa_kubeconfig(job: Job, admin_kubeconfig: str | Path, cluster_name: str,
                         ns: str = SA_NAMESPACE, sa: str = SA_NAME,
                         role: str = SA_CLUSTER_ROLE) -> str:
    """Create a service account with a long lived token and return a kubeconfig for it.

    repctl stores the kubeconfigs it is given and uses them for every later call, so a
    token that does not expire is what makes unattended failover work.
    """
    manifest = SA_MANIFEST.format(ns=ns, sa=sa, role=role)
    job.log(f"creating service account {ns}/{sa} bound to cluster role {role}")
    proc = run(admin_kubeconfig, ["apply", "-f", "-"], stdin_text=manifest, timeout=90)
    job.log((proc.stdout or "") + (proc.stderr or ""))
    if proc.returncode != 0:
        raise ClusterError("could not create the service account: "
                           + (proc.stderr or proc.stdout).strip()[:300])

    token, ca = "", ""
    for attempt in range(30):
        try:
            secret = run_json(admin_kubeconfig, ["-n", ns, "get", "secret", f"{sa}-token"])
        except ClusterError:
            secret = {}
        data = secret.get("data", {})
        if data.get("token"):
            token = base64.b64decode(data["token"]).decode()
            ca = data.get("ca.crt", "")
            break
        time.sleep(2)
        if attempt == 4:
            job.log("waiting for the token controller to fill in the secret")
    if not token:
        job.log("no token secret appeared, falling back to a request-token (may expire)")
        proc = run(admin_kubeconfig, ["create", "token", sa, "-n", ns,
                                      "--duration=87600h"], timeout=60)
        if proc.returncode != 0:
            raise ClusterError("could not obtain a token for the service account")
        token = proc.stdout.strip()

    server = run(admin_kubeconfig, ["config", "view", "--minify", "--raw", "-o",
                                    "jsonpath={.clusters[0].cluster.server}"]).stdout.strip()
    if not ca:
        ca = run(admin_kubeconfig, ["config", "view", "--minify", "--raw", "-o",
                                    "jsonpath={.clusters[0].cluster.certificate-authority-data}"]
                 ).stdout.strip()
    cluster_entry: dict = {"server": server}
    if ca:
        cluster_entry["certificate-authority-data"] = ca
    else:
        cluster_entry["insecure-skip-tls-verify"] = True

    cfg = {
        "apiVersion": "v1", "kind": "Config", "preferences": {},
        "clusters": [{"name": cluster_name, "cluster": cluster_entry}],
        "users": [{"name": f"{sa}/{cluster_name}", "user": {"token": token}}],
        "contexts": [{"name": cluster_name,
                      "context": {"cluster": cluster_name, "user": f"{sa}/{cluster_name}",
                                  "namespace": ns}}],
        "current-context": cluster_name,
    }
    job.log(f"kubeconfig built for {server} using the service account token")
    return yaml.safe_dump(cfg, sort_keys=False)


# --- inventory -----------------------------------------------------------------
def driver_status(kubeconfig: str | Path, namespace: str) -> dict:
    out: dict = {"namespace": namespace, "pods": [], "controller_ready": False,
                 "node_ready": False, "storage_classes": [], "csidriver": False,
                 "replication_crds": False, "replication_controller": False,
                 "replication_groups": [], "error": ""}
    try:
        pods = run_json(kubeconfig, ["-n", namespace, "get", "pods"]).get("items", [])
    except ClusterError as exc:
        out["error"] = str(exc)
        return out
    for p in pods:
        name = p["metadata"]["name"]
        # only the driver's own pods; anything else in the namespace is the user's
        if "-controller-" not in name and "-node-" not in name:
            continue
        statuses = p.get("status", {}).get("containerStatuses") or []
        ready = sum(1 for c in statuses if c.get("ready"))
        out["pods"].append({
            "name": name,
            "phase": p.get("status", {}).get("phase", ""),
            "ready": f"{ready}/{len(statuses)}" if statuses else "0/0",
            "restarts": sum(c.get("restartCount", 0) for c in statuses),
            "node": p.get("spec", {}).get("nodeName", ""),
            "all_ready": bool(statuses) and ready == len(statuses),
        })
    out["controller_ready"] = any(p["all_ready"] and "controller" in p["name"] for p in out["pods"])
    out["node_ready"] = any(p["all_ready"] and "-node-" in p["name"] for p in out["pods"])

    try:
        scs = run_json(kubeconfig, ["get", "storageclass"]).get("items", [])
        out["storage_classes"] = [{
            "name": s["metadata"]["name"],
            "provisioner": s.get("provisioner", ""),
            "replicated": s.get("parameters", {}).get(
                "replication.storage.dell.com/isReplicationEnabled") == "true",
            "remote_cluster": s.get("parameters", {}).get(
                "replication.storage.dell.com/remoteClusterID", ""),
            "rpo": s.get("parameters", {}).get("replication.storage.dell.com/rpo", ""),
        } for s in scs if "isilon" in s.get("provisioner", "")]
    except ClusterError:
        pass

    out["csidriver"] = run(kubeconfig, ["get", "csidriver", "csi-isilon.dellemc.com"]).returncode == 0
    out["replication_crds"] = run(
        kubeconfig, ["get", "crd", "dellcsireplicationgroups.replication.storage.dell.com"]
    ).returncode == 0
    try:
        rc = run_json(kubeconfig, ["-n", "dell-replication-controller", "get", "pods"]).get("items", [])
        out["replication_controller"] = any(
            p.get("status", {}).get("phase") == "Running" for p in rc)
    except ClusterError:
        pass
    if out["replication_crds"]:
        try:
            rgs = run_json(kubeconfig, ["get", "dellcsireplicationgroups"]).get("items", [])
            out["replication_groups"] = [summarize_rg(r) for r in rgs]
        except ClusterError:
            pass
    return out


def summarize_rg(rg: dict) -> dict:
    spec = rg.get("spec", {})
    status = rg.get("status", {})
    conditions = status.get("conditions") or []
    return {
        "name": rg["metadata"]["name"],
        "created": rg["metadata"].get("creationTimestamp", ""),
        "remote_cluster": spec.get("remoteClusterId", ""),
        "protection_group": spec.get("protectionGroupId", ""),
        "action": spec.get("action", ""),
        "state": status.get("state", ""),
        "link_state": (status.get("replicationLinkState") or {}).get("state", ""),
        "last_action": (status.get("lastAction") or {}).get("condition", ""),
        "is_source": (status.get("replicationLinkState") or {}).get("isSource"),
        "condition": conditions[0].get("condition", "") if conditions else "",
    }
