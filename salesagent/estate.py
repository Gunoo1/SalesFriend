"""SalesAgent's OWN data estate — reference data this app downloaded itself.

Layout (all under data/, nothing outside this project is ever read):

    data/estate/<domain>/runs/<UTC-stamp>/   one immutable snapshot per build
        k12ref.db                            the queryable snapshot
        manifest.json                        sources, years, counts, built_at
    data/estate/<domain>/current.json        pointer to the run tools read

Each build job writes a NEW run directory and atomically flips current.json;
past runs stay on disk as browsable history but are never depended on — a
fresh install just builds a fresh run from public sources. Memoization of
live lookups (company_locations, business_status, api_cache) lives in app.db
and is likewise populated only by this app's own activity.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import get_ro_conn


class EstateMissing(Exception):
    """A tool needs a reference estate that hasn't been built in this app."""


K12_MISSING_MSG = (
    "No K12 reference estate has been built in this app yet. Call "
    "k12_build_reference (free background job, a few minutes) — it downloads "
    "fresh public NCES CCD / F-33 finance / CRDC data and builds this app's "
    "own database — then retry once the job finishes."
)

# Schema of a k12 run snapshot. Shared by the build job and the offline tests
# so the query layer is exercised against the exact DDL the builder writes.
K12REF_SCHEMA = """
CREATE TABLE IF NOT EXISTS districts (
  leaid             TEXT PRIMARY KEY,          -- zero-padded 7-char NCES id
  name              TEXT NOT NULL,
  state             TEXT,                      -- USPS 2-letter
  city              TEXT,
  county_name       TEXT,
  zip               TEXT,
  phone             TEXT,
  enrollment        INTEGER,
  number_of_schools INTEGER,
  agency_type       INTEGER,                   -- 1 regular, 7 charter agency
  charter           INTEGER,                   -- derived: agency_type = 7
  locale            INTEGER,                   -- urban_centric_locale
  latitude          REAL,
  longitude         REAL,
  year              INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dist_state ON districts(state);

CREATE TABLE IF NOT EXISTS district_finance (
  leaid             TEXT PRIMARY KEY,
  year              INTEGER,
  rev_total         REAL,
  exp_total         REAL,
  rev_title_i       REAL,                      -- rev_fed_state_title_i
  rev_vocational    REAL,                      -- rev_fed_state_vocational
  rev_math_sci      REAL,                      -- rev_fed_state_math_sci_teach
  cap_instruc_equip REAL,                      -- outlay_capital_instruc_equip
  exp_tech_equipment REAL,
  exp_textbooks     REAL,
  debt_issued_fy    REAL                       -- debt_longterm_issued_fy
);

CREATE TABLE IF NOT EXISTS district_crdc (
  leaid          TEXT PRIMARY KEY,
  year           INTEGER,
  sci_sections   REAL,                         -- sum bio+chem+physics classes
  ap_sci_schools INTEGER                       -- schools offering AP science
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

LABS_MISSING_MSG = (
    "No labs reference estate has been built in this app yet. Call "
    "labs_build_reference (free background job, a few minutes) — it downloads "
    "the current CMS CLIA registry (every clinical lab in the US, with phone "
    "numbers) and builds this app's own database — then retry once the job "
    "finishes.")

# Schema of a labs run snapshot (from the CMS Provider of Services CLIA file).
# Shared by the build job and the offline tests, same convention as K12REF.
LABSREF_SCHEMA = """
CREATE TABLE IF NOT EXISTS labs (
  clia_num       TEXT PRIMARY KEY,      -- CLIA certificate number (PRVDR_NUM)
  name           TEXT NOT NULL,
  addl_name      TEXT,
  street         TEXT,
  street2        TEXT,
  city           TEXT,
  state          TEXT,                  -- USPS 2-letter
  zip            TEXT,                  -- 5-digit
  county_fips    TEXT,
  phone          TEXT,                  -- 10 digits, no punctuation
  fax            TEXT,
  fac_type       INTEGER,               -- GNRL_FAC_TYPE_CD (15 = independent lab)
  cert_type      INTEGER,               -- CRTFCT_TYPE_CD (1 compliance, 2 waiver,
                                        --   3 accreditation, 4 PPM, 9 registration)
  control_type   INTEGER,               -- GNRL_CNTL_TYPE_CD ownership (best-effort labels)
  active         INTEGER,               -- PGM_TRMNTN_CD == '00'
  term_code      TEXT,
  cert_expire    TEXT,                  -- ISO date
  first_certified TEXT,                 -- ISO date (ORGNL_PRTCPTN_DT)
  accreditors    TEXT,                  -- comma list of matched accreditors (CAP,COLA,...)
  test_volume    INTEGER,               -- annual tests, all certificate forms summed
  affiliated_labs INTEGER,              -- DRCTLY_AFLTD_LAB_CNT (multi-site signal)
  urban_rural    TEXT                   -- U / R
);
CREATE INDEX IF NOT EXISTS idx_labs_state ON labs(state);
CREATE INDEX IF NOT EXISTS idx_labs_fac ON labs(fac_type);
CREATE INDEX IF NOT EXISTS idx_labs_active ON labs(active);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


