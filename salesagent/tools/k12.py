"""K12 district intel tools — free reads over SalesAgent's OWN estate
(data/estate/k12, built by the k12_build job from fresh public NCES/CRDC
downloads) and its own contact history (contacts_app). No other project's
data is read; if no estate exists yet, tools return an actionable error and
k12_build_reference builds one.
"""
from __future__ import annotations

from .. import estate
from ..integrations import k12_local
from ..jobs.manager import JobManager
from .envelope import envelope, error_envelope, prov, table_envelope
from .registry import CostClass, tool_spec

_STATES_PARAM = {"type": "array", "items": {"type": "string",
                                            "pattern": "^[A-Za-z]{2}$"},
                 "description": "2-letter USPS state codes"}


def _k12_prov(manifest: dict) -> dict:
    y = manifest.get("years") or {}
    return prov(
        "SalesAgent k12 estate (self-built)",
        f"run {manifest.get('run')}: NCES CCD {y.get('directory')} directory "
        f"+ F-33 {y.get('finance')} finance + CRDC {y.get('crdc')} science, "
        "downloaded fresh from the public educationdata.urban.org mirror",
        "https://educationdata.urban.org")


def _coverage_error(manifest: dict, states: list[str]) -> dict | None:
    """Partial estates must never silently read as 'no districts there'."""
    cov = estate.covered_states(manifest)
    if cov is None:
        return None
    req = {s.upper() for s in states or []}
    if req and req <= cov:
        return None
    missing = ", ".join(sorted(req - cov)) if req else "a national query"
    return error_envelope(
        f"the current k12 estate covers only {', '.join(sorted(cov))} — "
        f"this needs {missing}. Run k12_build_reference (omit states for a "
        "national build) and retry.", error_type="EstateCoverage")


@tool_spec(
    name="k12_build_reference",
    description=(
        "Build or refresh SalesAgent's OWN K12 reference estate by "
        "downloading fresh public data (NCES CCD district directory + F-33 "
        "finance + CRDC science offerings via educationdata.urban.org). "
        "Run when k12 tools report the estate is missing, stale, or lacking "
        "a state. Omit states for the full national build (~5-15 min); a "
        "states list builds faster but REPLACES the estate with just those "
        "states. Free, background job."),
    input_schema={"properties": {
        "states": _STATES_PARAM,
        "force": {"type": "boolean", "default": False,
                  "description": "rebuild even if a fresh estate exists"},
    }},
    cost_class=CostClass.SLOW_JOB,
)
def k12_build_reference(ctx, states: list[str] | None = None,
                        force: bool = False) -> dict:
    states = k12_local.clean_states(states)
    m = estate.current_manifest(ctx.settings, "k12")
    if m and not force:
        cov = estate.covered_states(m)
        covers = cov is None or (states and set(states) <= cov)
        fresh = (m.get("built_at") or "") >= _days_ago(30)
        if covers and fresh:
            c = m.get("counts") or {}
            return envelope(
                kind="markdown",
                summary=f"estate already built {m.get('built_at', '')[:10]} "
                        f"({m.get('scope')}, {c.get('districts', '?')} "
                        "districts) and is <30 days old — no rebuild needed. "
                        "Pass force=true to rebuild anyway.",
                markdown="", provenance=[_k12_prov(m)])
    job_id = JobManager.get(ctx.settings).submit(
        "k12_build", {"states": states},
        conversation_id=ctx.conversation_id, user_id=ctx.user.get("id"),
        tool_name="k12_build_reference")
    scope = ", ".join(states) if states else "national"
    ctx.emit("job_started", {"job_id": job_id, "tool": "k12_build_reference",
                             "title": f"K12 estate build — {scope}"})
    return envelope(
        kind="job_ref", job_id=job_id,
        summary=f"k12 estate build job {job_id} started ({scope}) — fresh "
                "public NCES/CRDC download, a few minutes. Tell the rep it's "
                "running; k12 tools work as soon as it finishes (job_status "
                "checks progress).",
        provenance=[])


