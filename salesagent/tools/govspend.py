"""Government-spend tools: USAspending (federal) + open checkbooks (state/
city). All keyless & free; recipes from timMtesting t01/t03b/t04/t05."""
from __future__ import annotations

from collections import defaultdict

from ..integrations.claude_cached import cached_call
from ..integrations.http_cache import cached_post_json
from ..integrations.socrata import (buyer_of, detect, is_edu, q_search,
                                    q_search_paged, to_float)
from .envelope import error_envelope, prov, table_envelope
from .registry import CostClass, tool_spec

USA_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
USA_FIELDS = ["Award ID", "Recipient Name", "Start Date", "End Date",
              "Award Amount", "Awarding Agency", "Awarding Sub Agency",
              "Description"]
_STATES_PARAM = {"type": "array", "items": {"type": "string",
                                            "pattern": "^[A-Za-z]{2}$"}}

COVERAGE_NOTE = ("Checkbook coverage is PARTIAL by construction: school-district-"
                 "level vendor spend exists ONLY in DE (statewide), MD (county "
                 "boards), NYC, Providence RI; state-agency level CT/MO/VT/OR; "
                 "elsewhere only big cities/counties. No rows for a state = NO "
                 "DATA PUBLISHED, not no purchases.")


def _usa_pages(conn, filters: dict, max_pages: int, ttl_days: float
               ) -> tuple[list[dict], bool]:
    out, any_hit = [], False
    for page in range(1, max_pages + 1):
        payload = {"filters": filters, "fields": USA_FIELDS,
                   "limit": 100, "page": page}
        data, hit = cached_post_json(conn, "usaspending", USA_URL, payload,
                                     ttl_days)
        any_hit = any_hit or hit
        results = (data or {}).get("results") or []
        out.extend(results)
        if len(results) < 100:
            break
    return out, any_hit


@tool_spec(
    name="usaspending_vendor_customers",
    description=(
        "Which FEDERAL agencies buy from a vendor (e.g. ULINE, FASTENAL, "
        "GRAINGER, FISHER SCIENTIFIC): prime contracts where the vendor is the "
        "recipient, from USAspending.gov. The Description column IS the "
        "product mix — read it. All 50 states. Keyless, free."),
    input_schema={"properties": {
        "vendor": {"type": "string"},
        "since_fy": {"type": "integer", "description": "first federal FY, e.g. 2022"},
        "min_amount": {"type": "number"},
        "max_pages": {"type": "integer", "default": 5, "maximum": 20},
    }, "required": ["vendor"]},
    cost_class=CostClass.FREE,
)
def usaspending_vendor_customers(ctx, vendor: str, since_fy: int | None = None,
                                 min_amount: float | None = None,
                                 max_pages: int = 5) -> dict:
    filters: dict = {"award_type_codes": ["A", "B", "C", "D"],
                     "recipient_search_text": [vendor.upper()]}
    if since_fy:
        filters["time_period"] = [{"start_date": f"{since_fy - 1}-10-01",
                                   "end_date": "2030-09-30"}]
    results, cache_hit = _usa_pages(ctx.rw(), filters, max_pages, ttl_days=7)
    if min_amount is not None:
        results = [r for r in results
                   if to_float(r.get("Award Amount")) >= min_amount]
    cols = [
        {"key": "agency", "label": "Awarding agency", "type": "string"},
        {"key": "sub_agency", "label": "Sub-agency", "type": "string"},
        {"key": "recipient", "label": "Recipient", "type": "string"},
        {"key": "amount", "label": "Amount", "type": "number", "format": "money"},
        {"key": "start", "label": "Start", "type": "string", "format": "date"},
        {"key": "description", "label": "What they bought", "type": "string"},
        {"key": "award_id", "label": "Award ID", "type": "string"},
    ]
    rows = [[r.get("Awarding Agency"), r.get("Awarding Sub Agency"),
             r.get("Recipient Name"), to_float(r.get("Award Amount")),
             r.get("Start Date"), r.get("Description"), r.get("Award ID")]
            for r in results]
    by_agency: dict[str, float] = defaultdict(float)
    for r in rows:
        by_agency[r[0] or "?"] += r[3]
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="usaspending_vendor_customers",
        title=f"Federal buyers of {vendor.upper()}",
        columns=cols, rows=rows,
        provenance=[prov("USAspending.gov",
                         "spending_by_award, prime contracts (A-D), "
                         f"recipient_search_text={vendor.upper()}",
                         "https://api.usaspending.gov")],
        summary=f"{len(rows)} federal contracts to {vendor.upper()}.",
        stats={"total_usd": round(sum(r[3] for r in rows)),
               "by_agency_usd": {k: round(v) for k, v in
                                 sorted(by_agency.items(), key=lambda kv: -kv[1])}},
        warnings=[])


RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {"relevant": {"type": "boolean"},
                   "why": {"type": "string"}},
    "required": ["relevant"], "additionalProperties": False}


@tool_spec(
    name="usaspending_keyword_vendors",
    description=(
        "Find VENDORS selling a product category to the federal government by "
        "keyword (e.g. 'science kit', 'dissection kit'). Raw keyword hits "
        "include medical/diagnostics noise (Illumina reagent kits etc.) — a "
        "Haiku relevance filter is applied automatically (filter_noise). "
        "Keyless API + pennies of cached Haiku. Cheap."),
    input_schema={"properties": {
        "keywords": {"type": "array", "items": {"type": "string"}},
        "relevance_context": {"type": "string",
                              "default": "school/education science lab supplies and kits"},
        "filter_noise": {"type": "boolean", "default": True},
        "max_pages": {"type": "integer", "default": 3, "maximum": 10},
    }, "required": ["keywords"]},
    cost_class=CostClass.CHEAP,
)
def usaspending_keyword_vendors(ctx, keywords: list[str],
                                relevance_context: str = "school/education science lab supplies and kits",
                                filter_noise: bool = True,
                                max_pages: int = 3) -> dict:
    conn = ctx.rw()
    vendors: dict[str, dict] = {}
    any_hit = False
    for kw in keywords[:8]:
        results, hit = _usa_pages(
            conn, {"award_type_codes": ["A", "B", "C", "D"], "keywords": [kw]},
            max_pages, ttl_days=30)
        any_hit = any_hit or hit
        for r in results:
            name = r.get("Recipient Name") or "?"
            v = vendors.setdefault(name, {"n": 0, "total": 0.0,
                                          "descriptions": set(),
                                          "agencies": set()})
            v["n"] += 1
            v["total"] += to_float(r.get("Award Amount"))
            if r.get("Description"):
                v["descriptions"].add(str(r["Description"])[:110])
            if r.get("Awarding Agency"):
                v["agencies"].add(r["Awarding Agency"])

    dropped = 0
    if filter_noise and vendors:
        keep = {}
        for name, v in vendors.items():
            verdict = cached_call(
                ctx.settings, conn, task="usaspending_relevance",
                prompt_version="1",
                input_obj={"vendor": name,
                           "descriptions": sorted(v["descriptions"])[:5],
                           "context": relevance_context},
                prompt=("Is this federal contractor relevant to the context "
                        f"'{relevance_context}'?\nVendor: {name}\nAward "
                        "descriptions:\n- "
                        + "\n- ".join(sorted(v["descriptions"])[:5])
                        + "\nMedical/molecular-diagnostics reagent kits, oil "
                          "test kits, military hardware kits are NOT relevant "
                          "unless the context says so. Answer strictly."),
                schema=RELEVANCE_SCHEMA, max_tokens=200)
            if verdict.get("relevant"):
                keep[name] = v
            else:
                dropped += 1
        vendors = keep

    cols = [{"key": "vendor", "label": "Vendor", "type": "string"},
            {"key": "awards", "label": "Awards", "type": "number", "format": "int"},
            {"key": "total", "label": "Total $", "type": "number", "format": "money"},
            {"key": "agencies", "label": "Agencies", "type": "string"},
            {"key": "sample", "label": "Sample descriptions", "type": "string"}]
    rows = [[name, v["n"], round(v["total"]),
             "; ".join(sorted(v["agencies"])[:3]),
             " | ".join(sorted(v["descriptions"])[:2])]
            for name, v in sorted(vendors.items(), key=lambda kv: -kv[1]["total"])]
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="usaspending_keyword_vendors",
        title=f"Federal vendors: {', '.join(keywords[:3])}",
        columns=cols, rows=rows,
        provenance=[prov("USAspending.gov", f"keywords={keywords}",
                         "https://api.usaspending.gov")],
        summary=f"{len(rows)} relevant vendors"
                + (f" ({dropped} noise vendors filtered out by Haiku)."
                   if filter_noise else "."),
        warnings=[])