COLLEGES_MISSING_MSG = (
    "No colleges reference estate has been built in this app yet. Call "
    "colleges_build_reference (free background job, ~1 minute — a 1MB "
    "download of the official IPEDS directory of every US college) and "
    "retry once the job finishes.")

# Schema of a colleges run snapshot (IPEDS HD directory file).
COLLEGESREF_SCHEMA = """
CREATE TABLE IF NOT EXISTS colleges (
  unitid      INTEGER PRIMARY KEY,      -- IPEDS UNITID
  name        TEXT NOT NULL,
  alias       TEXT,
  street      TEXT,
  city        TEXT,
  state       TEXT,                     -- USPS 2-letter
  zip         TEXT,                     -- 5-digit
  county      TEXT,
  phone       TEXT,                     -- 10 digits
  website     TEXT,
  chief_name  TEXT,                     -- president/chancellor (CHFNM)
  chief_title TEXT,
  sector      INTEGER,                  -- IPEDS SECTOR (0 = admin unit)
  level       INTEGER,                  -- ICLEVEL 1=4yr+ 2=2yr 3=<2yr
  control     INTEGER,                  -- 1 public, 2 private-NP, 3 for-profit
  size_class  INTEGER,                  -- INSTSIZE 1..5 (<1k .. 20k+)
  locale      INTEGER,                  -- NCES urban-centric (41-43 = rural)
  hbcu        INTEGER,
  tribal      INTEGER,
  hospital    INTEGER,                  -- runs a hospital
  medical     INTEGER,                  -- grants medical degrees
  carnegie    INTEGER,                  -- C21BASIC (15/16 = R1/R2)
  active      INTEGER,                  -- CYACTIVE == 1
  latitude    REAL,
  longitude   REAL
);
CREATE INDEX IF NOT EXISTS idx_col_state ON colleges(state);
CREATE INDEX IF NOT EXISTS idx_col_locale ON colleges(locale);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

PSS_MISSING_MSG = (
    "No private-schools reference estate has been built in this app yet. "
    "Call private_schools_build_reference (free background job, ~1 minute — "
    "a 4MB download of the official NCES Private School Universe Survey, "
    "every US private school with phone numbers) and retry once the job "
    "finishes.")

# Schema of a private-schools run snapshot (NCES PSS).
PSSREF_SCHEMA = """
CREATE TABLE IF NOT EXISTS private_schools (
  ppin        TEXT PRIMARY KEY,         -- PSS school id
  name        TEXT NOT NULL,
  street      TEXT,
  city        TEXT,
  state       TEXT,                     -- USPS 2-letter
  zip         TEXT,                     -- 5-digit
  county      TEXT,
  phone       TEXT,                     -- 10 digits
  level       INTEGER,                  -- 1 elementary, 2 secondary, 3 combined
  lo_grade    TEXT,                     -- decoded (PK/K/1..12)
  hi_grade    TEXT,
  religious   INTEGER,                  -- 1 Catholic, 2 other religious, 3 nonsectarian
  typology    INTEGER,                  -- 9-way PSS typology
  enrollment  INTEGER,                  -- NUMSTUDS
  teachers    REAL,                     -- NUMTEACH (FTE)
  size_class  INTEGER,                  -- 1..6 (<50 .. 750+)
  locale      INTEGER,                  -- NCES urban-centric (41-43 = rural)
  latitude    REAL,
  longitude   REAL,
  year        TEXT                      -- school year, e.g. 2021-22
);
CREATE INDEX IF NOT EXISTS idx_pss_state ON private_schools(state);
CREATE INDEX IF NOT EXISTS idx_pss_locale ON private_schools(locale);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def domain_dir(settings, domain: str) -> Path:
    return Path(settings.estate_dir) / domain


