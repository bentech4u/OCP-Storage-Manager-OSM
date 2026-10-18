"""Prechecks: everything to be sure of before provisioning or moving a site."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..services import audit, prechecks as service
from ..templating import templates

router = APIRouter()


@router.get("/prechecks", response_class=HTMLResponse)
def prechecks_page(request: Request):
    return templates.TemplateResponse(request, "prechecks.html", {"page": "prechecks",
                                                                  "result": None})


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
