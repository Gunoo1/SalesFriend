"""Serper.dev (Google SERP/Maps API). Metered ~$0.001/query — cached org-wide.
The /maps closed-status field name is verified on first live use (parser
tolerates both the maps field and knowledge-graph text)."""
from __future__ import annotations

import sqlite3

import requests

from ..settings import Settings
from .http_cache import cache_get, cache_key, cache_put


class SerperError(RuntimeError):
    pass


def _post(settings: Settings, conn: sqlite3.Connection, endpoint: str,
          payload: dict, ttl_days: float) -> tuple[dict, bool]:
    if not settings.serper_api_key:
        raise SerperError("SERPER_API_KEY not configured in .env")
    key = cache_key("serper", endpoint, payload)
    hit = cache_get(conn, key)
    if hit is not None:
        return hit, True
    r = requests.post(f"https://google.serper.dev/{endpoint}", json=payload,
                      headers={"X-API-KEY": settings.serper_api_key,
                               "Content-Type": "application/json"}, timeout=30)
    if r.status_code != 200:
        raise SerperError(f"serper {endpoint} HTTP {r.status_code}: {r.text[:150]}")
    data = r.json()
    cache_put(conn, key, "serper", endpoint, payload, data, ttl_days)
    return data, False


def search(settings, conn, q: str, num: int = 10) -> tuple[dict, bool]:
    return _post(settings, conn, "search", {"q": q, "num": min(num, 20)},
                 ttl_days=7)


def maps(settings, conn, q: str) -> tuple[dict, bool]:
    return _post(settings, conn, "maps", {"q": q}, ttl_days=7)


def closed_signal(place: dict) -> str | None:
    """'closed_permanent' | 'closed_temporary' | None — tolerant of field-name
    drift. Live-verified 2026-08-06: serper /maps sends `isPermanentlyClosed`
    (true on closed places, e.g. shuttered Sears stores)."""
    for k in ("isPermanentlyClosed", "permanentlyClosed", "permanently_closed",
              "is_permanently_closed"):
        if place.get(k) is True:
            return "closed_permanent"
    for k in ("isTemporarilyClosed", "temporarilyClosed", "temporarily_closed",
              "is_temporarily_closed"):
        if place.get(k) is True:
            return "closed_temporary"
    blob = " ".join(str(place.get(k) or "") for k in
                    ("status", "businessStatus", "description", "type",
                     "openingHours")).lower()
    if "permanently closed" in blob:
        return "closed_permanent"
    if "temporarily closed" in blob:
        return "closed_temporary"
    return None
