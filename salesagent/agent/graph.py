"""The agent <-> tools loop.

Custom tools node (not the prebuilt ToolNode) so envelopes, artifact writes,
the confirm-before-spend gate and the ledger stay ours. The gate runs FIRST —
on resume the node re-executes from its start, so no handler may run before
it (free handlers are cache-backed, making re-execution after an approved
resume harmless).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..db import utcnow
from ..settings import Settings
from ..tools.registry import CostClass, ToolSpec, execute_tool
from .state import AgentState

# Built once at startup by the runner; nodes read it (settings/registry/llm
# are not checkpoint-serializable, so they never ride in config).
RUNTIME: dict = {}

_PROMPT_CACHE: dict = {"path": None, "mtime": None, "text": ""}


def load_system_prompt(settings: Settings) -> str:
    path = Path(settings.prompts_dir) / "system.md"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "You are SalesAgent, a data assistant for sales reps."
    if _PROMPT_CACHE["path"] != path or _PROMPT_CACHE["mtime"] != mtime:
        _PROMPT_CACHE.update(path=path, mtime=mtime,
                             text=path.read_text(encoding="utf-8"))
    return _PROMPT_CACHE["text"]


def render_system_prompt(settings: Settings, config: dict) -> str:
    conf = config.get("configurable", {})
    user = conf.get("user") or {}
    lines = [load_system_prompt(settings), "",
             f"Today (UTC): {utcnow()[:10]}. Signed-in rep: "
             f"{user.get('username', 'unknown')}."]
    return "\n".join(lines)


def _args_preview(args: dict, cap: int = 220) -> str:
    s = json.dumps(args or {}, ensure_ascii=False, default=str)
    return s if len(s) <= cap else s[:cap] + "…"


def _heal_dangling(msgs: list) -> list:
    """A stopped turn (or a new message typed past a pending approval card)
    leaves an AIMessage whose tool_calls have no results — the API rejects
    that outright ('tool_use without tool_result'). Rebuild the PROMPT list
    with synthesized cancelled results inserted right after the dangling
    call. Never persisted; recomputed per call from the true current state,
    so there is no checkpoint race."""
    answered = {getattr(m, "tool_call_id", None)
                for m in msgs if isinstance(m, ToolMessage)}
    out: list = []
    for m in msgs:
        out.append(m)
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                if tc["id"] not in answered:
                    out.append(ToolMessage(
                        content=json.dumps({
                            "ok": False, "cancelled": True,
                            "summary": "cancelled — the user stopped the turn "
                                       "(or moved on) before this tool ran"}),
                        tool_call_id=tc["id"], name=tc.get("name") or ""))
    return out


async def agent_node(state: AgentState, config: RunnableConfig) -> dict:
    """Streams the model call itself so sonnet-5's adaptive-thinking deltas
    can be surfaced live: LangGraph's messages-mode callback tap carries TEXT
    tokens fine but drops thinking parts (verified 2026-08-06), so thinking
    goes out through the custom stream writer instead. Text still reaches the
    UI via messages mode — nothing is emitted twice."""
    llm = RUNTIME["llm"]
    writer = get_stream_writer()
    sysmsg = SystemMessage(content=render_system_prompt(RUNTIME["settings"], config))
    resp = None
    async for chunk in llm.astream([sysmsg, *_heal_dangling(list(state["messages"]))]):
        resp = chunk if resp is None else resp + chunk
        c = chunk.content
        if isinstance(c, list):
            think = "".join(p.get("thinking", "") for p in c
                            if isinstance(p, dict) and p.get("type") == "thinking")
            if think:
                writer({"event": "thinking", "data": {"text": think}})
    return {"messages": [resp] if resp is not None else []}


def route(state: AgentState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


async def tools_node(state: AgentState, config: RunnableConfig) -> dict:
    settings: Settings = RUNTIME["settings"]
    registry: dict[str, ToolSpec] = RUNTIME["registry"]
    writer = get_stream_writer()
    conf = config.get("configurable", {})
    user = conf.get("user") or {}
    conversation_id = conf.get("thread_id") or ""

    last = state["messages"][-1]
    calls = list(last.tool_calls)

    # ---- confirm gate (BEFORE any handler runs) ----
    gated = []
    for tc in calls:
        spec = registry.get(tc["name"])
        if spec and (spec.needs_confirmation
                     or spec.cost_class == CostClass.PAID_CREDITS):
            est = None
            if spec.estimate_cost:
                try:
                    est = int(spec.estimate_cost(tc["args"] or {}))
                except Exception:
                    est = None
            gated.append({"id": tc["id"], "tool": tc["name"],
                          "params": tc["args"] or {}, "estimated_credits": est})
    declined_note: str | None = None
    declined_ids: set[str] = set()
    if gated:
        parts = []
        for g in gated:
            reason = (g["params"].get("reason") or "").strip()
            n = g["estimated_credits"]
            parts.append(f"{g['tool']}"
                         + (f" (~{n} credits)" if n is not None else "")
                         + (f" — {reason}" if reason else ""))
        decision = interrupt({
            "tools": gated,
            "description": "; ".join(parts),
            "estimated_credits": sum(g["estimated_credits"] or 0 for g in gated),
        })
        if not (decision or {}).get("approved"):
            declined_note = (decision or {}).get("note") or ""
            declined_ids = {g["id"] for g in gated}

    def emit(event: str, data: dict) -> None:
        writer({"event": event, "data": data})

    results: list[ToolMessage] = []
    credits_turn = int(state.get("credits_spent_turn") or 0)
    for tc in calls:
        name, args, tcid = tc["name"], tc["args"] or {}, tc["id"]
        if tcid in declined_ids:
            content = {"ok": False, "declined": True,
                       "summary": ("The user DECLINED this paid action. Do not "
                                   "retry it; adapt or ask what they'd like. "
                                   + (f"User note: {declined_note}" if declined_note else "")).strip()}
            results.append(ToolMessage(content=json.dumps(content),
                                       tool_call_id=tcid, name=name))
            continue
        emit("tool_start", {"tool": name, "args_preview": _args_preview(args)})
        t0 = time.monotonic()
        env = await asyncio.to_thread(execute_tool, settings, registry, user,
                                      conversation_id, emit, name, args)
        credits_turn += int(env.get("credits_spent") or 0)
        emit("tool_result", {
            "tool": name, "ok": env.get("ok", True), "kind": env.get("kind"),
            "row_count": env.get("row_count"),
            "artifact_id": env.get("artifact_id"),
            "cache_hit": bool(env.get("cache_hit")),
            "credits_spent": env.get("credits_spent") or 0,
            "duration_ms": int((time.monotonic() - t0) * 1000)})
        results.append(ToolMessage(content=json.dumps(env, ensure_ascii=False,
                                                      default=str),
                                   tool_call_id=tcid, name=name))
    return {"messages": results, "credits_spent_turn": credits_turn}


def build_graph(checkpointer):
    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)
