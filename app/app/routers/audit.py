"""The record: who did what, filterable, exportable, and checkable."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..services import audit as audit_service
from ..templating import templates

router = APIRouter()


PAGE_SIZES = [10, 20, 50, 100]


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, actor: str = "", action: str = "", target: str = "",
               since: str = "", until: str = "", text: str = "", size: int = 20,
               page: int = 1):
    size = size if size in PAGE_SIZES else 20
    page = max(1, page)
    filters = {"actor": actor, "action": action, "target": target, "since": since,
               "until": until, "text": text}
    total = audit_service.count(**filters)
    pages = max(1, -(-total // size))                 # round up
    page = min(page, pages)
    rows = audit_service.entries(limit=size, offset=(page - 1) * size, **filters)
    query = dict(filters, size=size)
    keep = urlencode({k: v for k, v in query.items() if v})
    return templates.TemplateResponse(request, "audit.html", {
        "page": "audit", "rows": rows, "q": dict(query, page=page),
        "actions": audit_service.actions(),
        "download": "&".join(f"{k}={v}" for k, v in filters.items() if v),
        "total": total, "pages": pages, "page_no": page, "size": size,
        "sizes": PAGE_SIZES, "keep": keep,
        "first_row": 0 if not total else (page - 1) * size + 1,
        "last_row": min(page * size, total),
        "archived": audit_service.archived(),
    })


@router.get("/audit/export.csv", response_class=PlainTextResponse)
def audit_csv(request: Request, actor: str = "", action: str = "", target: str = "",
              since: str = "", until: str = "", text: str = "", limit: int = 20000):
    rows = audit_service.entries(limit=limit, actor=actor, action=action, target=target,
                                 since=since, until=until, text=text)
    return PlainTextResponse(audit_service.to_csv(rows), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="osm-audit.csv"'})


@router.get("/audit/export.jsonl", response_class=PlainTextResponse)
def audit_jsonl(request: Request, actor: str = "", action: str = "", target: str = "",
                since: str = "", until: str = "", text: str = "", limit: int = 20000):
    import json
    rows = audit_service.entries(limit=limit, actor=actor, action=action, target=target,
                                 since=since, until=until, text=text)
    body = "\n".join(json.dumps(r, separators=(",", ":")) for r in reversed(rows))
    return PlainTextResponse(body + "\n", media_type="application/x-ndjson", headers={
        "Content-Disposition": 'attachment; filename="osm-audit.jsonl"'})


@router.post("/audit/verify", response_class=HTMLResponse)
def audit_verify(request: Request):
    result = audit_service.verify()
    if result["ok"]:
        files = ", ".join(result.get("files", [])) or "none yet"
        return HTMLResponse(
            '<div class="banner ok"><span>✓</span><div>The record is intact: '
            f'{result["checked"]} entries check out against the entry before them.'
            f'<div class="small muted">files: {files}</div></div></div>')
    return HTMLResponse(
        '<div class="banner bad"><span>⚠</span><div>The record does not add up. '
        f'{result["problem"]}<div class="small muted">{result["checked"]} entries were fine '
        'before that point.</div></div></div>')
