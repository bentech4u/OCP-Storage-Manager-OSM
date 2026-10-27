"""Install and remove the PowerScale driver, by Helm or by the Dell CSM Operator."""
from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

from ..config import (DEFAULT_VALUES, DRIVER_CHART, OPERATOR_NAMESPACE, REPLICATION_CHART,
                      REPLICATION_CRDS, tool_env)
from . import manifests
from .arrays import secret_yaml
from .jobs import Job
from .k8s import ClusterError, cli, run, run_json
from .tools import which

DRIVER_RELEASE = "isilon"

# Dell's ExecuteAction and CreateVolume accept exactly these RPO strings.
RPO_VALUES = ["Five_Minutes", "Fifteen_Minutes", "Thirty_Minutes", "One_Hour",
              "Six_Hours", "Twelve_Hours", "One_Day"]

# CSM 1.17.1 = csi-powerscale v2.17.1 + csm-replication v1.15.0.
CSM_VERSION = "v1.17.1"
DRIVER_VERSION = "v2.17.1"
REPLICATION_MODULE_VERSION = "v1.15.0"

# What Dell's CSM 1.17.0 support matrix lists. Anything outside this still works in a
# lab but is worth saying out loud.
SUPPORTED = {
    "onefs": ["9.4", "9.5", "9.7", "9.8", "9.9", "9.10", "9.11", "9.12", "9.13", "9.14"],
    "openshift": ["4.18", "4.19", "4.20", "4.21"],
    "kubernetes_min": "1.34", "kubernetes_max": "1.36",
}


def support_warnings(openshift_version: str = "", onefs_versions: list[str] | None = None) -> list[str]:
    out = []
    if openshift_version:
        major_minor = ".".join(openshift_version.split(".")[:2])
        if major_minor and major_minor not in SUPPORTED["openshift"]:
            out.append(f"OpenShift {major_minor} is outside Dell's CSM 1.17 support matrix "
                       f"({', '.join(SUPPORTED['openshift'])}).")
    for v in onefs_versions or []:
        major_minor = ".".join(v.split(".")[:2])
        if major_minor and major_minor not in SUPPORTED["onefs"]:
            out.append(f"OneFS {v} is outside Dell's CSM 1.17 support matrix "
                       f"(up to 9.14).")
    return out
DEFAULT_VALUES_FILE = DEFAULT_VALUES

# --- values -------------------------------------------------------------------
def base_values() -> dict:
    """Start from the repo's tuned values when present, else the chart defaults."""
    src = DEFAULT_VALUES_FILE if DEFAULT_VALUES_FILE.exists() else DRIVER_CHART / "values.yaml"
    return yaml.safe_load(src.read_text()) or {}


def build_values(opts: dict) -> dict:
    v = copy.deepcopy(base_values())
    v["endpointPort"] = str(opts.get("endpoint_port", 8080))
    v["skipCertificateValidation"] = bool(opts.get("skip_cert_validation", True))
    v["isiAuthType"] = int(opts.get("auth_type", 1))
    v["isiAccessZone"] = opts.get("access_zone", "System")
    v["isiPath"] = opts.get("isi_path", "/ifs/data/csi")
    v["enableQuota"] = bool(opts.get("enable_quota", True))
    v.setdefault("controller", {})
    v["controller"]["replication"] = dict(v["controller"].get("replication", {}))
    v["controller"]["replication"]["enabled"] = bool(opts.get("replication", False))
    v["controller"].setdefault("snapshot", {})["enabled"] = bool(opts.get("snapshots", True))
    if opts.get("controller_count"):
        v["controller"]["controllerCount"] = int(opts["controller_count"])
    if opts.get("log_level"):
        v["logLevel"] = opts["log_level"]
    return v


def values_file(opts: dict) -> Path:
    handle, name = tempfile.mkstemp(prefix="isilon-values-", suffix=".yaml")
    os.close(handle)                       # mkstemp hands back an open descriptor
    path = Path(name)
    path.write_text(yaml.safe_dump(build_values(opts), sort_keys=False))
    os.chmod(path, 0o600)
    return path


