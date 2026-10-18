"""Everything to be sure of before a volume is provisioned or a site is moved.

These are the checks that catch a fault which otherwise shows up halfway through:
a SyncIQ policy appears on the source array, and nothing appears in Kubernetes,
because the far side refused. An expired certificate does exactly that, and says
nothing about itself anywhere an operator would look.

Each check is pass, warn or fail. A failure stops replication working; a warning is
something that will hurt later, such as a certificate with three weeks left on it.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from .. import store
from .arrays import ArrayError, client_for, endpoint_certificate, node_check, resolve_all
from .k8s import ClusterError, run, run_json

CERT_WARN_DAYS = 45

# What has to be open, and from where. SyncIQ on OneFS 9.x makes its encrypted
# connection on 2098; 3148 is the older unencrypted path and is not required here.
PAPI_PORT = 8080
NFS_PORT = 2049
SYNCIQ_PORT = 2098
API_PORT = 6443


def _check(group: str, what: str, level: str, detail: str, fix: str = "",
           where: str = "") -> dict:
    return {"group": group, "what": what, "level": level, "detail": detail, "fix": fix,
            "where": where}


def _days(not_after: int) -> int | None:
    if not not_after:
        return None
    return int((int(not_after) - time.time()) // 86400)


# --- arrays ----------------------------------------------------------------------
def array_checks(array: dict, others: list[dict]) -> list[dict]:
    name = array.get("name", array.get("id", "array"))
    group = f"array {name}"
    out: list[dict] = []
    try:
        client = client_for(array)
        info = client.inspect()
    except (ArrayError, Exception) as exc:                   # noqa: BLE001
        return [_check(group, "reachable", "fail", str(exc)[:200],
                       "nothing else about this array can be checked", name)]

    out.append(_check(group, "reachable", "pass",
                      f"OneFS {info.get('onefs_version','')} over "
                      f"{info.get('auth_mode','')} authentication", "", name))

    missing = info.get("missing_privileges") or []
    out.append(_check(group, "account privileges", "fail" if missing else "pass",
                      ", ".join(missing) or "complete",
                      "add the missing privileges to the role this account holds", name))

    licences = info.get("licenses") or {}
    for key in ("SYNCIQ", "SNAPSHOTIQ", "SMARTQUOTAS"):
        state = licences.get(key, "not listed")
        ok = state in ("Licensed", "Evaluation", "Activated")
        out.append(_check(group, f"{key.lower()} licence", "pass" if ok else "fail", state,
                          "replication, snapshots or quotas will fail without it", name))

    out.append(_check(group, "NFS service", "pass" if info.get("nfs_enabled") else "fail",
                      "on" if info.get("nfs_enabled") else "off",
                      "volumes are exported over NFS", name))

    out.append(_check(group, "SyncIQ service",
                      "pass" if info.get("synciq_service") in (True, "on") else "fail",
                      str(info.get("synciq_service")),
                      "isi sync settings modify --service on", name))

    try:
        path_ok = client.check_path(array.get("isi_path", "/ifs/data/csi"))["exists"]
    except ArrayError:
        path_ok = False
    out.append(_check(group, "base path", "pass" if path_ok else "fail",
                      array.get("isi_path", ""), "create it, or fix the path on the array "
                      "record", name))

    # the management certificate, which is what a driver or this console talks to
    cert = endpoint_certificate(array["endpoint"], int(array.get("port", 8080)))
    if not cert["known"]:
        out.append(_check(group, "management certificate", "warn",
                          cert.get("error", "could not be read"), "", name))
    else:
        days = cert["days"]
        level = "fail" if days is not None and days < 0 else (
            "warn" if days is not None and days < CERT_WARN_DAYS else "pass")
        out.append(_check(
            group, "management certificate", level,
            ("expired " + str(abs(days)) + " days ago" if days is not None and days < 0
             else f"{days} days left") + f", {cert['subject'][:60]}",
            "replace the certificate on the array; an expired one is accepted by this "
            "console only because it does not verify", name))

    # SyncIQ certificates: the pair that lets an encrypted policy connect at all
    encryption = bool(info.get("synciq_encryption_required"))
    try:
        server = client.sync_certificates("server")
        peers = client.sync_certificates("peer")
    except ArrayError as exc:
        out.append(_check(group, "SyncIQ certificates", "warn", str(exc)[:160], "", name))
        return out

    own_id = info.get("synciq_cluster_certificate_id", "")
    for cert_row in server:
        days = _days(cert_row["not_after"])
        bad = cert_row["status"] not in ("valid", "")
        level = "fail" if bad or (days is not None and days < 0) else (
            "warn" if days is not None and days < CERT_WARN_DAYS else "pass")
        mine = " (the one this array presents)" if cert_row["id"] == own_id else ""
        out.append(_check(
            group, f"SyncIQ server certificate {cert_row['name']}", level,
            f"{cert_row['status']}, "
            + ("expired" if days is not None and days < 0 else f"{days} days left") + mine,
            "re-issue it and set it with isi sync settings modify "
            "--cluster-certificate-id", name))

    if encryption and not server:
        out.append(_check(group, "SyncIQ server certificate", "fail",
                          "encryption is required here but this array has none",
                          "import a server certificate and point SyncIQ at it", name))

    for other in others:
        wanted = other.get("replication_certificate_id", "")
        match = next((p for p in peers if p["id"] == wanted), None) if wanted else None
        if not wanted:
            out.append(_check(group, f"peer certificate for {other['name']}", "warn",
                              "that array has no certificate id recorded here",
                              "register the far array again so its id is read", name))
            continue
        if not match:
            level = "fail" if encryption else "warn"
            out.append(_check(group, f"peer certificate for {other['name']}", level,
                              "not imported on this array",
                              "isi sync certificates peer import, with the far array's "
                              "certificate", name))
            continue
        days = _days(match["not_after"])
        bad = match["status"] not in ("valid", "")
        level = "fail" if bad or (days is not None and days < 0) else (
            "warn" if days is not None and days < CERT_WARN_DAYS else "pass")
        out.append(_check(
            group, f"peer certificate for {other['name']}", level,
            f"{match['status']}, " + ("expired" if days is not None and days < 0
                                      else f"{days} days left"),
            "an expired peer certificate lets the policy be created here and refused "
            "at the far end, which is why nothing appears in Kubernetes", name))

    if len(resolve_all(array["endpoint"])) > 1:
        nodes = node_check(array["endpoint"], int(array.get("port", 8080)),
                           array["username"], array["password"],
                           int(array.get("auth_type", 1)), timeout=8)
        bad = [n for n in nodes if not n["ok"]]
        out.append(_check(group, "every node answers", "fail" if bad else "pass",
                          f"{len(nodes) - len(bad)} of {len(nodes)} nodes answered"
                          + ("; " + ", ".join(n["address"] for n in bad) if bad else ""),
                          "a name that balances across nodes hides one that refuses", name))
    return out


# --- clusters --------------------------------------------------------------------
def cluster_checks(cluster: dict, arrays: list[dict], clusters: list[dict]) -> list[dict]:
    cid = cluster["id"]
    group = f"cluster {cid}"
    kc = cluster.get("kubeconfig", "")
    out: list[dict] = []
    try:
        version = run_json(kc, ["version"], timeout=30)
        out.append(_check(group, "reachable", "pass",
                          (version.get("serverVersion") or {}).get("gitVersion", "answered"),
                          "", cid))
    except (ClusterError, Exception) as exc:                 # noqa: BLE001
        return [_check(group, "reachable", "fail", str(exc)[:200],
                       "nothing else about this cluster can be checked", cid)]

    ns = cluster.get("namespace", "isilon")
    try:
        pods = run_json(kc, ["-n", ns, "get", "pods"], timeout=60).get("items", [])
    except ClusterError:
        pods = []
    driver = [p for p in pods if "-controller-" in p["metadata"]["name"]
              or "-node-" in p["metadata"]["name"]]
    ready = [p for p in driver
             if all(c.get("ready") for c in (p.get("status", {}).get("containerStatuses") or []))]
    out.append(_check(group, "driver pods", "pass" if driver and len(ready) == len(driver)
                      else "fail", f"{len(ready)} of {len(driver)} ready in {ns}",
                      "the driver has to be healthy before anything is provisioned", cid))

    crd = run(kc, ["get", "crd", "dellcsireplicationgroups.replication.storage.dell.com"],
              timeout=30).returncode == 0
    out.append(_check(group, "replication CRDs", "pass" if crd else "fail",
                      "present" if crd else "missing",
                      "install the CSM replication CRDs", cid))

    try:
        rc_pods = run_json(kc, ["-n", "dell-replication-controller", "get", "pods"],
                           timeout=30).get("items", [])
    except ClusterError:
        rc_pods = []
    running = [p for p in rc_pods if p.get("status", {}).get("phase") == "Running"]
    out.append(_check(group, "replication controller",
                      "pass" if running else "fail",
                      f"{len(running)} running" if rc_pods else "not installed",
                      "without it a group never changes state", cid))

    from .replication import controller_config
    cfg = controller_config(kc) or {}
    targets = [t.get("clusterId") for t in (cfg.get("targets") or [])]
    peers = [c["id"] for c in clusters if c["id"] != cid]
    missing_peers = [p for p in peers if p not in targets]
    out.append(_check(group, "knows its peer",
                      "pass" if targets and not missing_peers else
                      ("warn" if targets else "fail"),
                      f"self {cfg.get('clusterId') or 'unset'}, targets "
                      + (", ".join(targets) or "none"),
                      "run the replication wiring step for each pair", cid))

    # storage classes: the pair has to mirror, and name things this console knows
    try:
        classes = [s for s in run_json(kc, ["get", "storageclass"], timeout=30).get("items", [])
                   if "isilon" in s.get("provisioner", "")]
    except ClusterError:
        classes = []
    known_clusters = {c["id"] for c in clusters}
    known_arrays = {a["name"] for a in arrays}
    replicated = [s for s in classes
                  if (s.get("parameters") or {}).get(
                      "replication.storage.dell.com/isReplicationEnabled") == "true"]
    out.append(_check(group, "storage classes", "pass" if classes else "warn",
                      f"{len(classes)} for this driver, {len(replicated)} replicated",
                      "a replicated class is what gives a volume a group", cid))
    for sc in replicated:
        params = sc.get("parameters") or {}
        sc_name = sc["metadata"]["name"]
        remote = params.get("replication.storage.dell.com/remoteClusterID", "")
        system = params.get("replication.storage.dell.com/remoteSystem", "")
        problems = []
        if remote not in known_clusters:
            problems.append(f"remoteClusterID {remote or 'unset'} is not a cluster here")
        if system not in known_arrays:
            problems.append(f"remoteSystem {system or 'unset'} is not an array here")
        if remote == cid:
            problems.append("it points at its own cluster")
        out.append(_check(group, f"class {sc_name}", "fail" if problems else "pass",
                          "; ".join(problems) or f"replicates to {remote} on {system}",
                          "fix the class, or register what it names", cid))
    return out


def pair_checks(clusters: list[dict]) -> list[dict]:
    """What has to agree between two sites."""
    out: list[dict] = []
    if len(clusters) < 2:
        out.append(_check("pairs", "two sites", "warn",
                          f"{len(clusters)} cluster registered",
                          "replication needs both ends registered here"))
        return out
    from .replication import list_clusters
    registered = {r["id"] for r in list_clusters().get("rows", [])}
    for cluster in clusters:
        ok = cluster["id"] in registered
        out.append(_check("pairs", f"repctl knows {cluster['id']}", "pass" if ok else "fail",
                          "registered" if ok else "missing from repctl's store",
                          "run the repctl registration step", cluster["id"]))
    return out


# --- the network between all of it ------------------------------------------------
def _reachable(host: str, port: int, timeout: float = 4.0) -> tuple[bool, str]:
    """A plain TCP connection from this host."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} open"
    except OSError as exc:
        return False, f"{host}:{port} {exc.strerror or exc}"


