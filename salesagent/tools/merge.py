"""Cross-artifact combination tools, all server-side (rows never transit the
model's context):

entity_merge — merge/dedupe organization artifacts into one ranked table
with in_<source> flags. Match ladder generalized from K12Intel k12/match.py:
exact-after-normalization -> fuzzy (same state) >= 0.88 -> unmatched kept.

geo_rank — rank the rows of one lat/lng artifact by what's NEARBY in another
(haversine join): branches vs districts, branches vs universities, etc.
"""
from __future__ import annotations

import difflib
import math
import re

from ..artifacts import store
from .envelope import error_envelope, prov, table_envelope
from .registry import CostClass, tool_spec

_STOP = {"the", "of", "inc", "llc", "corp", "corporation", "company", "co",
         "dept", "department", "public", "school", "district", "county",
         "city", "board", "education", "schools", "univ", "university",
         # district abbreviations (NCES/checkbook spellings)
         "sd", "isd", "usd", "csd", "hsd", "cusd", "ccsd", "boe", "twp",
         "township", "regional", "reg"}
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def norm_org(name: str) -> str:
    s = _PUNCT.sub(" ", str(name or "").lower())
    toks = [t for t in s.split() if t not in _STOP]
    return " ".join(toks) or s.strip()


NAME_KEYS = ("name", "district", "buyer", "company", "vendor", "location",
             "recipient", "title")


def _entity_cols(columns: list[dict]) -> tuple[int | None, int | None]:
    keys = [c["key"].lower() for c in columns]
    name_i = next((keys.index(k) for k in NAME_KEYS if k in keys), None)
    state_i = keys.index("state") if "state" in keys else None
    return name_i, state_i


@tool_spec(
    name="entity_merge",
    description=(
        "Merge/dedupe 2+ table artifacts of ORGANIZATIONS into one ranked "
        "table with in_<source> presence flags and match_method — the core of "
        "prospect-discovery playbooks (k12 districts x nearby orgs x "
        "checkbook buyers). Matching: exact normalized name -> fuzzy >=0.88 "
        "within the same state. Server-side over full data. Cheap."),
    input_schema={"properties": {
        "artifact_ids": {"type": "array", "items": {"type": "string"},
                         "minItems": 2},
        "labels": {"type": "array", "items": {"type": "string"},
                   "description": "optional short label per artifact (same order)"},
        "keep_unmatched": {"type": "boolean", "default": True},
        "rank_by_sources": {"type": "boolean", "default": True,
                            "description": "sort by how many sources contain the org"},
    }, "required": ["artifact_ids"]},
    cost_class=CostClass.CHEAP,
)
def entity_merge(ctx, artifact_ids: list[str], labels: list[str] | None = None,
                 keep_unmatched: bool = True,
                 rank_by_sources: bool = True) -> dict:
    conn = ctx.rw()
    specs = []
    for aid in artifact_ids[:6]:
        spec = store.get(conn, aid)
        if not spec or spec.get("conversation_id") != ctx.conversation_id:
            return error_envelope(f"artifact {aid} not found in this conversation")
        specs.append(spec)
    if len(specs) < 2:
        return error_envelope("need at least 2 artifacts")

    labels = labels or []
    src_labels = []
    for i, spec in enumerate(specs):
        if i < len(labels) and labels[i]:
            lbl = re.sub(r"[^\w]+", "_", labels[i].lower())[:20]
        else:
            lbl = re.sub(r"[^\w]+", "_",
                         (spec.get("created_by") or f"src{i+1}").lower())[:20]
        while lbl in src_labels:
            lbl += "2"
        src_labels.append(lbl)

    canon: list[dict] = []          # {name, state, norm, flags: set, method}
    by_norm: dict[tuple, int] = {}  # (norm, state) -> canon index
    by_state: dict[str, list[int]] = {}
    skipped_sources = []

    for i, spec in enumerate(specs):
        name_i, state_i = _entity_cols(spec["columns"])
        if name_i is None:
            skipped_sources.append(
                f"{src_labels[i]}: no name-like column "
                f"({[c['key'] for c in spec['columns']][:8]})")
            continue
        for row in spec["rows"]:
            raw = str(row[name_i] or "").strip()
            if not raw or raw == "(unlabeled)":
                continue
            state = str(row[state_i] or "").strip().upper() if state_i is not None else ""
            n = norm_org(raw)
            if not n:
                continue
            hit = by_norm.get((n, state))
            method = "exact"
            if hit is None and state:
                best, score = None, 0.0
                for ci in by_state.get(state, []):
                    r = difflib.SequenceMatcher(None, canon[ci]["norm"], n).ratio()
                    if r > score:
                        best, score = ci, r
                if best is not None and score >= 0.88:
                    hit, method = best, "fuzzy"
            if hit is None:
                canon.append({"name": raw, "state": state, "norm": n,
                              "flags": {i}, "method": "-"})
                idx = len(canon) - 1
                by_norm[(n, state)] = idx
                by_state.setdefault(state, []).append(idx)
            else:
                canon[hit]["flags"].add(i)
                if method == "fuzzy" and canon[hit]["method"] == "-":
                    canon[hit]["method"] = "fuzzy"

    rows_out = []
    for c in canon:
        n_sources = len(c["flags"])
        if not keep_unmatched and n_sources < 2:
            continue
        flags = [1 if i in c["flags"] else 0 for i in range(len(specs))]
        rows_out.append([c["name"], c["state"], n_sources, *flags,
                         "exact" if c["method"] == "-" else c["method"]])
    if rank_by_sources:
        rows_out.sort(key=lambda r: (-r[2], r[1], r[0].lower()))

    cols = ([{"key": "name", "label": "Organization", "type": "string"},
             {"key": "state", "label": "State", "type": "string"},
             {"key": "n_sources", "label": "Sources", "type": "number",
              "format": "int"}]
            + [{"key": f"in_{lbl}", "label": f"in {lbl}", "type": "number",
                "format": "int"} for lbl in src_labels]
            + [{"key": "match_method", "label": "Match", "type": "string"}])
    prov_all = []
    for spec in specs:
        prov_all += spec.get("provenance") or []
    warnings = []
    if skipped_sources:
        warnings.append("skipped: " + "; ".join(skipped_sources))
    multi = sum(1 for r in rows_out if r[2] >= 2)
    return table_envelope(
        conn, ctx.emit, conversation_id=ctx.conversation_id,
        tool="entity_merge",
        title=f"Merged prospects ({len(specs)} sources)",
        columns=cols, rows=rows_out, provenance=prov_all
        + [prov("entity_merge", f"exact->fuzzy>=0.88 ladder over "
                                f"{len(specs)} artifacts")],
        summary=f"{len(rows_out)} unique organizations; {multi} appear in "
                f"2+ sources (the hot list).",
        styling={"tier_rules": [{"column": "n_sources", "gte": 3, "class": "hot"},
                                {"column": "n_sources", "gte": 2, "class": "warm"}]},
        warnings=warnings)


