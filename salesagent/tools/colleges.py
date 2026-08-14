"""US higher-ed prospecting tools — free reads over SalesAgent's OWN
colleges estate (data/estate/colleges, built by the colleges_build job from
the official IPEDS directory). The census answer for "find colleges":
every US institution with phone, website, chief administrator by name, and
the NCES locale code (41-43 = rural campuses reps rarely visit)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import estate
from ..integrations import ipeds
from ..jobs.manager import JobManager
from .envelope import envelope, error_envelope, prov, table_envelope
from .registry import CostClass, tool_spec

_STATES_PARAM = {"type": "array", "items": {"type": "string",
                                            "pattern": "^[A-Za-z]{2}$"},
                 "description": "2-letter USPS state codes; omit = national"}


def _colleges_prov(manifest: dict) -> dict:
    return prov(
        "IPEDS directory (self-built estate)",
        f"run {manifest.get('run')}: {manifest.get('source_file')} — every "
        "US Title IV institution files IPEDS by law; downloaded fresh from "
        "nces.ed.gov",
        "https://nces.ed.gov/ipeds/")


@tool_spec(
    name="colleges_build_reference",
    description=(
        "Build or refresh SalesAgent's OWN colleges estate by downloading "
        "the current IPEDS directory (official census of US higher ed: "
        "~6,000 institutions with address/PHONE/website/president name/"
        "size/locale). Run when colleges_find reports the estate missing. "
        "~1MB download, under a minute. Free, background job."),
    input_schema={"properties": {
        "force": {"type": "boolean", "default": False,
                  "description": "rebuild even if a fresh estate exists"},
    }},
    cost_class=CostClass.SLOW_JOB,
)
def colleges_build_reference(ctx, force: bool = False) -> dict:
    m = estate.current_manifest(ctx.settings, "colleges")
    if m and not force:
        fresh_cut = (datetime.now(timezone.utc)
                     - timedelta(days=180)).isoformat()
        if (m.get("built_at") or "") >= fresh_cut:
            c = m.get("counts") or {}
            return envelope(
                kind="markdown",
                summary=f"colleges estate already built "
                        f"{m.get('built_at', '')[:10]} "
                        f"({c.get('active', '?')} active institutions) and "
                        "is <180 days old (IPEDS is annual) — no rebuild "
                        "needed. Pass force=true to rebuild anyway.",
                markdown="", provenance=[_colleges_prov(m)])
    job_id = JobManager.get(ctx.settings).submit(
        "colleges_build", {},
        conversation_id=ctx.conversation_id, user_id=ctx.user.get("id"),
        tool_name="colleges_build_reference")
    ctx.emit("job_started", {"job_id": job_id,
                             "tool": "colleges_build_reference",
                             "title": "Colleges estate build — IPEDS"})
    return envelope(
        kind="job_ref", job_id=job_id,
        summary=f"colleges estate build job {job_id} started — ~1MB IPEDS "
                "download, under a minute. colleges_find works as soon as "
                "it finishes.",
        provenance=[])


def _columns() -> list[dict]:
    cols = []
    for key, label, fmt in ipeds.FIND_COLUMNS:
        c = {"key": key, "label": label}
        if fmt == "hidden":
            c["type"] = "number"
            c["hidden"] = True
        elif fmt == "link":
            c["type"] = "string"
            c["format"] = "link"
        elif fmt == "int":
            c["type"] = "number"
            c["format"] = "int"
        else:
            c["type"] = "string"
        cols.append(c)
    return cols


@tool_spec(
    name="colleges_find",
    description=(
        "Search the official IPEDS census of ALL US higher-ed institutions "
        "(app's own estate; ~6,000 with phone, website, and the chief "
        "administrator's NAME). THE tool for college/university lead lists "
        "— includes community colleges and rural campuses OSM misses. "
        "Filters: state, level (4-year/2-year), control (public/private/"
        "for-profit), size class 1-5 (<1k to 20k+ students), rural_only or "
        "locale_groups (city/suburb/town/rural), HBCU, hospital-on-campus "
        "(= big lab buyer). Returns lat/lng for mapping. Free, local."),
    input_schema={"properties": {
        "states": _STATES_PARAM,
        "q": {"type": "string", "description": "name/city/alias substring"},
        "levels": {"type": "array", "items": {"type": "integer"},
                   "description": "1=4-year+, 2=2-year, 3=<2-year"},
        "controls": {"type": "array", "items": {},
                     "description": "public | private nonprofit | "
                                    "private for-profit (or codes 1/2/3)"},
        "rural_only": {"type": "boolean", "default": False,
                       "description": "only campuses in NCES rural locales "
                                      "(41-43) — the middle-of-nowhere list"},
        "locale_groups": {"type": "array", "items": {"type": "string"},
                          "description": "any of city|suburb|town|rural"},
        "min_size_class": {"type": "integer",
                           "description": "1=<1k 2=1-5k 3=5-10k 4=10-20k 5=20k+"},
        "max_size_class": {"type": "integer"},
        "hbcu": {"type": "boolean"},
        "with_hospital": {"type": "boolean",
                          "description": "true = campus runs a hospital"},
        "require_phone": {"type": "boolean", "default": False},
        "sort": {"type": "string", "enum": ["size", "name", "state"],
                 "default": "size"},
        "limit": {"type": "integer", "default": 500, "maximum": 5000},
    }},
    cost_class=CostClass.FREE,
)
def colleges_find(ctx, **params) -> dict:
    m = estate.current_manifest(ctx.settings, "colleges")
    if not m:
        return error_envelope(estate.COLLEGES_MISSING_MSG,
                              error_type="EstateMissing")
    conn = estate.open_colleges(ctx.settings)
    try:
        try:
            rows, warnings = ipeds.query_colleges(conn, **params)
        except ValueError as e:
            return error_envelope(str(e), error_type="BadParams")
    finally:
        conn.close()
    cols = _columns()
    keys = [c["key"] for c in cols]
    data = [[r.get(k) for k in keys] for r in rows]
    by_state: dict[str, int] = {}
    by_locale: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        ll = r.get("locale_label") or "unknown"
        by_locale[ll] = by_locale.get(ll, 0) + 1
    states = params.get("states") or []
    title = (f"Colleges — {', '.join(s.upper() for s in states)}" if states
             else "Colleges — national")
    if params.get("rural_only"):
        title += " (rural)"
    hit_limit = len(rows) >= int(params.get("limit") or 500)
    if hit_limit:
        warnings.append(
            f"result hit the {params.get('limit') or 500}-row limit — MORE "
            "matching institutions exist; raise limit (max 5000) or narrow")
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="colleges_find", title=title,
        columns=cols, rows=data, provenance=[_colleges_prov(m)],
        summary=f"{len(rows)} institutions matched"
                + (" (LIMIT HIT — more exist)" if hit_limit else "")
                + f". Estate: {m.get('counts', {}).get('active', '?')} "
                  "active US institutions total.",
        warnings=warnings,
        stats={"by_state": by_state, "by_locale": by_locale},
        map_spec={"lat": "lat", "lng": "lng", "label": "name",
                  "popup_cols": ["name", "city", "state", "phone"]}
        if any(r.get("lat") for r in rows) else None)
