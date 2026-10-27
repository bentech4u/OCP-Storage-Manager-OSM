"""Can this pair take replication, and what would it cost to get there.

A cluster that has been serving volumes for months is not a blank site. The driver
may predate the replication module, it may have been installed by a method you no
longer use, its secret may know only the local array, and every volume it already
serves was provisioned from a class that has no replication in it. None of that
stops replication being added, but all of it decides the order of the work.

This reads both sides and says: what blocks it, what has to be done, and what to
expect afterwards. It changes nothing.
"""
from __future__ import annotations

from .. import store
from .arrays import ArrayError, client_for
from .installer import DRIVER_VERSION, REPLICATION_MODULE_VERSION
from .prechecks import installed_report


def _version_tuple(text: str) -> tuple:
    cleaned = (text or "").lstrip("v").split("-")[0]
    parts = []
    for piece in cleaned.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            break
    return tuple(parts)


def _finding(level: str, what: str, detail: str, work: str = "") -> dict:
    return {"level": level, "what": what, "detail": detail, "work": work}


def assess(source_id: str, target_id: str) -> dict:
    """Everything that stands between these two clusters and a replicated pair."""
    state = store.load()
    source = state["clusters"].get(source_id)
    target = state["clusters"].get(target_id)
    arrays = list(state["arrays"].values())
    if not source or not target:
        return {"error": "both clusters have to be registered here", "findings": [],
                "plan": []}
    if source_id == target_id:
        return {"error": "a pair needs two different clusters", "findings": [], "plan": []}

    here = installed_report(source)
    there = installed_report(target)
    findings: list[dict] = []

    for report, cluster in ((here, source_id), (there, target_id)):
        if not report["reachable"]:
            findings.append(_finding("blocker", f"{cluster} cannot be reached",
                                     "nothing can be assessed or installed there",
                                     "fix access to the API server first"))
    if any(f["level"] == "blocker" for f in findings):
        return {"findings": findings, "plan": ["Reach both clusters, then assess again."],
                "counts": {"blocker": len(findings), "work": 0, "note": 0}, "ready": False,
                "here": here, "there": there, "source": source_id, "target": target_id}

    # --- the driver itself ---------------------------------------------------------
    shipped = _version_tuple(DRIVER_VERSION)
    for report, cluster in ((here, source_id), (there, target_id)):
        version = report["driver_version"]
        if not version:
            findings.append(_finding(
                "blocker", f"{cluster} has no PowerScale driver",
                f"nothing matching csi-isilon is running in {report['namespace']}",
                "install the driver there before pairing the sites"))
            continue
        running = _version_tuple(version)
        if running and running < shipped:
            findings.append(_finding(
                "work", f"{cluster} runs an older driver than this console ships",
                f"{version} installed, {DRIVER_VERSION} here with replication module "
                f"{REPLICATION_MODULE_VERSION}",
                "Dell pairs each driver release with one replication module version, so "
                "upgrade the driver as part of enabling replication rather than mixing "
                "the two"))
        elif running and running > shipped:
            findings.append(_finding(
                "note", f"{cluster} runs a newer driver than this console ships",
                f"{version} installed, {DRIVER_VERSION} here",
                "enable replication with whatever installed it, and keep this console for "
                "the wiring, classes and operations"))

    if here["driver_version"] and there["driver_version"] \
            and here["driver_version"] != there["driver_version"]:
        findings.append(_finding(
            "work", "the two sites run different driver versions",
            f"{source_id} {here['driver_version']}, {target_id} {there['driver_version']}",
            "level them before the first failover: the far side has to understand the "
            "group it is handed"))

    method_here = "the operator" if here["operator"] else ("helm" if here["helm"] else "")
    method_there = "the operator" if there["operator"] else ("helm" if there["helm"] else "")
    for report, cluster, method in ((here, source_id, method_here),
                                    (there, target_id, method_there)):
        if not method:
            findings.append(_finding(
                "note", f"{cluster}: neither helm nor the operator claims the driver",
                "it was installed another way, or the release lives in another namespace",
                "find out what owns it before changing it; a second install beside it "
                "fights the first over the CSIDriver object and the node plugin socket"))
    if method_here and method_there and method_here != method_there:
        findings.append(_finding(
            "note", "the two sites were installed differently",
            f"{source_id} by {method_here}, {target_id} by {method_there}",
            "replication can still be enabled on both, but each needs its own method"))

    # an operator that cannot reconcile its own resource changes nothing, quietly
    for report, cluster in ((here, source_id), (there, target_id)):
        for item in report["operator"]:
            if (item.get("state") or "").lower() not in ("succeeded", "running", ""):
                findings.append(_finding(
                    "blocker", f"{cluster}: the operator rejected its own resource",
                    f"ContainerStorageModule {item['name']} is {item['state']} at version "
                    f"{item.get('version') or 'unset'}",
                    "while it is in that state the operator changes nothing, so enabling "
                    "replication has no effect. The usual cause is a version the installed "
                    "operator does not know: compare the operator's version with the one "
                    "in the resource"))

    # --- what replication adds -----------------------------------------------------
    for report, cluster in ((here, source_id), (there, target_id)):
        if not report["replicator"]:
            findings.append(_finding(
                "work", f"{cluster} has no replication sidecar",
                "the driver is running without dell-csi-replicator",
                "enable replication on the driver: a values change with helm, or the "
                "replication module in the ContainerStorageModule with the operator"))
        if not report["replication_controller"]:
            findings.append(_finding(
                "work", f"{cluster} has no replication controller",
                "nothing is running in dell-replication-controller",
                "install the controller and its CRDs; the CRDs must exist before the "
                "sidecar starts, or it crash-loops"))

    # --- the secret has to know both arrays ----------------------------------------
    names = {a["name"] for a in arrays}
    for report, cluster in ((here, source_id), (there, target_id)):
        listed = set(report["secret_arrays"])
        if listed and not names.issubset(listed):
            findings.append(_finding(
                "work", f"{cluster}: isilon-creds knows only part of the estate",
                "it lists " + (", ".join(sorted(listed)) or "nothing")
                + f"; a pair needs {', '.join(sorted(names))}",
                "rewrite the secret with both arrays on both clusters: the driver resolves "
                "the far array out of its own config, and the controller pods pick the new "
                "secret up when they restart"))

    # --- volumes that already exist ------------------------------------------------
    plain = [c for c in here["classes"] if not c["replicated"]]
    if here["volumes"]:
        findings.append(_finding(
            "note", f"{source_id} already serves {here['volumes']} volume(s)",
            "they were provisioned from "
            + (", ".join(c["name"] for c in plain) or "a class with no replication")
            + ", and enabling replication does not reach back for them",
            "a volume is protected by the class it was created from. Existing claims stay "
            "unprotected until their data is moved onto a claim from a replicated class"))
    if not any(c["replicated"] for c in here["classes"]):
        findings.append(_finding(
            "work", f"{source_id} has no replicated storage class",
            "every class there provisions without replication",
            "create a replicated pair, which this console writes on both clusters at once"))

    if here["namespace"] != there["namespace"]:
        findings.append(_finding(
            "note", "the driver sits in different namespaces on the two sites",
            f"{source_id} {here['namespace']}, {target_id} {there['namespace']}",
            "allowed; just read each side in its own namespace"))
    if not here["snapshot_crds"] or not there["snapshot_crds"]:
        missing = [c for c, r in ((source_id, here), (target_id, there))
                   if not r["snapshot_crds"]]
        findings.append(_finding(
            "note", "volume snapshot CRDs are missing on " + ", ".join(missing),
            "replication does not need them, the snapshotter sidecar does",
            "install them if snapshots are wanted, or the sidecar will crash-loop"))

    # --- the arrays ------------------------------------------------------------------
    for array in arrays:
        try:
            client = client_for(array)
            info = client.inspect()
        except (ArrayError, Exception) as exc:                # noqa: BLE001
            findings.append(_finding("blocker", f"{array['name']} cannot be read",
                                     str(exc)[:160], "fix access to the array"))
            continue
        licence = (info.get("licenses") or {}).get("SYNCIQ", "not listed")
        if licence not in ("Licensed", "Evaluation", "Activated"):
            findings.append(_finding("blocker", f"{array['name']} has no SyncIQ licence",
                                     licence, "replication cannot be configured without it"))
        if info.get("synciq_service") not in (True, "on"):
            findings.append(_finding("work", f"{array['name']}: the SyncIQ service is off",
                                     str(info.get("synciq_service")),
                                     "isi sync settings modify --service on"))
        if not info.get("can_replicate"):
            findings.append(_finding(
                "blocker", f"{array['name']}: the account cannot drive SyncIQ",
                "ISI_PRIV_SYNCIQ is missing or read-only for "
                f"{array.get('username', 'this account')}",
                "a driver that only provisioned volumes never needed it; add it with write "
                "access to the role"))
        if info.get("synciq_encryption_required"):
            own = info.get("synciq_cluster_certificate_id", "")
            peers = {c["id"] for c in client.sync_certificates("peer")}
            for other in arrays:
                if other["id"] == array["id"]:
                    continue
                wanted = other.get("replication_certificate_id", "")
                if wanted and wanted not in peers:
                    findings.append(_finding(
                        "work", f"{array['name']} does not trust {other['name']}",
                        "this array requires encrypted SyncIQ and has no peer certificate "
                        "for the other one",
                        "import the far array's certificate, or push it from the "
                        "Certificates card"))
            if not own:
                findings.append(_finding(
                    "blocker", f"{array['name']} requires encryption but presents nothing",
                    "no cluster certificate is set",
                    "set one with isi sync settings modify --cluster-certificate-id"))

    zones = {a.get("access_zone", "System") for a in arrays}
    paths = {a.get("isi_path", "") for a in arrays}
    if len(arrays) > 1 and len(zones) == 1 and len(paths) == 1:
        findings.append(_finding(
            "note", "both arrays use the same access zone and base path",
            f"zone {next(iter(zones))}, path {next(iter(paths))}",
            "that is fine for one cluster pair: the volume group name carries the far "
            "array's address, so the two sites do not collide. Give a second cluster pair "
            "its own volumeGroupPrefix if one is ever added"))

    # --- the order of the work --------------------------------------------------------
    plan = []
    if any(f["level"] == "blocker" for f in findings):
        plan.append("Clear the blockers above first; nothing below will hold without them.")
    plan += [
        f"Register both arrays here, if they are not already, so the secret can list both.",
        f"Apply the CSM replication CRDs to {source_id} and {target_id}.",
        f"Install the replication controller on both, in dell-replication-controller.",
        "Enable the replication sidecar on each driver: with helm, keep the values the "
        "release already has and change only that switch; with the operator, add the "
        "replication module to the ContainerStorageModule.",
        "Rewrite isilon-creds on both clusters so each lists both arrays.",
        f"Register {source_id} and {target_id} with repctl and inject the peer configs.",
        "Create a replicated storage class pair, with a volume group prefix of its own.",
        "Provision one claim from it, and watch the group appear on both sides before "
        "moving anything real onto it.",
        "Run Prechecks, then a failover and failback of that one claim, before the first "
        "workload depends on it.",
    ]
    counts = {level: sum(1 for f in findings if f["level"] == level)
              for level in ("blocker", "work", "note")}
    return {"findings": findings, "plan": plan, "counts": counts, "here": here,
            "there": there, "source": source_id, "target": target_id,
            "ready": counts["blocker"] == 0}