# --- prerequisites ------------------------------------------------------------
def ensure_namespace(job: Job, kubeconfig: str, ns: str) -> None:
    if run(kubeconfig, ["get", "ns", ns]).returncode == 0:
        job.log(f"namespace {ns} already exists")
        return
    job.log(f"creating namespace {ns}")
    proc = run(kubeconfig, ["create", "ns", ns], timeout=60)
    job.log((proc.stdout or "") + (proc.stderr or ""))
    if proc.returncode != 0:
        raise ClusterError(f"could not create namespace {ns}")


def apply_manifest(job: Job, kubeconfig: str, manifest: str, what: str) -> None:
    job.log(f"applying {what}")
    kept = manifests.save(manifest, what, job=job.id, cluster=job.cluster or "")
    if kept:
        job.log(f"  manifest written to {kept}")
    proc = run(kubeconfig, ["apply", "-f", "-"], stdin_text=manifest, timeout=180)
    job.log((proc.stdout or "") + (proc.stderr or ""))
    if proc.returncode != 0:
        raise ClusterError(f"applying {what} failed")


def create_array_secret(job: Job, kubeconfig: str, ns: str, arrays: list[dict],
                        default_array_id: str | None = None) -> None:
    """isilon-creds lists every registered array.

    Replication needs both ends in the secret on both clusters, because the driver
    resolves the remote array out of its own config. Exactly one entry per cluster
    carries isDefault, which is the array this site provisions from by default.
    """
    arrays = [dict(a) for a in arrays]
    if default_array_id:
        for a in arrays:
            a["is_default"] = a["id"] == default_array_id
    if arrays and not any(a.get("is_default") for a in arrays):
        arrays[0]["is_default"] = True
    payload = secret_yaml(arrays)
    names = ", ".join(a["name"] for a in arrays)
    job.log(f"writing secret isilon-creds in {ns} with array(s): {names}")
    secret = {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": "isilon-creds", "namespace": ns},
        "data": {"config": base64.b64encode(payload.encode()).decode()},
    }
    apply_manifest(job, kubeconfig, yaml.safe_dump(secret), "secret isilon-creds")
    certs = {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": "isilon-certs-0", "namespace": ns},
        "data": {"cert-0": ""},
    }
    apply_manifest(job, kubeconfig, yaml.safe_dump(certs), "secret isilon-certs-0")


def install_replication_crds(job: Job, kubeconfig: str) -> None:
    if not REPLICATION_CRDS.exists():
        raise ClusterError(f"replication CRDs not found at {REPLICATION_CRDS}")
    job.log("installing the CSM replication CRDs (required before the driver's replication sidecar)")
    proc = run(kubeconfig, ["apply", "-f", str(REPLICATION_CRDS)], timeout=180)
    job.log((proc.stdout or "") + (proc.stderr or ""))
    if proc.returncode != 0:
        raise ClusterError("could not apply the replication CRDs")


def snapshot_crds_present(kubeconfig: str) -> bool:
    return run(kubeconfig, ["get", "crd", "volumesnapshots.snapshot.storage.k8s.io"]).returncode == 0


# --- helm ---------------------------------------------------------------------
def helm() -> str:
    path = which("helm")
    if not path:
        raise ClusterError("helm was not found on this host")
    return path


