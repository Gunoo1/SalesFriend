"""Query layer over SalesAgent's OWN k12 estate (data/estate/k12, built by
the k12_build job from fresh public NCES/CRDC downloads) plus the app's own
contact store (contacts_app in app.db, filled by this app's Seamless research
and uploads). No other project's files or databases are read.

leaid is TEXT and zero-padded to 7 chars at every boundary — LLMs pass ints.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

_STATE_RE = re.compile(r"^[A-Z]{2}$")


def coerce_leaid(v: Any) -> str:
    return str(v).strip().zfill(7)


def clean_states(states: list[str] | None) -> list[str]:
    out = []
    for s in states or []:
        s = str(s).strip().upper()
        if _STATE_RE.match(s):
            out.append(s)
    return out


FIND_SORTS = {
    "enrollment": "d.enrollment DESC NULLS LAST",
    "sci_sections": "cr.sci_sections DESC NULLS LAST",
    "title_i": "f.rev_title_i DESC NULLS LAST",
    "cap_equip": "f.cap_instruc_equip DESC NULLS LAST",
    "name": "d.name ASC",
}

FIND_COLUMNS = [
    ("leaid", "District ID", "string"),
    ("name", "District", "string"),
    ("state", "State", "string"),
    ("city", "City", "string"),
    ("county_name", "County", "string"),
    ("enrollment", "Students", "int"),
    ("number_of_schools", "Schools", "int"),
    ("sci_sections", "Sci sections", "number"),
    ("ap_sci_schools", "AP-sci schools", "int"),
    ("rev_title_i", "Title I $", "money"),
    ("rev_vocational", "CTE/Perkins $", "money"),
    ("rev_math_sci", "Math/Sci $", "money"),
    ("cap_instruc_equip", "Capital equip $", "money"),
    ("rev_total", "Revenue $", "money"),
    ("charter", "Charter", "int"),
    ("lat", "", "hidden"),
    ("lng", "", "hidden"),
]


def find_districts(conn: sqlite3.Connection, *,
                   states: list[str] | None = None,
                   q: str | None = None,
                   min_enrollment: int | None = None,
                   max_enrollment: int | None = None,
                   min_sci_sections: float | None = None,
                   requires_ap_science: bool = False,
                   charter: bool | None = None,
                   county: str | None = None,
                   min_rev_title_i: float | None = None,
                   min_rev_vocational: float | None = None,
                   min_cap_instruc_equip: float | None = None,
                   sort: str = "enrollment",
                   limit: int = 100,
                   ) -> tuple[list[dict], list[str]]:
    """Rich district search over the app's own estate. Returns (rows, warnings)."""
    warnings: list[str] = []
    where: list[str] = ["d.agency_type IN (1, 7)"]  # regular + charter agencies
    args: list = []

    states = clean_states(states)
    if states:
        where.append(f"d.state IN ({','.join('?' * len(states))})")
        args += states
    if q:
        where.append("(d.name LIKE ? OR d.city LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if min_enrollment is not None:
        where.append("d.enrollment >= ?"); args.append(int(min_enrollment))
    if max_enrollment is not None:
        where.append("d.enrollment <= ?"); args.append(int(max_enrollment))
    if min_sci_sections is not None:
        where.append("cr.sci_sections >= ?"); args.append(float(min_sci_sections))
    if requires_ap_science:
        where.append("cr.ap_sci_schools > 0")
    if min_sci_sections is not None or requires_ap_science:
        warnings.append("CRDC science data lags ~2 years (latest federal "
                        "collection in this estate build).")
    if charter is not None:
        where.append("d.charter = ?"); args.append(1 if charter else 0)
    if county:
        where.append("d.county_name LIKE ?"); args.append(f"%{county}%")
    if min_rev_title_i is not None:
        where.append("f.rev_title_i >= ?"); args.append(float(min_rev_title_i))
    if min_rev_vocational is not None:
        where.append("f.rev_vocational >= ?"); args.append(float(min_rev_vocational))
    if min_cap_instruc_equip is not None:
        where.append("f.cap_instruc_equip >= ?")
        args.append(float(min_cap_instruc_equip))
    if any(v is not None for v in (min_rev_title_i, min_rev_vocational,
                                   min_cap_instruc_equip)):
        warnings.append("Finance figures are the latest federal F-33 release "
                        "in this estate build (lags ~2-3 years).")

    order = FIND_SORTS.get(sort, FIND_SORTS["enrollment"])
    limit = max(1, min(int(limit or 100), 5000))

    sql = f"""
    SELECT d.leaid, d.name, d.state, d.city, d.county_name, d.enrollment,
           d.number_of_schools,
           cr.sci_sections, cr.ap_sci_schools,
           f.rev_title_i, f.rev_vocational, f.rev_math_sci,
           f.cap_instruc_equip, f.rev_total,
           d.charter, d.latitude AS lat, d.longitude AS lng
    FROM districts d
    LEFT JOIN district_crdc    cr ON cr.leaid = d.leaid
    LEFT JOIN district_finance f  ON f.leaid = d.leaid
    WHERE {' AND '.join(where)}
    ORDER BY {order}, d.leaid
    LIMIT ?"""
    rows = [dict(r) for r in conn.execute(sql, args + [limit])]
    return rows, warnings


# App-owned contacts (contacts_app): ranking semantics kept from the K12Intel
# BEST_CONTACT convention — role purchasing > curriculum > cte > science >
# admin > other, then source seamless > upload > manual, then email-first.
CONTACTS_SQL = """
SELECT * FROM (
  SELECT ct.leaid, ct.company, ct.source, ct.full_name, ct.title, ct.email,
         ct.email_validation, ct.seamless_confidence, ct.phone, ct.role_bucket,
         ct.researched_at,
         ROW_NUMBER() OVER (
           PARTITION BY ct.leaid
           ORDER BY CASE ct.role_bucket
                      WHEN 'purchasing' THEN 0 WHEN 'curriculum' THEN 1
                      WHEN 'cte' THEN 2 WHEN 'science' THEN 3
                      WHEN 'admin' THEN 4 ELSE 5 END,
                    CASE ct.source
                      WHEN 'seamless' THEN 0 WHEN 'upload' THEN 1 ELSE 2 END,
                    CASE WHEN ct.email IS NOT NULL AND ct.email != '' THEN 0 ELSE 1 END,
                    ct.id
         ) AS rn
  FROM contacts_app ct
  WHERE ct.leaid IS NOT NULL AND ct.leaid != '' {extra}
) WHERE 1=1 {rn_clause}
ORDER BY leaid, rn
LIMIT ?"""


def contacts(app_conn: sqlite3.Connection,
             k12_conn: sqlite3.Connection | None, *,
             leaids: list[str] | None = None,
             states: list[str] | None = None,
             role_bucket: str | None = None,
             require_email: bool = False,
             best_only: bool = True,
             limit: int = 500) -> list[dict]:
    """Contacts from the app's OWN research history, enriched with district
    name/state from the estate when it exists. States filter needs the estate
    (contacts_app stores leaid only)."""
    extra: list[str] = []
    args: list = []
    if leaids:
        ids = [coerce_leaid(x) for x in leaids]
        extra.append(f"AND ct.leaid IN ({','.join('?' * len(ids))})")
        args += ids
    states = clean_states(states)
    if states and k12_conn is not None:
        ph = ",".join("?" * len(states))
        in_states = [r["leaid"] for r in k12_conn.execute(
            f"SELECT leaid FROM districts WHERE state IN ({ph})", states)]
        if not in_states:
            return []
        # chunk-safe: contacts_app is small (own research only)
        extra.append(f"AND ct.leaid IN ({','.join('?' * len(in_states))})")
        args += in_states
    if role_bucket:
        extra.append("AND ct.role_bucket = ?"); args.append(role_bucket)
    if require_email:
        extra.append("AND ct.email IS NOT NULL AND ct.email != ''")
    sql = CONTACTS_SQL.format(extra=" ".join(extra),
                              rn_clause="AND rn = 1" if best_only else "")
    limit = max(1, min(int(limit or 500), 5000))
    rows = [dict(r) for r in app_conn.execute(sql, args + [limit])]

    names: dict[str, sqlite3.Row] = {}
    if rows and k12_conn is not None:
        ids = sorted({r["leaid"] for r in rows})
        ph = ",".join("?" * len(ids))
        names = {r["leaid"]: r for r in k12_conn.execute(
            f"SELECT leaid, name, state FROM districts WHERE leaid IN ({ph})",
            ids)}
    for r in rows:
        hit = names.get(r["leaid"])
        r["district"] = hit["name"] if hit else None
        r["state"] = hit["state"] if hit else None
    return rows


def districts_without_contact(app_conn: sqlite3.Connection,
                              leaids: list[str]) -> list[str]:
    ids = [coerce_leaid(x) for x in leaids]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    have = {r["leaid"] for r in app_conn.execute(
        f"SELECT DISTINCT leaid FROM contacts_app WHERE leaid IN ({ph})", ids)}
    return [i for i in ids if i not in have]


def profile_data(k12_conn: sqlite3.Connection, app_conn: sqlite3.Connection,
                 leaid: str) -> dict | None:
    """District dossier from the app's own estate + own contact history.
    Returns None for an unknown leaid."""
    leaid = coerce_leaid(leaid)
    d = k12_conn.execute("SELECT * FROM districts WHERE leaid=?",
                         (leaid,)).fetchone()
    if not d:
        return None
    fin = k12_conn.execute("SELECT * FROM district_finance WHERE leaid=?",
                           (leaid,)).fetchone()
    crdc = k12_conn.execute("SELECT * FROM district_crdc WHERE leaid=?",
                            (leaid,)).fetchone()
    cts = contacts(app_conn, k12_conn, leaids=[leaid], best_only=False,
                   limit=25)
    return {"district": dict(d),
            "finance": dict(fin) if fin else None,
            "crdc": dict(crdc) if crdc else None,
            "contacts": cts}
