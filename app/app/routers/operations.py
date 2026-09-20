"""Operations: replication groups, failover and friends, SyncIQ state, job history."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import store
from ..services import inventory, replication
from ..services.arrays import ArrayError, OneFS
from ..services.jobs import Job, get as get_job, recent, start
from ..templating import templates

router = APIRouter()


def _groups() -> list[dict]:
    state = store.load()
    rows = []
    for cid, cluster in state["clusters"].items():
        for rg in replication.replication_groups(cluster["kubeconfig"]):
            rg["cluster"] = cid
            rows.append(rg)
    return rows


@router.get("/operations", response_class=HTMLResponse)
async def operations_page(request: Request, job: str = ""):
    state = store.load()
    data = inventory.snapshot()
    return templates.TemplateResponse(request, "operations.html", {
        "page": "operations", "data": data, "groups": _groups(),
        "clusters": list(state["clusters"].values()),
        "arrays": data["arrays"], "jobs": recent(25),
        "actions": replication.ACTIONS,
        "selected_job": get_job(job).snapshot() if get_job(job) else None,
        "repctl_ready": data["tools"]["repctl"]["found"],
    })


@router.get("/partials/groups", response_class=HTMLResponse)
async def groups_partial(request: Request):
    state = store.load()
    return templates.TemplateResponse(request, "partials/groups.html", {
        "groups": _groups(), "clusters": list(state["clusters"].values()),
        "actions": replication.ACTIONS,
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
async def action_form(request: Request, rg: str = "", cluster: str = "", action: str = "failover"):
    state = store.load()
    spec = replication.ACTIONS.get(action, replication.ACTIONS["failover"])
    others = [c for c in state["clusters"].values() if c["id"] != cluster]
    return templates.TemplateResponse(request, "partials/action_form.html", {
        "rg": rg, "cluster": cluster, "action": action, "spec": spec,
        "clusters": list(state["clusters"].values()), "others": others,
        "preview": replication.action_preview(rg, action,
                                              others[0]["id"] if others and spec["needs_target"] else None),
    })


@router.post("/operations/action", response_class=HTMLResponse)
async def run_action(request: Request, rg: str = Form(...), action: str = Form(...),
                     cluster: str = Form(""), target: str = Form(""),
                     unplanned: str = Form(""), discard: str = Form("")):
    if action not in replication.ACTIONS:
        return HTMLResponse('<div class="banner bad">Unknown action.</div>')
    preview = replication.action_preview(rg, action, target or None,
                                         unplanned == "on", discard == "on")

    def work(job: Job):
        job.log(f"replication group {rg} on cluster {cluster or 'unknown'}")
        job.log(f"command: {preview}")
        rc = replication.act(job, rg, action, target or None,
                             unplanned == "on", discard == "on")
        if rc != 0:
            raise RuntimeError(f"repctl exited {rc}")
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
