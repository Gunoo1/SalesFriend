"""General research tools: web search (Serper), page/PDF fetch with optional
Haiku extraction, and per-district sales briefs."""
from __future__ import annotations

from ..integrations import serper
from ..integrations.claude_cached import cached_call
from ..integrations.fetchers import ddg_resolve, fetch, keyword_windows
from .envelope import envelope, error_envelope, prov, table_envelope
from .registry import CostClass, tool_spec


@tool_spec(
    name="web_search",
    description=(
        "General Google web search (Serper API) for anything a specialized "
        "tool doesn't cover: company facts, news, sites to fetch. Returns "
        "organic results + knowledge graph. Metered (~$0.001/query, cached "
        "7 days org-wide)."),
    input_schema={"properties": {
        "q": {"type": "string"},
        "num": {"type": "integer", "default": 10, "maximum": 20},
        "site": {"type": "string", "description": "restrict to a domain"},
        "gov_only": {"type": "boolean", "default": False},
    }, "required": ["q"]},
    cost_class=CostClass.CHEAP,
)
def web_search(ctx, q: str, num: int = 10, site: str | None = None,
               gov_only: bool = False) -> dict:
    query = q
    if site:
        query += f" site:{site}"
    elif gov_only:
        query += " site:.gov"
    try:
        data, cache_hit = serper.search(ctx.settings, ctx.rw(), query, num)
    except serper.SerperError as e:
        # graceful fallback for ANY Serper failure (key unset, no credits,
        # rate limit): DDG resolver (slower, no snippets)
        hits = ddg_resolve(query, prefer_gov=gov_only)
        if not hits:
            return error_envelope(str(e) + " — and DDG fallback found "
                                  "nothing (or was bot-challenged)")
        rows = [[h["title"], h["url"], ""] for h in hits]
        return table_envelope(
            ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
            tool="web_search", title=f"Search — {q}"[:60],
            columns=[{"key": "title", "label": "Title", "type": "string"},
                     {"key": "url", "label": "URL", "type": "string", "format": "link"},
                     {"key": "snippet", "label": "Snippet", "type": "string"}],
            rows=rows,
            provenance=[prov("DuckDuckGo HTML (Serper unavailable)", query)],
            summary=f"{len(rows)} results via DDG fallback (Serper: {e}).",
            warnings=[f"Serper unavailable ({e}) — DDG fallback has no "
                      "snippets. Check the serper.dev account/credits."])

    rows = []
    kg = data.get("knowledgeGraph") or {}
    if kg:
        rows.append([f"[KG] {kg.get('title', '')} — {kg.get('type', '')}",
                     kg.get("website") or "", str(kg.get("description") or "")[:200]])
    for it in data.get("organic") or []:
        rows.append([it.get("title"), it.get("link"),
                     str(it.get("snippet") or "")[:200]])
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="web_search", title=f"Search — {q}"[:60],
        columns=[{"key": "title", "label": "Title", "type": "string"},
                 {"key": "url", "label": "URL", "type": "string", "format": "link"},
                 {"key": "snippet", "label": "Snippet", "type": "string"}],
        rows=rows,
        provenance=[prov("Serper (Google)", query, "https://serper.dev")],
        summary=f"{len(rows)} results.",
        warnings=[])


@tool_spec(
    name="fetch_page",
    description=(
        "Fetch a URL (browser-impersonated) or PDF and return readable text; "
        "optionally extract structured facts with a JSON schema (Haiku, "
        "cached). resolve_query mode searches DuckDuckGo first and fetches "
        "the best .gov hit. status='blocked' means UNKNOWN — never conclude "
        "absence from a blocked fetch. Cheap."),
    input_schema={"properties": {
        "url": {"type": "string"},
        "resolve_query": {"type": "string",
                          "description": "search instead of a direct URL"},
        "extract": {"type": "object",
                    "description": "optional: {question: str, keywords: [str]} -> Haiku JSON extraction"},
        "max_chars": {"type": "integer", "default": 5000, "maximum": 12000},
    }},
    cost_class=CostClass.CHEAP,
)
def fetch_page(ctx, url: str | None = None, resolve_query: str | None = None,
               extract: dict | None = None, max_chars: int = 5000) -> dict:
    resolved_from = None
    if not url and resolve_query:
        hits = ddg_resolve(resolve_query)
        if not hits:
            return error_envelope("DDG found nothing (or bot-challenged all "
                                  "3 attempts) — try again later or give a URL",
                                  error_type="Blocked")
        url = hits[0]["url"]
        resolved_from = resolve_query
    if not url:
        return error_envelope("pass url or resolve_query")

    result = fetch(url)
    if result["status"] == "blocked":
        return error_envelope(
            f"{url} is BLOCKED (anti-bot): content UNKNOWN — do not treat as "
            f"absence. {result.get('note', '')}", error_type="Blocked")
    if result["status"] in ("error", "empty"):
        return error_envelope(f"fetch {result['status']}: "
                              f"{result.get('note') or result.get('http_status')}")

    text = result["text"]
    extraction = None
    if extract and extract.get("question"):
        window = keyword_windows(text, extract.get("keywords") or
                                 extract["question"].split()[:4])
        extraction = cached_call(
            ctx.settings, ctx.rw(), task="fetch_extract", prompt_version="1",
            input_obj={"url": url, "q": extract["question"],
                       "window_hash": hash(window) % 10**10},
            prompt=(f"From this page text, answer: {extract['question']}\n"
                    "Null anything the text doesn't state.\n\n" + window),
            schema={"type": "object",
                    "properties": {"answer": {"type": ["string", "null"]},
                                   "quote": {"type": ["string", "null"]},
                                   "confidence": {"type": "string",
                                                  "enum": ["high", "medium", "low"]}},
                    "required": ["answer", "confidence"],
                    "additionalProperties": False},
            max_tokens=500)

    md = (f"**Fetched:** {url}"
          + (f" (resolved from '{resolved_from}')" if resolved_from else "")
          + f" · {result.get('content_type')}\n\n")
    if extraction:
        md += (f"**Extracted:** {extraction.get('answer')} "
               f"(confidence {extraction.get('confidence')})\n"
               + (f"> {extraction.get('quote')}\n\n" if extraction.get("quote") else "\n"))
    md += text[:max_chars]
    return envelope(kind="markdown",
                    summary=(extraction.get("answer") or f"fetched {url} "
                             f"({len(text)} chars)") if extraction
                    else f"fetched {url} ({len(text)} chars)",
                    markdown=md,
                    provenance=[prov(url, "fetch_page", url)])


