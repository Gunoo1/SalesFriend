"""DB-memoized Haiku calls for in-tool extraction/classification — port of
K12Intel k12/claude_client.py (same sha256 key scheme: task|model|prompt_ver|
canonical input JSON; only invalidation is bumping prompt_version)."""
from __future__ import annotations

import hashlib
import json
import sqlite3

from ..db import utcnow
from ..settings import Settings


def cache_key(task: str, model: str, prompt_version: str, input_obj) -> str:
    canonical = json.dumps(input_obj, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(
        f"{task}|{model}|{prompt_version}|{canonical}".encode("utf-8")).hexdigest()


def cached_call(settings: Settings, conn: sqlite3.Connection, *, task: str,
                prompt_version: str, input_obj, prompt: str, schema: dict,
                max_tokens: int = 1000, model: str | None = None) -> dict:
    model = model or settings.extract_model
    key = cache_key(task, model, prompt_version, input_obj)
    row = conn.execute("SELECT response_json FROM claude_cache WHERE cache_key=?",
                       (key,)).fetchone()
    if row:
        return json.loads(row["response_json"])

    if model.startswith("claude"):
        import anthropic  # lazy: only needed on a cache miss
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model=model, max_tokens=max_tokens,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in resp.content if b.type == "text")
        out = json.loads(text)
    else:
        # Ollama structured outputs: format=<json schema> constrains decoding,
        # same contract as Anthropic's output_config above. think=False skips
        # qwen3-style deliberation (pure latency at CPU speeds); retried
        # without the flag for models that reject it.
        import requests
        payload = {"model": model, "stream": False, "think": False,
                   "format": schema,
                   "messages": [{"role": "user", "content": prompt}],
                   "options": {"num_ctx": settings.ollama_num_ctx,
                               "num_predict": max_tokens}}
        r = requests.post(f"{settings.ollama_base_url}/api/chat",
                          json=payload, timeout=600)
        if r.status_code == 400 and "think" in r.text.lower():
            payload.pop("think")
            r = requests.post(f"{settings.ollama_base_url}/api/chat",
                              json=payload, timeout=600)
        r.raise_for_status()
        out = json.loads(r.json()["message"]["content"])
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO claude_cache (cache_key, task, model,"
            " prompt_version, response_json, created_at) VALUES (?,?,?,?,?,?)",
            (key, task, model, prompt_version, json.dumps(out), utcnow()))
    return out
