"""CMS CLIA registry (Provider of Services file, Clinical Laboratories).

The census of US labs: every clinical lab must hold a CLIA certificate, and
CMS publishes the registry quarterly as a public CSV (~680k rows incl.
history; ~300k active) with name, address, PHONE, facility type, certificate
class, ownership, and annual test volumes. This module owns:

  - resolving the newest quarterly CSV URL from the data.cms.gov catalog
  - the code maps (facility type / certificate / control), validated
    empirically against the Q2 2026 file on 2026-08-14 (fac 14=hospital,
    15=independent, 20=pharmacy, 21=physician office all confirmed by
    sample facility names; control labels are best-effort — raw codes are
    stored alongside)
  - the chain/franchise name filter ("small independent" is Eisco's ask,
    not a CLIA field — this is a curated heuristic, said so in the tool)
  - row transform (CSV dict -> labs table tuple) and the query layer for
    labs_find, both pure enough for offline tests
"""
from __future__ import annotations

import re
import sqlite3
from typing import Iterable

import requests

CATALOG_URL = "https://data.cms.gov/data.json"
DATASET_TITLE = "Provider of Services File - Clinical Laboratories"

FAC_TYPES = {
    1: "ambulance", 2: "ambulatory surgical center", 3: "ancillary test site",
    4: "assisted living", 5: "blood bank", 6: "community clinic",
    7: "outpatient rehab", 8: "dialysis (ESRD)", 9: "FQHC clinic",
    10: "health fair", 11: "HMO", 12: "home health agency", 13: "hospice",
    14: "hospital", 15: "independent lab", 16: "industrial (in-house lab)",
    17: "insurance", 18: "intermediate care facility", 19: "mobile lab",
    20: "pharmacy", 21: "physician office", 22: "other practitioner",
    23: "prison", 24: "public health lab", 25: "rural health clinic",
    26: "school/student health", 27: "skilled nursing facility",
    28: "tissue bank", 29: "other",
}
FAC_NAME_TO_CODE = {v: k for k, v in FAC_TYPES.items()}

CERT_TYPES = {1: "compliance", 2: "waiver", 3: "accreditation", 4: "PPM",
              9: "registration"}
CERT_NAME_TO_CODE = {v.lower(): k for k, v in CERT_TYPES.items()}

# GNRL_CNTL_TYPE_CD — best-effort labels (standard POS control codes); the
# raw code is stored too so nothing depends on these strings.
CONTROL_TYPES = {
    1: "nonprofit (church)", 2: "nonprofit (private)", 3: "nonprofit (other)",
    4: "for-profit", 5: "government (federal)", 6: "government (state)",
    7: "government (local)", 8: "government (other)", 9: "physician-owned",
    10: "other",
}

# National chains / franchises / plasma networks — the "not small independent"
# name screen. Curated heuristic, matched on normalized name substrings.
CHAIN_PATTERNS = [
    "LABCORP", "LABORATORY CORPORATION", "QUEST DIAGNOSTIC", "EXAMONE",
    "EXAM ONE", "BIOREFERENCE", "SONIC HEALTHCARE", "SONIC REFERENCE",
    "CLINICAL PATHOLOGY LABORATORIES", "AEGIS SCIENCES", "MILLENNIUM HEALTH",
    "ARUP LABORATORIES", "MAYO CLINIC", "PATHGROUP", "AMERIPATH",
    "DERMPATH", "LABONE",
    "BIOLIFE", "CSL PLASMA", "GRIFOLS", "TALECRIS", "OCTAPHARMA",
    "KEDPLASMA", "IMMUNOTEK", "AMERICAN RED CROSS", "VITALANT", "LIFESOUTH",
    "VERSITI", "NEW YORK BLOOD CENTER",
    "ARCPOINT", "ANY LAB TEST", "FASTEST LABS",
    "DAVITA", "FRESENIUS", "KAISER", "CONCENTRA",
    "CVS ", "WALGREEN", "RITE AID", "KROGER", "WALMART", "WAL-MART",
    "PUBLIX", "COSTCO", "SAFEWAY", "ALBERTSON",
]
_CHAIN_RE = re.compile("|".join(re.escape(p.strip()) for p in CHAIN_PATTERNS))


def is_chain(name: str) -> bool:
    return bool(_CHAIN_RE.search(f" {(name or '').upper()} "))