def helm_releases(kubeconfig: str, ns: str) -> list[dict]:
    """What helm already has in this namespace, so an existing install can be adopted."""
    env = tool_env()
    env["KUBECONFIG"] = kubeconfig
    try:
        proc = subprocess.run([helm(), "list", "-n", ns, "-o", "json"], capture_output=True,
                              text=True, timeout=60, env=env)
    except (ClusterError, OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [{"name": r.get("name", ""), "chart": r.get("chart", ""),
             "app_version": r.get("app_version", ""), "status": r.get("status", ""),
             "updated": (r.get("updated") or "")[:19],
             "is_driver": "csi-isilon" in (r.get("chart") or "")} for r in rows]


def helm_values(kubeconfig: str, release: str, ns: str) -> dict:
    """The values a release was installed with, as helm reports them."""
    env = tool_env()
    env["KUBECONFIG"] = kubeconfig
    try:
        proc = subprocess.run([helm(), "get", "values", release, "-n", ns, "-o", "json"],
                              capture_output=True, text=True, timeout=60, env=env)
        return json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
    except (ClusterError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def helm_install_driver(job: Job, kubeconfig: str, ns: str, opts: dict,
                        arrays: list[dict], upgrade: bool = False,
                        default_array_id: str | None = None,
                        release: str = DRIVER_RELEASE, reuse_values: bool = False,
                        manage_secret: bool = True) -> None:
    """Install the driver, or adopt one that is already here.

    An installation this console did not create carries values and a credentials
    secret someone chose deliberately. reuse_values keeps helm's own stored values
    and changes only the switches that have to change; manage_secret=False leaves
    isilon-creds exactly as it is. Together they make it safe to enable replication
    on a driver that is already serving volumes.
    """
    if not DRIVER_CHART.exists():
        raise ClusterError(f"the csi-isilon chart is missing at {DRIVER_CHART}")
    existing = {r["name"]: r for r in helm_releases(kubeconfig, ns)}
    clashing = [r for name, r in existing.items() if r["is_driver"] and name != release]
    if clashing and not upgrade:
        names = ", ".join(r["name"] for r in clashing)
        raise ClusterError(
            f"{ns} already has the driver installed by helm as {names}. A second release "
            "of the same driver fights the first over the CSIDriver object and the node "
            "plugin socket, which are one per cluster. Install under that release name to "
            "adopt it, or remove it first.")
    if reuse_values and release not in existing:
        raise ClusterError(
            f"there is no helm release called {release} in {ns} to keep the values of. "
            "Clear that option to install fresh.")
    ensure_namespace(job, kubeconfig, ns)
    if manage_secret:
        create_array_secret(job, kubeconfig, ns, arrays, default_array_id)
    else:
        job.log("leaving the existing isilon-creds secret alone, as asked")
    if opts.get("replication"):
        install_replication_crds(job, kubeconfig)
    if opts.get("snapshots", True) and not snapshot_crds_present(kubeconfig):
        job.log("warning: volume snapshot CRDs are missing; the snapshotter sidecar needs them")

    env = tool_env()
    env["KUBECONFIG"] = kubeconfig
    vf = None
    if reuse_values:
        # keep everything this release already has and change only what must change
        job.log(f"keeping the values release {release} already has; changing only the "
                "replication switch")
        cmd = [helm(), "upgrade", release, str(DRIVER_CHART), "-n", ns, "--reuse-values",
               "--wait", "--timeout", "10m",
               "--set", f"controller.replication.enabled={str(bool(opts.get('replication'))).lower()}"]
    else:
        vf = values_file(opts)
        kept = manifests.save(vf.read_text(), f"helm values for {release}", job=job.id,
                              cluster=job.cluster or "")
        job.log(f"values written to {vf}" + (f", kept at {kept}" if kept else ""))
        job.log(yaml.safe_dump({k: v for k, v in build_values(opts).items()
                                if k in ("isiAuthType", "isiAccessZone", "isiPath", "enableQuota",
                                         "endpointPort", "skipCertificateValidation")},
                               sort_keys=False))
        cmd = [helm(), "upgrade" if upgrade else "install", release, str(DRIVER_CHART),
               "-n", ns, "-f", str(vf), "--wait", "--timeout", "10m"]
        if upgrade:
            cmd.insert(2, "--install")
        if opts.get("openshift", True):
            cmd += ["--set", "openshift=true"]
    rc = job.run(cmd, env=env)
    if vf:
        try:
            vf.unlink()
        except OSError:
            pass
    if rc != 0:
        raise ClusterError(f"helm exited {rc}")
    job.log("driver installed")


def helm_uninstall_driver(job: Job, kubeconfig: str, ns: str,
                          release: str = DRIVER_RELEASE) -> None:
    env = tool_env()
    env["KUBECONFIG"] = kubeconfig
    rc = job.run([helm(), "uninstall", release, "-n", ns], env=env)
    if rc != 0:
        raise ClusterError(f"helm uninstall exited {rc}")


REPLICATION_NS = "dell-replication-controller"


def replication_controller_state(kubeconfig: str) -> dict:
    """Is a replication controller already running, and who put it there?

    The CSM Operator installs its own copy, which carries none of Helm's ownership
    labels, so a Helm install would refuse to adopt it. Knowing the owner lets the
    wiring step leave an operator-managed controller alone.
    """
    state = {"present": False, "managed_by": "", "release": ""}
    try:
        deploy = run_json(kubeconfig, ["-n", REPLICATION_NS, "get", "deploy",
                                       "dell-replication-controller-manager"], timeout=60)
    except ClusterError:
        return state
    state["present"] = True
    meta = deploy.get("metadata", {})
    labels = meta.get("labels", {}) or {}
    annotations = meta.get("annotations", {}) or {}
    if labels.get("app.kubernetes.io/managed-by") == "Helm":
        state["managed_by"] = "helm"
        state["release"] = annotations.get("meta.helm.sh/release-name", "")
    else:
        state["managed_by"] = "operator"
    return state


def helm_install_replication_controller(job: Job, kubeconfig: str, cluster_id: str,
                                        targets: list[str]) -> None:
    """dell-replication-controller, one per cluster, aware of its own id and its peers."""
    ns = REPLICATION_NS
    install_replication_crds(job, kubeconfig)

    existing = replication_controller_state(kubeconfig)
    if existing["present"] and existing["managed_by"] == "operator":
        job.log(f"{cluster_id}: a replication controller is already running and the CSM Operator "
                "manages it, so the chart is skipped; only its configuration is set here")
        return
    if existing["present"] and existing["managed_by"] == "helm":
        job.log(f"{cluster_id}: upgrading the existing helm release "
                f"{existing['release'] or 'replication'}")

    if not REPLICATION_CHART.exists():
        raise ClusterError(f"the csm-replication chart is missing at {REPLICATION_CHART}")
    ensure_namespace(job, kubeconfig, ns)
    env = tool_env()
    env["KUBECONFIG"] = kubeconfig
    release = existing["release"] or "replication"
    cmd = [helm(), "upgrade", "--install", release, str(REPLICATION_CHART),
           "-n", ns, "--wait", "--timeout", "5m"]
    rc = job.run(cmd, env=env)
    if rc != 0:
        raise ClusterError(f"helm exited {rc}")
    cm = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "dell-replication-controller-config", "namespace": ns},
        "data": {"config.yaml": yaml.safe_dump(
            {"clusterId": cluster_id, "targets": [{"clusterId": t} for t in targets]},
            sort_keys=False)},
    }
    apply_manifest(job, kubeconfig, yaml.safe_dump(cm), "dell-replication-controller-config")
    job.log("replication controller installed")


