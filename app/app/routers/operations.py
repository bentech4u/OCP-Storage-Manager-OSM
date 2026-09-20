"""Operations: replication groups, failover and friends, SyncIQ state, job history."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import store
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from ..services import inventory, k8s, replication
from ..services.arrays import ArrayError, OneFS
from ..services.jobs import Job, get as get_job, recent, start
from ..templating import templates

router = APIRouter()


def _groups() -> list[dict]:
    """One entry per replication group, with both sides merged into a single view."""
    state = store.load()
    arrays = {a["name"]: a for a in state["arrays"].values()}
    merged: dict[str, dict] = {}

    def collect(item):
        cid, cluster = item
        rgs = replication.replication_groups(cluster["kubeconfig"])
        volumes = k8s.replicated_volumes(cluster["kubeconfig"])
        return cid, cluster, rgs, volumes

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(collect, state["clusters"].items()))

    for cid, cluster, rgs, volumes in results:
        for rg in rgs:
            entry = merged.setdefault(rg["name"], {"name": rg["name"], "sides": []})
            array = (rg.get("protection_group") or "").split("::")[0]
            vols = volumes.get(rg["name"], {})
            entry["sides"].append({
                "cluster": cid, "role": cluster.get("role", ""), "array": array,
                "array_endpoint": (arrays.get(array) or {}).get("endpoint", ""),
                "is_source": rg.get("is_source"), "link": rg.get("link_state", ""),
                "state": rg.get("state", ""), "path": rg.get("protection_group", ""),
                "last_action": rg.get("last_action") or rg.get("condition", ""),
                "volumes": vols.get("total", 0), "bound": vols.get("bound", 0),
                "available": vols.get("available", 0), "claims": vols.get("claims", []),
                "capacity": vols.get("capacity", []),
            })

    groups = []
    for entry in merged.values():
        sides = entry["sides"]
        source = next((s for s in sides if s["is_source"]), None)
        target = next((s for s in sides if s is not source), None)
        link = (source or sides[0])["link"]
        groups.append({
            "name": entry["name"], "sides": sides, "source": source, "target": target,
            "link": link,
            "healthy": link in ("SYNCHRONIZED", "Synchronized"),
            "failed_over": link == "FAILEDOVER",
            "in_progress": any("IN_PROGRESS" in (s["state"] or "") for s in sides),
            "volumes": max((s["volumes"] for s in sides), default=0),
            "claims": sorted({c for s in sides for c in s["claims"]}),
            "last_action": next((s["last_action"] for s in sides if s["last_action"]), ""),
        })
    return groups


@router.get("/operations", response_class=HTMLResponse)
async def operations_page(request: Request, job: str = ""):
    state = store.load()
    data = inventory.snapshot()
    return templates.TemplateResponse(request, "operations.html", {
        "page": "operations", "data": data, "groups": _groups(),
        "clusters": list(state["clusters"].values()),
        "arrays": data["arrays"], "jobs": recent(25),
        "actions": replication.ACTIONS,
        "updated": datetime.now().strftime("%H:%M:%S"),
        "selected_job": get_job(job).snapshot() if get_job(job) else None,
        "repctl_ready": data["tools"]["repctl"]["found"],
    })


@router.get("/partials/groups", response_class=HTMLResponse)
async def groups_partial(request: Request):
    state = store.load()
    return templates.TemplateResponse(request, "partials/groups.html", {
        "groups": _groups(), "clusters": list(state["clusters"].values()),
        "actions": replication.ACTIONS, "updated": datetime.now().strftime("%H:%M:%S"),
    })


@router.get("/partials/synciq", response_class=HTMLResponse)
async def synciq_partial(request: Request):
    rows = []
    for array in store.load()["arrays"].values():
        try:
            client = OneFS(array["endpoint"], int(array.get("port", 8080)),
                           array["username"], array["password"])
            rows.append({"array": array["name"], "policies": client.sync_policies(),
                         "reports": client.sync_reports(6), "error": ""})
        except ArrayError as exc:
            rows.append({"array": array.get("name", array["id"]), "policies": [],
                         "reports": [], "error": str(exc)})
    return templates.TemplateResponse(request, "partials/synciq.html", {"rows": rows})


@router.get("/partials/action-form", response_class=HTMLResponse)
async def action_form(request: Request, rg: str = "", cluster: str = "", action: str = "failover",
                      target: str = ""):
    """The confirmation form, with the cluster picker and the command always in step."""
    state = store.load()
    spec = replication.ACTIONS.get(action, replication.ACTIONS["failover"])
    clusters = list(state["clusters"].values())
    others = [c for c in clusters if c["id"] != cluster]

    # Which cluster the action should name by default.
    #   failover and failback move service to the other site
    #   reprotect runs where service is now, which after a failover is the other site
    now = {}
    if cluster and cluster in state["clusters"]:
        now = replication.rg_state(state["clusters"][cluster]["kubeconfig"], rg)
    failed_over = (now.get("link") or "") == "FAILEDOVER"
    if action == "reprotect":
        choices = clusters
        default = (others[0]["id"] if others and failed_over else cluster)
    else:
        choices = others or clusters
        default = choices[0]["id"] if choices else ""
    chosen = target or default

    return templates.TemplateResponse(request, "partials/action_form.html", {
        "rg": rg, "cluster": cluster, "action": action, "spec": spec,
        "choices": choices, "chosen": chosen, "failed_over": failed_over,
        "link": now.get("link", ""), "is_source": now.get("is_source"),
        "preview": replication.action_preview(rg, action,
                                              chosen if spec["needs_target"] else None),
    })


@router.post("/operations/action", response_class=HTMLResponse)
async def run_action(request: Request, rg: str = Form(...), action: str = Form(...),
                     cluster: str = Form(""), target: str = Form(""),
                     unplanned: str = Form(""), discard: str = Form("")):
    if action not in replication.ACTIONS:
        return HTMLResponse('<div class="banner bad">Unknown action.</div>')
    preview = replication.action_preview(rg, action, target or None,
                                         unplanned == "on", discard == "on")

    kubeconfig = (store.load()["clusters"].get(cluster) or {}).get("kubeconfig")

    def work(job: Job):
        job.log(f"replication group {rg} on cluster {cluster or 'unknown'}")
        job.log(f"command: {preview}")
        rc = replication.act(job, rg, action, target or None,
                             unplanned == "on", discard == "on", kubeconfig=kubeconfig)
        if rc != 0:
            raise RuntimeError("the action did not complete; see the output above")
        inventory.invalidate()

    job = start(f"{action} {rg}", "replication-action", work, cluster=cluster or None)
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": True})


@router.get("/operations/jobs/{job_id}", response_class=HTMLResponse)
async def show_job(request: Request, job_id: str):
    job = get_job(job_id)
    if not job:
        return HTMLResponse('<div class="empty">That job is no longer in memory.</div>')
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": job.status == "running"})
