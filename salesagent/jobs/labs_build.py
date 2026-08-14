"""labs_build job — download the CMS CLIA registry and build the labs estate.

One ~220MB CSV download (streamed, progress by bytes), parsed straight into
labsref.db (only the ~20 useful columns of 103 are kept; the raw CSV is
deleted after load so a run costs ~70MB on disk, not 300). All 680k rows are
loaded — inactive labs keep their active=0 flag so "closed" is a fact, not
an absence — and labs_find defaults to active-only.
"""
from __future__ import annotations

import csv
import sqlite3

import requests

from .. import estate
from ..artifacts import store
from ..db import utcnow
from ..integrations import clia
from .manager import JobCtx


def labs_build(ctx: JobCtx) -> str:
    sess = requests.Session()
    ctx.log("resolving newest CLIA quarterly file from data.cms.gov catalog")
    src = clia.latest_csv(sess)
    ctx.log(f"source: {src['label']}")

    run_dir = estate.new_run_dir(ctx.settings, "labs")
    raw_path = run_dir / "clia_raw.csv"

    # --- stream download (~220MB) ----------------------------------------
    with sess.get(src["url"], stream=True, timeout=600) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(raw_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and done % (20 << 20) < (1 << 20):
                    ctx.progress(done, total,
                                 f"downloading {done >> 20}/{total >> 20} MB")
    ctx.log(f"downloaded {raw_path.stat().st_size >> 20} MB")

    # --- parse into the snapshot db ---------------------------------------
    db_path = run_dir / "labsref.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(estate.LABSREF_SCHEMA)
    n_rows = n_active = n_skipped = 0
    try:
        with conn, open(raw_path, encoding="latin-1", newline="") as f:
            for r in csv.DictReader(f):
                t = clia.row_to_lab(r)
                if t is None:
                    n_skipped += 1
                    continue
                conn.execute(clia.INSERT_SQL, t)
                n_rows += 1
                n_active += t[14]
                if n_rows % 100000 == 0:
                    ctx.log(f"loaded {n_rows:,} rows...")
        with conn:
            counts = {code: cnt for code, cnt in conn.execute(
                "SELECT fac_type, COUNT(*) FROM labs WHERE active=1 "
                "GROUP BY fac_type")}
            for k, v in (("built_at", utcnow()), ("source", src["label"]),
                         ("url", src["url"])):
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                             (k, str(v)))
    finally:
        conn.close()
    raw_path.unlink(missing_ok=True)
    n_independent = counts.get(15, 0)
    ctx.log(f"loaded {n_rows:,} labs ({n_active:,} active, "
            f"{n_independent:,} active independent), skipped {n_skipped}")

    manifest = {"domain": "labs", "db_file": "labsref.db",
                "built_at": utcnow(), "scope": "national",
                "source_file": src["label"],
                "counts": {"labs": n_rows, "active": n_active,
                           "active_independent": n_independent},
                "sources": [{"dataset": clia.DATASET_TITLE,
                             "url": src["url"], "rows": n_rows,
                             "fetched_at": utcnow()}]}
    estate.set_current(ctx.settings, "labs", run_dir, manifest)
    ctx.log(f"estate flipped to run {run_dir.name}")

    md = (f"## Labs estate built — run {run_dir.name}\n\n"
          f"- **{n_rows:,} CLIA-certified labs** loaded ({n_active:,} "
          f"currently active)\n"
          f"- **{n_independent:,} active independent labs** (facility type "
          f"15) — the core outreach universe\n"
          f"- Every row: name, address, phone, certificate class, ownership, "
          f"annual test volume, accreditations\n\n"
          f"Source: CMS Provider of Services file ({src['label']}), the "
          f"official CLIA registry — every US clinical lab must hold a CLIA "
          f"certificate. Snapshot: `data/estate/labs/runs/{run_dir.name}/`. "
          f"labs_find now reads this build.")
    aconn = ctx.rw()
    try:
        art = store.create(
            aconn, conversation_id=ctx.conversation_id,
            tool="labs_build_reference", kind="markdown",
            title="Labs estate — CLIA registry build",
            columns=[{"key": "markdown", "label": "markdown"}], rows=[[md]],
            provenance=[{"source": "CMS CLIA registry (data.cms.gov)",
                         "detail": src["label"], "url": src["url"],
                         "fetched_at": utcnow()}])
        return art["artifact_id"]
    finally:
        aconn.close()