def _days_ago(n: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _columns() -> list[dict]:
    cols = []
    for key, label, fmt in k12_local.FIND_COLUMNS:
        c = {"key": key, "label": label or key}
        if fmt == "hidden":
            c["type"] = "number"
            c["hidden"] = True
        elif fmt in ("money", "int", "score", "number"):
            c["type"] = "number"
            c["format"] = fmt if fmt != "number" else None
        else:
            c["type"] = "string"
        cols.append({k: v for k, v in c.items() if v is not None})
    return cols


@tool_spec(
    name="k12_find_districts",
    description=(
        "Search all ~19,600 US public school districts (app's own estate, "
        "fresh NCES data) with filters: state, name/city, enrollment, weekly "
        "science class sections + AP science (CRDC), charter, county, and "
        "federal dollars (Title I, CTE/Perkins, math/science, capital "
        "instructional equipment). Returns a table artifact with lat/lng for "
        "mapping. Sort by enrollment|sci_sections|title_i|cap_equip|name. "
        "Free, local."),
    input_schema={"properties": {
        "states": _STATES_PARAM,
        "q": {"type": "string", "description": "district or city name substring"},
        "min_enrollment": {"type": "integer"},
        "max_enrollment": {"type": "integer"},
        "min_sci_sections": {"type": "number",
                             "description": "weekly bio/chem/physics sections district-wide (CRDC, ~2yr lag)"},
        "requires_ap_science": {"type": "boolean"},
        "charter": {"type": "boolean"},
        "county": {"type": "string"},
        "min_rev_title_i": {"type": "number", "description": "federal Title I $/yr"},
        "min_rev_vocational": {"type": "number", "description": "CTE/Perkins $/yr"},
        "min_cap_instruc_equip": {"type": "number",
                                  "description": "capital outlay on instructional equipment $/yr"},
        "sort": {"type": "string",
                 "enum": ["enrollment", "sci_sections", "title_i", "cap_equip",
                          "name"], "default": "enrollment"},
        "limit": {"type": "integer", "default": 100, "maximum": 5000},
    }},
    cost_class=CostClass.FREE,
)
def k12_find_districts(ctx, **params) -> dict:
    m = estate.current_manifest(ctx.settings, "k12")
    if not m:
        return error_envelope(estate.K12_MISSING_MSG, error_type="EstateMissing")
    cov_err = _coverage_error(m, params.get("states") or [])
    if cov_err:
        return cov_err
    rows, warnings = k12_local.find_districts(ctx.k12(), **params)
    cols = _columns()
    keys = [c["key"] for c in cols]
    data = [[r.get(k) for k in keys] for r in rows]
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    states = params.get("states") or []
    title = f"Districts — {', '.join(s.upper() for s in states)}" if states \
        else "Districts — national"
    top = f" Top: {rows[0]['name']} ({rows[0].get('enrollment') or '?'} students)." \
        if rows else ""
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="k12_find_districts", title=title,
        columns=cols, rows=data, provenance=[_k12_prov(m)],
        summary=f"{len(rows)} districts matched.{top}",
        warnings=warnings, stats={"by_state": by_state})


@tool_spec(
    name="k12_district_profile",
    description=(
        "Dossier for ONE district by leaid (or exact-ish name + state) from "
        "the app's own estate: identity, size, science footprint (CRDC), "
        "federal finance (Title I / CTE / math-sci / capital equipment $), "
        "plus every contact this app has researched for it. Free, local."),
    input_schema={"properties": {
        "leaid": {"type": "string", "description": "7-digit NCES district id"},
        "name": {"type": "string", "description": "used when leaid unknown"},
        "state": {"type": "string", "pattern": "^[A-Za-z]{2}$"},
    }},
    cost_class=CostClass.FREE,
)
def k12_district_profile(ctx, leaid: str | None = None, name: str | None = None,
                         state: str | None = None) -> dict:
    m = estate.current_manifest(ctx.settings, "k12")
    if not m:
        return error_envelope(estate.K12_MISSING_MSG, error_type="EstateMissing")
    conn = ctx.k12()
    if not leaid and name:
        st = (state or "").upper()
        q = ("SELECT leaid, name, state, enrollment FROM districts "
             "WHERE name LIKE ? " + ("AND state=? " if st else "") +
             "ORDER BY enrollment DESC LIMIT 5")
        args = [f"%{name}%"] + ([st] if st else [])
        cands = [dict(r) for r in conn.execute(q, args)]
        if not cands:
            return error_envelope(f"no district matching '{name}'"
                                  + (f" in {st}" if st else ""))
        if len(cands) > 1 and not (cands[0]["name"].lower() == name.lower()):
            listing = "; ".join(f"{c['name']} ({c['state']}, leaid {c['leaid']})"
                                for c in cands)
            return error_envelope(
                f"ambiguous name '{name}' — candidates: {listing}. "
                "Call again with the leaid.", error_type="Ambiguous")
        leaid = cands[0]["leaid"]
    if not leaid:
        return error_envelope("pass leaid or name")
    p = k12_local.profile_data(conn, ctx.rw(), leaid)
    if p is None:
        return error_envelope(f"unknown leaid {k12_local.coerce_leaid(leaid)}")

    d, fin, crdc = p["district"], p.get("finance") or {}, p.get("crdc") or {}
    md = [f"## {d['name']} ({d['state']})",
          f"**leaid** {d['leaid']} · {d.get('city') or ''} "
          f"{d.get('county_name') or ''}"
          + (f" · enrollment **{d['enrollment']:,}**" if d.get("enrollment") else "")
          + (f" · {d['number_of_schools']} schools"
             if d.get("number_of_schools") else "")
          + (" · charter" if d.get("charter") else ""),
          ""]
    if crdc.get("sci_sections") is not None:
        md += [f"**Science footprint (CRDC {crdc.get('year')}):** "
               f"{crdc['sci_sections']:.0f} bio/chem/physics sections, "
               f"{crdc.get('ap_sci_schools') or 0} AP-science schools", ""]
    money = [(lbl, fin.get(k)) for lbl, k in (
        ("Title I", "rev_title_i"), ("CTE/Perkins", "rev_vocational"),
        ("Math/Sci", "rev_math_sci"), ("Capital equip", "cap_instruc_equip"),
        ("Tech equip", "exp_tech_equipment"), ("Textbooks", "exp_textbooks"),
        ("Debt issued", "debt_issued_fy")) if fin.get(k)]
    if money:
        md += [f"**Finance (F-33 {fin.get('year')}):** "
               + " · ".join(f"{lbl}: ${v:,.0f}" for lbl, v in money), ""]
    cts = p.get("contacts") or []
    if cts:
        md += ["**Contacts (this app's research history):**"] + [
            f"- {x.get('full_name')} — {x.get('title')} ({x.get('source')}"
            + (f", {x.get('email')}" if x.get("email") else "") + ")"
            for x in cts[:8]] + [""]
    else:
        md += ["_No contacts researched yet — k12_contacts / seamless_search "
               "can find them._", ""]

    text = "\n".join(md)
    from ..artifacts import store
    art = store.create(ctx.rw(), conversation_id=ctx.conversation_id,
                       tool="k12_district_profile", kind="markdown",
                       title=f"Profile — {d['name']}",
                       columns=[{"key": "markdown", "label": "markdown"}],
                       rows=[[text]], provenance=[_k12_prov(m)])
    ctx.emit("artifact", {"artifact_id": art["artifact_id"], "version": 1,
                          "kind": "markdown", "title": f"Profile — {d['name']}"})
    return envelope(kind="markdown",
                    summary=f"profile for {d['name']} ({d['state']}), "
                            f"leaid {d['leaid']}",
                    artifact=art, markdown=text, provenance=[_k12_prov(m)])


