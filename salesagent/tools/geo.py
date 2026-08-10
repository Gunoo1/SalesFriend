"""Geo tools: nearby orgs (Overpass), company branch finding (job), and
open/closed verification (Serper + KG second source).

Company locations are memoized in company_locations (app.db) — populated
ONLY by this app's own scrape/OSM jobs, never copied from another project.
"""
from __future__ import annotations

import difflib
import json
from functools import lru_cache
from pathlib import Path

from ..db import utcnow
from ..integrations import overpass, serper
from ..jobs.manager import JobManager
from .envelope import envelope, error_envelope, prov, table_envelope
from .registry import CostClass, tool_spec

_STATES_PARAM = {"type": "array", "items": {"type": "string",
                                            "pattern": "^[A-Za-z]{2}$"}}
_BBOX_FILE = Path(__file__).resolve().parent.parent / "ref" / "seeds" / \
    "state_bboxes.json"
POI_COLS = [
    {"key": "name", "label": "Name", "type": "string"},
    {"key": "category", "label": "Category", "type": "string"},
    {"key": "city", "label": "City", "type": "string"},
    {"key": "state", "label": "State", "type": "string"},
    {"key": "phone", "label": "Phone", "type": "string"},
    {"key": "website", "label": "Website", "type": "string", "format": "link"},
    {"key": "lat", "label": "", "type": "number", "hidden": True},
    {"key": "lng", "label": "", "type": "number", "hidden": True},
]


@lru_cache(maxsize=1)
def _bboxes() -> dict:
    data = json.loads(_BBOX_FILE.read_text(encoding="utf-8"))
    return {k: tuple(v) for k, v in data.items() if not k.startswith("_")}


def state_bbox(state: str):
    """(south, west, north, east) from the vendored static table — static
    geometry, no database dependency."""
    return _bboxes().get(str(state).upper())


def _poi_category(p: dict) -> str:
    t = p.get("tags") or {}
    if t.get("amenity"):
        return t["amenity"]
    if t.get("healthcare"):
        return "laboratory"
    if t.get("industrial"):
        return "chemical"
    if t.get("office"):
        return "research office"
    return "poi"


@tool_spec(
    name="find_nearby_orgs",
    description=(
        "Universities/colleges/research institutes, labs, and chemical plants "
        "in a state or around a point, from OpenStreetMap. Tag-based ONLY — "
        "there is no name-keyword search (it times out the server). Chemical "
        "sites are badly undertagged in OSM: treat those counts as a floor, "
        "not a census. One state per call (~30-60s). Free."),
    input_schema={"properties": {
        "state": {"type": "string", "pattern": "^[A-Za-z]{2}$"},
        "center": {"type": "object",
                   "properties": {"lat": {"type": "number"},
                                  "lng": {"type": "number"}}},
        "radius_miles": {"type": "number", "default": 25},
        "categories": {"type": "array",
                       "items": {"type": "string",
                                 "enum": ["academic", "lab", "chemical",
                                          "research_office"]},
                       "default": ["academic", "lab"]},
    }},
    cost_class=CostClass.FREE,
)
def find_nearby_orgs(ctx, state: str | None = None, center: dict | None = None,
                     radius_miles: float = 25,
                     categories: list[str] | None = None) -> dict:
    categories = categories or ["academic", "lab"]
    if center and center.get("lat") is not None:
        d = radius_miles / 69.0
        bbox = (center["lat"] - d, center["lng"] - d / 0.75,
                center["lat"] + d, center["lng"] + d / 0.75)
        where = f"{radius_miles}mi around ({center['lat']:.3f},{center['lng']:.3f})"
    elif state:
        state = state.upper()
        bbox = state_bbox(state)
        if not bbox:
            return error_envelope(f"no bounding box for state '{state}'")
        where = state
    else:
        return error_envelope("pass state or center")
    try:
        pois = overpass.search_categories(bbox, categories)
    except overpass.OverpassError as e:
        return error_envelope(str(e), error_type="OverpassError")
    rows = [[p["name"], _poi_category(p), p.get("city"),
             p.get("state") or (state or ""), p.get("phone"),
             p.get("website"), p["lat"], p["lng"]] for p in pois]
    warn = []
    if "chemical" in categories:
        warn.append("OSM chemical tagging is sparse — this is a floor, not a census.")
    by_cat: dict[str, int] = {}
    for r in rows:
        by_cat[r[1]] = by_cat.get(r[1], 0) + 1
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="find_nearby_orgs", title=f"Orgs — {where}",
        columns=POI_COLS, rows=rows,
        provenance=[prov("OpenStreetMap (Overpass)",
                         f"categories={categories} in {where}",
                         "https://overpass-api.de")],
        summary=f"{len(rows)} organizations in {where}.",
        stats={"by_category": by_cat}, warnings=warn,
        map_spec={"lat": "lat", "lng": "lng", "label": "name",
                  "popup_cols": ["name", "category", "city", "website"]})


SCRAPE_DENYLIST = {"grainger.com": "grainger.com scraping ruled off-limits "
                                   "2026-06-18 — OSM/Overture data only"}


