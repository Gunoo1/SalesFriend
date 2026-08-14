"""NCES Private School Universe Survey (PSS) — the census of US private
schools (~22k schools, biennial). The private-school answer the public-school
CCD estate can't give: name, address, PHONE, enrollment, teacher count,
religious orientation (Catholic / other religious / nonsectarian + 9-way
typology), grade span, and the same NCES locale code the district estate
uses (41-43 = rural).

Newest CSV is resolved live from the PSS data page (biennial releases named
pss{yy}{yy}_pu_csv.zip; 2021-22 is current as of 2026-08). Column semantics
verified empirically on pss2122 (2026-08-14): first ~80 columns are replicate
weights; grade codes 1=ungraded 2=PK 3=K 4=TK 5=T1 then 6..17 = grades 1..12
(confirmed via LEVEL crosstab: elementary tops at 13=8th, secondary/combined
at 17=12th).
"""
from __future__ import annotations

import re
import sqlite3

DATA_PAGE = "https://nces.ed.gov/surveys/pss/pssdata.asp"
ZIP_URL = "https://nces.ed.gov/surveys/pss/zip/{name}"
DATASET_TITLE = "NCES Private School Universe Survey (PSS)"

LEVEL_LABELS = {1: "Elementary", 2: "Secondary", 3: "Combined"}
RELIG_LABELS = {1: "Catholic", 2: "Other religious", 3: "Nonsectarian"}
TYPOLOGY_LABELS = {
    1: "Catholic — parochial", 2: "Catholic — diocesan",
    3: "Catholic — private order", 4: "Conservative Christian",
    5: "Religious — affiliated", 6: "Religious — unaffiliated",
    7: "Nonsectarian — regular", 8: "Nonsectarian — special emphasis",
    9: "Nonsectarian — special education",
}
SIZE_LABELS = {1: "<50", 2: "50-149", 3: "150-299", 4: "300-499",
               5: "500-749", 6: "750+"}
GRADE_LABELS = {1: "Ungraded", 2: "PK", 3: "K", 4: "TK", 5: "T1",
                **{c: str(c - 5) for c in range(6, 18)}}