# --- CSM operator --------------------------------------------------------------
OPERATOR_SUB = """
apiVersion: v1
kind: Namespace
metadata:
  name: {ns}
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: dell-csm-operator
  namespace: {ns}
spec:
  targetNamespaces:
    - {ns}
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: {package}
  namespace: {ns}
spec:
  channel: {channel}
  name: {package}
  source: {source}
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
"""


def operator_catalog_options(kubeconfig: str) -> list[dict]:
    """Which Dell operator packages this cluster's catalogs offer."""
    out = []
    try:
        pms = run_json(kubeconfig, ["get", "packagemanifests", "-n", "openshift-marketplace"],
                       timeout=120).get("items", [])
    except ClusterError:
        return out
    for pm in pms:
        name = pm["metadata"]["name"]
        if "dell" not in name and "csm" not in name:
            continue
        status = pm.get("status", {})
        out.append({
            "package": name,
            "catalog": status.get("catalogSource", ""),
            "default_channel": status.get("defaultChannel", ""),
            "channels": [c["name"] for c in status.get("channels", [])],
        })
    return out


def operator_install(job: Job, kubeconfig: str, package: str, channel: str,
                     source: str, ns: str = OPERATOR_NAMESPACE) -> None:
    manifest = OPERATOR_SUB.format(ns=ns, package=package, channel=channel, source=source)
    apply_manifest(job, kubeconfig, manifest, f"subscription for {package}")
    job.log("waiting for the operator to reach Succeeded")
    import time
    for _ in range(60):
        try:
            csvs = run_json(kubeconfig, ["-n", ns, "get", "csv"], timeout=60).get("items", [])
        except ClusterError:
            csvs = []
        for csv in csvs:
            phase = csv.get("status", {}).get("phase", "")
            if csv["metadata"]["name"].startswith(("dell-csm-operator", package)):
                job.log(f"  {csv['metadata']['name']}: {phase}")
                if phase == "Succeeded":
                    return
        time.sleep(10)
    raise ClusterError("the operator did not become ready in time")


