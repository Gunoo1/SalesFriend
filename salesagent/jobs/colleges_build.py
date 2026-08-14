"""colleges_build job — download the IPEDS HD directory and build the
colleges estate. One ~1MB zip; the whole build takes seconds."""
from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from datetime import datetime, timezone

import requests

from .. import estate
from ..artifacts import store
from ..db import utcnow
from ..integrations import ipeds
from .manager import JobCtx


def colleges_build(ctx: JobCtx) -> str:
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0"
    year_now = datetime.now(timezone.utc).year
    ctx.log("resolving newest IPEDS HD directory file")
    src = ipeds.latest_hd(sess, start_year=year_now)
    ctx.log(f"source: {src['label']}")

    run_dir = estate.new_run_dir(ctx.settings, "colleges")
    zip_path = run_dir / "hd_raw.zip"
    with sess.get(src["url"], stream=True, timeout=300) as resp:
        resp.raise_for_status()
        zip_path.write_bytes(resp.content)
    ctx.log(f"downloaded {zip_path.stat().st_size >> 10} KB")

    db_path = run_dir / "collegesref.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(estate.COLLEGESREF_SCHEMA)
    n_rows = n_active = n_rural = n_skipped = 0
    try:
        with conn, zipfile.ZipFile(zip_path) as z:
            csv_name = next(n for n in z.namelist()
                            if n.lower().endswith(".csv"))
            with z.open(csv_name) as f:
                reader = csv.DictReader(
                    io.TextIOWrapper(f, encoding="latin-1", newline=""))
                for r in reader:
                    t = ipeds.row_to_college(r)
                    if t is None:
                        n_skipped += 1
                        continue
                    conn.execute(ipeds.INSERT_SQL, t)
                    n_rows += 1
        with conn:
            n_active = conn.execute(
                "SELECT COUNT(*) FROM colleges WHERE active=1 AND sector!=0"
            ).fetchone()[0]
            n_rural = conn.execute(
                "SELECT COUNT(*) FROM colleges WHERE active=1 AND sector!=0 "
                "AND locale BETWEEN 41 AND 43").fetchone()[0]
            for k, v in (("built_at", utcnow()), ("source", src["label"]),
                         ("url", src["url"])):
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                             (k, str(v)))
    finally:
        conn.close()
    zip_path.unlink(missing_ok=True)
    ctx.log(f"loaded {n_rows:,} institutions ({n_active:,} active, "
            f"{n_rural:,} rural-coded), skipped {n_skipped}")

    manifest = {"domain": "colleges", "db_file": "collegesref.db",
                "built_at": utcnow(), "scope": "national",
                "source_file": src["label"], "year": src["year"],
                "counts": {"institutions": n_rows, "active": n_active,
                           "active_rural": n_rural},
                "sources": [{"dataset": ipeds.DATASET_TITLE,
                             "url": src["url"], "rows": n_rows,
                             "fetched_at": utcnow()}]}
    estate.set_current(ctx.settings, "colleges", run_dir, manifest)
    ctx.log(f"estate flipped to run {run_dir.name}")

    md = (f"## Colleges estate built — run {run_dir.name}\n\n"
          f"- **{n_active:,} active US higher-ed institutions** "
          f"({src['label']}) with address, phone, website, and the chief "
          f"administrator's NAME + title\n"
          f"- **{n_rural:,} rural-coded campuses** (NCES locale 41-43) — "
          f"the 'middle of nowhere' schools reps rarely visit\n"
          f"- Filters: state, level (4yr/2yr), control (public/private/"
          f"for-profit), size class, locale, HBCU, hospital on campus\n\n"
          f"colleges_find now reads this build.")
    aconn = ctx.rw()
    try:
        art = store.create(
            aconn, conversation_id=ctx.conversation_id,
            tool="colleges_build_reference", kind="markdown",
            title="Colleges estate — IPEDS directory build",
            columns=[{"key": "markdown", "label": "markdown"}], rows=[[md]],
            provenance=[{"source": "IPEDS (nces.ed.gov)",
                         "detail": src["label"], "url": src["url"],
                         "fetched_at": utcnow()}])
        return art["artifact_id"]
    finally:
        aconn.close()