# --------------------------------------------------------------------------
# geo_rank — proximity join between two lat/lng artifacts
# --------------------------------------------------------------------------

_LAT_KEYS = ("lat", "latitude")
_LNG_KEYS = ("lng", "lon", "longitude")


def _coord_idx(columns: list[dict]) -> tuple[int | None, int | None]:
    keys = [c["key"].lower() for c in columns]
    lat = next((keys.index(k) for k in _LAT_KEYS if k in keys), None)
    lng = next((keys.index(k) for k in _LNG_KEYS if k in keys), None)
    return lat, lng


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(a))


def nearby_stats(a_pts: list[tuple], b_rows: list[dict],
                 radius_miles: float) -> list[dict]:
    """For each (lat, lng) in a_pts: count of b_rows within radius, sum of
    their 'weight', and the nearest one's name/distance. Pure — unit-tested.
    b_rows: [{lat, lng, weight, name}] with weight possibly None."""
    out = []
    for alat, alng in a_pts:
        if alat is None or alng is None:
            out.append(None)
            continue
        n, wsum, best_name, best_d = 0, 0.0, None, None
        for b in b_rows:
            d = haversine_miles(alat, alng, b["lat"], b["lng"])
            if best_d is None or d < best_d:
                best_d, best_name = d, b["name"]
            if d <= radius_miles:
                n += 1
                if b["weight"] is not None:
                    wsum += b["weight"]
        out.append({"count": n, "weight_sum": wsum,
                    "nearest": best_name,
                    "nearest_miles": round(best_d, 1) if best_d is not None else None})
    return out


