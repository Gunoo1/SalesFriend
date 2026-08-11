"""All ChatAnthropic construction lives here — the one seam to swap if
langchain-anthropic ever lags a model/API change (raw-SDK fallback documented
in the plan).

claude-sonnet-5 notes (verified by the M1 smoke test, 2026-08-05, pins
langchain-anthropic==1.5.4):
- never set temperature/top_p (400 on non-default sampling params)
- adaptive thinking is on by default; max_tokens caps thinking + text
  TOGETHER — 8192 proved too small live (2026-08-06: a deliberation-heavy
  turn spent the whole budget thinking and returned an EMPTY answer), so
  16384 gives room for both
- model id is exactly "claude-sonnet-5" (no date suffix)

Prompt caching (added 2026-08-11 — turn logs showed 87% of spend was input
tokens, one tool-loop turn alone re-sent 1.3M): top-level
cache_control={"type":"ephemeral"} makes the API cache the whole prompt
(tools + system + history) with automatic breakpoints. Cached reads bill at
0.1x, so every superstep after the first re-reads the prefix at ~90% off.
langchain-anthropic 1.5.4 passes the kwarg through verbatim on the direct
API path (_get_request_payload); model_kwargs applies it to every call.
"""
from __future__ import annotations

from langchain_anthropic import ChatAnthropic

from ..settings import Settings


def make_orchestrator(settings: Settings, anthropic_tools: list[dict]):
    llm = ChatAnthropic(model=settings.orchestrator_model,
                        max_tokens=16384, timeout=240, max_retries=2,
                        api_key=settings.anthropic_api_key,
                        model_kwargs={"cache_control": {"type": "ephemeral"}})
    return llm.bind_tools(anthropic_tools) if anthropic_tools else llm