@tool_spec(
    name="find_company_locations",
    description=(
        "Find a company's physical branches/locations. Fastenal uses the "
        "proven locator scrape; every other company uses OpenStreetMap "
        "exact brand match (ToS-safe; coverage depends on OSM mapping of "
        "that chain — decent for big chains like Fastenal/Grainger/Uline). "
        "Runs as a background job the FIRST time; results are memoized in "
        "this app's company_locations store, so repeat asks within "
        "max_age_days are instant. Slow job (first run)."),
    input_schema={"properties": {
        "company": {"type": "string"},
        "states": _STATES_PARAM,
        "max_age_days": {"type": "integer", "default": 30},
    }, "required": ["company", "states"]},
    cost_class=CostClass.SLOW_JOB,
)
def find_company_locations(ctx, company: str, states: list[str],
                           max_age_days: int = 30) -> dict:
    states = [s.upper() for s in states][:12]
    conn = ctx.rw()
    # memoized from this app's own past runs — fresh enough skips the job
    ph = ",".join("?" * len(states))
    cached = conn.execute(
        f"SELECT COUNT(*) AS n FROM company_locations WHERE company=? AND "
        f"state IN ({ph}) AND fetched_at >= datetime('now', ?)",
        [company.lower(), *states, f"-{max_age_days} days"]).fetchone()
    if cached["n"] > 0:
        rows = conn.execute(
            f"SELECT * FROM company_locations WHERE company=? AND state IN ({ph})",
            [company.lower(), *states]).fetchall()
        data = [[r["name"], r["city"], r["state"], r["street"], r["phone"],
                 r["location_type"], r["provider"], r["lat"], r["lng"]]
                for r in rows]
        return table_envelope(
            conn, ctx.emit, conversation_id=ctx.conversation_id,
            tool="find_company_locations",
            title=f"{company.title()} locations — {', '.join(states)}",
            columns=_LOC_COLS, rows=data,
            provenance=[prov("company_locations (this app's own earlier run)",
                             f"provider={rows[0]['provider']}, fetched "
                             f"{rows[0]['fetched_at'][:10]}")],
            summary=f"{len(data)} locations memoized from this app's own "
                    f"earlier run (<= {max_age_days} days old).",
            map_spec={"lat": "lat", "lng": "lng", "label": "name",
                      "popup_cols": ["name", "city", "state", "phone"]})

    job_id = JobManager.get(ctx.settings).submit(
        "branch_finder", {"company": company, "states": states},
        conversation_id=ctx.conversation_id, user_id=ctx.user.get("id"),
        tool_name="find_company_locations")
    ctx.emit("job_started", {"job_id": job_id, "tool": "find_company_locations",
                             "title": f"{company.title()} — {', '.join(states)}"})
    return envelope(
        kind="job_ref", job_id=job_id,
        summary=f"branch-finder job {job_id} started for {company} in "
                f"{', '.join(states)} — tell the rep it's running; the result "
                "artifact will arrive when done (job_status checks it).",
        provenance=[])


_LOC_COLS = [
    {"key": "name", "label": "Location", "type": "string"},
    {"key": "city", "label": "City", "type": "string"},
    {"key": "state", "label": "State", "type": "string"},
    {"key": "street", "label": "Street", "type": "string"},
    {"key": "phone", "label": "Phone", "type": "string"},
    {"key": "type", "label": "Type", "type": "string"},
    {"key": "provider", "label": "Source", "type": "string"},
    {"key": "lat", "label": "", "type": "number", "hidden": True},
    {"key": "lng", "label": "", "type": "number", "hidden": True},
]


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