def _replication_components(opts: dict) -> list[dict]:
    return [
        {"name": "dell-csi-replicator", "envs": [
            {"name": "X_CSI_REPLICATION_PREFIX", "value": "replication.storage.dell.com"},
            {"name": "X_CSI_REPLICATION_CONTEXT_PREFIX", "value": "powerscale"},
        ]},
        {"name": "dell-replication-controller-manager", "envs": [
            {"name": "TARGET_CLUSTERS_IDS",
             "value": ",".join(opts.get("target_cluster_ids", []))},
            {"name": "REPLICATION_CTRL_LOG_LEVEL", "value": "info"},
            {"name": "REPLICATION_CTRL_REPLICAS", "value": "1"},
            {"name": "RETRY_INTERVAL_MIN", "value": "1s"},
            {"name": "RETRY_INTERVAL_MAX", "value": "5m"},
            {"name": "DISABLE_PVC_REMAP", "value": "false"},
            {"name": "REPLICATION_ALLOW_PVC_CREATION_ON_TARGET", "value": "false"},
        ]},
    ]


def existing_csm(kubeconfig: str, ns: str, name: str = "isilon") -> dict:
    """The ContainerStorageModule already here, if there is one."""
    try:
        return run_json(kubeconfig, ["-n", ns, "get", "containerstoragemodule", name],
                        timeout=40)
    except ClusterError:
        return {}


def csm_state(kubeconfig: str, ns: str, name: str = "isilon") -> dict:
    """What the operator makes of the resource: its state and anything it says."""
    cr = existing_csm(kubeconfig, ns, name)
    status = cr.get("status") or {}
    messages = []
    for key in ("state", "reason", "message"):
        if status.get(key) and key != "state":
            messages.append(str(status[key]))
    for item in (status.get("conditions") or []):
        text = item.get("message") or item.get("reason") or ""
        if text:
            messages.append(f"{item.get('type', '')}: {text}")
    return {"exists": bool(cr), "state": status.get("state", ""),
            "version": (cr.get("spec") or {}).get("version", ""),
            "config_version": ((cr.get("spec") or {}).get("driver") or {}).get(
                "configVersion", ""),
            "messages": messages[:4]}


