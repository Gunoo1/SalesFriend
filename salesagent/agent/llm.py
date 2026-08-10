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
"""
from __future__ import annotations

from langchain_anthropic import ChatAnthropic

from ..settings import Settings


def make_orchestrator(settings: Settings, anthropic_tools: list[dict]):
    llm = ChatAnthropic(model=settings.orchestrator_model,
                        max_tokens=16384, timeout=240, max_retries=2,
                        api_key=settings.anthropic_api_key)
    return llm.bind_tools(anthropic_tools) if anthropic_tools else llm
