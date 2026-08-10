"""The turn runner: builds the graph once (registry + bound LLM + SQLite
checkpointer) and maps LangGraph's stream onto the SSE event contract:

    thinking (live-only, never persisted), token, tool_start, tool_progress,
    tool_result, artifact, confirm_request, job_started, job_done, done, error

thread_id == conversation_id, so every turn resumes from the durable
checkpoint — which is also what makes interrupt/resume survive restarts.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from ..audit import log
from ..settings import Settings
from ..tools.registry import as_anthropic_tools, discover
from . import graph as graph_mod
from .llm import make_orchestrator

_LOCK = asyncio.Lock()
_GRAPH = None
_SAVER_CM = None

# Cooperative stop: the /stop endpoint flags a conversation; its stream winds
# down at the next event boundary and ends with done(state="stopped") — the
# normal persistence path runs, so status can never wedge at 'running'.
STOP_REQUESTS: set[str] = set()


def request_stop(conversation_id: str) -> None:
    STOP_REQUESTS.add(conversation_id)


async def _get_graph(settings: Settings):
    global _GRAPH, _SAVER_CM
    if _GRAPH is not None:
        return _GRAPH
    async with _LOCK:
        if _GRAPH is not None:
            return _GRAPH
        registry = discover()
        graph_mod.RUNTIME.update(
            settings=settings,
            registry=registry,
            llm=make_orchestrator(settings, as_anthropic_tools(registry)),
        )
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        _SAVER_CM = AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db))
        saver = await _SAVER_CM.__aenter__()   # long-lived server usage
        _GRAPH = graph_mod.build_graph(saver)
        log.info("agent graph ready: %d tools (%s)", len(registry),
                 ", ".join(sorted(registry)))
        return _GRAPH


def _chunk_text(msg: Any) -> str:
    """Text deltas only — thinking is emitted by agent_node through the
    CUSTOM stream (messages-mode callbacks drop thinking parts, verified
    2026-08-06), so extracting it here too would double-print."""
    c = getattr(msg, "content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


# NOTE: dangling tool_calls after a stopped turn are healed in-prompt inside
# agent_node (_heal_dangling) — doing it here from aget_state raced the
# in-flight checkpoint write and read stale state (found live 2026-08-06).


async def _stream(settings: Settings, user: dict, conversation_id: str,
                  graph_input: Any) -> AsyncIterator[tuple[str, dict]]:
    graph = await _get_graph(settings)
    config = {"configurable": {"thread_id": conversation_id,
                               "user": {"id": user.get("id"),
                                        "username": user.get("username")}},
              "recursion_limit": settings.agent_recursion_limit}
    usage = {"input_tokens": 0, "output_tokens": 0}
    interrupted = False
    steps = 0                                 # completed supersteps this turn
    warn_every = settings.agent_step_warn_every
    STOP_REQUESTS.discard(conversation_id)   # clear any stale flag

    try:
        async for mode, payload in graph.astream(
                graph_input, config, stream_mode=["messages", "updates", "custom"]):
            if conversation_id in STOP_REQUESTS:
                STOP_REQUESTS.discard(conversation_id)
                yield "done", {"state": "stopped", "usage": usage}
                return
            if mode == "messages":
                msg, meta = payload
                if meta.get("langgraph_node") == "agent":
                    text = _chunk_text(msg)
                    if text:
                        yield "token", {"text": text}
            elif mode == "custom":
                if isinstance(payload, dict) and "event" in payload:
                    yield payload["event"], payload.get("data", {})
            elif mode == "updates":
                if not isinstance(payload, dict):
                    continue
                if "__interrupt__" in payload:
                    interrupted = True
                    intr = payload["__interrupt__"]
                    value = intr[0].value if isinstance(intr, (list, tuple)) and intr \
                        else getattr(intr, "value", {})
                    yield "confirm_request", value if isinstance(value, dict) else {}
                    continue
                for delta in payload.values():
                    if isinstance(delta, dict):
                        for m in delta.get("messages") or []:
                            if isinstance(m, AIMessage) and m.usage_metadata:
                                usage["input_tokens"] += m.usage_metadata.get("input_tokens", 0)
                                usage["output_tokens"] += m.usage_metadata.get("output_tokens", 0)
                steps += 1
                if warn_every and steps % warn_every == 0:
                    toks = usage["input_tokens"] + usage["output_tokens"]
                    yield "warning", {
                        "steps": steps,
                        "message": (f"⚠ {steps} steps into this turn "
                                    f"(~{toks:,} tokens so far) — still "
                                    "working; hit Stop if this looks stuck.")}
    except GraphRecursionError:
        # the turn ran out of supersteps mid-task; everything completed so far
        # is checkpointed, and _heal_dangling covers any unanswered tool call —
        # the rep can simply continue.
        yield "error", {"message":
                        f"I hit this turn's step limit "
                        f"({settings.agent_recursion_limit} steps) before "
                        "finishing. All progress so far is saved — say "
                        "\"continue\" and I'll pick up where I left off."}
        yield "done", {"state": "stopped", "usage": usage}
        return

    yield "done", {"state": "awaiting_confirmation" if interrupted else "complete",
                   "usage": usage}


async def run_turn(*, settings: Settings, user: dict, conversation_id: str,
                   text: str) -> AsyncIterator[tuple[str, dict]]:
    async for ev in _stream(settings, user, conversation_id,
                            {"messages": [HumanMessage(content=text)]}):
        yield ev


async def run_resume(*, settings: Settings, user: dict, conversation_id: str,
                     approved: bool, note: str = "") -> AsyncIterator[tuple[str, dict]]:
    async for ev in _stream(settings, user, conversation_id,
                            Command(resume={"approved": approved, "note": note})):
        yield ev
