"""Reference tools over seeded tables: state science-adoption calendar and
the science-kit-maker roster (timMtesting t05/t06b/t09 findings)."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .envelope import prov, table_envelope
from .registry import CostClass, tool_spec

_STATES_PARAM = {"type": "array", "items": {"type": "string",
                                            "pattern": "^[A-Za-z]{2}$"}}


def _months_out(next_adoption: str | None, window: str | None) -> int | None:
    """Crude horizon: first 4-digit year found in the window (preferred) or
    the next-adoption text -> months from now. None when unparseable."""
    for text in (window, next_adoption):
        if not text:
            continue
        m = re.search(r"(20\d{2})", str(text))
        if m:
            year = int(m.group(1))
            now = datetime.now(timezone.utc)
            return max(0, (year - now.year) * 12 - (now.month - 1))
    return None


@tool_spec(
    name="adoption_calendar",
    description=(
        "When each state adopts SCIENCE instructional materials statewide and "
        "when publishers can submit proposals. Only ~20 states run statewide "
        "adoptions — the other ~30 (NJ NY PA OH MI, New England...) choose "
        "district-by-district, so NO state schedule exists there. Seeded "
        "findings incl.: FL proposals Jan 2027-Jul 2028 (2028-29 adoption), "
        "OK 2026, IN 2026-27, WV 2029, MS bids Jul 2031, ID 2031, TX picks "
        "subjects annually (watch IMRA). Free, local."),
    input_schema={"properties": {
        "states": _STATES_PARAM,
        "active_within_months": {"type": "integer",
                                 "description": "only windows opening within N months"},
        "adoption_states_only": {"type": "boolean", "default": True},
    }},
    cost_class=CostClass.FREE,
)
def adoption_calendar(ctx, states: list[str] | None = None,
                      active_within_months: int | None = None,
                      adoption_states_only: bool = True) -> dict:
    conn = ctx.rw()
    q = "SELECT * FROM ref_adoption_calendar WHERE 1=1"
    args: list = []
    if states:
        sts = [s.upper() for s in states]
        q += f" AND state IN ({','.join('?' * len(sts))})"
        args += sts
    if adoption_states_only:
        q += " AND is_adoption_state = 1"
    recs = [dict(r) for r in conn.execute(q + " ORDER BY state", args)]

    rows = []
    for r in recs:
        months = _months_out(r.get("science_next_adoption"),
                             r.get("proposal_window"))
        urgency = ("ACT NOW" if months is not None and months <= 18 else
                   "NEXT UP" if months is not None and months <= 42 else
                   "LONG-RANGE" if months is not None else "NEEDS RESEARCH")
        if r["state"] == "TX":
            urgency = "WATCH ANNUALLY"
            months = 6 if months is None else min(months, 6)
        if active_within_months is not None and \
                (months is None or months > active_within_months):
            continue
        rows.append([r["state"], urgency, months,
                     r.get("science_next_adoption"),
                     r.get("proposal_window"),
                     r.get("science_last_adopted"),
                     r.get("notes"), r.get("confidence"),
                     r.get("source_url")])
    rows.sort(key=lambda x: (x[2] is None, x[2] if x[2] is not None else 999))

    cols = [{"key": "state", "label": "State", "type": "string"},
            {"key": "urgency", "label": "Urgency", "type": "string"},
            {"key": "months_out", "label": "Months out", "type": "number", "format": "int"},
            {"key": "next_adoption", "label": "Next science adoption", "type": "string"},
            {"key": "proposal_window", "label": "Publisher proposal window", "type": "string"},
            {"key": "last_adopted", "label": "Last adopted", "type": "string"},
            {"key": "notes", "label": "MS/HS notes", "type": "string"},
            {"key": "confidence", "label": "Confidence", "type": "string"},
            {"key": "source", "label": "Source", "type": "string", "format": "link"}]
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="adoption_calendar", title="State science adoption calendar",
        columns=cols, rows=rows,
        provenance=[prov("State DOE adoption schedules",
                         "timMtesting t07-t09 pipeline (DDG resolver + "
                         "pdfplumber + Haiku strict-JSON), pulled 2026-08-03")],
        summary=f"{len(rows)} adoption states"
                + (f" with windows within {active_within_months} months"
                   if active_within_months else "")
                + ". ~30 other states adopt district-by-district (no state schedule).",
        styling={"tier_rules": [{"column": "months_out", "lte": 18, "class": "now"},
                                {"column": "months_out", "lte": 42, "class": "warm"}]},
        warnings=["Non-adoption states have no statewide schedule at all — "
                  "district-by-district there."])


@tool_spec(
    name="kit_maker_roster",
    description=(
        "Companies that ASSEMBLE science kits (prime targets to buy bulk "
        "glassware/consumables): the 14 OpenSciEd certified suppliers + "
        "federal-award kit vendors (School Specialty $7.7M FOSS-to-DoDEA "
        "etc.). Free, local seed data."),
    input_schema={"properties": {
        "segment": {"type": "string",
                    "enum": ["all", "openscied", "federal_awards"],
                    "default": "all"},
    }},
    cost_class=CostClass.FREE,
)
def kit_maker_roster(ctx, segment: str = "all") -> dict:
    conn = ctx.rw()
    q = "SELECT * FROM ref_kit_makers"
    args: list = []
    if segment and segment != "all":
        q += " WHERE segment = ?"
        args.append(segment)
    recs = [dict(r) for r in conn.execute(q + " ORDER BY segment, name", args)]
    cols = [{"key": "name", "label": "Company", "type": "string"},
            {"key": "segment", "label": "Segment", "type": "string"},
            {"key": "detail", "label": "What they do", "type": "string"},
            {"key": "gov_total", "label": "Federal awards $", "type": "number",
             "format": "money"},
            {"key": "agencies", "label": "Agencies", "type": "string"},
            {"key": "domains", "label": "Domains", "type": "string"},
            {"key": "evidence_url", "label": "Evidence", "type": "string",
             "format": "link"}]
    rows = [[r.get("name"), r.get("segment"), r.get("detail"),
             r.get("gov_total"), r.get("agencies"), r.get("domains"),
             r.get("evidence_url")] for r in recs]
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="kit_maker_roster", title="Science kit makers",
        columns=cols, rows=rows,
        provenance=[prov("OpenSciEd certified-supplier pages + USAspending "
                         "keyword search", "timMtesting t05/t06b, 2026-08-03",
                         "https://www.openscied.org/purchase/")],
        summary=f"{len(rows)} kit companies ({segment}).",
        warnings=["Next tier (Lab-Aids, Flinn, Delta/FOSS, Bio-Rad Explorer, "
                  "Edvotek) not yet rostered — usaspending_keyword_vendors "
                  "can extend this live."])