@tool_spec(
    name="verify_business_status",
    description=(
        "Check whether locations are still OPEN (catches permanently/"
        "temporarily closed) via Serper Google Maps + a knowledge-graph "
        "second source. Policy: 'open' accepted from one source; any CLOSED "
        "verdict requires two. Cached 30 days per location. Pass explicit "
        "targets or an artifact (name/city/state columns). <=50 per call. "
        "Metered (~$0.001/query)."),
    input_schema={"properties": {
        "targets": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "city": {"type": "string"},
                           "state": {"type": "string"}},
            "required": ["name"]}},
        "artifact_id": {"type": "string"},
        "row_indices": {"type": "array", "items": {"type": "integer"}},
        "max_age_days": {"type": "integer", "default": 30},
    }},
    cost_class=CostClass.CHEAP,
)
def verify_business_status(ctx, targets: list[dict] | None = None,
                           artifact_id: str | None = None,
                           row_indices: list[int] | None = None,
                           max_age_days: int = 30) -> dict:
    conn = ctx.rw()
    if not targets and artifact_id:
        from ..artifacts import store
        spec = store.get(conn, artifact_id)
        if not spec or spec.get("conversation_id") != ctx.conversation_id:
            return error_envelope(f"artifact {artifact_id} not found here")
        keys = [c["key"].lower() for c in spec["columns"]]

        def col(*names):
            for n in names:
                if n in keys:
                    return keys.index(n)
            return None
        i_name, i_city, i_state = col("name", "location", "buyer"), \
            col("city"), col("state")
        if i_name is None:
            return error_envelope(f"no name column in artifact; columns: {keys}")
        idxs = row_indices if row_indices else range(min(50, len(spec["rows"])))
        targets = []
        for i in idxs:
            if 0 <= i < len(spec["rows"]):
                r = spec["rows"][i]
                targets.append({"name": r[i_name],
                                "city": r[i_city] if i_city is not None else None,
                                "state": r[i_state] if i_state is not None else None})
    if not targets:
        return error_envelope("pass targets[] or artifact_id")
    targets = targets[:50]

    rows_out = []
    checked = cache_hits = 0
    for t in targets:
        name, city, state = t.get("name"), t.get("city"), t.get("state")
        lockey = _norm(f"{name}|{city}|{state}")
        row = conn.execute(
            "SELECT * FROM business_status WHERE location_key=? AND "
            "verified_at >= datetime('now', ?)",
            (lockey, f"-{max_age_days} days")).fetchone()
        if row:
            cache_hits += 1
            rows_out.append([name, city, state, row["status"],
                             row["confidence"], "cache", row["verified_at"][:10]])
            continue
        q = " ".join(str(x) for x in (name, city, state) if x)
        try:
            data, _ = serper.maps(ctx.settings, conn, q)
        except serper.SerperError as e:
            return error_envelope(str(e), error_type="SerperError")
        checked += 1
        places = data.get("places") or []
        tn = _norm(name)
        cands: list[tuple[float, dict]] = []
        for p in places[:8]:
            tp = _norm(p.get("title"))
            sim = difflib.SequenceMatcher(None, tp, tn).ratio()
            # brand targets vs longer official titles ("Fastenal" vs
            # "Fastenal Fulfillment Center", live-verified): containment in a
            # geo-scoped query is a strong match the ratio under-scores
            if tn and tp and (tn in tp or tp in tn):
                sim = max(sim, 0.8)
            if sim >= 0.55:
                cands.append((sim, p))
        status, confidence, evidence = "unverified", "low", "no maps match"
        # Google keeps stale closed duplicate pins next to live branches
        # (live-verified: closed 'Fastenal Fulfillment Center' beside the open
        # Newark DE branch) — so an open strong-match wins over a closed one.
        open_c = [(s, p) for s, p in cands if not serper.closed_signal(p)]
        closed_c = [(s, p) for s, p in cands if serper.closed_signal(p)]
        if open_c:
            score, best = max(open_c, key=lambda t: t[0])
            status = "open"
            confidence = "high" if score >= 0.75 else "medium"
            evidence = f"maps match '{best.get('title')}' (sim {score:.2f})" + \
                ("; hours listed" if best.get("openingHours") else "")
            if closed_c:
                evidence += (f"; note: closed listing "
                             f"'{closed_c[0][1].get('title')}' also on maps")
        elif closed_c:
            score, best = max(closed_c, key=lambda t: t[0])
            signal = serper.closed_signal(best)
            # closed verdicts need a second source (plan policy)
            try:
                sdata, _ = serper.search(ctx.settings, conn,
                                         f"{name} {city or ''} {state or ''}")
                blob = str(sdata.get("knowledgeGraph") or "") + " ".join(
                    str(o.get("snippet") or "")
                    for o in (sdata.get("organic") or [])[:5])
                confirmed = "permanently closed" in blob.lower() or \
                            "temporarily closed" in blob.lower()
            except serper.SerperError:
                confirmed = False
            status = signal if confirmed else "unverified"
            confidence = "high" if confirmed else "low"
            evidence = (f"maps:{signal} '{best.get('title')}' (sim {score:.2f})"
                        + ("; KG confirms" if confirmed
                           else "; KG did NOT confirm"))
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO business_status (location_key, company,"
                " name, city, state, status, confidence, evidence_json,"
                " sources, verified_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (lockey, None, name, city, state, status, confidence,
                 evidence[:300], "serper", utcnow()))
        rows_out.append([name, city, state, status, confidence, evidence[:120],
                         utcnow()[:10]])

    cols = [{"key": "name", "label": "Location", "type": "string"},
            {"key": "city", "label": "City", "type": "string"},
            {"key": "state", "label": "State", "type": "string"},
            {"key": "status", "label": "Status", "type": "string"},
            {"key": "confidence", "label": "Confidence", "type": "string"},
            {"key": "evidence", "label": "Evidence", "type": "string"},
            {"key": "verified", "label": "Verified", "type": "string"}]
    closed = sum(1 for r in rows_out if str(r[3]).startswith("closed"))
    return table_envelope(
        conn, ctx.emit, conversation_id=ctx.conversation_id,
        tool="verify_business_status", title="Open/closed verification",
        columns=cols, rows=rows_out,
        provenance=[prov("Serper Google Maps + knowledge graph",
                         f"{checked} live checks, {cache_hits} cached")],
        summary=f"{len(rows_out)} locations: {closed} closed, "
                f"{sum(1 for r in rows_out if r[3] == 'open')} open, "
                f"rest unverified.",
        warnings=[])
