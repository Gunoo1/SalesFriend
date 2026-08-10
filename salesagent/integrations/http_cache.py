"""Shared external-API access with the org-wide api_cache table.

Every provider gets (a) response caching with a TTL so one rep's pull is the
next rep's cache hit, and (b) a uniform cache_key scheme. Envelope meta can
then report cache_hit truthfully.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import requests

from ..db import utcnow

UA = {"User-Agent": "SalesAgent/1.0 (Enalas internal; contact gunoo.shin@enalasconsulting.com)"}


def _with_retry(fn, attempts: int = 3):
    """Transient-network retry (connection resets happen; a blip must not fail
    a whole tool call). Backoff 2s/5s; non-network errors propagate."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last = e
            if i < attempts - 1:
                time.sleep((2, 5)[min(i, 1)])
    raise last


def cache_key(provider: str, endpoint: str, params) -> str:
    blob = json.dumps([provider, endpoint, params], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_get(conn: sqlite3.Connection, key: str) -> dict | list | None:
    row = conn.execute(
        "SELECT response_json FROM api_cache WHERE cache_key=? AND expires_at > ?",
        (key, utcnow())).fetchone()
    return json.loads(row["response_json"]) if row else None


def cache_put(conn: sqlite3.Connection, key: str, provider: str, endpoint: str,
              params, response, ttl_days: float) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(days=ttl_days)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO api_cache (cache_key, provider, endpoint,"
            " params_json, response_json, fetched_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (key, provider, endpoint, json.dumps(params, default=str),
             json.dumps(response, default=str), utcnow(), expires))


def cached_get_json(conn: sqlite3.Connection, provider: str, url: str,
                    params: dict | None, ttl_days: float,
                    timeout: int = 45) -> tuple[dict | list | None, bool]:
    """(response, cache_hit). Raises for network errors; non-200 returns None."""
    key = cache_key(provider, url, params)
    hit = cache_get(conn, key)
    if hit is not None:
        return hit, True
    r = _with_retry(lambda: requests.get(url, params=params or {}, headers=UA,
                                         timeout=timeout))
    if r.status_code != 200:
        return None, False
    try:
        data = r.json()
    except ValueError:
        return None, False
    cache_put(conn, key, provider, url, params, data, ttl_days)
    return data, False


def cached_post_json(conn: sqlite3.Connection, provider: str, url: str,
                     payload: dict, ttl_days: float,
                     headers: dict | None = None,
                     timeout: int = 60) -> tuple[dict | None, bool]:
    key = cache_key(provider, url, payload)
    hit = cache_get(conn, key)
    if hit is not None:
        return hit, True
    r = _with_retry(lambda: requests.post(url, json=payload,
                                          headers={**UA, **(headers or {})},
                                          timeout=timeout))
    if r.status_code != 200:
        return None, False
    try:
        data = r.json()
    except ValueError:
        return None, False
    cache_put(conn, key, provider, url, payload, data, ttl_days)
    return data, False
