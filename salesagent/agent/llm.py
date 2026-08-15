"""All orchestrator LLM construction lives here — the one seam that decides
which backend serves the agent loop, keyed off ORCHESTRATOR_MODEL:

- names starting with "claude" -> ChatAnthropic (hosted, needs API key)
- anything else -> ChatOllama against OLLAMA_BASE_URL (e.g. qwen3:8b
  served by Ollama on the droplet)

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

Ollama notes (added 2026-08-15, targeting qwen3:8b on the 8GB/4vCPU DO
droplet):
- tools are converted to OpenAI function format here rather than trusting
  langchain-core's dict auto-detection of Anthropic-format specs, so the
  registry stays backend-agnostic across library versions
- num_ctx MUST be set explicitly: Ollama's small default would silently
  truncate system prompt + ~38 tool schemas + history and the agent
  degrades into nonsense with no error anywhere
- reasoning=False: qwen3's thinking mode is minutes of extra latency at
  CPU token rates; tool calling works without it
- num_predict caps generation (Claude's 16384 at ~5 tok/s would be an
  hour of CPU time)
"""
from __future__ import annotations

from langchain_anthropic import ChatAnthropic

from ..settings import Settings


def _openai_tools(anthropic_tools: list[dict]) -> list[dict]:
    """Registry specs ({name, description, input_schema}) -> OpenAI format."""
    return [{"type": "function",
             "function": {"name": t["name"],
                          "description": t.get("description", ""),
                          "parameters": t.get("input_schema")
                          or {"type": "object", "properties": {}}}}
            for t in anthropic_tools]


def make_orchestrator(settings: Settings, anthropic_tools: list[dict]):
    if settings.orchestrator_model.startswith("claude"):
        llm = ChatAnthropic(model=settings.orchestrator_model,
                            max_tokens=16384, timeout=240, max_retries=2,
                            api_key=settings.anthropic_api_key,
                            model_kwargs={"cache_control": {"type": "ephemeral"}})
        return llm.bind_tools(anthropic_tools) if anthropic_tools else llm

    from langchain_ollama import ChatOllama  # lazy: Claude-only installs skip it
    llm = ChatOllama(model=settings.orchestrator_model,
                     base_url=settings.ollama_base_url,
                     num_ctx=settings.ollama_num_ctx,
                     num_predict=settings.ollama_num_predict,
                     reasoning=False)
    return llm.bind_tools(_openai_tools(anthropic_tools)) if anthropic_tools else llm
