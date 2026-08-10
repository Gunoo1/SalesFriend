"""k12 estate builder job: downloads FRESH public data — NCES CCD district
directory, F-33 district finance, CRDC school offerings — from the Urban
Institute Education Data API (the maintained public mirror of those federal
files) into a new immutable run under data/estate/k12/runs/<stamp>/.

Nothing is copied from any other project or past pipeline; every build is a
from-source snapshot with its provenance in manifest.json.

API shapes verified live 2026-08-06:
  - 10,000-row pages, {count, next, results}; `next` is an absolute URL
  - ?fips=NN filters to one state (34 -> 700 NJ districts, single page)
  - directory latest year 2024; agency_type 1=regular 7=charter agency;
    agency_charter_indicator is null in 2024 -> derive charter from type 7
  - finance latest year 2020 (2021+ return count=0); dollar fields include
    outlay_capital_instruc_equip, rev_fed_state_title_i/vocational/math_sci
  - CRDC offerings latest year 2021; num_classes_biology/chemistry/physics
    arrive as STRINGS ("7.000"); negative values are reserve codes = missing
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import requests

from .. import estate
from ..artifacts import store
from ..db import utcnow
from .manager import JobCtx

BASE = "https://educationdata.urban.org/api/v1"
UA = {"User-Agent": "SalesAgent-estate-builder/1.0"}

FIPS = {"AL": 1, "AK": 2, "AZ": 4, "AR": 5, "CA": 6, "CO": 8, "CT": 9,
        "DE": 10, "DC": 11, "FL": 12, "GA": 13, "HI": 15, "ID": 16, "IL": 17,
        "IN": 18, "IA": 19, "KS": 20, "KY": 21, "LA": 22, "ME": 23, "MD": 24,
        "MA": 25, "MI": 26, "MN": 27, "MS": 28, "MO": 29, "MT": 30, "NE": 31,
        "NV": 32, "NH": 33, "NJ": 34, "NM": 35, "NY": 36, "NC": 37, "ND": 38,
        "OH": 39, "OK": 40, "OR": 41, "PA": 42, "RI": 44, "SC": 45, "SD": 46,
        "TN": 47, "TX": 48, "UT": 49, "VT": 50, "VA": 51, "WA": 53, "WV": 54,
        "WI": 55, "WY": 56}

DIR_YEARS = (2025, 2024, 2023)
FIN_YEARS = (2022, 2021, 2020, 2019)
CRDC_YEARS = (2023, 2021, 2020, 2017)


def _get(sess: requests.Session, url: str) -> dict:
    """Patient retries: this is a background job against a public API that
    throws transient Cloudflare 524s under load (seen live 2026-08-06)."""
    last: Exception | None = None
    for wait in (0, 5, 15, 40, 90):
        if wait:
            time.sleep(wait)
        try:
            r = sess.get(url, headers=UA, timeout=180)
            if r.status_code == 200:
                return r.json()
            last = RuntimeError(f"HTTP {r.status_code} for {url}")
        except (requests.RequestException, ValueError) as e:
            # RequestException covers mid-transfer drops too
            # (ChunkedEncodingError 'Response ended prematurely', seen live
            # while the API recovered from its 524 outage)
            last = e
    raise RuntimeError(f"educationdata.urban.org unreachable: {last}")


def _first_year(sess, path: str, years: tuple[int, ...]) -> int:
    """Newest year with data (probed against DE = small single page)."""
    for y in years:
        j = _get(sess, f"{BASE}/{path}/{y}/?fips=10")
        if j.get("count"):
            return y
    raise RuntimeError(f"no data year found for {path} (tried {years})")


def _pages(sess, ctx: JobCtx, path: str, year: int, states: list[str],
           label: str, counter: list, total_hint: list):
    """Yield result rows page by page; counter[0] = pages fetched overall."""
    if states:
        starts = [f"{BASE}/{path}/{year}/?fips={FIPS[s]}"
                  for s in states if s in FIPS]
    else:
        starts = [f"{BASE}/{path}/{year}/"]
    fetched = 0
    for start in starts:
        url = start
        while url:
            j = _get(sess, url)
            rows = j.get("results") or []
            fetched += 1
            counter[0] += 1
            ctx.progress(counter[0], max(total_hint[0], counter[0]),
                         f"{label}: page {fetched} ({len(rows)} rows)")
            yield from rows
            url = j.get("next")
            time.sleep(0.5)
    ctx.log(f"{label}: {fetched} page(s)")


def _num(v):
    """Federal files use negative reserve codes for missing — treat as NULL.
    CRDC numerics arrive as strings. NEVER use for coordinates (US longitudes
    are legitimately negative — use _coord)."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f < 0 else f


