"""Match what the clusters hold against what the arrays hold.

A replicated volume leaves traces in four places: a claim and its volume in
Kubernetes, a DellCSIReplicationGroup, a SyncIQ policy on each array, and a
directory under the base path. They are created together and they do not always
go away together. A project deleted in OpenShift can leave a policy that the
driver then keeps recreating, or a directory nobody will ever look in again.

Everything here reads first and acts only when asked, one item at a time.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .. import store
from .arrays import ArrayError, client_for
from .jobs import Job
from .k8s import ClusterError, run, run_json

RPO_SUFFIXES = ("Five_Minutes", "Fifteen_Minutes", "Thirty_Minutes", "One_Hour",
                "Six_Hours", "Twelve_Hours", "One_Day")


def key_of(name: str) -> str:
    """Policies may not carry dots; the directory they copy does. Compare on one form."""
    return (name or "").strip("/").replace(".", "-").lower()


def volume_group_of(protection_group: str) -> str:
    path = (protection_group or "").split("::", 1)[-1]
    return path.rstrip("/").rsplit("/", 1)[-1]


# --- reading ----------------------------------------------------------------------
def _cluster_facts(cluster: dict) -> dict:
    kc = cluster.get("kubeconfig", "")
    out = {"id": cluster["id"], "reachable": False, "groups": [], "volumes": [], "error": ""}
    try:
        items = run_json(kc, ["get", "dellcsireplicationgroups"], timeout=60).get("items", [])
        out["reachable"] = True
    except ClusterError as exc:
        out["error"] = str(exc)[:200]
        return out
    for rg in items:
        spec, status = rg.get("spec", {}), rg.get("status", {})
        out["groups"].append({
            "cluster": cluster["id"], "name": rg["metadata"]["name"],
            "protection_group": spec.get("protectionGroupId", ""),
            "remote_protection_group": spec.get("remoteProtectionGroupId", ""),
            "remote_cluster": spec.get("remoteClusterId", ""),
            "link": (status.get("replicationLinkState") or {}).get("state", ""),
            "is_source": (status.get("replicationLinkState") or {}).get("isSource"),
            "finalizers": rg["metadata"].get("finalizers") or [],
            "deleting": bool(rg["metadata"].get("deletionTimestamp")),
            "vg": volume_group_of(spec.get("protectionGroupId", "")),
        })
    try:
        pvs = run_json(kc, ["get", "pv"], timeout=60).get("items", [])
    except ClusterError:
        pvs = []
    label = "replication.storage.dell.com/replicationGroupName"
    for pv in pvs:
        csi = pv.get("spec", {}).get("csi") or {}
        if "isilon" not in csi.get("driver", ""):
            continue
        claim = pv.get("spec", {}).get("claimRef") or {}
        out["volumes"].append({
            "cluster": cluster["id"], "pv": pv["metadata"]["name"],
            "handle": csi.get("volumeHandle", ""),
            "namespace": claim.get("namespace", ""), "claim": claim.get("name", ""),
            "phase": pv.get("status", {}).get("phase", ""),
            "size": (pv.get("spec", {}).get("capacity") or {}).get("storage", ""),
            "storage_class": pv.get("spec", {}).get("storageClassName", ""),
            "group": (pv["metadata"].get("labels") or {}).get(label, ""),
            "path": (csi.get("volumeAttributes") or {}).get("Path", ""),
            "reclaim": pv.get("spec", {}).get("persistentVolumeReclaimPolicy", ""),
        })
    return out


def _array_facts(array: dict) -> dict:
    out = {"id": array["id"], "name": array["name"], "endpoint": array["endpoint"],
           "reachable": False, "policies": [], "targets": [], "dirs": [], "error": "",
           "base": array.get("isi_path", "/ifs/data/csi")}
    try:
        client = client_for(array)
        out["policies"] = client.sync_policies()
        out["targets"] = client.sync_target_policies()
        out["reachable"] = True
    except (ArrayError, Exception) as exc:                   # noqa: BLE001
        out["error"] = str(exc)[:200]
        return out
    try:
        out["dirs"] = [d for d in client.directories(out["base"], depth=1)]
    except (ArrayError, Exception):                          # noqa: BLE001
        pass
    return out


def survey() -> dict:
    state = store.load()
    clusters = list(state["clusters"].values())
    arrays = list(state["arrays"].values())
    with ThreadPoolExecutor(max_workers=6) as pool:
        cluster_facts = list(pool.map(_cluster_facts, clusters))
        array_facts = list(pool.map(_array_facts, arrays))
    return {"clusters": cluster_facts, "arrays": array_facts,
            "reachable": all(c["reachable"] for c in cluster_facts)
            and all(a["reachable"] for a in array_facts)}


# --- the volume map -----------------------------------------------------------------
def volume_map(facts: dict | None = None) -> list[dict]:
    """One row per volume: claim, volume, group, policy, and the far side."""
    facts = facts or survey()
    groups = {g["name"]: g for c in facts["clusters"] for g in c["groups"]}
    rows = []
    for cluster in facts["clusters"]:
        for volume in cluster["volumes"]:
            group = groups.get(volume["group"], {})
            vg = group.get("vg", "")
            policy, policy_state, policy_array = "", "", ""
            for array in facts["arrays"]:
                for candidate in array["policies"]:
                    if key_of(candidate.get("name", "")) == key_of(vg):
                        policy = candidate.get("name", "")
                        policy_state = candidate.get("last_job_state", "")
                        policy_array = array["name"]
            rows.append({
                "cluster": volume["cluster"], "namespace": volume["namespace"],
                "claim": volume["claim"], "pv": volume["pv"], "size": volume["size"],
                "phase": volume["phase"], "storage_class": volume["storage_class"],
                "path": volume["path"], "group": volume["group"], "vg": vg,
                "replicated": bool(volume["group"]),
                "link": group.get("link", ""), "is_source": group.get("is_source"),
                "here": group.get("protection_group", ""),
                "far": group.get("remote_protection_group", ""),
                "remote_cluster": group.get("remote_cluster", ""),
                "policy": policy, "policy_state": policy_state, "policy_array": policy_array,
                "reclaim": volume["reclaim"],
            })
    rows.sort(key=lambda r: (r["namespace"], r["claim"], r["cluster"]))
    return rows


# --- orphans --------------------------------------------------------------------------
def orphans(facts: dict | None = None) -> dict:
    """What exists on one side and not the other."""
    facts = facts or survey()
    group_keys = {key_of(g["vg"]) for c in facts["clusters"] for g in c["groups"] if g["vg"]}
    volume_groups = {key_of(v["group"]) for c in facts["clusters"] for v in c["volumes"]
                     if v["group"]}
    found: list[dict] = []

    for array in facts["arrays"]:
        if not array["reachable"]:
            continue
        policy_keys = {key_of(p.get("name", "")) for p in array["policies"]}
        for policy in array["policies"]:
            name = policy.get("name", "")
            if key_of(name) in group_keys:
                continue
            found.append({
                "kind": "policy", "where": array["name"], "array": array["id"],
                "name": name, "detail": f"source policy for {policy.get('source') or '?'}"
                f" → {policy.get('target_host','?')}, last job "
                f"{policy.get('last_job_state') or 'never run'}",
                "why": "no replication group in any registered cluster matches it",
                "danger": "the driver recreates a policy while its group exists; with no "
                          "group, this one is left over"})
        for target in array["targets"]:
            name = target.get("name") or target.get("id", "")
            if key_of(name) in group_keys or key_of(name) in policy_keys:
                continue
            found.append({
                "kind": "target", "where": array["name"], "array": array["id"],
                "name": name,
                "detail": f"incoming from {target.get('source_host','?')}, "
                          f"{target.get('target_path','')}",
                "why": "an association with no policy and no group behind it",
                "danger": "breaking it only removes the association, not the data"})
        for directory in array["dirs"]:
            leaf = directory.rstrip("/").rsplit("/", 1)[-1]
            if not any(leaf.endswith(suffix) for suffix in RPO_SUFFIXES):
                continue                                     # not a volume group folder
            if key_of(leaf) in group_keys or key_of(leaf) in policy_keys \
                    or key_of(leaf) in volume_groups:
                continue
            found.append({
                "kind": "directory", "where": array["name"], "array": array["id"],
                "name": directory, "detail": "volume group directory",
                "why": "no group, no policy and no volume refer to it",
                "danger": "it is only removed when empty"})

    policy_keys_all = {key_of(p.get("name", "")) for a in facts["arrays"]
                       for p in a["policies"]}
    for cluster in facts["clusters"]:
        for group in cluster["groups"]:
            if key_of(group["vg"]) in policy_keys_all:
                continue
            found.append({
                "kind": "group", "where": cluster["id"], "cluster": cluster["id"],
                "name": group["name"],
                "detail": f"{group['protection_group'] or 'no protection group'}, link "
                          f"{group['link'] or 'unknown'}"
                          + (", being deleted" if group["deleting"] else ""),
                "why": "no SyncIQ policy on any registered array matches it",
                "danger": "a group whose finalizer cannot complete blocks the namespace "
                          "from being removed"})
    return {"items": found,
            "counts": {kind: sum(1 for f in found if f["kind"] == kind)
                       for kind in ("policy", "target", "directory", "group")},
            "clean": not found}


# --- acting, one thing at a time -------------------------------------------------
def _remove_group(job: Job, cluster: dict, name: str) -> bool:
    """Delete a replication group, and release it if its finalizer cannot finish."""
    kc = cluster["kubeconfig"]
    job.log(f"{cluster['id']}: deleting replication group {name}")
    proc = run(kc, ["delete", "dellcsireplicationgroup", name, "--wait=false"], timeout=60)
    job.log("  " + ((proc.stdout or proc.stderr).strip() or "requested"))
    import time as _time
    for _ in range(6):
        _time.sleep(3)
        check = run(kc, ["get", "dellcsireplicationgroup", name], timeout=30)
        if check.returncode != 0:
            job.log("  gone")
            return True
    job.log("  still there: the controller cannot finish its finalizer, releasing it")
    run(kc, ["patch", "dellcsireplicationgroup", name, "--type=merge",
             "-p", '{"metadata":{"finalizers":[]}}'], timeout=30)
    _time.sleep(2)
    gone = run(kc, ["get", "dellcsireplicationgroup", name], timeout=30).returncode != 0
    job.log("  gone" if gone else "  it is still there; look at it by hand")
    return gone


def _remove_policy(job: Job, array: dict, name: str) -> bool:
    client = client_for(array)
    stopped = client.cancel_jobs(name)
    if stopped:
        job.log(f"  cancelled a running job for {name}")
    ok, why = client.delete_policy(name, force=True)
    if ok:
        job.log(f"{array['name']}: policy {name} deleted")
        return True
    job.log(f"{array['name']}: {why}")
    job.log("  trying again without touching the far side (local only)")
    ok, why = client.delete_policy(name, force=True, local_only=True)
    job.log(f"{array['name']}: policy {name} " + ("deleted locally" if ok else why))
    return ok


def clean_group(job: Job, name: str, drop_directory: bool = True) -> dict:
    """Take one replication group apart, in the order that works.

    The group goes first: while it exists the driver puts the policy back. Then the
    policies, then the association the far array still holds, then the directory if
    it is empty. Volumes are never deleted here; they are reported instead.
    """
    facts = survey()
    state = store.load()
    result = {"group": name, "removed": [], "left": []}
    groups = [g for c in facts["clusters"] for g in c["groups"] if g["name"] == name]
    if not groups:
        job.log(f"no replication group called {name} in any registered cluster")
    vg = next((g["vg"] for g in groups if g["vg"]), "")
    if not vg:
        # the group may be gone already; fall back to the name the caller gave us
        vg = name
    job.log(f"volume group: {vg or 'unknown'}")

    still_bound = [v for c in facts["clusters"] for v in c["volumes"]
                   if v["group"] == name and v["phase"] == "Bound"]
    if still_bound:
        for volume in still_bound:
            job.log(f"warning: {volume['cluster']} still has {volume['namespace']}/"
                    f"{volume['claim']} bound to this group")
        result["left"] = [f"{v['namespace']}/{v['claim']}" for v in still_bound]
        job.log("those volumes are left alone: delete the claims first if they should go")

    for group in groups:
        cluster = state["clusters"].get(group["cluster"])
        if cluster and _remove_group(job, cluster, group["name"]):
            result["removed"].append(f"group {group['name']} on {group['cluster']}")

    for array in facts["arrays"]:
        if not array["reachable"]:
            job.log(f"{array['name']} is unreachable; its side is untouched")
            continue
        record = state["arrays"][array["id"]]
        for policy in array["policies"]:
            if key_of(policy.get("name", "")) != key_of(vg):
                continue
            if _remove_policy(job, record, policy["name"]):
                result["removed"].append(f"policy {policy['name']} on {array['name']}")
        for target in array["targets"]:
            target_name = target.get("name") or target.get("id", "")
            if key_of(target_name) != key_of(vg):
                continue
            ok, why = client_for(record).break_target(target_name)
            job.log(f"{array['name']}: target association {target_name} "
                    + ("broken" if ok else why))
            if ok:
                result["removed"].append(f"target {target_name} on {array['name']}")
        if drop_directory:
            for directory in array["dirs"]:
                if key_of(directory.rstrip("/").rsplit("/", 1)[-1]) != key_of(vg):
                    continue
                ok, why = client_for(record).remove_directory(directory)
                job.log(f"{array['name']}: directory {directory} "
                        + ("removed" if ok else "kept, " + why))
                if ok:
                    result["removed"].append(f"directory {directory} on {array['name']}")
    if not result["removed"]:
        job.log("nothing was removed")
    return result


def clean_item(job: Job, kind: str, where: str, name: str) -> bool:
    """Remove one orphan, named exactly as the list shows it."""
    state = store.load()
    if kind == "group":
        cluster = state["clusters"].get(where)
        if not cluster:
            raise ClusterError(f"unknown cluster {where}")
        return _remove_group(job, cluster, name)
    array = state["arrays"].get(where)
    if not array:
        raise ArrayError(f"unknown array {where}")
    if kind == "policy":
        return _remove_policy(job, array, name)
    if kind == "target":
        ok, why = client_for(array).break_target(name)
        job.log(f"{array['name']}: target association {name} "
                + ("broken" if ok else why))
        return ok
    if kind == "directory":
        ok, why = client_for(array).remove_directory(name)
        job.log(f"{array['name']}: directory {name} " + ("removed" if ok else "kept, " + why))
        return ok
    raise ArrayError(f"nothing here removes a {kind}")
