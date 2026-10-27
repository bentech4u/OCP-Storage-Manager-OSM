"""Repair a ContainerStorageModule that was overwritten, or is being refused.

The CSM Operator keeps a copy of the resource as it was before the last change, in
the annotation storage.dell.com/PreviouslyAppliedConfiguration. That copy is what
makes a bad change recoverable: put it back and the operator is working from the
settings it had, at a version it understands.

Two details decide whether the copy can be re-applied as it stands. The operator
fills in the image fields it derives from configVersion, and the schema then refuses
them on the way back, so those are dropped. And the module list in that copy is the
operator's own, including the replication module at the version matching the driver:
enabling replication means turning that one on, not substituting another.
"""
from __future__ import annotations

import copy
import json
import time

import yaml

from . import manifests
from .jobs import Job
from .k8s import ClusterError, run, run_json

NAME = "isilon"
SAVED = "storage.dell.com/PreviouslyAppliedConfiguration"
CONTROLLER_NS = "dell-replication-controller"


def _get(kubeconfig: str, ns: str, name: str = NAME) -> dict:
    try:
        return run_json(kubeconfig, ["-n", ns, "get", "containerstoragemodule", name],
                        timeout=40)
    except ClusterError:
        return {}


def strip_images(spec: dict) -> list[str]:
    """Remove what the operator derives from configVersion and then refuses back."""
    dropped = []
    driver = spec.get("driver") or {}
    if (driver.get("common") or {}).pop("image", None):
        dropped.append("driver.common")
    for car in driver.get("sideCars") or []:
        if car.pop("image", None):
            dropped.append("sidecar " + str(car.get("name", "?")))
    for module in spec.get("modules") or []:
        for component in module.get("components") or []:
            if component.pop("image", None):
                dropped.append(f"{module.get('name')}/{component.get('name')}")
    return dropped


def _summary(spec: dict) -> dict:
    driver = spec.get("driver") or {}
    envs = (driver.get("common") or {}).get("envs") or []
    return {
        "version": spec.get("version", ""),
        "config_version": driver.get("configVersion", ""),
        "isi_path": next((e.get("value") for e in envs
                          if e.get("name") == "X_CSI_ISI_PATH"), ""),
        "modules": {m.get("name"): bool(m.get("enabled")) for m in spec.get("modules") or []},
    }


def inspect(kubeconfig: str, ns: str, name: str = NAME) -> dict:
    """What the resource says, what the operator made of it, and what can be done."""
    cr = _get(kubeconfig, ns, name)
    if not cr:
        return {"exists": False}
    status = cr.get("status") or {}
    spec = cr.get("spec") or {}
    out = {"exists": True, "name": name, "namespace": ns,
           "state": status.get("state", ""), "saved_copy": SAVED in
           (cr["metadata"].get("annotations") or {}),
           "messages": [c.get("message") or c.get("reason") or ""
                        for c in (status.get("conditions") or [])][:3]}
    out.update(_summary(spec))
    try:
        deploy = run_json(kubeconfig, ["-n", ns, "get", "deploy", f"{name}-controller"],
                          timeout=30)
        containers = [c.get("name") for c in
                      deploy["spec"]["template"]["spec"]["containers"]]
        out["containers"] = containers
        out["sidecar"] = "dell-csi-replicator" in containers
        out["image"] = next((c.get("image") for c in
                             deploy["spec"]["template"]["spec"]["containers"]
                             if c.get("name") == "driver"), "")
    except (ClusterError, KeyError):
        out["containers"], out["sidecar"], out["image"] = [], False, ""
    return out


def restore_plan(kubeconfig: str, ns: str, name: str = NAME) -> dict:
    """The resource as the operator last had it, ready to apply."""
    cr = _get(kubeconfig, ns, name)
    if not cr:
        raise ClusterError(f"there is no ContainerStorageModule called {name} in {ns}")
    saved = (cr["metadata"].get("annotations") or {}).get(SAVED)
    if not saved:
        raise ClusterError(
            "the operator has kept no copy of an earlier resource on this cluster, so "
            "there is nothing to put back. Rebuild the spec by hand instead")
    try:
        old = json.loads(saved)
    except json.JSONDecodeError as exc:
        raise ClusterError(f"the operator's copy cannot be read: {exc}") from exc
    spec = copy.deepcopy(old.get("spec") or {})
    dropped = strip_images(spec)
    manifest = {"apiVersion": old.get("apiVersion", "storage.dell.com/v1"),
                "kind": "ContainerStorageModule",
                "metadata": {"name": old["metadata"]["name"],
                             "namespace": old["metadata"].get("namespace", ns)},
                "spec": spec}
    return {"manifest": manifest, "yaml": yaml.safe_dump(manifest, sort_keys=False),
            "dropped": dropped, "was": _summary(cr.get("spec") or {}),
            "becomes": _summary(spec)}