def _coord(v):
    """Plain float parse for lat/lng — negatives are real (US longitude)."""
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _intn(v):
    f = _num(v)
    return int(f) if f is not None else None


def k12_build(ctx: JobCtx) -> str:
    states = [str(s).upper() for s in (ctx.params.get("states") or [])
              if str(s).upper() in FIPS]
    scope = "states" if states else "national"
    sess = requests.Session()

    ctx.log(f"probing latest data years ({scope} build)")
    dir_year = _first_year(sess, "school-districts/ccd/directory", DIR_YEARS)
    fin_year = _first_year(sess, "school-districts/ccd/finance", FIN_YEARS)
    crdc_year = _first_year(sess, "schools/crdc/offerings", CRDC_YEARS)
    ctx.log(f"years: directory {dir_year}, finance {fin_year}, CRDC {crdc_year}")

    # page-count estimate for the progress bar (national: ~2 + ~2 + ~10)
    total_hint = [len(states) * 3 if states else 14]
    pages = [0]

    run_dir = estate.new_run_dir(ctx.settings, "k12")
    db_path = run_dir / "k12ref.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(estate.K12REF_SCHEMA)
    sources = []
    try:
        # --- districts (CCD directory) ---------------------------------------
        n_dist = 0
        with conn:
            for r in _pages(sess, ctx, "school-districts/ccd/directory",
                            dir_year, states, "CCD directory", pages,
                            total_hint):
                lat, lng = _coord(r.get("latitude")), _coord(r.get("longitude"))
                if lat == 0.0 and lng == 0.0:
                    lat = lng = None
                conn.execute(
                    "INSERT OR REPLACE INTO districts (leaid, name, state,"
                    " city, county_name, zip, phone, enrollment,"
                    " number_of_schools, agency_type, charter, locale,"
                    " latitude, longitude, year)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(r["leaid"]).zfill(7), r.get("lea_name"),
                     r.get("state_location") or r.get("state_mailing"),
                     r.get("city_location"), r.get("county_name"),
                     r.get("zip_location"), r.get("phone"),
                     _intn(r.get("enrollment")),
                     _intn(r.get("number_of_schools")),
                     r.get("agency_type"),
                     1 if r.get("agency_type") == 7 else 0,
                     r.get("urban_centric_locale"), lat, lng, dir_year))
                n_dist += 1
        sources.append({"dataset": "NCES CCD district directory",
                        "url": f"{BASE}/school-districts/ccd/directory/{dir_year}/",
                        "year": dir_year, "rows": n_dist,
                        "fetched_at": utcnow()})
        ctx.log(f"districts: {n_dist}")

        # --- finance (F-33) ---------------------------------------------------
        n_fin = 0
        with conn:
            for r in _pages(sess, ctx, "school-districts/ccd/finance",
                            fin_year, states, "F-33 finance", pages,
                            total_hint):
                conn.execute(
                    "INSERT OR REPLACE INTO district_finance (leaid, year,"
                    " rev_total, exp_total, rev_title_i, rev_vocational,"
                    " rev_math_sci, cap_instruc_equip, exp_tech_equipment,"
                    " exp_textbooks, debt_issued_fy)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(r["leaid"]).zfill(7), fin_year,
                     _num(r.get("rev_total")), _num(r.get("exp_total")),
                     _num(r.get("rev_fed_state_title_i")),
                     _num(r.get("rev_fed_state_vocational")),
                     _num(r.get("rev_fed_state_math_sci_teach")),
                     _num(r.get("outlay_capital_instruc_equip")),
                     _num(r.get("exp_tech_equipment")),
                     _num(r.get("exp_textbooks")),
                     _num(r.get("debt_longterm_issued_fy"))))
                n_fin += 1
        sources.append({"dataset": "F-33 district finance",
                        "url": f"{BASE}/school-districts/ccd/finance/{fin_year}/",
                        "year": fin_year, "rows": n_fin,
                        "fetched_at": utcnow()})
        ctx.log(f"finance: {n_fin}")

        # --- CRDC science (school level -> district aggregate) ----------------
        agg: dict[str, list] = {}   # leaid -> [sci_sections, ap_schools]
        n_sch = 0
        for r in _pages(sess, ctx, "schools/crdc/offerings", crdc_year,
                        states, "CRDC offerings", pages, total_hint):
            n_sch += 1
            leaid = str(r.get("leaid") or "").zfill(7)
            if not leaid.strip("0"):
                continue
            sci = sum(v for v in (_num(r.get("num_classes_biology")),
                                  _num(r.get("num_classes_chemistry")),
                                  _num(r.get("num_classes_physics")))
                      if v is not None)
            ap = 1 if _num(r.get("ap_courses_science_indicator")) == 1 else 0
            cur = agg.setdefault(leaid, [0.0, 0])
            cur[0] += sci
            cur[1] += ap
        with conn:
            for leaid, (sci, ap) in agg.items():
                conn.execute(
                    "INSERT OR REPLACE INTO district_crdc (leaid, year,"
                    " sci_sections, ap_sci_schools) VALUES (?,?,?,?)",
                    (leaid, crdc_year, sci, ap))
        sources.append({"dataset": "CRDC school offerings (science classes)",
                        "url": f"{BASE}/schools/crdc/offerings/{crdc_year}/",
                        "year": crdc_year, "rows": n_sch,
                        "fetched_at": utcnow()})
        ctx.log(f"CRDC: {n_sch} school rows -> {len(agg)} districts")

        with conn:
            for k, v in (("built_at", utcnow()), ("scope", scope),
                         ("states", ",".join(states)),
                         ("dir_year", dir_year), ("fin_year", fin_year),
                         ("crdc_year", crdc_year)):
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                             (k, str(v)))
    finally:
        conn.close()

    manifest = {"domain": "k12", "db_file": "k12ref.db", "built_at": utcnow(),
                "scope": scope, "states": states,
                "years": {"directory": dir_year, "finance": fin_year,
                          "crdc": crdc_year},
                "counts": {"districts": n_dist, "finance": n_fin,
                           "crdc_districts": len(agg)},
                "sources": sources}
    estate.set_current(ctx.settings, "k12", run_dir, manifest)
    ctx.log(f"estate flipped to run {run_dir.name}")

    md = (f"## K12 estate built — run {run_dir.name}\n\n"
          f"Scope: **{scope}**"
          + (f" ({', '.join(states)})" if states else "") + "\n\n"
          f"- **{n_dist:,} districts** (NCES CCD directory {dir_year})\n"
          f"- **{n_fin:,} finance rows** (F-33 {fin_year}: Title I, CTE, "
          f"math/sci, capital instructional equipment $)\n"
          f"- **{len(agg):,} districts with science data** (CRDC {crdc_year}: "
          f"bio/chem/physics classes, AP science schools; from {n_sch:,} "
          f"school records)\n\n"
          f"Source: educationdata.urban.org (public NCES/CRDC mirror), "
          f"downloaded fresh by this app. Snapshot: "
          f"`data/estate/k12/runs/{run_dir.name}/`. k12 tools now read this "
          f"build; re-run k12_build_reference any time to refresh.")
    aconn = ctx.rw()
    try:
        art = store.create(
            aconn, conversation_id=ctx.conversation_id, tool="k12_build_reference",
            kind="markdown", title=f"K12 estate — {scope} build",
            columns=[{"key": "markdown", "label": "markdown"}], rows=[[md]],
            provenance=[{"source": "educationdata.urban.org",
                         "detail": f"CCD {dir_year} + F-33 {fin_year} + "
                                   f"CRDC {crdc_year}, {scope}",
                         "url": BASE, "fetched_at": utcnow()}])
        return art["artifact_id"]
    finally:
        aconn.close()
