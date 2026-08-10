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
