"""Install and Configure: tools, clusters, arrays, driver install, replication setup."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from .. import store
from ..config import DRIVER_NAMESPACE_DEFAULT, match_owner
from ..services import installer, inventory, k8s, replication, tools
from ..services.arrays import ArrayError, OneFS
from ..services.jobs import Job, start
from ..templating import templates

router = APIRouter()


def _page(request: Request, **extra):
    state = store.load()
    data = inventory.snapshot()
    # the driver status strips are rendered with the page, then refreshed in place
    rows = [{"cluster": c, "status": c.get("driver") or {"pods": [], "namespace":
             c.get("namespace", DRIVER_NAMESPACE_DEFAULT), "error": c.get("error", "")},
             "install": state["installs"].get(c["id"]), "selected": False}
            for c in data["clusters"]]
    ctx = {
        "page": "install", "state": state, "data": data,
        "clusters": list(state["clusters"].values()),
        "arrays": list(state["arrays"].values()),
        "tools": data["tools"],
        "repctl_default": tools.default_repctl_choice(),
        "namespace_default": DRIVER_NAMESPACE_DEFAULT,
        "driver_rows": rows,
    }
    ctx.update(extra)
    return templates.TemplateResponse(request, "install.html", ctx)


@router.get("/install", response_class=HTMLResponse)
async def install_page(request: Request):
    return _page(request)


# --- tools ---------------------------------------------------------------------
@router.get("/install/repctl/releases", response_class=HTMLResponse)
async def repctl_releases(request: Request):
    try:
        releases = tools.repctl_releases()
        error = ""
    except RuntimeError as exc:
        releases, error = [], str(exc)
    return templates.TemplateResponse(request, "partials/repctl_releases.html",
                                      {"releases": releases, "error": error})


@router.post("/install/repctl", response_class=HTMLResponse)
async def install_repctl(request: Request, source: str = Form("download"),
                         version: str = Form(""), url: str = Form(""),
                         binary: UploadFile | None = File(None)):
    payload = await binary.read() if binary and binary.filename else None
    filename = binary.filename if binary else ""

    def work(job: Job):
        if source == "upload":
            if not payload:
                raise RuntimeError("choose a repctl binary to upload")
            tools.install_repctl_from_upload(job, payload, filename)
        elif source == "build":
            tools.build_repctl_from_source(job, version or "v1.15.0")
        else:
            tools.install_repctl_from_url(job, url or tools.default_repctl_choice()["url"])
        inventory.invalidate()

    job = start(f"Install repctl ({source})", "repctl", work)
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": True})


@router.get("/install/repctl/clusters", response_class=HTMLResponse)
async def repctl_clusters(request: Request):
    state = store.load()
    return templates.TemplateResponse(request, "partials/repctl_clusters.html", {
        "repctl": replication.list_clusters(),
        "clusters": list(state["clusters"].values()),
    })


@router.post("/install/repctl/register", response_class=HTMLResponse)
async def repctl_register(request: Request):
    """repctl cluster add for every cluster the console knows, on its own."""
    clusters = list(store.load()["clusters"].values())
    if len(clusters) < 1:
        return HTMLResponse('<div class="banner bad">Add at least one cluster first.</div>')

    def work(job: Job):
        job.log("repctl keeps its own copy of each kubeconfig, separate from this console")
        rc = replication.add_clusters(job, clusters)
        if rc != 0:
            raise RuntimeError(f"repctl exited {rc}")
        listing = replication.list_clusters()
        job.log("repctl now lists: " + (", ".join(r["id"] for r in listing["rows"])
                                        or listing.get("error", "nothing")))
        job.log("store: " + listing["path"])

    job = start("Register clusters with repctl", "repctl", work)
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": True})


# --- clusters ------------------------------------------------------------------
@router.post("/install/clusters", response_class=HTMLResponse)
async def add_cluster(request: Request, cluster_id: str = Form(...), role: str = Form("source"),
                      namespace: str = Form(DRIVER_NAMESPACE_DEFAULT),
                      connect_via: str = Form("file"),
                      kubeconfig_path_in: str = Form(""),
                      api_server: str = Form(""), username: str = Form(""),
                      password: str = Form(""), insecure: str = Form("on"),
                      kubeconfig_file: UploadFile | None = File(None)):
    cid = cluster_id.strip().lower().replace(" ", "-")
    if not cid:
        return _page(request, error="give the cluster an id")

    try:
        if connect_via == "login":
            if not api_server or not username or not password:
                return _page(request, error="the login route needs an API address, a user and a password")
            path = k8s.login_kubeconfig(api_server, username.strip(), password, cid,
                                        insecure=(insecure == "on"))
        else:
            content = None
            if kubeconfig_file and kubeconfig_file.filename:
                content = (await kubeconfig_file.read()).decode("utf-8", errors="replace")
            elif kubeconfig_path_in.strip():
                try:
                    content = open(kubeconfig_path_in.strip()).read()
                except OSError as exc:
                    return _page(request, error=f"cannot read {kubeconfig_path_in}: {exc}")
            if not content:
                return _page(request, error="choose a kubeconfig file, give a path, "
                                            "or switch to the username and password route")
            path = k8s.save_kubeconfig(cid, content)
        info = k8s.probe(path)
    except k8s.ClusterError as exc:
        return _page(request, error=f"{cid}: {exc}")

    record = {
        "id": cid, "role": role, "namespace": namespace.strip() or DRIVER_NAMESPACE_DEFAULT,
        "kubeconfig": str(path), "server": info.get("server", ""),
        "added": store.now(), "admin_kubeconfig": str(path),
        "service_account_kubeconfig": "", "openshift": info.get("is_openshift", False),
        "openshift_version": info.get("openshift_version", ""),
        "kubernetes_version": info.get("kubernetes_version", ""),
        "connected_via": connect_via, "connected_as": info.get("user", ""),
    }
    store.update(lambda s: s["clusters"].update({cid: record}))
    inventory.invalidate()
    who = info.get("user") or "the supplied identity"
    admin = "with cluster-admin" if info.get("cluster_admin") else "WITHOUT cluster-admin"
    return _page(request, notice=f"Cluster {cid} added: {info.get('server','')}, connected as "
                                 f"{who} {admin}.")


@router.post("/install/clusters/{cid}/delete", response_class=HTMLResponse)
async def delete_cluster(request: Request, cid: str):
    store.update(lambda s: s["clusters"].pop(cid, None))
    inventory.invalidate()
    return _page(request, notice=f"Cluster {cid} removed from the console (nothing was uninstalled).")


@router.get("/install/clusters/{cid}/rbac", response_class=HTMLResponse)
async def cluster_rbac(request: Request, cid: str):
    state = store.load()
    cluster = state["clusters"].get(cid)
    if not cluster:
        return HTMLResponse('<div class="empty">unknown cluster</div>')
    kc = cluster.get("service_account_kubeconfig") or cluster["kubeconfig"]
    try:
        report = k8s.rbac_report(kc)
        info = k8s.probe(kc)
        error = ""
    except k8s.ClusterError as exc:
        report, info, error = [], {}, str(exc)
    return templates.TemplateResponse(request, "partials/rbac.html", {
        "cluster": cluster, "report": report, "info": info, "error": error,
        "which": "service account" if cluster.get("service_account_kubeconfig") else "admin",
    })


@router.post("/install/clusters/{cid}/serviceaccount", response_class=HTMLResponse)
async def make_service_account(request: Request, cid: str,
                               sa_name: str = Form(k8s.SA_NAME),
                               sa_namespace: str = Form(k8s.SA_NAMESPACE),
                               role: str = Form(k8s.SA_CLUSTER_ROLE)):
    state = store.load()
    cluster = state["clusters"].get(cid)
    if not cluster:
        return HTMLResponse('<div class="empty">unknown cluster</div>')

    def work(job: Job):
        content = k8s.create_sa_kubeconfig(job, cluster["admin_kubeconfig"], cid,
                                           ns=sa_namespace, sa=sa_name, role=role)
        path = k8s.kubeconfig_path(f"{cid}-sa")
        path.write_text(content)
        path.chmod(0o600)
        match_owner(path)
        job.log(f"saved {path}")
        info = k8s.probe(path)
        job.log(f"verified: {info.get('user') or 'service account'} on {info.get('server')}, "
                f"cluster-admin={info.get('cluster_admin')}")
        store.update(lambda s: s["clusters"][cid].update({
            "service_account_kubeconfig": str(path), "kubeconfig": str(path),
            "sa_name": sa_name, "sa_namespace": sa_namespace, "sa_role": role,
        }))
        inventory.invalidate()

    job = start(f"Create service account kubeconfig on {cid}", "kubeconfig", work, cluster=cid)
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": True})


# --- arrays --------------------------------------------------------------------
@router.post("/install/arrays", response_class=HTMLResponse)
async def add_array(request: Request, endpoint: str = Form(...), username: str = Form(...),
                    password: str = Form(...), port: int = Form(8080),
                    isi_path: str = Form("/ifs/data/csi"), access_zone: str = Form("System"),
                    skip_cert_validation: str = Form("on"), is_default: str = Form(""),
                    create_path: str = Form(""), auth_type: int = Form(1)):
    client = OneFS(endpoint, port, username, password,
                   verify=(skip_cert_validation != "on"), auth_type=int(auth_type))
    try:
        info = client.inspect()
    except ArrayError as exc:
        return _page(request, error=f"{endpoint}: {exc}")
    if create_path == "on" and not client.check_path(isi_path)["exists"]:
        client.create_path(isi_path)
    path_ok = client.check_path(isi_path)["exists"]
    aid = (info.get("name") or endpoint).lower().replace(" ", "-")
    record = {
        "id": aid, "name": info.get("name") or aid, "endpoint": endpoint, "port": port,
        "username": username, "password": password, "isi_path": isi_path,
        "access_zone": access_zone, "skip_cert_validation": skip_cert_validation == "on",
        "auth_type": int(auth_type), "auth_mode": info.get("auth_mode", "session"),
        "is_default": is_default == "on", "added": store.now(),
        "replication_certificate_id": info.get("synciq_cluster_certificate_id", ""),
        "onefs_version": info.get("onefs_version", ""), "path_ok": path_ok,
    }
    store.update(lambda s: s["arrays"].update({aid: record}))
    inventory.invalidate()
    missing = ", ".join(info.get("missing_privileges", []))
    supported = info.get("auth_supported", {})
    accepted = ", ".join(k for k, v in supported.items() if v) or "session"
    notice = (f"Array {record['name']} added, reached with {record['auth_mode']} "
              f"authentication (isiAuthType {record['auth_type']}). "
              f"This array accepts: {accepted}.")
    if not supported.get("basic"):
        notice += (" Basic is off on the array; turn it on with "
                   "isi_gconfig -t web-config auth_basic=true if you prefer it.")
    if missing:
        notice += f" Missing privileges: {missing}."
    if not path_ok:
        notice += f" The base path {isi_path} does not exist yet."
    return _page(request, notice=notice)


@router.post("/install/arrays/{aid}/delete", response_class=HTMLResponse)
async def delete_array(request: Request, aid: str):
    store.update(lambda s: s["arrays"].pop(aid, None))
    inventory.invalidate()
    return _page(request, notice=f"Array {aid} removed from the console.")


@router.get("/install/driver/status", response_class=HTMLResponse)
async def driver_status_partial(request: Request, cluster_id: str = ""):
    """What is running on every registered cluster, shown above the install form."""
    from concurrent.futures import ThreadPoolExecutor

    state = store.load()
    clusters = list(state["clusters"].values())
    if not clusters:
        return HTMLResponse('<div class="banner"><span>•</span><div>Add a cluster first.</div></div>')

    def look(cluster: dict) -> dict:
        ns = cluster.get("namespace", DRIVER_NAMESPACE_DEFAULT)
        try:
            status = k8s.driver_status(cluster["kubeconfig"], ns)
        except k8s.ClusterError as exc:
            status = {"error": str(exc)[:200], "pods": [], "namespace": ns}
        return {"cluster": cluster, "status": status,
                "install": state["installs"].get(cluster["id"]),
                "selected": cluster["id"] == cluster_id}

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(look, clusters))
    return templates.TemplateResponse(request, "partials/driver_status.html", {"rows": rows})


# --- driver install ------------------------------------------------------------
def _opts(form: dict) -> dict:
    return {
        "endpoint_port": int(form.get("endpoint_port", 8080)),
        "skip_cert_validation": form.get("skip_cert_validation") == "on",
        "auth_type": int(form.get("auth_type", 1)),
        "access_zone": form.get("access_zone", "System"),
        "isi_path": form.get("isi_path", "/ifs/data/csi"),
        "enable_quota": form.get("enable_quota") == "on",
        "replication": form.get("mode") == "replication",
        "snapshots": form.get("snapshots") == "on",
        "controller_count": int(form.get("controller_count", 2)),
        "openshift": form.get("openshift") == "on",
        "root_client_enabled": form.get("root_client_enabled") == "on",
    }


@router.post("/install/driver", response_class=HTMLResponse)
async def install_driver(request: Request):
    form = dict(await request.form())
    cid = form.get("cluster_id", "")
    method = form.get("method", "helm")
    state = store.load()
    cluster = state["clusters"].get(cid)
    if not cluster:
        return HTMLResponse('<div class="banner bad">Choose a cluster first.</div>')
    arrays = list(state["arrays"].values())
    if not arrays:
        return HTMLResponse('<div class="banner bad">Register at least one array first.</div>')
    opts = _opts(form)
    ns = form.get("namespace") or cluster.get("namespace", DRIVER_NAMESPACE_DEFAULT)
    kc = cluster["kubeconfig"]
    upgrade = form.get("upgrade") == "on"
    # Every cluster's secret lists every array: the driver resolves the remote end of a
    # SyncIQ pair from its own config, so both ends must be present on both sites.
    default_array = form.get("default_array") or (arrays[0]["id"] if arrays else "")
    home = state["arrays"].get(default_array, {})
    if not form.get("auth_type") and home.get("auth_type") is not None:
        opts["auth_type"] = int(home["auth_type"])        # follow the array registration
    peers = [c for c in state["clusters"].values() if c["id"] != cid]
    opts["target_cluster_ids"] = [c["id"] for c in peers]

    def work(job: Job):
        job.log(f"cluster {cid} ({cluster.get('server','')}), namespace {ns}, method {method}")
        job.log("mode: " + ("replication" if opts["replication"] else "standalone"))
        job.log(f"secret isilon-creds will list: {', '.join(a['name'] for a in arrays)} "
                f"(default {default_array})")
        for w in dict.fromkeys(installer.support_warnings(
                cluster.get("openshift_version", ""),
                [a.get("onefs_version", "") for a in arrays])):
            job.log("note: " + w)
        if method == "operator":
            pkgs = installer.operator_catalog_options(kc)
            job.log(f"catalog packages found: {[p['package'] for p in pkgs] or 'none'}")
            pkg = form.get("operator_package") or (pkgs[0]["package"] if pkgs else "")
            if not pkg:
                raise RuntimeError("no Dell operator package in this cluster's catalogs; "
                                   "use the Helm method or add the certified catalog")
            channel = form.get("operator_channel") or next(
                (p["default_channel"] for p in pkgs if p["package"] == pkg), "stable")
            source = next((p["catalog"] for p in pkgs if p["package"] == pkg), "certified-operators")
            installer.operator_install(job, kc, pkg, channel, source)
            installer.operator_apply_cr(job, kc, ns, opts, arrays, default_array)
        else:
            installer.helm_install_driver(job, kc, ns, opts, arrays, upgrade=upgrade,
                                          default_array_id=default_array)
        store.update(lambda s: s["installs"].update({cid: {
            "method": method, "namespace": ns, "mode": "replication" if opts["replication"] else "standalone",
            "when": store.now(), "arrays": [a["id"] for a in arrays],
            "default_array": default_array,
        }}))
        inventory.invalidate()

    job = start(f"Install driver on {cid} ({method})", "driver", work, cluster=cid)
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": True})


@router.post("/install/driver/uninstall", response_class=HTMLResponse)
async def uninstall_driver(request: Request, cluster_id: str = Form(...)):
    state = store.load()
    cluster = state["clusters"].get(cluster_id)
    if not cluster:
        return HTMLResponse('<div class="banner bad">unknown cluster</div>')
    ns = cluster.get("namespace", DRIVER_NAMESPACE_DEFAULT)

    def work(job: Job):
        installer.helm_uninstall_driver(job, cluster["kubeconfig"], ns)
        inventory.invalidate()

    job = start(f"Uninstall driver on {cluster_id}", "driver", work, cluster=cluster_id)
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": True})


# --- storage classes -----------------------------------------------------------
@router.post("/install/storageclass", response_class=HTMLResponse)
async def create_storage_class(request: Request):
    form = dict(await request.form())
    state = store.load()
    src = state["clusters"].get(form.get("source_cluster", ""))
    array = state["arrays"].get(form.get("array", ""))
    if not src or not array:
        return HTMLResponse('<div class="banner bad">Pick a cluster and an array.</div>')
    replicated = form.get("replicated") == "on"
    name = form.get("name") or ("isilon-replicated" if replicated else "isilon")
    opts = {
        "access_zone": form.get("access_zone", array.get("access_zone", "System")),
        "isi_path": form.get("isi_path", array.get("isi_path", "/ifs/data/csi")),
        "az_service_ip": form.get("az_service_ip") or array["endpoint"],
        "root_client_enabled": form.get("root_client_enabled") == "on",
        "reclaim_policy": form.get("reclaim_policy", "Delete"),
        "binding_mode": form.get("binding_mode", "Immediate"),
    }
    tgt = state["clusters"].get(form.get("target_cluster", "")) if replicated else None
    tgt_array = state["arrays"].get(form.get("target_array", "")) if replicated else None
    if replicated and (not tgt or not tgt_array):
        return HTMLResponse('<div class="banner bad">Replicated classes need a target cluster and array.</div>')

    def work(job: Job):
        if replicated:
            src_rep = {
                "remote_storage_class": name, "remote_cluster_id": tgt["id"],
                "remote_system": tgt_array["name"],
                "remote_az_service_ip": form.get("target_az_service_ip") or tgt_array["endpoint"],
                "rpo": form.get("rpo", "Five_Minutes"),
                "ignore_namespaces": form.get("ignore_namespaces") == "on",
                "volume_group_prefix": form.get("volume_group_prefix", "csi-prod"),
                "remote_access_zone": tgt_array.get("access_zone", "System"),
            }
            tgt_rep = dict(src_rep)
            tgt_rep.update({"remote_cluster_id": src["id"], "remote_system": array["name"],
                            "remote_az_service_ip": opts["az_service_ip"],
                            "remote_access_zone": array.get("access_zone", "System")})
            job.log(f"creating {name} on {src['id']} (source) and {tgt['id']} (target)")
            installer.apply_storage_class(
                job, src["kubeconfig"], installer.storage_class_manifest(name, array, opts, src_rep))
            tgt_opts = dict(opts)
            tgt_opts["az_service_ip"] = tgt_rep["remote_az_service_ip"] and (
                form.get("target_az_service_ip") or tgt_array["endpoint"])
            installer.apply_storage_class(
                job, tgt["kubeconfig"],
                installer.storage_class_manifest(name, tgt_array, tgt_opts, tgt_rep))
        else:
            job.log(f"creating storage class {name} on {src['id']}")
            installer.apply_storage_class(
                job, src["kubeconfig"], installer.storage_class_manifest(name, array, opts))
        inventory.invalidate()

    job = start(f"Storage class {name}", "storageclass", work, cluster=src["id"])
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": True})


# --- replication wiring ---------------------------------------------------------
@router.get("/install/replication/preflight", response_class=HTMLResponse)
async def replication_preflight(request: Request, source_cluster: str = "", target_cluster: str = ""):
    state = store.load()
    chosen = [state["clusters"][c] for c in (source_cluster, target_cluster)
              if c and c in state["clusters"]]
    if len(chosen) != 2:
        return HTMLResponse('<div class="muted small">Pick two clusters to check them.</div>')
    data = inventory.snapshot()
    servers = {c["id"]: c.get("server", "") for c in data["clusters"]}
    for c in chosen:
        c.setdefault("server", servers.get(c["id"], ""))
    return templates.TemplateResponse(request, "partials/preflight.html", {
        "checks": replication.preflight_use_sa(chosen),
        "reach": replication.reachability(chosen),
        "clusters": chosen,
    })


@router.post("/install/replication/setup", response_class=HTMLResponse)
async def replication_setup(request: Request, source_cluster: str = Form(...),
                            target_cluster: str = Form(...), use_sa: str = Form("on")):
    state = store.load()
    src = state["clusters"].get(source_cluster)
    tgt = state["clusters"].get(target_cluster)
    if not src or not tgt or src["id"] == tgt["id"]:
        return HTMLResponse('<div class="banner bad">Pick two different clusters.</div>')

    def work(job: Job):
        job.log("step 1: replication controller on both clusters")
        installer.helm_install_replication_controller(job, src["kubeconfig"], src["id"], [tgt["id"]])
        installer.helm_install_replication_controller(job, tgt["kubeconfig"], tgt["id"], [src["id"]])
        job.log("step 2: register both clusters with repctl")
        replication.add_clusters(job, [src, tgt])
        job.log("step 3: inject each cluster's config into the other")
        rc = replication.inject(job, [src["id"], tgt["id"]], use_sa=(use_sa == "on"))
        if rc != 0:
            job.log("inject reported a problem; check the output above")
        job.log("step 4: confirm the controllers see their peer")
        replication.configure_controller(job, src["kubeconfig"], src["id"], [tgt["id"]])
        replication.configure_controller(job, tgt["kubeconfig"], tgt["id"], [src["id"]])
        inventory.invalidate()

    job = start(f"Wire replication {source_cluster} to {target_cluster}", "replication", work,
                cluster=source_cluster)
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": True})
