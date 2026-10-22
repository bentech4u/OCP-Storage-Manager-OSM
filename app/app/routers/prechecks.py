"""Prechecks: everything to be sure of before provisioning or moving a site."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fastapi import Form

from ..services import audit, clustercheck, prechecks as service, readiness
from ..templating import templates

router = APIRouter()


@router.get("/prechecks", response_class=HTMLResponse)
def prechecks_page(request: Request):
    from .. import store
    return templates.TemplateResponse(request, "prechecks.html", {
        "page": "prechecks", "result": None,
        "clusters": list(store.load()["clusters"].values())})


@router.post("/prechecks/cluster", response_class=HTMLResponse)
def check_one_cluster(request: Request, api: str = Form(...), username: str = Form(""),
                      password: str = Form(""), insecure: str = Form("on")):
    """Everything between this host and a working sign-in, before adding anything."""
    report = clustercheck.check(api.strip(), username.strip(), password,
                                insecure=insecure == "on")
    audit.record("prechecks.cluster", actor=getattr(request.state, "user", ""),
                 via=getattr(request.state, "via", ""), target=report.get("address", api),
                 outcome="ok" if report.get("ready") else "failed",
                 detail=f"{sum(1 for f in report['findings'] if f['level'] == 'fail')} "
                        "failure(s)")
    return templates.TemplateResponse(request, "partials/cluster_check.html", {"r": report})


@router.post("/prechecks/readiness", response_class=HTMLResponse)
def readiness_report(request: Request, source: str = Form(...), target: str = Form(...)):
    """What stands between these two clusters and a replicated pair."""
    report = readiness.assess(source, target)
    audit.record("prechecks.readiness", actor=getattr(request.state, "user", ""),
                 via=getattr(request.state, "via", ""), target=f"{source} to {target}",
                 outcome="ok" if report.get("ready") else "blocked",
                 detail=f"{(report.get('counts') or {}).get('blocker', 0)} blocker(s), "
                        f"{(report.get('counts') or {}).get('work', 0)} item(s) of work")
    return templates.TemplateResponse(request, "partials/readiness.html", {"r": report})


@router.get("/prechecks/run", response_class=HTMLResponse)
def prechecks_run(request: Request):
    result = service.run_all()
    audit.record("prechecks.run", actor=getattr(request.state, "user", ""),
                 via=getattr(request.state, "via", ""),
                 outcome="ok" if result["ok"] else "failed",
                 detail=f"{result['counts']['pass']} passed, {result['counts']['warn']} "
                        f"warnings, {result['counts']['fail']} failed")
    return templates.TemplateResponse(request, "partials/prechecks_body.html",
                                      {"result": result})