def latest_csv(session) -> dict:
    """Resolve the newest pss{yyyy}_pu_csv.zip from the PSS data page.
    Falls back to the known 2021-22 file if the page is unreadable."""
    fallback = {"label": "PSS 2021-22", "school_year": "2021-22",
                "url": ZIP_URL.format(name="pss2122_pu_csv.zip")}
    try:
        r = session.get(DATA_PAGE, timeout=60,
                        headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception:
        return fallback
    names = set(re.findall(r"zip/(pss(\d{4})_pu_csv\.zip)", r.text, re.I))
    if not names:
        return fallback
    name, yy = max(names, key=lambda t: t[1])
    y1, y2 = yy[:2], yy[2:]
    return {"label": f"PSS 20{y1}-{y2}", "school_year": f"20{y1}-{y2}",
            "url": ZIP_URL.format(name=name)}


def _i(v) -> int | None:
    try:
        n = int(str(v).strip())
        return None if n < 0 else n
    except (TypeError, ValueError):
        return None


def _f(v) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _phone(v) -> str | None:
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    return digits[:10] if len(digits) >= 10 else None


def _col(r: dict, prefix: str) -> str | None:
    """PSS suffixes locale/lat/lng columns with the survey year (ULOCALE22,
    LATITUDE22...) — match by prefix so newer releases keep working."""
    for k in r:
        if k.upper().startswith(prefix):
            return r[k]
    return None


def row_to_school(r: dict, year: str) -> tuple | None:
    ppin = (r.get("PPIN") or "").strip()
    name = (r.get("PINST") or "").strip()
    if not ppin or not name:
        return None
    zip5 = (r.get("PZIP") or "").strip()[:5] or None
    lo, hi = _i(_col(r, "LOGR20")), _i(_col(r, "HIGR20"))
    return (
        ppin, name,
        (r.get("PADDRS") or "").strip() or None,
        (r.get("PCITY") or "").strip() or None,
        (r.get("PSTABB") or "").strip().upper() or None,
        zip5,
        (r.get("PCNTNM") or "").strip() or None,
        _phone(r.get("PPHONE")),
        _i(r.get("LEVEL")),
        GRADE_LABELS.get(lo, None),
        GRADE_LABELS.get(hi, None),
        _i(r.get("RELIG")),
        _i(r.get("TYPOLOGY")),
        _i(r.get("NUMSTUDS")),
        _f(r.get("NUMTEACH")),
        _i(r.get("SIZE")),
        _i(_col(r, "ULOCALE")),
        _f(_col(r, "LATITUDE")),
        _f(_col(r, "LONGITUDE")),
        year,
    )


INSERT_SQL = ("INSERT OR REPLACE INTO private_schools (ppin, name, street,"
              " city, state, zip, county, phone, level, lo_grade, hi_grade,"
              " religious, typology, enrollment, teachers, size_class,"
              " locale, latitude, longitude, year)"
              " VALUES (" + ",".join("?" * 20) + ")")


def resolve_religious(vals) -> list[int]:
    names = {"catholic": 1, "other religious": 2, "religious": 2,
             "christian": 2, "nonsectarian": 3, "secular": 3,
             "non-religious": 3}
    out = []
    for v in vals or []:
        if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
            out.append(int(v))
            continue
        hit = names.get(str(v).strip().lower())
        if hit is None:
            raise ValueError(f"unknown religious filter {v!r}; use: "
                             "catholic, other religious, nonsectarian")
        out.append(hit)
    return out


FIND_COLUMNS = [
    ("name", "School", None),
    ("level_label", "Level", None),
    ("grades", "Grades", None),
    ("enrollment", "Students", "int"),
    ("teachers", "Teachers", "int"),
    ("typology_label", "Type", None),
    ("locale_label", "Area", None),
    ("city", "City", None), ("state", "St", None), ("zip", "Zip", None),
    ("county", "County", None),
    ("phone", "Phone", None),
    ("lat", "", "hidden"), ("lng", "", "hidden"),
]

_SORTS = {"enrollment": "enrollment DESC", "name": "name ASC",
          "state": "state ASC, name ASC"}


def query_private_schools(conn: sqlite3.Connection, *,
                          states: list[str] | None = None,
                          q: str | None = None,
                          min_enrollment: int | None = None,
                          max_enrollment: int | None = None,
                          levels: list[int] | None = None,
                          religious: list | None = None,
                          rural_only: bool = False,
                          locale_groups: list[str] | None = None,
                          require_phone: bool = True,
                          sort: str = "enrollment",
                          limit: int = 500) -> tuple[list[dict], list[str]]:
    """Filterable read over the private-schools estate. Returns
    (rows, warnings)."""
    from .rural import locale_label, locale_where
    where, args = ["1=1"], []
    if require_phone:
        where.append("phone IS NOT NULL")
    if states:
        ss = [s.upper() for s in states]
        where.append("state IN (%s)" % ",".join("?" * len(ss)))
        args += ss
    if q:
        where.append("(name LIKE ? OR city LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if min_enrollment is not None:
        where.append("enrollment >= ?"); args.append(int(min_enrollment))
    if max_enrollment is not None:
        where.append("enrollment <= ?"); args.append(int(max_enrollment))
    if levels:
        lv = [int(x) for x in levels]
        where.append("level IN (%s)" % ",".join("?" * len(lv)))
        args += lv
    if religious:
        rr = resolve_religious(religious)
        where.append("religious IN (%s)" % ",".join("?" * len(rr)))
        args += rr
    frag, fa = locale_where("locale", rural_only=rural_only,
                            locale_groups=locale_groups)
    if frag:
        where.append(frag)
        args += fa
    order = _SORTS.get(sort, _SORTS["enrollment"])
    limit = max(1, min(int(limit or 500), 5000))
    sql = ("SELECT * FROM private_schools WHERE " + " AND ".join(where)
           + f" ORDER BY {order}, ppin LIMIT ?")
    out = []
    for r in conn.execute(sql, args + [limit]):
        d = dict(r)
        d["level_label"] = LEVEL_LABELS.get(d["level"], None)
        d["typology_label"] = TYPOLOGY_LABELS.get(d["typology"], None)
        d["locale_label"] = locale_label(d["locale"])
        d["grades"] = (f"{d.get('lo_grade')}-{d.get('hi_grade')}"
                       if d.get("lo_grade") and d.get("hi_grade") else None)
        if d["phone"]:
            p = d["phone"]
            d["phone"] = f"({p[:3]}) {p[3:6]}-{p[6:]}"
        d["lat"], d["lng"] = d.get("latitude"), d.get("longitude")
        out.append(d)
    warnings = ["PSS is biennial — this snapshot is the newest federal "
                "release; very new schools may be missing"]
    return out, warnings