@tool_spec(
    name="checkbook_vendor_customers",
    description=(
        "Which PUBLIC agencies/schools pay a vendor, from state & city open "
        "checkbooks (full-text $q search across a curated dataset roster). "
        "Vendor-agnostic: works for ULINE, FASTENAL, GRAINGER, FISHER — "
        "competitor customer lists. " + COVERAGE_NOTE + " Free."),
    input_schema={"properties": {
        "vendor": {"type": "string"},
        "states": _STATES_PARAM,
        "level": {"type": "string",
                  "enum": ["any", "school_edu", "agency", "city_county"],
                  "default": "any"},
        "limit_per_dataset": {"type": "integer", "default": 1000, "maximum": 1000},
    }, "required": ["vendor"]},
    cost_class=CostClass.FREE,
)
def checkbook_vendor_customers(ctx, vendor: str, states: list[str] | None = None,
                               level: str = "any",
                               limit_per_dataset: int = 1000) -> dict:
    conn = ctx.rw()
    q = "SELECT * FROM ref_checkbook_datasets WHERE 1=1"
    args: list = []
    if states:
        sts = [s.upper() for s in states]
        q += f" AND state IN ({','.join('?' * len(sts))})"
        args += sts
    if level and level != "any":
        q += " AND level = ?"
        args.append(level)
    datasets = [dict(r) for r in conn.execute(q, args)]
    warnings = [COVERAGE_NOTE]
    if states:
        covered = {d["state"] for d in datasets}
        missing = [s.upper() for s in states if s.upper() not in covered]
        if missing:
            warnings.append(f"No checkbook data published for: "
                            f"{', '.join(missing)} — unknowable, not zero.")

    rows_out: list[list] = []
    any_cache = False
    by_state: dict[str, float] = defaultdict(float)
    ds_errors: list[str] = []
    for ds in datasets:
        if ds["adapter"] != "socrata":
            ds_errors.append(f"{ds['domain']} ({ds['adapter']} adapter lands in M4)")
            continue
        try:
            rows, hit = q_search(conn, ds["domain"], ds["dataset_id"], vendor,
                                 limit=limit_per_dataset)
        except Exception as e:
            ds_errors.append(f"{ds['domain']}/{ds['dataset_id']}: {type(e).__name__}")
            continue
        any_cache = any_cache or hit
        if not rows:
            continue
        vcol, bcols, acol, dcol = detect(rows, vendor)
        for x in rows:
            amt = to_float(x.get(acol)) if acol else 0.0
            buyer = buyer_of(x, bcols)
            edu = is_edu(x, bcols)
            rows_out.append([buyer or "(unlabeled)",
                             "SCHOOL/EDU" if edu or ds["level"] == "school_edu" else ds["level"],
                             ds["state"], amt,
                             str(x.get(dcol) or "")[:100] if dcol else "",
                             ds["name"], ds["domain"]])
            by_state[ds["state"]] += amt
    if ds_errors:
        warnings.append("skipped datasets: " + "; ".join(ds_errors[:6]))

    cols = [{"key": "buyer", "label": "Buyer", "type": "string"},
            {"key": "level", "label": "Level", "type": "string"},
            {"key": "state", "label": "State", "type": "string"},
            {"key": "amount", "label": "Amount", "type": "number", "format": "money"},
            {"key": "detail", "label": "Line detail", "type": "string"},
            {"key": "dataset", "label": "Dataset", "type": "string"},
            {"key": "domain", "label": "Source domain", "type": "string"}]
    edu_n = sum(1 for r in rows_out if r[1] == "SCHOOL/EDU")
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="checkbook_vendor_customers",
        title=f"Public buyers of {vendor.upper()} (checkbooks)",
        columns=cols, rows=rows_out,
        provenance=[prov("Socrata open checkbooks",
                         f"$q={vendor.upper()} across {len(datasets)} curated "
                         "datasets (timMtesting t02/t03b roster)",
                         "https://api.us.socrata.com")],
        summary=f"{len(rows_out)} payment lines across "
                f"{len(set(r[6] for r in rows_out))} sources; "
                f"{edu_n} school/edu lines.",
        stats={"by_state_usd": {k: round(v) for k, v in
                                sorted(by_state.items(), key=lambda kv: -kv[1])},
               "school_edu_lines": edu_n},
        warnings=warnings)