def new_run_dir(settings, domain: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = domain_dir(settings, domain) / "runs" / stamp
    p.mkdir(parents=True, exist_ok=True)
    return p


def set_current(settings, domain: str, run_dir: Path, manifest: dict) -> None:
    """Write the run's manifest and atomically flip the current pointer."""
    manifest = {**manifest, "run": run_dir.name}
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    cur = domain_dir(settings, domain) / "current.json"
    tmp = cur.with_name("current.json.tmp")
    tmp.write_text(json.dumps({"run": run_dir.name,
                               "built_at": manifest.get("built_at")}),
                   encoding="utf-8")
    os.replace(tmp, cur)


def current_manifest(settings, domain: str) -> dict | None:
    cur = domain_dir(settings, domain) / "current.json"
    if not cur.exists():
        return None
    try:
        ptr = json.loads(cur.read_text(encoding="utf-8"))
        run = domain_dir(settings, domain) / "runs" / ptr["run"]
        m = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        m["run_dir"] = str(run)
        return m
    except (OSError, KeyError, json.JSONDecodeError):
        return None


def list_runs(settings, domain: str) -> list[dict]:
    """Historical runs, newest first (manifest of each, if readable)."""
    runs = domain_dir(settings, domain) / "runs"
    out = []
    if runs.is_dir():
        for d in sorted(runs.iterdir(), reverse=True):
            mf = d / "manifest.json"
            if mf.exists():
                try:
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    m["run_dir"] = str(d)
                    out.append(m)
                except (OSError, json.JSONDecodeError):
                    continue
    return out


def covered_states(manifest: dict) -> set[str] | None:
    """None = national coverage; otherwise the set of built states."""
    if manifest.get("scope") == "states":
        return {str(s).upper() for s in manifest.get("states") or []}
    return None


def open_k12(settings) -> sqlite3.Connection:
    """Read-only connection to the current k12 snapshot. Raises EstateMissing
    with an actionable message when no build exists yet."""
    m = current_manifest(settings, "k12")
    if not m:
        raise EstateMissing(K12_MISSING_MSG)
    db = Path(m["run_dir"]) / m.get("db_file", "k12ref.db")
    if not db.exists():
        raise EstateMissing(K12_MISSING_MSG)
    return get_ro_conn(db)


def open_labs(settings) -> sqlite3.Connection:
    """Read-only connection to the current labs snapshot (CLIA registry)."""
    m = current_manifest(settings, "labs")
    if not m:
        raise EstateMissing(LABS_MISSING_MSG)
    db = Path(m["run_dir"]) / m.get("db_file", "labsref.db")
    if not db.exists():
        raise EstateMissing(LABS_MISSING_MSG)
    return get_ro_conn(db)


def open_colleges(settings) -> sqlite3.Connection:
    """Read-only connection to the current colleges snapshot (IPEDS)."""
    m = current_manifest(settings, "colleges")
    if not m:
        raise EstateMissing(COLLEGES_MISSING_MSG)
    db = Path(m["run_dir"]) / m.get("db_file", "collegesref.db")
    if not db.exists():
        raise EstateMissing(COLLEGES_MISSING_MSG)
    return get_ro_conn(db)


def open_private_schools(settings) -> sqlite3.Connection:
    """Read-only connection to the current private-schools snapshot (PSS)."""
    m = current_manifest(settings, "private_schools")
    if not m:
        raise EstateMissing(PSS_MISSING_MSG)
    db = Path(m["run_dir"]) / m.get("db_file", "pssref.db")
    if not db.exists():
        raise EstateMissing(PSS_MISSING_MSG)
    return get_ro_conn(db)