def csm_cr(ns: str, opts: dict, existing: dict | None = None) -> dict:
    """ContainerStorageModule for csi-isilon.

    spec.version names the CSM release, which the operator maps to driver v2.17.1 and
    replication v1.15.0. Setting spec.version and spec.driver.configVersion together is
    rejected by the CRD, so only one of them is ever emitted.
    """
    replication_on = bool(opts.get("replication", False))
    if existing and existing.get("spec") and not opts.get("operator_upgrade"):
        # An operator install that is already here was built by somebody, at a version
        # that operator supports. Replacing its spec with this console's defaults is how
        # a working driver ends up Failed and unchanged. Add the module, touch nothing
        # else, and leave the version alone unless an upgrade was actually asked for.
        import copy as _copy
        spec = _copy.deepcopy(existing["spec"])
        if replication_on:
            modules = list(spec.get("modules") or [])
            mine = next((m for m in modules if m.get("name") == "replication"), None)
            if mine is not None:
                # The operator writes this module into the resource itself, disabled,
                # with the version and images that go with the installed driver. Turning
                # it on is the whole change: replacing it with this console's idea of the
                # components would drop that version and pin nothing in its place.
                mine["enabled"] = True
                for component in mine.get("components") or []:
                    if component.get("name") != "dell-replication-controller-manager":
                        continue
                    envs = component.setdefault("envs", [])
                    target = ",".join(opts.get("target_cluster_ids", []))
                    for env in envs:
                        if env.get("name") == "TARGET_CLUSTERS_IDS":
                            env["value"] = target
                            break
                    else:
                        envs.append({"name": "TARGET_CLUSTERS_IDS", "value": target})
            else:
                modules.append({"name": "replication", "enabled": True,
                                "components": _replication_components(opts)})
            spec["modules"] = modules
        return {"apiVersion": existing.get("apiVersion", "storage.dell.com/v1"),
                "kind": "ContainerStorageModule",
                "metadata": {"name": existing["metadata"]["name"],
                             "namespace": existing["metadata"].get("namespace", ns)},
                "spec": spec}
    common_envs = [
        {"name": "X_CSI_ISI_ENDPOINT_PORT", "value": str(opts.get("endpoint_port", 8080))},
        {"name": "X_CSI_ISI_SKIP_CERTIFICATE_VALIDATION",
         "value": str(bool(opts.get("skip_cert_validation", True))).lower()},
        {"name": "X_CSI_ISI_AUTH_TYPE", "value": str(opts.get("auth_type", 1))},
        {"name": "X_CSI_ISI_PATH", "value": opts.get("isi_path", "/ifs/data/csi")},
        {"name": "X_CSI_ISI_ACCESS_ZONE", "value": opts.get("access_zone", "System")},
        {"name": "X_CSI_ISI_QUOTA_ENABLED",
         "value": str(bool(opts.get("enable_quota", True))).lower()},
    ]
    spec: dict = {
        "driver": {
            "csiDriverType": "isilon",
            "authSecret": "isilon-creds",
            "tlsCertSecret": "isilon-certs-0",
            "replicas": int(opts.get("controller_count", 2)),
            "forceRemoveDriver": True,
            "common": {"name": "driver", "envs": common_envs},
        },
    }
    if opts.get("config_version"):                 # advanced: pin the driver version
        spec["driver"]["configVersion"] = opts["config_version"]
    else:
        spec["version"] = CSM_VERSION
    if replication_on:
        module: dict = {"name": "replication", "enabled": True,
                        "components": _replication_components(opts)}
        if opts.get("config_version"):
            module["configVersion"] = REPLICATION_MODULE_VERSION
        spec["modules"] = [module]
    return {
        "apiVersion": "storage.dell.com/v1", "kind": "ContainerStorageModule",
        "metadata": {"name": "isilon", "namespace": ns}, "spec": spec,
    }


