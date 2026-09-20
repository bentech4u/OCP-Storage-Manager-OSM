"""Dashboard."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..services import inventory
from ..services.jobs import recent
from ..templating import templates

router = APIRouter()


async def dashboard(request: Request, force: bool = False) -> HTMLResponse:
    data = inventory.snapshot(force=force)
    warnings = []
    if not data["tools"]["repctl"]["found"]:
        warnings.append(("repctl is not installed", "Replication actions need it. "
                         "Install it on the Install and Configure page.", "/install#tools"))
    for c in data["clusters"]:
        if not c.get("reachable"):
            warnings.append((f"Cluster {c['id']} is unreachable", c.get("error", ""), "/install#clusters"))
        elif not c.get("driver", {}).get("controller_ready"):
            warnings.append((f"No running driver on {c['id']}",
                             "The PowerScale driver is not installed or not ready.", "/install#driver"))
    for a in data["arrays"]:
        if not a.get("reachable"):
            warnings.append((f"Array {a.get('name', a.get('id'))} is unreachable",
                             a.get("error", ""), "/install#arrays"))
        elif a.get("missing_privileges"):
            warnings.append((f"Array {a['name']} is missing privileges",
                             ", ".join(a["missing_privileges"]), "/install#arrays"))
    return templates.TemplateResponse(request, "home.html", {
        "page": "home", "data": data, "jobs": recent(8), "warnings": warnings,
    })


@router.get("/partials/dashboard", response_class=HTMLResponse)
async def dashboard_partial(request: Request):
    data = inventory.snapshot(force=True)
    return templates.TemplateResponse(request, "partials/dashboard_body.html",
                                      {"data": data, "page": "home"})