@tool_spec(
    name="k12_contacts",
    description=(
        "District contacts from THIS app's own research history (Seamless "
        "research results, uploads, manual adds) ranked purchasing > "
        "curriculum > CTE > science. Starts empty on a fresh install and "
        "grows as reps research. ALWAYS try before paid Seamless tools. "
        "best_only=true returns one best contact per district. Free."),
    input_schema={"properties": {
        "leaids": {"type": "array", "items": {"type": "string"}},
        "states": _STATES_PARAM,
        "role_bucket": {"type": "string",
                        "enum": ["purchasing", "curriculum", "cte", "science",
                                 "admin", "other"]},
        "require_email": {"type": "boolean", "default": False},
        "best_only": {"type": "boolean", "default": True},
        "limit": {"type": "integer", "default": 500, "maximum": 5000},
    }},
    cost_class=CostClass.FREE,
)
def k12_contacts(ctx, **params) -> dict:
    leaids = params.get("leaids") or []
    try:
        k12_conn = ctx.k12()
    except estate.EstateMissing:
        k12_conn = None      # contacts still work; names/states just blank
    if params.get("states") and k12_conn is None:
        return error_envelope(
            "filtering contacts by state needs the k12 estate (contacts "
            "store leaid only). " + estate.K12_MISSING_MSG,
            error_type="EstateMissing")
    rows = k12_local.contacts(ctx.rw(), k12_conn, **params)
    cols = [
        {"key": "leaid", "label": "District ID", "type": "string"},
        {"key": "district", "label": "District", "type": "string"},
        {"key": "state", "label": "State", "type": "string"},
        {"key": "full_name", "label": "Name", "type": "string"},
        {"key": "title", "label": "Title", "type": "string"},
        {"key": "role_bucket", "label": "Role", "type": "string"},
        {"key": "email", "label": "Email", "type": "string"},
        {"key": "email_validation", "label": "Email valid?", "type": "string"},
        {"key": "phone", "label": "Phone", "type": "string"},
        {"key": "source", "label": "Source", "type": "string"},
    ]
    data = [[r.get(c["key"]) for c in cols] for r in rows]
    stats: dict = {"with_email": sum(1 for r in rows if r.get("email"))}
    warnings = []
    if leaids:
        missing = k12_local.districts_without_contact(ctx.rw(), leaids)
        stats["districts_without_contact"] = len(missing)
        if missing:
            warnings.append(
                f"{len(missing)} of the requested districts have no contact "
                "in this app's history yet — propose seamless_search for "
                "those (paid preview).")
    if not rows and not leaids:
        warnings.append("contact history is empty so far — it fills as reps "
                        "research via Seamless or upload contact lists.")
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="k12_contacts", title="District contacts (app research history)",
        columns=cols, rows=data,
        provenance=[prov("contacts_app (this app's own research)",
                         "Seamless research + uploads + manual adds")],
        summary=f"{len(rows)} contacts.", warnings=warnings, stats=stats)