def _host_of(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse
    parsed = urlparse(url if "//" in url else "https://" + url)
    return parsed.hostname or "", parsed.port or API_PORT


def _reachable_from_pod(kubeconfig: str, namespace: str, pod: str, container: str,
                        host: str, port: int) -> tuple[bool | None, str]:
    """Open a TCP connection from inside the cluster, in a container already running.

    Nothing is created: this is the driver's own pod being asked to open a socket,
    which is the same path the driver itself takes.
    """
    script = (f'timeout 4 bash -c "</dev/tcp/{host}/{port}" >/dev/null 2>&1 '
              f'&& echo OPEN || echo CLOSED')
    proc = run(kubeconfig, ["-n", namespace, "exec", pod, "-c", container, "--",
                            "bash", "-c", script], timeout=40)
    text = (proc.stdout or "") + (proc.stderr or "")
    if "OPEN" in text:
        return True, f"{host}:{port} open"
    if "CLOSED" in text:
        return False, f"{host}:{port} refused or filtered"
    return None, (text.strip()[:140] or "the test could not be run in this pod")


def _driver_pods(kubeconfig: str, namespace: str) -> dict:
    """One controller pod and one node pod, whichever are running."""
    out = {"controller": "", "node": ""}
    try:
        items = run_json(kubeconfig, ["-n", namespace, "get", "pods"], timeout=40).get("items", [])
    except ClusterError:
        return out
    for pod in items:
        name = pod["metadata"]["name"]
        if pod.get("status", {}).get("phase") != "Running":
            continue
        if "-controller-" in name and not out["controller"]:
            out["controller"] = name
        if "-node-" in name and not out["node"]:
            out["node"] = name
    return out


def console_check() -> list[dict]:
    """The console's own address, which is what everyone else has to reach."""
    from .release import _endpoint                      # reads the unit, imports nothing heavy
    port, tls = _endpoint()
    ok, detail = _reachable("127.0.0.1", port)
    scheme = "https" if tls else "http"
    return [_check("network from this installer", "this console's port",
                   "pass" if ok else "fail",
                   f"{scheme} on {port}, {detail.split(' ', 1)[-1]}",
                   "browsers and osmctl reach the console here; open it to the operators "
                   "who need it and no further")]


def array_path_evidence(arrays: list[dict]) -> list[dict]:
    """Whether the arrays have actually talked to each other.

    Nothing here can open a connection from one array to the other, so the question
    is answered with evidence instead: a policy that has run, or a target the far
    side has recorded.
    """
    out: list[dict] = []
    for array in arrays:
        name = array.get("name", array["id"])
        try:
            client = client_for(array)
            policies = client.sync_policies()
            incoming = client.sync_target_policies()
        except (ArrayError, Exception):                      # noqa: BLE001
            continue
        ran = [p for p in policies if (p.get("last_job_state") or "").lower()
               in ("finished", "running")]
        if ran:
            out.append(_check("network between the arrays", f"{name} has reached its peer",
                              "pass", f"{len(ran)} of {len(policies)} policies have run",
                              "", name))
        elif policies:
            states = ", ".join(sorted({(p.get("last_job_state") or "never run")
                                       for p in policies}))
            out.append(_check("network between the arrays", f"{name} has reached its peer",
                              "fail", f"{len(policies)} policies, none has run: {states}",
                              f"the arrays connect on {SYNCIQ_PORT}; if that is shut between "
                              "the sites the policy is created here and never runs", name))
        elif incoming:
            out.append(_check("network between the arrays", f"{name} receives from its peer",
                              "pass", f"{len(incoming)} incoming policies", "", name))
        else:
            out.append(_check("network between the arrays", f"{name} path to its peer",
                              "warn", "no policies yet, so there is nothing to judge it by",
                              f"it cannot be tested from here; make sure {SYNCIQ_PORT} is "
                              "open from each array to the other", name))
    return out


def network_checks(clusters: list[dict], arrays: list[dict]) -> list[dict]:
    out: list[dict] = console_check()
    group = "network from this installer"

    for cluster in clusters:
        host, port = _host_of(cluster.get("server", ""))
        if not host:
            continue
        ok, detail = _reachable(host, port)
        out.append(_check(group, f"{cluster['id']} API", "pass" if ok else "fail", detail,
                          "the console and repctl both drive the cluster through this",
                          cluster["id"]))
    for array in arrays:
        host = array["endpoint"]
        ok, detail = _reachable(host, int(array.get("port", PAPI_PORT)))
        out.append(_check(group, f"{array['name']} management API", "pass" if ok else "fail",
                          detail, "the console reads and configures the array here",
                          array["name"]))
        ok, detail = _reachable(host, SYNCIQ_PORT)
        out.append(_check(group, f"{array['name']} SyncIQ port", "pass" if ok else "warn",
                          detail + (" (encrypted replication listens here)" if ok else ""),
                          "the arrays connect to each other on this port; if it is shut "
                          "between the sites, policies are created and never run",
                          array["name"]))

    # from inside each cluster: the peer cluster, and both arrays
    for cluster in clusters:
        cid = cluster["id"]
        kc = cluster.get("kubeconfig", "")
        ns = cluster.get("namespace", "isilon")
        label = f"network from cluster {cid}"
        pods = _driver_pods(kc, ns)
        if not pods["controller"] and not pods["node"]:
            out.append(_check(label, "a driver pod to test from", "warn",
                              f"no running driver pod in {ns}",
                              "these paths can only be tested from inside the cluster", cid))
            continue
        targets = []
        for peer in clusters:
            if peer["id"] == cid:
                continue
            host, port = _host_of(peer.get("server", ""))
            if host:
                targets.append(("controller", f"{peer['id']} API", host, port,
                                "the replication controller talks to its peer cluster here"))
        for array in arrays:
            targets.append(("controller", f"{array['name']} management API",
                            array["endpoint"], int(array.get("port", PAPI_PORT)),
                            "the driver provisions through this"))
            targets.append(("node", f"{array['name']} NFS", array["endpoint"], NFS_PORT,
                            "the nodes mount volumes over this"))
        for role, what, host, port, why in targets:
            pod = pods.get(role) or pods.get("controller") or pods.get("node")
            if not pod:
                continue
            ok, detail = _reachable_from_pod(kc, ns, pod, "driver", host, port)
            level = "pass" if ok else ("warn" if ok is None else "fail")
            out.append(_check(label, f"{role} to {what}", level,
                              detail + f" (from {pod})", why, cid))
    out.extend(array_path_evidence(arrays))
    return out


def run_all() -> dict:
    """Every check, arrays and clusters in parallel."""
    state = store.load()
    arrays = list(state["arrays"].values())
    clusters = list(state["clusters"].values())
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        array_jobs = [pool.submit(array_checks, a, [o for o in arrays if o["id"] != a["id"]])
                      for a in arrays]
        cluster_jobs = [pool.submit(cluster_checks, c, arrays, clusters) for c in clusters]
        for job in array_jobs + cluster_jobs:
            try:
                results.extend(job.result())
            except Exception as exc:                         # noqa: BLE001
                results.append(_check("checks", "a check did not finish", "warn",
                                      str(exc)[:200]))
    results.extend(pair_checks(clusters))
    try:
        results.extend(network_checks(clusters, arrays))
    except Exception as exc:                                 # noqa: BLE001
        results.append(_check("network", "the network checks did not finish", "warn",
                              str(exc)[:200]))
    if not arrays:
        results.append(_check("arrays", "an array is registered", "fail", "none yet",
                              "add the arrays on Install and Configure"))
    groups: dict[str, list[dict]] = {}
    for row in results:
        groups.setdefault(row["group"], []).append(row)
    counts = {level: sum(1 for r in results if r["level"] == level)
              for level in ("pass", "warn", "fail")}
    return {"groups": groups, "counts": counts, "total": len(results),
            "ran": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "ok": counts["fail"] == 0}