def latest_csv(session: requests.Session | None = None) -> dict:
    """Newest quarterly CSV of the CLIA POS dataset from the CMS catalog.
    Returns {"url", "label"}; distributions are listed newest-first and the
    file name carries the quarter (e.g. Clia_DATA.Q2_2026.csv)."""
    sess = session or requests
    cat = sess.get(CATALOG_URL, timeout=120).json()
    for ds in cat.get("dataset", []):
        if ds.get("title") == DATASET_TITLE:
            for dist in ds.get("distribution") or []:
                url = dist.get("downloadURL") or ""
                if (dist.get("mediaType") == "text/csv"
                        or url.lower().endswith(".csv")):
                    label = url.rsplit("/", 1)[-1]
                    return {"url": url, "label": label}
    raise RuntimeError(f"CMS catalog has no CSV distribution for "
                       f"'{DATASET_TITLE}' — layout may have changed")


_ACCRED = [("A2LA", "A2LA_ACRDTD_Y_MATCH_SW"), ("AABB", "AABB_ACRDTD_Y_MATCH_SW"),
           ("AOA", "AOA_ACRDTD_Y_MATCH_SW"), ("ASHI", "ASHI_ACRDTD_Y_MATCH_SW"),
           ("CAP", "CAP_ACRDTD_Y_MATCH_SW"), ("COLA", "COLA_ACRDTD_Y_MATCH_SW"),
           ("JCAHO", "JCAHO_ACRDTD_Y_MATCH_SW")]

_VOL_FIELDS = ["FORM_116_ACRDTD_TEST_VOL_CNT", "FORM_116_TEST_VOL_CNT",
               "FORM_1557_TEST_VOL_CNT", "PPMP_TEST_VOL_CNT",
               "WVD_TEST_VOL_CNT"]


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _iso(d: str) -> str | None:
    d = (d or "").strip()
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else None


def _intn(s) -> int | None:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def row_to_lab(r: dict) -> tuple | None:
    """CSV DictReader row -> labs-table tuple (None = unusable row)."""
    clia = (r.get("PRVDR_NUM") or "").strip()
    name = (r.get("FAC_NAME") or "").strip()
    if not clia or not name:
        return None
    vol = sum(_intn(r.get(f)) or 0 for f in _VOL_FIELDS)
    accr = ",".join(tag for tag, col in _ACCRED
                    if (r.get(col) or "").strip().upper() == "Y")
    phone = _digits(r.get("PHNE_NUM"))
    return (
        clia, name, (r.get("ADDTNL_FAC_NAME") or "").strip() or None,
        (r.get("ST_ADR") or "").strip() or None,
        (r.get("ADDTNL_ST_ADR") or "").strip() or None,
        (r.get("CITY_NAME") or "").strip() or None,
        (r.get("STATE_CD") or "").strip().upper() or None,
        (r.get("ZIP_CD") or "").strip()[:5] or None,
        (r.get("FIPS_CNTY_CD") or "").strip() or None,
        phone if len(phone) == 10 else None,
        _digits(r.get("FAX_PHNE_NUM")) or None,
        _intn(r.get("GNRL_FAC_TYPE_CD")),
        _intn(r.get("CRTFCT_TYPE_CD")),
        _intn(r.get("GNRL_CNTL_TYPE_CD")),
        1 if (r.get("PGM_TRMNTN_CD") or "").strip() == "00" else 0,
        (r.get("PGM_TRMNTN_CD") or "").strip() or None,
        _iso(r.get("TRMNTN_EXPRTN_DT")),
        _iso(r.get("ORGNL_PRTCPTN_DT")),
        accr or None, vol,
        _intn(r.get("DRCTLY_AFLTD_LAB_CNT")) or 0,
        (r.get("CBSA_URBN_RRL_IND") or "").strip() or None,
    )


INSERT_SQL = ("INSERT OR REPLACE INTO labs (clia_num, name, addl_name, street,"
              " street2, city, state, zip, county_fips, phone, fax, fac_type,"
              " cert_type, control_type, active, term_code, cert_expire,"
              " first_certified, accreditors, test_volume, affiliated_labs,"
              " urban_rural) VALUES (" + ",".join("?" * 22) + ")")


def resolve_fac_types(vals: Iterable) -> list[int]:
    """Accept codes or friendly names ('independent lab', 'hospital')."""
    out = []
    for v in vals or []:
        if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
            out.append(int(v))
            continue
        key = str(v).strip().lower()
        hit = next((c for c, n in FAC_TYPES.items()
                    if key == n or key in n), None)
        if hit is None:
            raise ValueError(
                f"unknown facility type {v!r}; use codes or names from: "
                + ", ".join(f"{c}={n}" for c, n in sorted(FAC_TYPES.items())))
        out.append(hit)
    return out


