"""Volumes: what exists where, and what is left over."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ..services import audit, cleanup, inventory
from ..services.jobs import Job, start
from ..templating import templates

router = APIRouter()


@router.get("/volumes", response_class=HTMLResponse)
def volumes_page(request: Request, q: str = ""):
    return templates.TemplateResponse(request, "volumes.html", {"page": "volumes", "q": q})


@router.get("/volumes/map", response_class=HTMLResponse)
def volumes_map(request: Request, q: str = ""):
    facts = cleanup.survey()
    rows = cleanup.volume_map(facts)
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows if needle in " ".join(
            str(v) for v in (r["namespace"], r["claim"], r["pv"], r["storage_class"],
                             r["group"], r["policy"], r["cluster"], r["path"])).lower()]
    return templates.TemplateResponse(request, "partials/volume_map.html",
                                      {"rows": rows, "q": q, "facts": facts})


@router.get("/volumes/orphans", response_class=HTMLResponse)
def volumes_orphans(request: Request):
    return templates.TemplateResponse(request, "partials/orphans.html",
                                      {"found": cleanup.orphans()})


@router.post("/volumes/orphans/remove", response_class=HTMLResponse)
def remove_orphan(request: Request, kind: str = Form(...), where: str = Form(...),
                  name: str = Form(...)):
    def work(job: Job):
        cleanup.clean_item(job, kind, where, name)
        audit.record("cleanup.item", actor=getattr(request.state, "user", ""),
                     via=getattr(request.state, "via", ""), target=f"{where}/{name}",
                     outcome="ok", detail=f"removed the {kind}")
        inventory.invalidate()

    job = start(f"remove {kind} {name}", "cleanup", work,
                actor=getattr(request.state, "user", ""),
                via=getattr(request.state, "via", ""))
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": True})


@router.post("/volumes/cleanup", response_class=HTMLResponse)
def cleanup_group(request: Request, rg: str = Form(...), keep_directory: str = Form("")):
    def work(job: Job):
        result = cleanup.clean_group(job, rg, drop_directory=keep_directory != "on")
        audit.record("cleanup.group", actor=getattr(request.state, "user", ""),
                     via=getattr(request.state, "via", ""), target=rg, outcome="ok",
                     detail=f"removed {len(result['removed'])} item(s)"
                            + (f"; left {', '.join(result['left'])}" if result["left"] else ""))
        inventory.invalidate()

    job = start(f"clean up {rg}", "cleanup", work, actor=getattr(request.state, "user", ""),
                via=getattr(request.state, "via", ""))
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": True})