def wait_for_state(job: Job, kubeconfig: str, ns: str, name: str = NAME,
                   seconds: int = 180) -> str:
    job.log("waiting for the operator")
    last = ""
    for _ in range(max(1, seconds // 6)):
        time.sleep(6)
        state = (_get(kubeconfig, ns, name).get("status") or {}).get("state", "")
        if state != last:
            job.log(f"  state {state or 'unknown'}")
            last = state
        if state.lower() in ("succeeded", "running"):
            return state
        if state.lower() == "failed":
            return state
    return last


def restore(job: Job, kubeconfig: str, ns: str, name: str = NAME) -> dict:
    """Put the operator's own copy back, keeping what is there now."""
    plan = restore_plan(kubeconfig, ns, name)
    current = _get(kubeconfig, ns, name)
    kept = manifests.save(yaml.safe_dump(current, sort_keys=False),
                          f"containerstoragemodule {name} before repair", job=job.id,
                          cluster=job.cluster or "")
    if kept:
        job.log(f"what is there now was written to {kept}")
    if plan["dropped"]:
        job.log(f"left out {len(plan['dropped'])} image field(s) the operator fills in "
                "from configVersion")
    job.log(f"putting back: configVersion {plan['becomes']['config_version'] or 'unset'}, "
            f"path {plan['becomes']['isi_path'] or 'unset'}, modules "
            + ", ".join(f"{k}={v}" for k, v in plan["becomes"]["modules"].items()))
    proc = run(kubeconfig, ["apply", "-f", "-"], stdin_text=plan["yaml"], timeout=120)
    job.log((proc.stdout or "") + (proc.stderr or ""))
    if proc.returncode != 0:
        raise ClusterError("the cluster refused the restored resource")
    state = wait_for_state(job, kubeconfig, ns, name)
    if state.lower() == "failed":
        raise ClusterError(
            "the operator still reports Failed. Its own log will say why; a version it "
            "does not know, or images it cannot pull, are the usual two")
    return {"state": state, "backup": str(kept) if kept else ""}


def enable_replication(job: Job, kubeconfig: str, ns: str, peer: str,
                       name: str = NAME) -> dict:
    """Turn on the replication module the operator already wrote into the resource."""
    from .tools import which
    helm = which("helm")
    if helm:
        import subprocess
        from ..config import tool_env
        env = tool_env()
        env["KUBECONFIG"] = kubeconfig
        proc = subprocess.run([helm, "list", "-n", CONTROLLER_NS, "-q"],
                              capture_output=True, text=True, timeout=60, env=env)
        releases = [r for r in (proc.stdout or "").split() if r.strip()]
        if releases:
            raise ClusterError(
                f"helm has {', '.join(releases)} installed in {CONTROLLER_NS}. The "
                "operator wants to manage that namespace too and the two will fight: "
                "remove the release first, then do this again. Expect to re-inject the "
                "peer credentials afterwards, because the service account is replaced")
    cr = _get(kubeconfig, ns, name)
    if not cr:
        raise ClusterError(f"there is no ContainerStorageModule called {name} in {ns}")
    spec = copy.deepcopy(cr.get("spec") or {})
    if spec.get("version"):
        raise ClusterError(
            "this resource still carries spec.version, which is what the operator "
            "refused. Restore it first")
    module = next((m for m in spec.get("modules") or []
                   if m.get("name") == "replication"), None)
    if module is None:
        raise ClusterError(
            "this resource has no replication module for the operator to enable. That "
            "usually means the installed operator does not offer one for this driver "
            "version")
    if spec.get("driver", {}).get("configVersion"):
        strip_images(spec)
    module["enabled"] = True
    for component in module.get("components") or []:
        if component.get("name") != "dell-replication-controller-manager":
            continue
        envs = component.setdefault("envs", [])
        for env_entry in envs:
            if env_entry.get("name") == "TARGET_CLUSTERS_IDS":
                env_entry["value"] = peer
                break
        else:
            envs.append({"name": "TARGET_CLUSTERS_IDS", "value": peer})
    manifest = {"apiVersion": cr.get("apiVersion", "storage.dell.com/v1"),
                "kind": "ContainerStorageModule",
                "metadata": {"name": name, "namespace": ns}, "spec": spec}
    body = yaml.safe_dump(manifest, sort_keys=False)
    kept = manifests.save(body, f"containerstoragemodule {name} with replication",
                          job=job.id, cluster=job.cluster or "")
    job.log(f"enabling the replication module at configVersion "
            f"{module.get('configVersion', '(the operator default)')}, target {peer}")
    if kept:
        job.log(f"what is being applied was written to {kept}")
    proc = run(kubeconfig, ["apply", "-f", "-"], stdin_text=body, timeout=120)
    job.log((proc.stdout or "") + (proc.stderr or ""))
    if proc.returncode != 0:
        raise ClusterError("the cluster refused the change")
    state = wait_for_state(job, kubeconfig, ns, name)
    after = inspect(kubeconfig, ns, name)
    job.log("containers now: " + ", ".join(after.get("containers") or []))
    if not after.get("sidecar"):
        job.log("the sidecar is not there yet; the operator may still be rolling the "
                "deployment, so look again in a minute")
    return {"state": state, "sidecar": after.get("sidecar", False)}