BRIEF_SCHEMA = {
    "type": "object",
    "properties": {"why_good": {"type": "string"},
                   "how_to_target": {"type": "string"},
                   "email_subject": {"type": "string"},
                   "email_body": {"type": "string"}},
    "required": ["why_good", "how_to_target", "email_subject", "email_body"],
    "additionalProperties": False}


@tool_spec(
    name="sales_brief",
    description=(
        "Write a rep-facing sales brief + example cold email for ONE K12 "
        "district from its dossier (facts only — no invention). Vendor "
        "identity comes from the request. Cheap (Haiku, cached)."),
    input_schema={"properties": {
        "leaid": {"type": "string"},
        "vendor_pitch": {"type": "string", "default":
                         "Eisco Scientific — lab glassware, physics/chem/bio "
                         "equipment and science kits for schools"},
    }, "required": ["leaid"]},
    cost_class=CostClass.CHEAP,
)
def sales_brief(ctx, leaid: str, vendor_pitch: str = "Eisco Scientific — lab "
                "glassware, physics/chem/bio equipment and science kits for "
                "schools") -> dict:
    from ..integrations import k12_local
    leaid = k12_local.coerce_leaid(leaid)
    p = k12_local.profile_data(ctx.k12(), ctx.rw(), leaid)
    if p is None:
        return error_envelope(f"unknown leaid {leaid}")
    d = p["district"]
    fin, crdc = p.get("finance") or {}, p.get("crdc") or {}
    dossier = {
        "name": d.get("name"), "state": d.get("state"), "city": d.get("city"),
        "enrollment": d.get("enrollment"),
        "schools": d.get("number_of_schools"),
        "charter": bool(d.get("charter")),
        "sci_sections_per_week": crdc.get("sci_sections"),
        "ap_sci_schools": crdc.get("ap_sci_schools"),
        "finance_latest": {k: fin.get(k) for k in
                           ("year", "rev_title_i", "rev_vocational",
                            "rev_math_sci", "cap_instruc_equip",
                            "exp_tech_equipment", "exp_textbooks")
                           if fin.get(k) is not None},
        "known_contacts": [f"{c.get('full_name')} ({c.get('title')})"
                           for c in (p.get("contacts") or [])[:3]],
    }
    out = cached_call(
        ctx.settings, ctx.rw(), task="sales_brief", prompt_version="2",
        input_obj={"leaid": leaid, "vendor": vendor_pitch, "dossier": dossier},
        prompt=("You write tight sales briefs for field reps selling: "
                f"{vendor_pitch}.\nDistrict facts (use ONLY these; do not "
                f"invent):\n{dossier}\n\nWrite: why_good (2-3 sentences), "
                "how_to_target (2-3 sentences grounded in the size/finance "
                "facts), email_subject, email_body (short, concrete, "
                "references real facts)."),
        schema=BRIEF_SCHEMA, max_tokens=1200)
    md = (f"## Sales brief — {d.get('name')} ({d.get('state')})\n\n"
          f"**Why it's good:** {out['why_good']}\n\n"
          f"**How to target:** {out['how_to_target']}\n\n"
          f"**Cold email:**\n\nSubject: {out['email_subject']}\n\n"
          f"{out['email_body']}")
    from ..artifacts import store
    art = store.create(ctx.rw(), conversation_id=ctx.conversation_id,
                       tool="sales_brief", kind="markdown",
                       title=f"Brief — {d.get('name')}",
                       columns=[{"key": "markdown", "label": "markdown"}],
                       rows=[[md]],
                       provenance=[prov("own k12 estate dossier + claude-haiku-4-5",
                                        f"leaid {leaid}")])
    ctx.emit("artifact", {"artifact_id": art["artifact_id"], "version": 1,
                          "kind": "markdown", "title": art["title"]})
    return envelope(kind="markdown", summary=f"brief for {d.get('name')}",
                    artifact=art, markdown=md,
                    provenance=[prov("own k12 estate + haiku", f"leaid {leaid}")])
