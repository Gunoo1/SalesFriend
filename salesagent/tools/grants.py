"""grants_find — corporate & foundation grant programs that fund science/
STEM equipment purchases, from the vendored ref_corporate_grants seed.

The COMPLEMENT to the government money already in the k12 estate (Title I /
CTE-Perkins / math-sci $ per district): this is the corporate side reps use
as a pitch angle ("fund this order with X grant"). Seed rows carry an as_of
date and confidence — cycles/deadlines shift yearly, so the agent should
verify the program URL (fetch_page) before a rep pitches a deadline."""
from __future__ import annotations

import json
import sqlite3

from .envelope import prov, table_envelope
from .registry import CostClass, tool_spec

GRANT_COLUMNS = [
    ("sponsor", "Sponsor", None),
    ("program", "Program", None),
    ("audience", "Audience", None),
    ("focus", "Focus", None),
    ("award_range", "Award", None),
    ("cycle", "Cycle / deadline", None),
    ("eligibility", "Eligibility", None),
    ("states_txt", "States", None),
    ("rural_priority", "Rural angle", "int"),
    ("confidence", "Confidence", None),
    ("url", "Program page", "link"),
    ("as_of", "As of", None),
]


def query_grants(conn: sqlite3.Connection, *,
                 audience: str | None = None,
                 state: str | None = None,
                 q: str | None = None,
                 rural_priority: bool = False,
                 min_confidence: str | None = None) -> list[dict]:
    """Pure-ish query over ref_corporate_grants (testable with any conn).
    state matching: national programs (states IS NULL) always match; state-
    scoped programs match when the state is in their list."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM ref_corporate_grants ORDER BY rural_priority DESC, "
        "sponsor")]
    conf_rank = {"high": 2, "medium": 1, "low": 0}
    out = []
    for r in rows:
        states = json.loads(r["states_json"]) if r.get("states_json") else None
        if audience and r.get("audience") not in (audience, "both"):
            continue
        if state and states and state.upper() not in states:
            continue
        if rural_priority and not r.get("rural_priority"):
            continue
        if min_confidence and conf_rank.get(
                str(r.get("confidence")), 0) < conf_rank.get(
                min_confidence.lower(), 0):
            continue
        if q:
            blob = " ".join(str(r.get(k) or "") for k in
                            ("sponsor", "program", "focus",
                             "eligibility")).lower()
            if q.lower() not in blob:
                continue
        r["states_txt"] = ", ".join(states) if states else "national"
        out.append(r)
    return out


@tool_spec(
    name="grants_find",
    description=(
        "Corporate & corporate-foundation grant programs that fund science/"
        "STEM equipment and classroom purchases (Toshiba, Bayer rural-"
        "district grants, ACS-Hach chemistry, utility foundations, Voya, "
        "chip makers...). THE pitch angle beside the government money "
        "already in the district data (Title I / CTE / math-sci $): 'fund "
        "this order with X grant'. Filters: audience (k12|higher_ed), "
        "state, rural_priority=true (programs that favor rural schools — "
        "pairs with the rural lead lists), free-text q. Seeded reference — "
        "ALWAYS verify current deadlines via the program URL (fetch_page) "
        "before a rep pitches one. Free, local."),
    input_schema={"properties": {
        "audience": {"type": "string", "enum": ["k12", "higher_ed"],
                     "description": "omit = all"},
        "state": {"type": "string", "pattern": "^[A-Za-z]{2}$",
                  "description": "keeps national programs + programs "
                                 "covering this state"},
        "q": {"type": "string",
              "description": "substring over sponsor/program/focus"},
        "rural_priority": {"type": "boolean", "default": False,
                           "description": "only programs with an explicit "
                                          "rural angle"},
        "min_confidence": {"type": "string", "enum": ["high", "medium"],
                           "description": "drop lower-confidence seed rows"},
    }},
    cost_class=CostClass.FREE,
)
def grants_find(ctx, audience: str | None = None, state: str | None = None,
                q: str | None = None, rural_priority: bool = False,
                min_confidence: str | None = None) -> dict:
    conn = ctx.rw()
    rows = query_grants(conn, audience=audience, state=state, q=q,
                        rural_priority=rural_priority,
                        min_confidence=min_confidence)
    cols = []
    for key, label, fmt in GRANT_COLUMNS:
        c = {"key": key, "label": label}
        if fmt == "int":
            c["type"] = "number"
            c["format"] = "int"
        elif fmt == "link":
            c["type"] = "string"
            c["format"] = "link"
        else:
            c["type"] = "string"
        cols.append(c)
    keys = [c["key"] for c in cols]
    data = [[r.get(k) for k in keys] for r in rows]
    n_rural = sum(1 for r in rows if r.get("rural_priority"))
    title = "Corporate grants — science/STEM"
    if state:
        title += f" ({state.upper()})"
    if rural_priority:
        title += " — rural angle"
    return table_envelope(
        conn, ctx.emit, conversation_id=ctx.conversation_id,
        tool="grants_find", title=title,
        columns=cols, rows=data,
        provenance=[prov(
            "Curated corporate-grants seed (ref_corporate_grants)",
            "hand-curated 2026-08 from program pages; cycles shift yearly — "
            "verify the URL before pitching a deadline")],
        summary=f"{len(rows)} grant programs matched ({n_rural} with an "
                "explicit rural priority). Deadlines are seed data — verify "
                "the program page before a rep pitches one.",
        warnings=["Grant cycles/deadlines change yearly — fetch_page the "
                  "program URL to confirm the current window before "
                  "pitching."],
        styling={"tier_rules": [
            {"column": "rural_priority", "gte": 1, "class": "hot",
             "label": "Rural-priority program"},
            {"column": "confidence", "eq": "low", "class": "std",
             "label": "Low confidence — verify first"}]})
