"""Conversation CRUD + the chat turn + confirm-resume endpoints.

Turns return a text/event-stream directly (frontend reads it with fetch +
ReadableStream — EventSource can't POST). Persistence lives HERE, not in the
runner: user message before the stream, assistant render-log message + JSONL
audit when the stream ends.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import AsyncIterator, Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..agent.runner import request_stop, run_resume, run_turn
from ..audit import log, log_turn
from ..auth import current_user, db_conn
from ..db import get_conn, utcnow
from ..settings import load_settings
from .routes_uploads import drain_job_notices, drain_upload_notices
from .sse import SSE_HEADERS, frame, with_keepalive

router = APIRouter(prefix="/api")

# Tool-family labels for the capabilities panel, keyed by the module a tool
# lives in (registry discovery is by module, so this can't drift from code).
_FAMILIES = [
    ("k12", "K12 district intel"),
    ("seamless", "People — Seamless.ai"),
    ("govspend", "Government spend & checkbooks"),
    ("geo", "Company locations & geo"),
    ("price", "Pricing"),
    ("trends", "Sales trends (temporary source)"),
    ("research", "Web research & briefs"),
    ("reference", "Reference data"),
    ("merge", "Prospect merging"),
    ("views", "Workspace views"),
    ("export", "Exports"),
    ("jobs_tool", "Background jobs"),
]


@router.get("/tools")
def list_tools(user: dict = Depends(current_user)):
    """The agent's live tool catalog (for the capabilities panel) — read from
    the same registry the agent binds, so the UI never drifts."""
    from ..tools.registry import discover
    by_module: dict[str, list] = {}
    for spec in discover().values():
        if not spec.enabled:
            continue
        mod = spec.handler.__module__.rsplit(".", 1)[-1]
        by_module.setdefault(mod, []).append({
            "name": spec.name,
            "description": spec.description,
            "cost_class": spec.cost_class.value,
            "needs_confirmation": bool(spec.needs_confirmation),
        })
    groups = []
    for mod, label in _FAMILIES:
        if by_module.get(mod):
            groups.append({"family": label,
                           "tools": sorted(by_module.pop(mod),
                                           key=lambda t: t["name"])})
    for mod, tools in sorted(by_module.items()):   # anything unmapped still shows
        groups.append({"family": mod, "tools": sorted(tools,
                                                      key=lambda t: t["name"])})
    return {"groups": groups}


def _own(conn: sqlite3.Connection, cid: str, user: dict) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(404, "conversation not found")
    return row


@router.get("/conversations")
def list_conversations(user: dict = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db_conn)):
    rows = conn.execute(
        "SELECT id, title, status, updated_at FROM conversations "
        "WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",
        (user["id"],)).fetchall()
    return {"conversations": [dict(r) for r in rows]}


@router.post("/conversations")
def create_conversation(user: dict = Depends(current_user),
                        conn: sqlite3.Connection = Depends(db_conn)):
    settings = load_settings()
    cid = uuid.uuid4().hex[:12]
    now = utcnow()
    with conn:
        conn.execute(
            "INSERT INTO conversations (id, user_id, title, status, "
            "budget_credits, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (cid, user["id"], None, "idle", settings.convo_budget_default,
             now, now))
    return {"id": cid}


@router.get("/conversations/{cid}")
def get_conversation(cid: str, user: dict = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db_conn)):
    convo = _own(conn, cid, user)
    msgs = conn.execute(
        "SELECT id, role, content_json, created_at FROM messages "
        "WHERE conversation_id=? ORDER BY id", (cid,)).fetchall()
    arts = conn.execute(
        "SELECT a.artifact_id, a.title, a.created_by_tool, a.created_at,"
        " a.archived,"
        " (SELECT MAX(version) FROM artifact_versions v"
        "  WHERE v.artifact_id = a.artifact_id) AS latest_version,"
        # kind can change across versions (to_map/to_chart) — report the
        # LATEST version's kind or reloads render the original one
        " COALESCE((SELECT kind FROM artifact_versions v"
        "  WHERE v.artifact_id = a.artifact_id"
        "  ORDER BY v.version DESC LIMIT 1), a.kind) AS kind "
        "FROM artifacts a WHERE a.conversation_id=? ORDER BY a.created_at",
        (cid,)).fetchall()
    return {"conversation": dict(convo),
            "messages": [
                {**dict(m), "content": json.loads(m["content_json"])}
                for m in msgs],
            "artifacts": [dict(a) for a in arts]}


def _turn_response(conn: sqlite3.Connection, settings, user: dict, cid: str,
                   events: AsyncIterator[tuple[str, dict]],
                   audit_input: str) -> StreamingResponse:
    """Shared SSE wrapper: stream events, persist the assistant render-log
    message and the JSONL turn audit when the stream ends."""

    async def gen() -> AsyncIterator[str]:
        assistant_text: list[str] = []
        artifact_events: list[dict] = []
        tool_calls: list[dict] = []
        final_state = "complete"
        usage: dict = {}
        error: str | None = None

        # Ordered render log of the turn (thinking runs, tool chips, text
        # runs) persisted with the assistant message so history replays look
        # exactly like the live stream did.
        events_log: list[dict] = []
        MAX_EVENTS, MAX_RUN = 400, 30_000

        def _append_run(kind: str, text: str) -> None:
            if events_log and events_log[-1]["kind"] == kind:
                if len(events_log[-1]["text"]) < MAX_RUN:
                    events_log[-1]["text"] += text
            elif len(events_log) < MAX_EVENTS:
                events_log.append({"kind": kind, "text": text})

        try:
            async for event, data in events:
                if event == "token":
                    assistant_text.append(data.get("text", ""))
                    _append_run("text", data.get("text", ""))
                elif event == "thinking":
                    _append_run("thinking", data.get("text", ""))
                elif event == "artifact":
                    artifact_events.append(data)
                elif event == "tool_start":
                    if len(events_log) < MAX_EVENTS:
                        events_log.append({
                            "kind": "tool", "tool": data.get("tool"),
                            "args_preview": data.get("args_preview"), "ok": None})
                elif event == "tool_result":
                    tool_calls.append(data)
                    hit = next((e for e in reversed(events_log)
                                if e["kind"] == "tool" and e.get("ok") is None
                                and e.get("tool") == data.get("tool")), None)
                    if hit is None and len(events_log) < MAX_EVENTS:
                        hit = {"kind": "tool", "tool": data.get("tool")}
                        events_log.append(hit)
                    if hit is not None:
                        hit.update(ok=data.get("ok", True),
                                   row_count=data.get("row_count"),
                                   cache_hit=bool(data.get("cache_hit")),
                                   credits_spent=data.get("credits_spent") or 0)
                elif event == "confirm_request":
                    if len(events_log) < MAX_EVENTS:
                        events_log.append({
                            "kind": "confirm",
                            "description": data.get("description"),
                            "estimated_credits": data.get("estimated_credits"),
                            "budget": data.get("budget")})
                elif event == "warning":
                    if len(events_log) < MAX_EVENTS:
                        events_log.append({"kind": "note", "variant": "warn",
                                           "text": data.get("message") or ""})
                elif event == "error":
                    # runner-yielded soft errors (e.g. step-limit) must persist
                    # like raised ones so reloads still show them
                    error = data.get("message") or "unknown error"
                elif event == "done":
                    final_state = data.get("state", "complete")
                    usage = data.get("usage", {})
                yield frame(event, data)
        except Exception as e:
            log.exception("turn failed convo=%s", cid)
            error = f"{type(e).__name__}: {e}"
            yield frame("error", {"message": error})
        finally:
            end_status = ("awaiting_confirmation"
                          if final_state == "awaiting_confirmation" else "idle")
            # a FRESH connection here, never the request-scoped one: when the
            # client disconnects mid-stream, FastAPI may tear down the request
            # dependency before this finally runs — writing on that closed
            # conn silently failed and left conversations wedged at 'running'
            # (found live 2026-08-06).
            if final_state == "stopped" and len(events_log) < MAX_EVENTS:
                events_log.append({"kind": "note",
                                   "text": "⏹ stopped — partial answer kept"})
            try:
                fconn = get_conn(settings.app_db)
                try:
                    with fconn:
                        if assistant_text or artifact_events or error or events_log:
                            content = {"text": "".join(assistant_text),
                                       "artifact_ids": [a.get("artifact_id")
                                                        for a in artifact_events],
                                       "error": error,
                                       "events": events_log}
                            fconn.execute(
                                "INSERT INTO messages (conversation_id, role, "
                                "content_json, created_at) VALUES (?,?,?,?)",
                                (cid, "assistant", json.dumps(content), utcnow()))
                        fconn.execute(
                            "UPDATE conversations SET status=?, updated_at=? "
                            "WHERE id=?", (end_status, utcnow(), cid))
                finally:
                    fconn.close()
            except Exception:
                log.exception("post-turn persistence failed convo=%s", cid)
            log_turn(settings, cid, {
                "user": user["username"], "input": audit_input,
                "tool_calls": tool_calls,
                "artifacts": [a.get("artifact_id") for a in artifact_events],
                "usage": usage, "state": final_state, "error": error})

    return StreamingResponse(with_keepalive(gen()),
                             media_type="text/event-stream", headers=SSE_HEADERS)


class MessageReq(BaseModel):
    text: str


@router.post("/conversations/{cid}/message")
async def post_message(cid: str, req: MessageReq,
                       user: dict = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db_conn)):
    convo = _own(conn, cid, user)
    if convo["status"] == "running":
        raise HTTPException(409, "a turn is already running in this conversation")
    settings = load_settings()
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "empty message")
    now = utcnow()
    with conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content_json, created_at)"
            " VALUES (?,?,?,?)",
            (cid, "user", json.dumps({"text": text}), now))
        conn.execute(
            "UPDATE conversations SET status='running', updated_at=?, "
            "title=COALESCE(title, ?) WHERE id=?",
            (now, text[:60], cid))
    # unnotified uploads + finished jobs ride into the agent's view of this
    # turn; the persisted user message stays clean
    notices = "\n".join(x for x in (drain_upload_notices(conn, cid),
                                    drain_job_notices(conn, cid)) if x)
    agent_text = f"{notices}\n\n{text}" if notices else text
    events = run_turn(settings=settings, user=user, conversation_id=cid,
                      text=agent_text)
    return _turn_response(conn, settings, user, cid, events, audit_input=text)


@router.post("/conversations/{cid}/stop")
def post_stop(cid: str, user: dict = Depends(current_user),
              conn: sqlite3.Connection = Depends(db_conn)):
    """Stop button: flag the running turn to wind down at its next event
    boundary (the client also aborts its fetch for instant UI feedback).
    Partial output persists via the normal end-of-stream path."""
    convo = _own(conn, cid, user)
    if convo["status"] != "running":
        return {"ok": True, "note": "nothing running"}
    request_stop(cid)
    return {"ok": True}


class ResumeReq(BaseModel):
    approved: bool
    note: str = ""


@router.post("/conversations/{cid}/resume")
async def post_resume(cid: str, req: ResumeReq,
                      user: dict = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db_conn)):
    convo = _own(conn, cid, user)
    if convo["status"] != "awaiting_confirmation":
        raise HTTPException(409, "nothing awaiting confirmation here")
    settings = load_settings()
    with conn:
        conn.execute("UPDATE conversations SET status='running', updated_at=? "
                     "WHERE id=?", (utcnow(), cid))
    events = run_resume(settings=settings, user=user, conversation_id=cid,
                        approved=req.approved, note=req.note)
    return _turn_response(conn, settings, user, cid, events,
                          audit_input=f"[resume approved={req.approved}] {req.note}")
