"""pss_build job — download the NCES Private School Universe Survey and
build the private-schools estate. One ~4MB zip; seconds to build."""
from __future__ import annotations

import csv
import io
import sqlite3
import zipfile

import requests

from .. import estate
from ..artifacts import store
from ..db import utcnow
from ..integrations import pss
from .manager import JobCtx


def pss_build(ctx: JobCtx) -> str:
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0"
    ctx.log("resolving newest PSS release from nces.ed.gov")
    src = pss.latest_csv(sess)
    ctx.log(f"source: {src['label']}")

    run_dir = estate.new_run_dir(ctx.settings, "private_schools")
    zip_path = run_dir / "pss_raw.zip"
    with sess.get(src["url"], stream=True, timeout=300) as resp:
        resp.raise_for_status()
        zip_path.write_bytes(resp.content)
    ctx.log(f"downloaded {zip_path.stat().st_size >> 10} KB")

    db_path = run_dir / "pssref.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(estate.PSSREF_SCHEMA)
    n_rows = n_rural = n_skipped = 0
    try:
        with conn, zipfile.ZipFile(zip_path) as z:
            csv_name = next(n for n in z.namelist()
                            if n.lower().endswith(".csv"))
            with z.open(csv_name) as f:
                reader = csv.DictReader(
                    io.TextIOWrapper(f, encoding="latin-1", newline=""))
                for r in reader:
                    t = pss.row_to_school(r, src["school_year"])
                    if t is None:
                        n_skipped += 1
                        continue
                    conn.execute(pss.INSERT_SQL, t)
                    n_rows += 1
        with conn:
            n_rural = conn.execute(
                "SELECT COUNT(*) FROM private_schools "
                "WHERE locale BETWEEN 41 AND 43").fetchone()[0]
            for k, v in (("built_at", utcnow()), ("source", src["label"]),
                         ("url", src["url"])):
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                             (k, str(v)))
    finally:
        conn.close()
    zip_path.unlink(missing_ok=True)
    ctx.log(f"loaded {n_rows:,} private schools ({n_rural:,} rural-coded), "
            f"skipped {n_skipped}")

    manifest = {"domain": "private_schools", "db_file": "pssref.db",
                "built_at": utcnow(), "scope": "national",
                "source_file": src["label"],
                "school_year": src["school_year"],
                "counts": {"schools": n_rows, "rural": n_rural},
                "sources": [{"dataset": pss.DATASET_TITLE,
                             "url": src["url"], "rows": n_rows,
                             "fetched_at": utcnow()}]}
    estate.set_current(ctx.settings, "private_schools", run_dir, manifest)
    ctx.log(f"estate flipped to run {run_dir.name}")

    md = (f"## Private schools estate built — run {run_dir.name}\n\n"
          f"- **{n_rows:,} US private schools** ({src['label']}) with "
          f"address, phone, enrollment, teachers, religious typology, grade "
          f"span, and locale\n"
          f"- **{n_rural:,} rural-coded schools** (NCES locale 41-43)\n"
          f"- Filters: state, enrollment, level, Catholic/other-religious/"
          f"nonsectarian, locale\n\n"
          f"private_schools_find now reads this build.")
    aconn = ctx.rw()
    try:
        art = store.create(
            aconn, conversation_id=ctx.conversation_id,
            tool="private_schools_build_reference", kind="markdown",
            title="Private schools estate — PSS build",
            columns=[{"key": "markdown", "label": "markdown"}], rows=[[md]],
            provenance=[{"source": "NCES PSS (nces.ed.gov)",
                         "detail": src["label"], "url": src["url"],
                         "fetched_at": utcnow()}])
        return art["artifact_id"]
    finally:
        aconn.close()
