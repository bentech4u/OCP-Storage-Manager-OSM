"""JSON and streaming endpoints used by the pages."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from ..services import inventory, jobs
from ..templating import templates

router = APIRouter(prefix="/api")


@router.get("/jobs/{job_id}")
async def job_state(job_id: str, after: int = 0):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"detail": "no such job"}, status_code=404)
    return job.snapshot(after=after)


@router.get("/jobs/{job_id}/stream")
async def job_stream(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"detail": "no such job"}, status_code=404)

    async def gen():
        sent = 0
        while True:
            snap = job.snapshot(after=sent)
            sent += len(snap["lines"])
            if snap["lines"] or snap["status"] != "running":
                yield f"data: {json.dumps(snap)}\n\n"
            if snap["status"] != "running":
                break
            await asyncio.sleep(0.7)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/jobs/{job_id}/console", response_class=HTMLResponse)
async def job_console(request: Request, job_id: str):
    job = jobs.get(job_id)
    if not job:
        return HTMLResponse('<div class="empty">That job is gone.</div>')
    return templates.TemplateResponse(request, "partials/console.html",
                                      {"job": job.snapshot(), "follow": job.status == "running"})


@router.get("/inventory")
async def inventory_json(force: bool = False):
    return inventory.snapshot(force=force)
