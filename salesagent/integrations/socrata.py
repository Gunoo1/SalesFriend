"""Socrata SoQL helpers, ported from timMtesting/t03b + t04:

- $q full-text search means we never need to know a dataset's vendor column
- detect() picks columns DATA-driven, not name-driven (CT's payment_id parses
  as float but isn't money; MO's agency_name contains 'name' but is the buyer)
- $offset paging until a short page (first-page-only silently undercounts)
"""
from __future__ import annotations

import re
import sqlite3

from .http_cache import cached_get_json

EDU = re.compile(r"(?i)school|educat|universit|college|district|academy|"
                 r"board of ed|pupil|isd\b|csd\b")
ID_WORDS = ("id", "code", "number", "no", "year", "fy", "date", "count",
            "order", "line")


def floatable(v) -> bool:
    try:
        float(str(v).replace("$", "").replace(",", ""))
        return True
    except (ValueError, TypeError):
        return False


def to_float(v) -> float:
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def detect(rows: list[dict], vendor: str) -> tuple[str | None, list[str], str | None, str | None]:
    """(vendor_col, buyer_cols, amount_col, desc_col) — verbatim semantics from
    t03b detect() plus a description-ish column pick from t04."""
    if not rows:
        return None, [], None, None
    cols = list(rows[0].keys())
    vendor_up = vendor.upper()
    vcol, best = None, 0
    for c in cols:
        n = sum(1 for x in rows if vendor_up in str(x.get(c, "")).upper())
        if n > best:
            vcol, best = c, n

    def amt_ok(c):
        toks = c.lower().replace("-", "_").split("_")
        if any(t in ID_WORDS for t in toks):
            return False
        vals = [x.get(c) for x in rows[:40] if x.get(c) not in (None, "")]
        return vals and sum(floatable(v) for v in vals) >= 0.8 * len(vals)

    def rank(c):
        toks = c.lower().replace("-", "_").split("_")
        return 0 if ("amount" in toks or "amt" in toks) else \
               1 if "total" in toks else 2

    amts = sorted((c for c in cols if any(h in c.lower() for h in
                   ("amount", "amt", "total", "payment", "expenditure", "price"))
                   and amt_ok(c)), key=rank)
    bcols = [c for c in cols if c != vcol and any(
        h in c.lower() for h in ("agency", "department", "dept", "district",
                                 "organization", "division", "school", "fund",
                                 "office"))]
    dcols = [c for c in cols if any(h in c.lower() for h in
             ("account", "category", "descr", "object", "appr"))
             and c not in bcols and c != vcol]
    return vcol, bcols, (amts[0] if amts else None), (dcols[0] if dcols else None)


def q_search(conn: sqlite3.Connection, domain: str, dataset_id: str,
             vendor: str, limit: int = 1000,
             ttl_days: float = 14) -> tuple[list[dict], bool]:
    url = f"https://{domain}/resource/{dataset_id}.json"
    data, hit = cached_get_json(conn, "socrata", url,
                                {"$q": vendor, "$limit": str(limit)}, ttl_days)
    if not isinstance(data, list):
        return [], hit
    vendor_up = vendor.upper()
    rows = [x for x in data if any(vendor_up in str(v).upper()
                                   for v in x.values())]
    return rows, hit


def q_search_paged(conn: sqlite3.Connection, domain: str, dataset_id: str,
                   vendor: str, page_size: int = 1000, max_pages: int = 10,
                   ttl_days: float = 14) -> tuple[list[dict], bool]:
    """$offset paging until a short page (the t04 lesson)."""
    url = f"https://{domain}/resource/{dataset_id}.json"
    out: list[dict] = []
    any_hit = False
    for page in range(max_pages):
        params = {"$q": vendor, "$limit": str(page_size),
                  "$offset": str(page * page_size)}
        data, hit = cached_get_json(conn, "socrata", url, params, ttl_days)
        any_hit = any_hit or hit
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < page_size:
            break
    vendor_up = vendor.upper()
    return [x for x in out if any(vendor_up in str(v).upper()
                                  for v in x.values())], any_hit


def buyer_of(row: dict, bcols: list[str]) -> str:
    vals = [str(row.get(c) or "").strip() for c in bcols]
    vals = [v for v in vals if v]
    return " / ".join(dict.fromkeys(vals))[:120] if vals else ""


def is_edu(row: dict, bcols: list[str]) -> bool:
    return any(EDU.search(str(row.get(c, ""))) for c in bcols)
