"""IPEDS institutional directory (NCES) — the census of US higher education.

Every Title IV degree/certificate-granting institution files IPEDS by law, so
the HD (header/directory) file is the college analog of the CLIA registry:
~6,000 institutions with address, PHONE, website, chief administrator
(president/chancellor by NAME), control, level, size class, Carnegie class,
and the NCES urban-centric locale code (41-43 = rural — the "middle of
nowhere" campuses distributor reps rarely visit).

One ~1MB zip: https://nces.ed.gov/ipeds/datacenter/data/HD{year}.zip
(annual release, provisional in fall for the prior year). Column semantics
verified empirically on HD2024 (2026-08-14): CYACTIVE '1'=active (5,994 of
6,072), GENTELE sometimes carries an extension beyond 10 digits, ZIP may be
ZIP+4, LOCALE/INSTSIZE use -1/-2/-3 for not-available.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

HD_URL = "https://nces.ed.gov/ipeds/datacenter/data/HD{year}.zip"
DATASET_TITLE = "IPEDS Institutional Characteristics — Directory (HD)"

LEVEL_LABELS = {1: "4-year+", 2: "2-year", 3: "<2-year"}
CONTROL_LABELS = {1: "Public", 2: "Private nonprofit", 3: "Private for-profit"}
SIZE_LABELS = {1: "<1,000", 2: "1,000-4,999", 3: "5,000-9,999",
               4: "10,000-19,999", 5: "20,000+"}
# C21BASIC research-activity classes worth calling out to a rep
CARNEGIE_NOTES = {15: "R1 research", 16: "R2 research"}


def latest_hd(session, *, start_year: int) -> dict:
    """Newest available HD{year}.zip by probing downward from start_year.
    IPEDS posts the new file each fall; 4 years of slack is plenty."""
    for year in range(start_year, start_year - 5, -1):
        url = HD_URL.format(year=year)
        try:
            r = session.head(url, timeout=30, allow_redirects=True)
            if r.status_code == 200:
                return {"year": year, "url": url,
                        "label": f"IPEDS HD{year} directory"}
        except Exception:
            continue
    raise RuntimeError(
        f"no IPEDS HD file found probing {start_year}..{start_year - 4} — "
        "nces.ed.gov may be unreachable")


def _i(v) -> int | None:
    try:
        n = int(str(v).strip())
        return None if n < 0 else n     # -1/-2/-3 = not available
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


def _yes(v) -> int:
    return 1 if str(v).strip() == "1" else 0


def row_to_college(r: dict) -> tuple | None:
    """CSV DictReader row -> colleges INSERT tuple. None = unusable row.
    The first header carries a UTF-8 BOM which latin-1 decoding turns into
    a 3-char prefix — match UNITID by suffix, not exact key."""
    unitid = None
    for k, v in r.items():
        if k and k.upper().endswith("UNITID"):
            unitid = _i(v)
            break
    if unitid is None:
        return None
    name = (r.get("INSTNM") or "").strip()
    if not name:
        return None
    zip5 = (r.get("ZIP") or "").strip().split("-")[0][:5] or None
    web = (r.get("WEBADDR") or "").strip() or None
    if web and not web.lower().startswith("http"):
        web = "https://" + web
    return (
        unitid, name,
        (r.get("IALIAS") or "").strip() or None,
        (r.get("ADDR") or "").strip() or None,
        (r.get("CITY") or "").strip() or None,
        (r.get("STABBR") or "").strip().upper() or None,
        zip5,
        (r.get("COUNTYNM") or "").strip() or None,
        _phone(r.get("GENTELE")),
        web,
        (r.get("CHFNM") or "").strip() or None,
        (r.get("CHFTITLE") or "").strip() or None,
        _i(r.get("SECTOR")),
        _i(r.get("ICLEVEL")),
        _i(r.get("CONTROL")),
        _i(r.get("INSTSIZE")),
        _i(r.get("LOCALE")),
        _yes(r.get("HBCU")),
        _yes(r.get("TRIBAL")),
        _yes(r.get("HOSPITAL")),
        _yes(r.get("MEDICAL")),
        _i(r.get("C21BASIC")),
        1 if str(r.get("CYACTIVE", "")).strip() == "1" else 0,
        _f(r.get("LATITUDE")),
        _f(r.get("LONGITUD")),
    )


INSERT_SQL = ("INSERT OR REPLACE INTO colleges (unitid, name, alias, street,"
              " city, state, zip, county, phone, website, chief_name,"
              " chief_title, sector, level, control, size_class, locale,"
              " hbcu, tribal, hospital, medical, carnegie, active,"
              " latitude, longitude) VALUES (" + ",".join("?" * 25) + ")")


def resolve_controls(vals: Iterable) -> list[int]:
    out = []
    names = {"public": 1, "private": 2, "private nonprofit": 2,
             "nonprofit": 2, "for-profit": 3, "private for-profit": 3,
             "proprietary": 3}
    for v in vals or []:
        if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
            out.append(int(v))
            continue
        hit = names.get(str(v).strip().lower())
        if hit is None:
            raise ValueError(f"unknown control {v!r}; use: public, "
                             "private nonprofit, private for-profit")
        out.append(hit)
    return out


FIND_COLUMNS = [
    ("name", "Institution", None),
    ("level_label", "Level", None),
    ("control_label", "Control", None),
    ("size_label", "Students", None),
    ("locale_label", "Area", None),
    ("city", "City", None), ("state", "St", None), ("zip", "Zip", None),
    ("county", "County", None),
    ("phone", "Phone", None),
    ("website", "Website", "link"),
    ("chief_name", "Chief admin", None),
    ("chief_title", "Title", None),
    ("research", "Research", None),
    ("lat", "", "hidden"), ("lng", "", "hidden"),
]

_SORTS = {"size": "size_class DESC, name ASC", "name": "name ASC",
          "state": "state ASC, name ASC"}


def query_colleges(conn: sqlite3.Connection, *,
                   states: list[str] | None = None,
                   q: str | None = None,
                   levels: list[int] | None = None,
                   controls: list | None = None,
                   rural_only: bool = False,
                   locale_groups: list[str] | None = None,
                   min_size_class: int | None = None,
                   max_size_class: int | None = None,
                   hbcu: bool | None = None,
                   with_hospital: bool | None = None,
                   active_only: bool = True,
                   require_phone: bool = False,
                   sort: str = "size",
                   limit: int = 500) -> tuple[list[dict], list[str]]:
    """Filterable read over the colleges estate. Returns (rows, warnings)."""
    from .rural import locale_label, locale_where
    where, args = ["sector != 0"], []       # 0 = administrative units
    if active_only:
        where.append("active = 1")
    if require_phone:
        where.append("phone IS NOT NULL")
    if states:
        ss = [s.upper() for s in states]
        where.append("state IN (%s)" % ",".join("?" * len(ss)))
        args += ss
    if q:
        where.append("(name LIKE ? OR city LIKE ? OR alias LIKE ?)")
        args += [f"%{q}%"] * 3
    if levels:
        lv = [int(x) for x in levels]
        where.append("level IN (%s)" % ",".join("?" * len(lv)))
        args += lv
    if controls:
        cc = resolve_controls(controls)
        where.append("control IN (%s)" % ",".join("?" * len(cc)))
        args += cc
    frag, fa = locale_where("locale", rural_only=rural_only,
                            locale_groups=locale_groups)
    if frag:
        where.append(frag)
        args += fa
    if min_size_class is not None:
        where.append("size_class >= ?"); args.append(int(min_size_class))
    if max_size_class is not None:
        where.append("size_class <= ?"); args.append(int(max_size_class))
    if hbcu is not None:
        where.append("hbcu = ?"); args.append(1 if hbcu else 0)
    if with_hospital is not None:
        where.append("hospital = ?"); args.append(1 if with_hospital else 0)
    order = _SORTS.get(sort, _SORTS["size"])
    limit = max(1, min(int(limit or 500), 5000))
    sql = ("SELECT * FROM colleges WHERE " + " AND ".join(where)
           + f" ORDER BY {order} LIMIT ?")
    out = []
    for r in conn.execute(sql, args + [limit]):
        d = dict(r)
        d["level_label"] = LEVEL_LABELS.get(d["level"], None)
        d["control_label"] = CONTROL_LABELS.get(d["control"], None)
        d["size_label"] = SIZE_LABELS.get(d["size_class"], None)
        d["locale_label"] = locale_label(d["locale"])
        d["research"] = CARNEGIE_NOTES.get(d["carnegie"], None)
        if d["phone"]:
            p = d["phone"]
            d["phone"] = f"({p[:3]}) {p[3:6]}-{p[6:]}"
        d["lat"], d["lng"] = d.get("latitude"), d.get("longitude")
        out.append(d)
    warnings = []
    if rural_only or (locale_groups and "rural" in [g.lower() for g in
                                                    locale_groups]):
        warnings.append("locale is the NCES urban-centric code of the campus "
                        "address — a rural-coded campus can still be a large "
                        "university (see the Students column)")
    return out, warnings
