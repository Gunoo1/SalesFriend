"""SSE framing helpers (pattern from Tim Montondo/price_comparison/backend/sse.py:
named event + JSON data per frame, comment keepalives so proxies don't cut idle
streams)."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

KEEPALIVE_S = 15


def frame(event: str, data: dict | list | str) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def with_keepalive(gen: AsyncIterator[str]) -> AsyncIterator[str]:
    """Wrap a frame generator; emit ': keepalive' comments during quiet gaps.

    MUST NOT use asyncio.wait_for here: its timeout CANCELS the pending
    __anext__(), which throws CancelledError into the underlying generator
    chain (the LLM/tool call in flight) and silently kills the turn — every
    stream with a >15s quiet gap died that way until 2026-08-06. asyncio.wait
    leaves the task running and just lets us tick a comment."""
    it = gen.__aiter__()
    task: asyncio.Task | None = None
    try:
        while True:
            task = asyncio.ensure_future(it.__anext__())
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=KEEPALIVE_S)
                if not done:
                    yield ": keepalive\n\n"
            try:
                nxt = task.result()
            except StopAsyncIteration:
                return
            task = None
            yield nxt
    finally:
        if task is not None and not task.done():
            task.cancel()   # client disconnected — now cancelling is correct


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}