def resolve_cert_types(vals: Iterable) -> list[int]:
    out = []
    for v in vals or []:
        if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
            out.append(int(v))
            continue
        hit = CERT_NAME_TO_CODE.get(str(v).strip().lower())
        if hit is None:
            raise ValueError(f"unknown certificate type {v!r}; use: "
                             + ", ".join(CERT_TYPES.values()))
        out.append(hit)
    return out


FIND_COLUMNS = [
    ("name", "Lab", None), ("fac_type_label", "Facility type", None),
    ("cert_type_label", "Certificate", None),
    ("control_label", "Ownership", None),
    ("city", "City", None), ("state", "St", None), ("zip", "Zip", None),
    ("area", "Area", None),
    ("phone", "Phone", None), ("test_volume", "Tests/yr", "int"),
    ("accreditors", "Accredited", None),
    ("first_certified", "Since", None),
    ("affiliated_labs", "Affil. labs", "int"),
    ("clia_num", "CLIA #", None),
]

_SORTS = {"name": "name ASC", "state": "state ASC, name ASC",
          "test_volume": "test_volume DESC",
          "oldest": "first_certified ASC"}


def query_labs(conn: sqlite3.Connection, *, states: list[str] | None = None,
               fac_types: list | None = None, cert_types: list | None = None,
               q: str | None = None, require_phone: bool = True,
               exclude_chains: bool = True, active_only: bool = True,
               min_test_volume: int | None = None,
               max_test_volume: int | None = None,
               max_affiliated_labs: int | None = None,
               rural_only: bool = False,
               sort: str = "test_volume",
               limit: int = 500) -> tuple[list[dict], list[str]]:
    """Filterable read over the labs estate. Returns (rows, warnings)."""
    fac_codes = resolve_fac_types(fac_types) if fac_types else [15]
    cert_codes = resolve_cert_types(cert_types) if cert_types else []
    where, args = [], []
    where.append("fac_type IN (%s)" % ",".join("?" * len(fac_codes)))
    args += fac_codes
    if active_only:
        where.append("active = 1")
    if require_phone:
        where.append("phone IS NOT NULL")
    if rural_only:
        where.append("urban_rural = 'R'")
    if states:
        ss = [s.upper() for s in states]
        where.append("state IN (%s)" % ",".join("?" * len(ss)))
        args += ss
    if cert_codes:
        where.append("cert_type IN (%s)" % ",".join("?" * len(cert_codes)))
        args += cert_codes
    if q:
        where.append("(name LIKE ? OR city LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if min_test_volume is not None:
        where.append("test_volume >= ?")
        args.append(min_test_volume)
    if max_test_volume is not None:
        where.append("test_volume <= ?")
        args.append(max_test_volume)
    if max_affiliated_labs is not None:
        where.append("affiliated_labs <= ?")
        args.append(max_affiliated_labs)
    order = _SORTS.get(sort, _SORTS["test_volume"])
    limit = max(1, min(int(limit or 500), 5000))
    sql = ("SELECT * FROM labs WHERE " + " AND ".join(where)
           + f" ORDER BY {order}")
    out, n_chain = [], 0
    for r in conn.execute(sql, args):
        d = dict(r)
        if exclude_chains and is_chain(d["name"]):
            n_chain += 1
            continue
        d["fac_type_label"] = FAC_TYPES.get(d["fac_type"], str(d["fac_type"]))
        d["cert_type_label"] = CERT_TYPES.get(d["cert_type"],
                                              str(d["cert_type"]))
        d["control_label"] = CONTROL_TYPES.get(d["control_type"], None)
        d["area"] = {"U": "Urban", "R": "Rural"}.get(d.get("urban_rural"))
        if d["phone"]:
            p = d["phone"]
            d["phone"] = f"({p[:3]}) {p[3:6]}-{p[6:]}"
        out.append(d)
        if len(out) >= limit:
            break
    warnings = []
    if n_chain:
        warnings.append(f"{n_chain} chain/franchise rows excluded by name "
                        "screen (LabCorp, Quest, plasma networks, ARCpoint-"
                        "style franchises...); pass exclude_chains=false to "
                        "include them")
    if exclude_chains:
        warnings.append("chain screen is a name heuristic — multi-site "
                        "regionals without a famous name can slip through; "
                        "affiliated_labs column is the multi-site signal")
    if rural_only:
        warnings.append("rural = CMS's CBSA-based flag (outside metro/micro "
                        "areas) — stricter than 'small town'; drop "
                        "rural_only and use classify_rural for graded "
                        "RUCA classes")
    return out, warnings