def operator_apply_cr(job: Job, kubeconfig: str, ns: str, opts: dict, arrays: list[dict],
                      default_array_id: str | None = None,
                      manage_secret: bool = True) -> None:
    ensure_namespace(job, kubeconfig, ns)
    if manage_secret:
        create_array_secret(job, kubeconfig, ns, arrays, default_array_id)
    else:
        job.log("leaving the existing isilon-creds secret alone, as asked")
    if opts.get("replication"):
        install_replication_crds(job, kubeconfig)

    existing = existing_csm(kubeconfig, ns)
    if existing:
        before = csm_state(kubeconfig, ns)
        job.log(f"a ContainerStorageModule is already here: version "
                f"{before['version'] or before['config_version'] or 'unset'}, state "
                f"{before['state'] or 'unknown'}")
        if opts.get("operator_upgrade"):
            job.log(f"it will be moved to {CSM_VERSION}, as asked. If this operator does "
                    "not support that version it will report Failed and leave the driver "
                    "as it is; upgrade the operator first in that case")
        else:
            job.log("keeping its version and settings; only the replication module is "
                    "added. Tick the upgrade box to move it to this console's version")
    cr = csm_cr(ns, opts, existing=existing)
    apply_manifest(job, kubeconfig, yaml.safe_dump(cr), "ContainerStorageModule isilon")

    # the operator answers in its own time, and a failure here is otherwise silent
    job.log("waiting for the operator to say what it made of that")
    import time as _time
    last = ""
    for _ in range(20):
        _time.sleep(6)
        state = csm_state(kubeconfig, ns)
        line = f"  state {state['state'] or 'unknown'}"
        if line != last:
            job.log(line)
            last = line
        if (state["state"] or "").lower() in ("succeeded", "running"):
            job.log("the operator accepted it")
            break
        if (state["state"] or "").lower() == "failed":
            for message in state["messages"]:
                job.log(f"  the operator says: {message[:220]}")
            raise ClusterError(
                "the operator could not reconcile the resource, so the driver is "
                "unchanged. The usual cause is a version this operator does not know: "
                "check the operator's own version against the one in the resource.")
    else:
        job.log("it has not settled yet; watch the driver pods on the dashboard")


# --- storage classes ------------------------------------------------------------
def storage_class_manifest(name: str, array: dict, opts: dict,
                           replication: dict | None = None) -> dict:
    params = {
        "AccessZone": opts.get("access_zone", "System"),
        "IsiPath": opts.get("isi_path", "/ifs/data/csi"),
        "ClusterName": array["name"],
        "AzServiceIP": opts.get("az_service_ip") or array["endpoint"],
        "RootClientEnabled": str(bool(opts.get("root_client_enabled", False))).lower(),
        "csi.storage.k8s.io/fstype": "nfs",
    }
    if replication:
        rpo = replication.get("rpo", "Five_Minutes")
        if rpo not in RPO_VALUES:
            raise ClusterError(f"RPO {rpo} is not one of {', '.join(RPO_VALUES)}")
        for key in ("remote_storage_class", "remote_cluster_id", "remote_system"):
            if not replication.get(key):
                raise ClusterError(f"replicated storage classes need {key}")
        prefix = "replication.storage.dell.com"
        params.update({
            f"{prefix}/isReplicationEnabled": "true",
            f"{prefix}/remoteStorageClassName": replication["remote_storage_class"],
            f"{prefix}/remoteClusterID": replication["remote_cluster_id"],
            f"{prefix}/remoteSystem": replication["remote_system"],
            f"{prefix}/rpo": rpo,
            f"{prefix}/ignoreNamespaces": str(bool(replication.get("ignore_namespaces", False))).lower(),
            f"{prefix}/volumeGroupPrefix": replication.get("volume_group_prefix", "csi-prod"),
            f"{prefix}/remoteAccessZone": replication.get("remote_access_zone",
                                                          opts.get("access_zone", "System")),
            f"{prefix}/remoteAzServiceIP": replication["remote_az_service_ip"],
            f"{prefix}/remoteRootClientEnabled":
                str(bool(opts.get("root_client_enabled", False))).lower(),
        })
    return {
        "apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
        "metadata": {"name": name},
        "provisioner": "csi-isilon.dellemc.com",
        "reclaimPolicy": opts.get("reclaim_policy", "Delete"),
        "allowVolumeExpansion": True,
        "volumeBindingMode": opts.get("binding_mode", "Immediate"),
        "parameters": params,
    }


def volume_group_budget(prefix: str, namespace: str, endpoint: str, rpo: str) -> tuple[int, bool]:
    """OneFS rejects SyncIQ policy names over 63 characters; the driver itself only
    truncates at 128, so check the real budget here."""
    name = f"{prefix}-{namespace}-{endpoint}-{rpo}"
    return len(name), len(name) <= 63


def apply_storage_class(job: Job, kubeconfig: str, manifest: dict) -> None:
    apply_manifest(job, kubeconfig, yaml.safe_dump(manifest),
                   f"storage class {manifest['metadata']['name']}")