@tool_spec(
    name="geo_rank",
    description=(
        "Rank the rows of one table artifact by what's NEARBY in another — a "
        "server-side distance join. target = things to rank (must carry "
        "lat/lng, e.g. company branches); around = things to count near them "
        "(also lat/lng, e.g. k12_find_districts output). Appends nearby_count, "
        "an optional weighted sum (weight_column, e.g. enrollment), and the "
        "nearest neighbor + distance, then sorts best-first. Typical use: "
        "rank branches by the K12 enrollment within N miles. Free, local."),
    input_schema={"properties": {
        "target_artifact_id": {"type": "string",
                               "description": "artifact whose rows get ranked"},
        "around_artifact_id": {"type": "string",
                               "description": "artifact whose rows are counted nearby"},
        "radius_miles": {"type": "number", "default": 15},
        "weight_column": {"type": "string",
                          "description": "numeric column of `around` to sum (e.g. enrollment)"},
    }, "required": ["target_artifact_id", "around_artifact_id"]},
    cost_class=CostClass.FREE,
)
def geo_rank(ctx, target_artifact_id: str, around_artifact_id: str,
             radius_miles: float = 15,
             weight_column: str | None = None) -> dict:
    conn = ctx.rw()
    specs = []
    for aid in (target_artifact_id, around_artifact_id):
        spec = store.get(conn, aid)
        if not spec or spec.get("conversation_id") != ctx.conversation_id:
            return error_envelope(f"artifact {aid} not found in this conversation")
        specs.append(spec)
    a, b = specs
    radius_miles = max(0.5, min(float(radius_miles or 15), 300))

    a_lat, a_lng = _coord_idx(a["columns"])
    b_lat, b_lng = _coord_idx(b["columns"])
    if a_lat is None or a_lng is None:
        return error_envelope(
            f"target artifact has no lat/lng columns "
            f"({[c['key'] for c in a['columns']][:10]})")
    if b_lat is None or b_lng is None:
        return error_envelope(
            f"around artifact has no lat/lng columns "
            f"({[c['key'] for c in b['columns']][:10]})")
    if len(a["rows"]) * len(b["rows"]) > 2_000_000:
        return error_envelope(
            f"{len(a['rows'])} x {len(b['rows'])} rows is too big for a "
            "distance join — narrow one side (filter first).")

    b_keys = [c["key"].lower() for c in b["columns"]]
    w_i = None
    if weight_column:
        wc = weight_column.strip().lower()
        if wc not in b_keys:
            return error_envelope(
                f"'{weight_column}' is not a column of the around-artifact; "
                f"available: {b_keys}")
        w_i = b_keys.index(wc)
    bname_i, _ = _entity_cols(b["columns"])

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    b_pts, b_skipped = [], 0
    for r in b["rows"]:
        lat, lng = _f(r[b_lat]), _f(r[b_lng])
        if lat is None or lng is None:
            b_skipped += 1
            continue
        b_pts.append({"lat": lat, "lng": lng,
                      "weight": _f(r[w_i]) if w_i is not None else None,
                      "name": r[bname_i] if bname_i is not None else None})
    if not b_pts:
        return error_envelope("around artifact has no rows with coordinates")

    a_pts = [( _f(r[a_lat]), _f(r[a_lng]) ) for r in a["rows"]]
    stats = nearby_stats(a_pts, b_pts, radius_miles)

    wlabel = f"{weight_column} within {radius_miles:g}mi" if w_i is not None else None
    cols = list(a["columns"]) + [
        {"key": "nearby_count", "label": f"Nearby (<= {radius_miles:g}mi)",
         "type": "number", "format": "int"}]
    if wlabel:
        cols.append({"key": "nearby_weight", "label": wlabel,
                     "type": "number", "format": "int"})
    cols += [{"key": "nearest", "label": "Nearest", "type": "string"},
             {"key": "nearest_miles", "label": "Nearest (mi)", "type": "number"}]

    rows_out, a_skipped = [], 0
    for r, s in zip(a["rows"], stats):
        if s is None:
            a_skipped += 1
            continue
        row = list(r) + [s["count"]]
        if wlabel:
            row.append(int(s["weight_sum"]))
        row += [s["nearest"], s["nearest_miles"]]
        rows_out.append(row)
    key_i = len(a["columns"]) + (1 if wlabel else 0)   # weight col else count
    rows_out.sort(key=lambda r: (-(r[key_i] or 0), -(r[len(a['columns'])] or 0)))

    warnings = []
    if a_skipped:
        warnings.append(f"{a_skipped} target rows had no coordinates — dropped")
    if b_skipped:
        warnings.append(f"{b_skipped} around-rows had no coordinates — not counted")
    aname_i, _ = _entity_cols(a["columns"])
    top = ""
    if rows_out and aname_i is not None:
        s0 = rows_out[0]
        top = (f" Top: {s0[aname_i]} ({s0[len(a['columns'])]} nearby"
               + (f", {s0[len(a['columns']) + 1]:,} {weight_column}" if wlabel else "")
               + ").")
    return table_envelope(
        conn, ctx.emit, conversation_id=ctx.conversation_id,
        tool="geo_rank",
        title=f"Ranked by nearby — {a.get('title') or 'targets'}"[:70],
        columns=cols, rows=rows_out,
        provenance=(a.get("provenance") or []) + (b.get("provenance") or [])
        + [prov("geo_rank", f"haversine join, radius {radius_miles:g}mi"
                            + (f", weighted by {weight_column}" if wlabel else ""))],
        summary=f"{len(rows_out)} targets ranked by what's within "
                f"{radius_miles:g} miles ({len(b_pts)} candidate neighbors)."
                + top,
        warnings=warnings)
