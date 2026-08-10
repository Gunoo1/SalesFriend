"""Seamless.ai tools: search = cheap preview (no email/phone), research =
PAID reveal behind the confirm interrupt. Credit rails live in the client
(ledger, day cap, dry-run) — not in the prompt."""
from __future__ import annotations

import json

from ..artifacts import store as artifact_store
from ..integrations.http_cache import cache_get, cache_key, cache_put
from ..integrations.seamless_client import (SeamlessClient, SeamlessError,
                                            best_email, best_phone,
                                            name_variants)
from ..db import utcnow
from .envelope import envelope, error_envelope, prov, table_envelope
from .registry import CostClass, tool_spec

SEARCH_COLS = [
    {"key": "name", "label": "Name", "type": "string"},
    {"key": "title", "label": "Title", "type": "string"},
    {"key": "company", "label": "Company", "type": "string"},
    {"key": "city", "label": "City", "type": "string"},
    {"key": "state", "label": "State", "type": "string"},
    {"key": "search_result_id", "label": "", "type": "string", "hidden": True},
]


@tool_spec(
    name="seamless_search",
    description=(
        "PREVIEW people at an organization by state and job titles via "
        "Seamless.ai: returns name/title/company/city ONLY — no email/phone "
        "(that's seamless_research, paid + confirmed). Costs ~1 credit per 10 "
        "rows, so keep limit small. Geo filter is STATE-level only (no "
        "zip/city filter exists in the API). Name variants (District "
        "abbreviation expansion, '{City} Board of Education') are tried "
        "automatically when the exact name returns nothing. Cheap."),
    input_schema={"properties": {
        "company": {"type": "string"},
        "state": {"type": "string", "pattern": "^[A-Za-z]{2}$"},
        "titles": {"type": "array", "items": {"type": "string"},
                   "description": "e.g. ['Purchasing Manager','Director of Procurement']"},
        "city": {"type": "string", "description": "improves name variants only"},
        "limit": {"type": "integer", "default": 10, "maximum": 25},
    }, "required": ["company", "state"]},
    cost_class=CostClass.CHEAP,
)
def seamless_search(ctx, company: str, state: str,
                    titles: list[str] | None = None, city: str | None = None,
                    limit: int = 10) -> dict:
    conn = ctx.rw()
    state = state.upper()
    key = cache_key("seamless_search",
                    "search", {"company": company.lower(), "state": state,
                               "titles": sorted(t.lower() for t in (titles or [])),
                               "limit": limit})
    cached = cache_get(conn, key)
    cache_hit = cached is not None
    variants_tried = []
    if cached is None:
        client = SeamlessClient(ctx.settings, conn, ctx.user.get("id"),
                                ctx.conversation_id)
        rows: list[dict] = []
        try:
            for variant in name_variants(company, city):
                variants_tried.append(variant)
                rows = client.search(variant, state, titles,
                                     search_type="exact", limit=limit)
                if rows:
                    break
                # exact miss is free-ish; one default-match fallback
                rows = client.search(variant, state, titles,
                                     search_type="default", limit=limit)
                if rows:
                    break
        except SeamlessError as e:
            return error_envelope(str(e), error_type="SeamlessError")
        cached = rows
        cache_put(conn, key, "seamless_search", "search",
                  {"company": company, "state": state}, rows, ttl_days=14)

    data = [[r.get("name"), r.get("title"), r.get("company"), r.get("city"),
             r.get("state"), r.get("searchResultId")] for r in cached]
    dry = " (DRY RUN — synthetic rows)" if ctx.settings.seamless_dry_run and not cache_hit else ""
    env = table_envelope(
        conn, ctx.emit, conversation_id=ctx.conversation_id,
        tool="seamless_search",
        title=f"Seamless preview — {company} ({state})",
        columns=SEARCH_COLS, rows=data,
        provenance=[prov("Seamless.ai /search/contacts",
                         f"company={company} state={state} titles={titles}")],
        summary=f"{len(data)} people previewed{dry}. Emails/phones require "
                f"seamless_research (1 credit each, user confirmation).",
        warnings=(["exact name returned nothing; matched via variant "
                   + variants_tried[-1]] if len(variants_tried) > 1 and data else []),
        stats={"cache_hit": cache_hit})
    env["cache_hit"] = cache_hit
    return env


def _estimate_research(args: dict) -> int:
    rows = args.get("row_indices") or []
    return max(1, len(rows))


