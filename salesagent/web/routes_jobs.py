"""Job listing + per-job SSE progress (poll-based over the ring log + row —
the PriceEngine pattern; snapshot replay for late subscribers)."""
from __future__ import annotations

import asyncio
import sqlite3
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ..auth import current_user, db_conn
from ..jobs.manager import JobManager
from ..settings import load_settings
from .sse import SSE_HEADERS, frame

router = APIRouter(prefix="/api")


@router.get("/jobs")
def list_jobs(conversation_id: str | None = None,
              user: dict = Depends(current_user),
              conn: sqlite3.Connection = Depends(db_conn)):
    q = ("SELECT id, kind, tool_name, status, progress_done, progress_total,"
         " message, result_artifact_id, error, conversation_id, created_at,"
         " finished_at FROM jobs")
    args: list = []
    if conversation_id:
        q += " WHERE conversation_id=?"
        args.append(conversation_id)
    q += " ORDER BY created_at DESC LIMIT 50"
    return {"jobs": [dict(r) for r in conn.execute(q, args)]}


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, user: dict = Depends(current_user)):
    settings = load_settings()
    mgr = JobManager.get(settings)
    if not mgr.status(job_id):
        raise HTTPException(404, "job not found")

    async def gen() -> AsyncIterator[str]:
        sent_logs = 0
        last_snapshot = None
        while True:
            st = mgr.status(job_id, log_tail=400)
            if st is None:
                yield frame("error", {"message": "job vanished"})
                return
            logs = st.pop("log", [])
            for entry in logs[sent_logs:]:
                yield frame("log", entry)
            sent_logs = len(logs)
            snap = {k: st.get(k) for k in
                    ("status", "progress_done", "progress_total", "message",
                     "result_artifact_id", "error")}
            if snap != last_snapshot:
                yield frame("job_update", {"job_id": job_id, **snap})
                last_snapshot = snap
            if st["status"] in ("done", "error", "cancelled"):
                yield frame("job_done" if st["status"] == "done" else "error",
                            {"job_id": job_id,
                             "artifact_id": st.get("result_artifact_id"),
                             "message": st.get("error")})
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers=SSE_HEADERS)
