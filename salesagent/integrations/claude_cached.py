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

    import anthropic  # lazy: only needed on a cache miss
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=model, max_tokens=max_tokens,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    out = json.loads(text)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO claude_cache (cache_key, task, model,"
            " prompt_version, response_json, created_at) VALUES (?,?,?,?,?,?)",
            (key, task, model, prompt_version, json.dumps(out), utcnow()))
    return out