@tool_spec(
    name="seamless_research",
    description=(
        "Reveal verified email + phone for contacts previewed by "
        "seamless_search. PAID: 1 Seamless credit per contact — ALWAYS pauses "
        "for the rep's approval with an itemized estimate. Already-researched "
        "contacts are recovered free first. Pass the preview artifact_id and "
        "the row indices (0-based) to research, plus a one-line reason."),
    input_schema={"properties": {
        "artifact_id": {"type": "string",
                        "description": "the seamless_search result artifact"},
        "row_indices": {"type": "array", "items": {"type": "integer"},
                        "description": "0-based rows in that artifact"},
        "reason": {"type": "string"},
    }, "required": ["artifact_id", "row_indices"]},
    cost_class=CostClass.PAID_CREDITS,
    needs_confirmation=True,
    estimate_cost=_estimate_research,
)
def seamless_research(ctx, artifact_id: str, row_indices: list[int],
                      reason: str = "") -> dict:
    conn = ctx.rw()
    spec = artifact_store.get(conn, artifact_id)
    if not spec or spec.get("conversation_id") != ctx.conversation_id:
        return error_envelope(f"artifact {artifact_id} not found here")
    keys = [c["key"] for c in spec["columns"]]
    try:
        i_srid = keys.index("search_result_id")
    except ValueError:
        return error_envelope(
            "that artifact is not a seamless_search preview (no "
            "search_result_id column)")
    i_name = keys.index("name") if "name" in keys else None
    i_company = keys.index("company") if "company" in keys else None

    targets = []   # (srid, name, company)
    for idx in row_indices[:50]:
        if 0 <= idx < len(spec["rows"]):
            r = spec["rows"][idx]
            if r[i_srid]:
                targets.append((str(r[i_srid]),
                                r[i_name] if i_name is not None else None,
                                r[i_company] if i_company is not None else None))
    if not targets:
        return error_envelope("no valid rows selected")

    # free-first: contacts already researched (this org) come from contacts_app
    known: dict[str, dict] = {}
    fresh: list[tuple] = []
    for srid, name, comp in targets:
        row = conn.execute("SELECT * FROM contacts_app WHERE search_result_id=?",
                           (srid,)).fetchone()
        if row:
            known[srid] = dict(row)
        else:
            fresh.append((srid, name, comp))

    client = SeamlessClient(ctx.settings, conn, ctx.user.get("id"),
                            ctx.conversation_id)
    results: dict[str, dict | None] = {}
    if fresh:
        try:
            results = client.research([t[0] for t in fresh])
        except SeamlessError as e:
            return error_envelope(str(e), error_type="SeamlessError")

    rows_out = []
    meta_by_srid = {t[0]: t for t in targets}
    for srid, _name, _comp in targets:
        if srid in known:
            k = known[srid]
            rows_out.append([k.get("full_name"), k.get("title"),
                             k.get("company"), k.get("email"),
                             k.get("email_validation"),
                             k.get("seamless_confidence"), k.get("phone"),
                             "already researched (free)"])
            continue
        contact = results.get(srid)
        name = meta_by_srid[srid][1]
        comp = meta_by_srid[srid][2]
        if not contact:
            rows_out.append([name, None, comp, None, None, None, None,
                             "not found / research failed"])
            continue
        email, validation, confidence = best_email(contact)
        phone = best_phone(contact)
        full_name = contact.get("fullName") or name
        title = contact.get("title")
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO contacts_app (leaid, company, source,"
                " full_name, title, email, email_validation,"
                " seamless_confidence, phone, role_bucket, search_result_id,"
                " researched_at, raw_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (None, comp, "seamless", full_name, title, email, validation,
                 confidence, phone, None, srid, utcnow(),
                 json.dumps(contact, default=str)[:20000], utcnow()))
        rows_out.append([full_name, title, comp, email, validation,
                         confidence, phone, "researched"])

    cols = [{"key": "name", "label": "Name", "type": "string"},
            {"key": "title", "label": "Title", "type": "string"},
            {"key": "company", "label": "Company", "type": "string"},
            {"key": "email", "label": "Email", "type": "string"},
            {"key": "email_validation", "label": "Email AI", "type": "string"},
            {"key": "confidence", "label": "Confidence", "type": "string"},
            {"key": "phone", "label": "Phone", "type": "string"},
            {"key": "status", "label": "Status", "type": "string"}]
    got = sum(1 for r in rows_out if r[3])
    free_n = len(known)
    dry = " (DRY RUN — synthetic)" if ctx.settings.seamless_dry_run else ""
    env = table_envelope(
        conn, ctx.emit, conversation_id=ctx.conversation_id,
        tool="seamless_research",
        title=f"Researched contacts ({got}/{len(rows_out)})",
        columns=cols, rows=rows_out,
        provenance=[prov("Seamless.ai /contacts/research",
                         f"{len(targets)} contacts; reason: {reason[:80]}")],
        summary=f"{got}/{len(rows_out)} contacts revealed{dry} "
                f"({free_n} recovered free).",
        warnings=[])
    env["credits_spent"] = max(0, len(fresh) - 0) if not ctx.settings.seamless_dry_run else 0
    return env
