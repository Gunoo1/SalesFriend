"""app.db schema + connection helpers.

Two databases by design (K12Intel's lock-contention lesson applied
preemptively): app.db holds everything below; checkpoints.db belongs to the
LangGraph checkpointer alone, which writes on every superstep of every
streaming turn and must never queue behind an artifact insert.

Write discipline: WAL, busy_timeout, single uvicorn worker, short
transactions, per-row commits inside job workers — never hold a write txn
across an LLM or network call.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .settings import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id          INTEGER PRIMARY KEY,
  username    TEXT UNIQUE NOT NULL,
  pw_hash     BLOB NOT NULL,
  salt        BLOB NOT NULL,
  is_admin    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token       TEXT PRIMARY KEY,
  user_id     INTEGER NOT NULL,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
  id              TEXT PRIMARY KEY,
  user_id         INTEGER NOT NULL,
  title           TEXT,
  status          TEXT NOT NULL DEFAULT 'idle',  -- idle|running|awaiting_confirmation
  budget_credits  INTEGER,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_convo_user ON conversations(user_id, updated_at);

-- UI render log ONLY. The LangGraph checkpoint is the canonical agent state;
-- never reconstruct one from the other.
CREATE TABLE IF NOT EXISTS messages (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id  TEXT NOT NULL,
  role             TEXT NOT NULL,            -- user|assistant|system
  content_json     TEXT NOT NULL,            -- {text, artifact_ids[], tool_cards[]}
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_convo ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id      TEXT PRIMARY KEY,
  conversation_id  TEXT NOT NULL,
  created_by_tool  TEXT NOT NULL,
  title            TEXT,
  kind             TEXT NOT NULL,             -- table|chart|map|markdown|metric_row
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifact_convo ON artifacts(conversation_id);

CREATE TABLE IF NOT EXISTS artifact_versions (
  artifact_id      TEXT NOT NULL,
  version          INTEGER NOT NULL,
  parent_version   INTEGER,
  ops_json         TEXT,                      -- the transform that produced this version
  columns_json     TEXT NOT NULL,
  rows_blob        BLOB NOT NULL,             -- zlib(JSON array-of-arrays)
  row_count        INTEGER NOT NULL,
  styling_json     TEXT,
  provenance_json  TEXT,
  chart_json       TEXT,
  map_json         TEXT,
  kind             TEXT NOT NULL,
  title            TEXT,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (artifact_id, version)
);

CREATE TABLE IF NOT EXISTS jobs (
  id                  TEXT PRIMARY KEY,
  kind                TEXT NOT NULL,
  params_json         TEXT,
  conversation_id     TEXT,
  user_id             INTEGER,
  tool_name           TEXT,
  status              TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|error|cancelled
  progress_done       INTEGER DEFAULT 0,
  progress_total      INTEGER,
  message             TEXT,
  external_ref        TEXT,      -- e.g. price_comparison job_id for delegated jobs
  result_artifact_id  TEXT,
  error               TEXT,
  notified            INTEGER NOT NULL DEFAULT 0,  -- 0 until surfaced into the conversation
  created_at          TEXT NOT NULL,
  started_at          TEXT,
  finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_convo ON jobs(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS uploads (
  id               TEXT PRIMARY KEY,
  user_id          INTEGER NOT NULL,
  conversation_id  TEXT NOT NULL,
  filename         TEXT NOT NULL,
  artifact_id      TEXT,
  path             TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

-- Org-shared: one rep's paid pull is the next rep's cache hit.
CREATE TABLE IF NOT EXISTS seamless_ledger (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                 TEXT NOT NULL,
  user_id            INTEGER,
  conversation_id    TEXT,
  action             TEXT NOT NULL,           -- search|research|poll|get_contacts
  target             TEXT,
  credits_spent      INTEGER NOT NULL DEFAULT 0,
  credits_remaining  INTEGER,                 -- X-PublicAPI-Credits header echo
  result_status      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_day ON seamless_ledger(ts);

-- Ported shape from K12Intel k12/claude_client.py: same sha256 key scheme so
-- semantics match (task|model|prompt_version|canonical input JSON).
CREATE TABLE IF NOT EXISTS claude_cache (
  cache_key      TEXT PRIMARY KEY,
  task           TEXT NOT NULL,
  model          TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  response_json  TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_matches (
  source      TEXT NOT NULL,
  input_hash  TEXT NOT NULL,
  input_name  TEXT,
  matched_id  TEXT,
  method      TEXT,
  confidence  REAL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (source, input_hash)
);

-- One shared external-API cache (uniform shape; provider column instead of
-- six near-identical tables). TTLs are set by the writing tool.
CREATE TABLE IF NOT EXISTS api_cache (
  cache_key      TEXT PRIMARY KEY,
  provider       TEXT NOT NULL,               -- serper|socrata|usaspending|overpass|page|seamless_search
  endpoint       TEXT,
  params_json    TEXT,
  response_json  TEXT NOT NULL,
  fetched_at     TEXT NOT NULL,
  expires_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_cache_provider ON api_cache(provider, fetched_at);

CREATE TABLE IF NOT EXISTS company_locations (
  company        TEXT NOT NULL,
  location_id    TEXT NOT NULL,
  name           TEXT,
  street         TEXT, city TEXT, state TEXT, zip TEXT, phone TEXT,
  lat REAL, lng REAL,
  location_type  TEXT,
  provider       TEXT,                        -- locator_scrape|overture
  source_url     TEXT,
  fetched_at     TEXT NOT NULL,
  PRIMARY KEY (company, location_id)
);

CREATE TABLE IF NOT EXISTS business_status (
  location_key  TEXT PRIMARY KEY,             -- norm(company|name|city|state)
  company       TEXT, name TEXT, city TEXT, state TEXT,
  status        TEXT NOT NULL,                -- open|closed_permanent|closed_temporary|unverified
  confidence    TEXT,
  evidence_json TEXT,
  sources       TEXT,
  verified_at   TEXT NOT NULL
);

-- App-owned contact store (K12Intel's contacts table stays untouched).
CREATE TABLE IF NOT EXISTS contacts_app (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  leaid               TEXT,
  company             TEXT,
  source              TEXT NOT NULL,          -- seamless|upload|manual
  full_name           TEXT NOT NULL,
  title               TEXT,
  email               TEXT,
  email_validation    TEXT,                   -- valid|invalid|risky
  seamless_confidence TEXT,
  phone               TEXT,
  role_bucket         TEXT,
  search_result_id    TEXT,
  researched_at       TEXT,
  raw_json            TEXT,
  created_at          TEXT NOT NULL,
  UNIQUE (company, leaid, source, full_name, title)
);
CREATE INDEX IF NOT EXISTS idx_contacts_app_leaid ON contacts_app(leaid);

-- Reference tables, seeded at first boot from salesagent/ref/seeds/*.json
-- (timMtesting probe outputs vendored into this repo).
CREATE TABLE IF NOT EXISTS ref_adoption_calendar (
  state                 TEXT PRIMARY KEY,
  is_adoption_state     INTEGER,
  science_last_adopted  TEXT,
  science_next_adoption TEXT,
  proposal_window       TEXT,
  notes                 TEXT,
  key_quote             TEXT,
  confidence            TEXT,
  source_url            TEXT,
  seeded_from           TEXT,
  seed_date             TEXT,
  updated_at            TEXT
);

CREATE TABLE IF NOT EXISTS ref_kit_makers (
  name         TEXT PRIMARY KEY,
  segment      TEXT NOT NULL,                 -- openscied|federal_awards
  domains      TEXT,
  detail       TEXT,
  gov_total    REAL,
  agencies     TEXT,
  evidence_url TEXT,
  seeded_from  TEXT,
  seed_date    TEXT
);

CREATE TABLE IF NOT EXISTS ref_checkbook_datasets (
  domain      TEXT NOT NULL,
  dataset_id  TEXT NOT NULL,
  name        TEXT,
  state       TEXT,
  level       TEXT,                           -- school_edu|agency|city_county
  adapter     TEXT NOT NULL DEFAULT 'socrata',-- socrata|checkbooknyc
  notes       TEXT,
  seeded_from TEXT,
  seed_date   TEXT,
  PRIMARY KEY (domain, dataset_id)
);

CREATE TABLE IF NOT EXISTS ref_basket_adjacency (
  category         TEXT PRIMARY KEY,
  suggestions_json TEXT NOT NULL,
  seeded_from      TEXT,
  seed_date        TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15,
                           detect_types=0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_ro_conn(db_path: Path) -> sqlite3.Connection:
    """Read-only URI open — used for the app's own estate snapshots so tool
    reads can never mutate a build."""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True,
                           timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# (table, column, type) — post-release columns; K12Intel's migration pattern
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("uploads", "notified", "INTEGER NOT NULL DEFAULT 0"),
    ("artifacts", "archived", "INTEGER NOT NULL DEFAULT 0"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, coltype in _MIGRATIONS:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    conn.commit()


def init_db(settings: Settings) -> sqlite3.Connection:
    conn = get_conn(settings.app_db)
    conn.executescript(SCHEMA)
    conn.commit()
    _apply_migrations(conn)
    _seed_reference(conn, settings)
    return conn


def _seed_reference(conn: sqlite3.Connection, settings: Settings) -> None:
    """Idempotent first-boot seeding from vendored probe outputs. A table that
    already has rows is left alone (refresh jobs overwrite via updated_at)."""
    seeds = settings.root / "salesagent" / "ref" / "seeds"
    if not seeds.exists():
        return
    now = utcnow()

    if not conn.execute("SELECT 1 FROM ref_adoption_calendar LIMIT 1").fetchone():
        f = seeds / "t09_output.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            with conn:
                for st, rec in data.items():
                    if not isinstance(rec, dict):
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO ref_adoption_calendar VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (st,
                         1 if rec.get("is_adoption_state") else 0,
                         rec.get("science_last_adopted"),
                         rec.get("science_next_adoption"),
                         rec.get("proposal_submission_window"),
                         rec.get("middle_high_school_notes"),
                         rec.get("key_quote"),
                         rec.get("confidence"),
                         rec.get("source_url") or rec.get("url"),
                         "timMtesting/t09_output.json", now, now))

    if not conn.execute("SELECT 1 FROM ref_kit_makers LIMIT 1").fetchone():
        f = seeds / "kit_makers.json"
        if f.exists():
            rows = json.loads(f.read_text(encoding="utf-8"))
            with conn:
                for r in rows:
                    conn.execute(
                        "INSERT OR REPLACE INTO ref_kit_makers VALUES "
                        "(?,?,?,?,?,?,?,?,?)",
                        (r["name"], r["segment"], r.get("domains"),
                         r.get("detail"), r.get("gov_total"), r.get("agencies"),
                         r.get("evidence_url"), r.get("seeded_from"), now))

    if not conn.execute("SELECT 1 FROM ref_checkbook_datasets LIMIT 1").fetchone():
        f = seeds / "checkbook_datasets.json"
        if f.exists():
            rows = json.loads(f.read_text(encoding="utf-8"))
            with conn:
                for r in rows:
                    conn.execute(
                        "INSERT OR REPLACE INTO ref_checkbook_datasets VALUES "
                        "(?,?,?,?,?,?,?,?,?)",
                        (r["domain"], r["dataset_id"], r.get("name"),
                         r.get("state"), r.get("level"),
                         r.get("adapter", "socrata"), r.get("notes"),
                         r.get("seeded_from"), now))

    if not conn.execute("SELECT 1 FROM ref_basket_adjacency LIMIT 1").fetchone():
        f = seeds / "basket_adjacency.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            with conn:
                for cat, sugg in data.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO ref_basket_adjacency VALUES (?,?,?,?)",
                        (cat, json.dumps(sugg),
                         "timMtesting/t10_uline_crosssell.py ADJACENT", now))