@tool_spec(
    name="checkbook_basket",
    description=(
        "What a public buyer ACTUALLY purchases from a vendor: spend by "
        "account/category from checkbook line detail, plus Uline-carried "
        "adjacency cross-sell suggestions per category. Deep line detail "
        "exists mainly in DE (plus MD/Providence). Free."),
    input_schema={"properties": {
        "vendor": {"type": "string"},
        "states": _STATES_PARAM,
        "buyer_query": {"type": "string",
                        "description": "optional buyer-name substring, e.g. a district"},
        "max_pages": {"type": "integer", "default": 5, "maximum": 10},
    }, "required": ["vendor"]},
    cost_class=CostClass.FREE,
)
def checkbook_basket(ctx, vendor: str, states: list[str] | None = None,
                     buyer_query: str | None = None, max_pages: int = 5) -> dict:
    conn = ctx.rw()
    sts = [s.upper() for s in (states or ["DE"])]
    datasets = [dict(r) for r in conn.execute(
        "SELECT * FROM ref_checkbook_datasets WHERE adapter='socrata' AND "
        f"level='school_edu' AND state IN ({','.join('?' * len(sts))})", sts)]
    if not datasets:
        return error_envelope(
            f"no school-level checkbook detail for {sts} — deep basket data "
            "exists in DE, MD, RI (and NYC via the M4 adapter). " + COVERAGE_NOTE)

    adjacency = {r["category"]: r["suggestions_json"] for r in
                 conn.execute("SELECT category, suggestions_json "
                              "FROM ref_basket_adjacency")}
    import json as _json
    basket: dict[str, dict] = defaultdict(lambda: {"lines": 0, "total": 0.0})
    scanned = 0
    for ds in datasets:
        rows, _hit = q_search_paged(conn, ds["domain"], ds["dataset_id"],
                                    vendor, max_pages=max_pages)
        if not rows:
            continue
        vcol, bcols, acol, dcol = detect(rows, vendor)
        for x in rows:
            if buyer_query and buyer_query.lower() not in buyer_of(x, bcols).lower():
                continue
            scanned += 1
            cat = str(x.get(dcol) or "(uncategorized)").strip().upper()[:60] \
                if dcol else "(uncategorized)"
            basket[cat]["lines"] += 1
            basket[cat]["total"] += to_float(x.get(acol)) if acol else 0.0

    cols = [{"key": "category", "label": "Category", "type": "string"},
            {"key": "lines", "label": "Lines", "type": "number", "format": "int"},
            {"key": "total", "label": "Total $", "type": "number", "format": "money"},
            {"key": "adjacent", "label": "Uline-carried adjacencies (pitch)",
             "type": "string"}]
    rows_out = []
    for cat, agg in sorted(basket.items(), key=lambda kv: -kv[1]["total"]):
        sugg = ""
        for known, sjson in adjacency.items():
            if known in cat:
                sugg = ", ".join(_json.loads(sjson))
                break
        rows_out.append([cat, agg["lines"], round(agg["total"]), sugg])
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="checkbook_basket",
        title=f"{vendor.upper()} basket — {', '.join(sts)}"
              + (f" ({buyer_query})" if buyer_query else ""),
        columns=cols, rows=rows_out,
        provenance=[prov("Socrata checkbook line detail",
                         f"$q={vendor.upper()} paged, category aggregation "
                         "(t04 recipe); adjacency map from t10",
                         "https://data.delaware.gov")],
        summary=f"{scanned} payment lines aggregated into {len(rows_out)} "
                "categories.",
        warnings=["Line-category detail is mainly a DE strength; other states "
                  "may bucket coarsely."])
