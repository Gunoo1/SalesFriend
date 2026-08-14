"""Pure-function tests (FBI convention as a hard rule: everything testable
without credits or network). Run: .venv\\Scripts\\python.exe tests\\run_tests.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from salesagent import estate
from salesagent.artifacts.transforms import TransformError, apply_ops
from salesagent.integrations import k12_local
from salesagent.integrations.seamless_client import (best_email, expand_name,
                                                     name_variants)
from salesagent.integrations.socrata import detect
from salesagent.tools.envelope import envelope
from salesagent.tools.merge import haversine_miles, nearby_stats, norm_org

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, label
    PASS += 1
    print(f"  ok  {label}")


BASE = {"columns": [{"key": "state", "label": "State", "type": "string"},
                    {"key": "n", "label": "N", "type": "number", "format": "int"}],
        "rows": [["NJ", 5], ["PA", 12], ["NJ", 7], ["NY", None]],
        "kind": "table", "title": "t", "styling": None, "chart": None,
        "map": None}

print("transforms:")
out = apply_ops(BASE, [{"op": "filter", "col": "state", "cmp": "eq", "value": "nj"}])
ok(len(out["rows"]) == 2, "filter eq is case-insensitive")
out = apply_ops(BASE, [{"op": "sort", "by": [{"col": "n", "dir": "asc"}]}])
ok([r[1] for r in out["rows"]] == [5, 7, 12, None], "sort asc, nulls last")
out = apply_ops(BASE, [{"op": "groupby", "by": ["state"],
                        "aggs": [{"col": "n", "fn": "sum", "as": "total"}]}])
ok(dict((r[0], r[1]) for r in out["rows"]) == {"NJ": 12, "PA": 12, "NY": None},
   "groupby sum with null group")
out = apply_ops(BASE, [{"op": "to_chart", "chart": {"kind": "bar", "x": "state", "y": "n"}}])
ok(out["kind"] == "chart" and out["chart"]["x"] == "state", "to_chart validates cols")
try:
    apply_ops(BASE, [{"op": "filter", "col": "zzz", "cmp": "eq", "value": 1}])
    raise AssertionError("should raise")
except TransformError as e:
    ok("available columns" in str(e), "transform error names columns")

print("custom tables (append_rows / concat / join):")
out = apply_ops(BASE, [{"op": "append_rows",
                        "rows": [{"state": "DE", "n": 3}, {"state": "MD"}]}])
ok(len(out["rows"]) == 6 and out["rows"][-1] == ["MD", None],
   "append_rows fills missing cols with null")
try:
    apply_ops(BASE, [{"op": "append_rows", "rows": [{"zzz": 1}]}])
    raise AssertionError("should raise")
except TransformError as e:
    ok("available columns" in str(e), "append_rows rejects unknown column")

OTHER = {"columns": [{"key": "state"}, {"key": "score", "type": "number"}],
         "rows": [["NJ", 88], ["PA", 71], ["DE", 90]]}
out = apply_ops(BASE, [{"op": "concat", "_other": OTHER}])
ok(len(out["rows"]) == 7, "concat stacks rows")
ok([c["key"] for c in out["columns"]] == ["state", "n", "score"],
   "concat unions columns by key")
ok(out["rows"][0] == ["NJ", 5, None] and out["rows"][4] == ["NJ", None, 88],
   "concat pads both sides with nulls")

out = apply_ops(BASE, [{"op": "join", "on": "state", "_other": OTHER}])
ok([c["key"] for c in out["columns"]] == ["state", "n", "score"],
   "join appends right columns minus key")
ok(out["rows"][0] == ["NJ", 5, 88] and out["rows"][3] == ["NY", None, None],
   "left join matches + nulls unmatched")
out = apply_ops(BASE, [{"op": "join", "on": "state", "how": "inner",
                        "_other": OTHER}])
ok(len(out["rows"]) == 3, "inner join drops unmatched")
DUP = {"columns": [{"key": "state"}, {"key": "n"}], "rows": [["nj ", 999]]}
out = apply_ops(BASE, [{"op": "join", "on": "state", "_other": DUP}])
ok(out["columns"][-1]["key"] == "n_2", "join suffixes colliding column keys")
ok(out["rows"][0] == ["NJ", 5, 999], "join key match is case/space-insensitive")
try:
    apply_ops(BASE, [{"op": "join", "on": "state", "right_on": "zzz",
                      "_other": OTHER}])
    raise AssertionError("should raise")
except TransformError as e:
    ok("its columns" in str(e), "join names the other table's columns on error")

print("styling / colors:")
out = apply_ops(BASE, [{"op": "set_styling", "styling": {"tier_rules": [
    {"column": "state", "eq": "NJ", "class": "hot"},
    {"column": "n", "gte": 10, "class": "warm"}]}}])
ok(out["styling"]["tier_rules"][0]["eq"] == "NJ", "set_styling accepts text rules")
try:
    apply_ops(BASE, [{"op": "set_styling", "styling": {"tier_rules": [
        {"column": "state", "eq": "NJ", "class": "purple"}]}}])
    raise AssertionError("should raise")
except TransformError as e:
    ok("hot" in str(e), "bad tier class rejected with palette named")
try:
    apply_ops(BASE, [{"op": "set_styling", "styling": {"tier_rules": [
        {"column": "state", "class": "hot"}]}}])
    raise AssertionError("should raise")
except TransformError as e:
    ok("test" in str(e), "rule without a test rejected")
try:
    apply_ops(BASE, [{"op": "set_styling", "styling": {"tier_rules": [
        {"column": "zzz", "eq": "x", "class": "hot"}]}}])
    raise AssertionError("should raise")
except TransformError as e:
    ok("available columns" in str(e), "rule with unknown column rejected")
out = apply_ops(BASE, [{"op": "set_styling", "styling": {"tier_rules": [
    {"column": "n", "gte": 10, "class": "hot",
     "label": "  Top target — 200k+ students nearby  " + "x" * 90}]}}])
lbl = out["styling"]["tier_rules"][0]["label"]
ok(lbl.startswith("Top target") and len(lbl) == 80,
   "tier_rule label trimmed + capped at 80")
try:
    apply_ops(BASE, [{"op": "set_styling", "styling": {"tier_rules": [
        {"column": "n", "gte": 10, "class": "hot", "label": 42}]}}])
    raise AssertionError("should raise")
except TransformError as e:
    ok("legend" in str(e), "non-string tier_rule label rejected")
out = apply_ops(BASE, [{"op": "to_chart", "chart": {
    "kind": "bar", "x": "state", "y": "n",
    "colors": {"NJ": "now", "PA": "#123456"}}}])
ok(out["chart"]["colors"]["NJ"] == "now", "to_chart accepts colors map")
try:
    apply_ops(BASE, [{"op": "to_chart", "chart": {
        "kind": "bar", "x": "state", "colors": {"NJ": 5}}}])
    raise AssertionError("should raise")
except TransformError as e:
    ok("label -> color" in str(e), "non-string chart color rejected")

from salesagent.integrations.xlsx import tier_class
STY = {"tier_rules": [{"column": "status", "eq": "closed", "class": "now"},
                      {"column": "status", "contains": "open", "class": "hot"},
                      {"column": "n", "gte": 50000, "class": "warm"}]}
COLS = [{"key": "status"}, {"key": "n"}]
ok(tier_class(STY, COLS, ["Closed", 1]) == "now", "xlsx eq text (case-insensitive)")
ok(tier_class(STY, COLS, ["OPEN (verified)", 1]) == "hot", "xlsx contains")
ok(tier_class(STY, COLS, ["?", "62,000"]) == "warm", "xlsx numeric with commas")
ok(tier_class(STY, COLS, ["?", 10]) is None, "xlsx no match")
ok(tier_class(STY, COLS, ["closed", 99999]) == "now", "xlsx first rule wins")
ok(tier_class({"tier_rules": [{"column": "n", "eq": 5, "class": "hot"}]},
              COLS, ["x", "5.0"]) == "hot", "xlsx numeric eq crosses formats")
ok(tier_class({"tier_rules": [{"column": "n", "gte": "bad", "class": "hot"}]},
              COLS, ["x", 99]) is None, "xlsx junk gte value tolerated")
ok(tier_class({"tier_rules": [{"column": "n", "gte": 1, "class": "purple"}]},
              COLS, ["x", 99]) is None, "xlsx unknown class ignored")

from salesagent.integrations.xlsx import _rule_legend
ok(_rule_legend({"column": "n", "gte": 200000, "class": "hot",
                 "label": "Top target"}) == "hot: Top target",
   "xlsx legend uses the rule label")
ok(_rule_legend({"column": "n", "gte": 200000, "class": "warm"})
   == "warm: n gte 200000", "xlsx legend falls back to the raw test")
ok(_rule_legend({"column": "n", "gte": 1, "class": "purple"}) == "",
   "xlsx legend skips unknown classes")

from salesagent.integrations.xlsx import build_workbook
LSPEC = {"artifact_id": "a1", "title": "Grid", "kind": "table", "row_count": 2,
         "created_by": "price_scrape", "styling": None, "provenance": [],
         "columns": [{"key": "sku"},
                     {"key": "p", "label": "P $", "format": "money",
                      "link_col": "p__url"},
                     {"key": "p__url", "hidden": True}],
         "rows": [["A1", 12.5, "https://x/1"], ["A2", None, "https://x/2"]]}
lws = build_workbook([LSPEC])["Grid"]
ok(lws.cell(row=2, column=2).hyperlink.target == "https://x/1",
   "xlsx price cell hyperlinked via link_col")
ok(lws.cell(row=3, column=2).value == "verify"
   and lws.cell(row=3, column=2).hyperlink.target == "https://x/2",
   "xlsx blank price -> clickable verify")
ok(lws.max_column == 2, "xlsx hidden url column not written")
WSPEC = {"artifact_id": "a2", "title": "Wrap", "kind": "table", "row_count": 1,
         "created_by": "t", "styling": None, "provenance": [],
         "columns": [{"key": "sku"}, {"key": "desc"}],
         "rows": [["A1", "x" * 90]]}
wws = build_workbook([WSPEC])["Wrap"]
ok(wws.cell(row=2, column=2).alignment.wrap_text is True,
   "xlsx capped-width column wraps text")
ok(not wws.cell(row=2, column=1).alignment.wrap_text,
   "xlsx short column left unwrapped")

print("price plausibility + stock aggregation:")
from salesagent.integrations.acumatica import _resolve_current, aggregate_stock
from salesagent.jobs.runners import _price_grid, price_plausible

ok(price_plausible(12.80, 11.50), "normal gap passes")
ok(not price_plausible(12.80, 3.10), "4x+ cheap competitor -> suspect")
ok(not price_plausible(10.0, 45.0), "4x+ expensive competitor -> suspect")
ok(price_plausible(None, 5.0) and price_plausible(10.0, None),
   "unjudgeable passes through")
ok(price_plausible(0, 99), "zero eisco passes through")

GRID = {"meta": {"cells": [{"key": "b@amazon", "label": "Brand @ Amazon"}]},
        "scan_view": [
            {"sku": "A1", "title": "Bottle", "eisco": {"total": 12.80, "trust": "exact",
             "url": "https://e/a1"},
             "cells": {"b@amazon": {"total": 3.10, "conf": 0.9, "flags": [],
                                    "url": "https://amz/x"}},
             "cheapest": "b@amazon", "gap": 9.70, "gap_pct": 76, "decision_ready": True},
            {"sku": "A2", "title": "Flask", "eisco": {"total": 10.0, "trust": "exact"},
             "cells": {"b@amazon": {"total": 9.0, "conf": 0.9, "flags": []}},
             "cheapest": "b@amazon", "gap": 1.0, "gap_pct": 10, "decision_ready": True}]}
cols, rows, warns = _price_grid(GRID)
keys = [c["key"] for c in cols]
si, ci, gi_, ri = (keys.index("suspect"), keys.index("cheapest"),
                   keys.index("gap"), keys.index("ready"))
ok(rows[0][keys.index("b@amazon")] is None, "implausible cell blanked")
ok("pack-size" in rows[0][si] and "https://amz/x" in rows[0][si],
   "suspect note carries reason + verify link")
ok(rows[0][ci] is None and rows[0][gi_] is None and rows[0][ri] == "no",
   "remote cheapest/gap discarded when its input was implausible")
ok(rows[1][keys.index("b@amazon")] == 9.0 and rows[1][ci] == "b@amazon"
   and rows[1][ri] == "yes", "plausible row untouched")
ok(any("implausible" in w for w in warns), "grid warning emitted")
cell_col = next(c for c in cols if c["key"] == "b@amazon")
ok(cell_col.get("link_col") == "b@amazon__url", "price col points at url col")
url_col = next(c for c in cols if c["key"] == "b@amazon__url")
ok(url_col.get("hidden") is True, "url col hidden")
ok(rows[0][keys.index("b@amazon__url")] == "https://amz/x",
   "listing url kept even when price blanked as suspect")
ok(rows[0][keys.index("eisco__url")] == "https://e/a1"
   and rows[1][keys.index("eisco__url")] is None, "eisco url per row")
ok(next(c for c in cols if c["key"] == "eisco").get("link_col") == "eisco__url",
   "eisco price col linked")
ok(len(rows[0]) == len(cols) and len(rows[1]) == len(cols),
   "row width matches column count with url cols")

STOCK_ROWS = [
    {"InventoryID": "CH0162C   ", "Warehouse": "01-MAIN ", "Location": "S4",
     "QtyOnHand": "3.000000", "QtyAvailable": "0.000000", "ABCCode": "D",
     "LastRun": "8/7/2026 4:51:40 PM"},
    {"InventoryID": "CH0162C   ", "Warehouse": "01-MAIN ", "Location": "R2",
     "QtyOnHand": "2.000000", "QtyAvailable": "1.000000", "ABCCode": "D",
     "LastRun": "8/7/2026 4:51:40 PM"},
    {"InventoryID": "CH0162C   ", "Warehouse": "02-EAST ", "Location": "A1",
     "QtyOnHand": "10.000000", "QtyAvailable": "10.000000", "ABCCode": "D",
     "LastRun": "8/7/2026 4:51:40 PM"}]
agg = {(a["sku"], a["warehouse"]): a for a in aggregate_stock(STOCK_ROWS)}
ok(agg[("CH0162C", "01-MAIN")]["qty_on_hand"] == 5.0
   and agg[("CH0162C", "01-MAIN")]["qty_available"] == 1.0
   and agg[("CH0162C", "01-MAIN")]["bins"] == 2,
   "bins collapse per SKU x warehouse")
ok(agg[("CH0162C", "02-EAST")]["qty_available"] == 10.0,
   "warehouses stay separate")

ok(_resolve_current([
    {"EffectiveDate": "2025-01-01T00:00:00", "ExpirationDate": None, "Price": "1"},
    {"EffectiveDate": "2026-01-01T00:00:00", "ExpirationDate": None, "Price": "2"},
], "2026-08-07")["Price"] == "2", "current price row = latest active version")

print("category trends (temp):")
from salesagent.tools.trends import (category_keywords,
                                     looks_like_category_export,
                                     resolve_trends_source, set_trends_source,
                                     trend_metrics)

CAT_HDR = ["Year", "2026", "2026"]
CAT_BODY = [["Quarter", "01", "01"], ["Month", "01", "01"],
            ["CMT Level 4", "Channel Cost (USD)", "Organic Growth Channel Cost % (USD)"],
            ["Glass Beakers", 100, 0.5]]
ok(looks_like_category_export(CAT_HDR, CAT_BODY), "category export detected")
ok(not looks_like_category_export(["sku", "qty"], [["A", 1]]),
   "normal upload not mistaken for trends export")
ok(not looks_like_category_export([], []), "empty upload safe")

with tempfile.TemporaryDirectory() as td:
    fake = SimpleNamespace(data_dir=Path(td),
                           category_xlsx=Path(td) / "Category.xlsx")
    p, label, uid = resolve_trends_source(fake)
    ok(p == fake.category_xlsx and uid is None, "no pointer -> root fallback")
    up = Path(td) / "up_cat.xlsx"
    up.write_bytes(b"x")
    set_trends_source(fake, path=up, filename="cat.xlsx", upload_id="u1",
                      conversation_id="c1")
    p, label, uid = resolve_trends_source(fake)
    ok(p == up and uid == "u1" and "cat.xlsx" in label,
       "pointer wins when its file exists")
    up.unlink()
    p, _, uid = resolve_trends_source(fake)
    ok(p == fake.category_xlsx and uid is None,
       "deleted upload -> falls back to root")

PTS = [{"month": f"2026-0{i}", "cost": c, "growth": g}
       for i, (c, g) in enumerate([(100, 0.10), (110, 0.05), (120, -0.02),
                                   (200, 0.20), (220, 0.30), (260, 0.40)], 1)]
m = trend_metrics(PTS, 3)
ok(m["latest_month"] == "2026-06" and m["latest_cost"] == 260,
   "latest month picked")
ok(m["avg_growth_pct"] == 30.0, "avg growth over window (fractions -> %)")
ok(abs(m["momentum_pct"] - 106.1) < 0.1, "momentum = last 3 vs prior 3")
ok(trend_metrics([{"month": "x", "cost": None}]) is None,
   "all-null series -> None")
m2 = trend_metrics(PTS[:2], 3)
ok(m2["momentum_pct"] is None, "no prior window -> momentum None")

kw, words = category_keywords("Glass Beakers")
ok(kw == "beaker" and "glass" in words, "beakers -> beaker + glass qualifier")
ok(category_keywords("Single Channel Pipetters")[0] == "pipette",
   "pipetters stems to pipette (hits pipette/pipettor)")
ok(category_keywords("Educational Apparatus")[0] == "apparat",
   "apparatus stems within 7 chars")
ok(category_keywords("Racks and Supports")[0] == "support"
   and "rack" in category_keywords("Racks and Supports")[1],
   "stopwords dropped, plurals singularized")

print("envelope caps:")
env = envelope(kind="table", summary="s",
               columns=[{"key": f"c{i}", "label": "x"} for i in range(30)],
               sample_rows=[[i] for i in range(99)], row_count=99)
ok(len(env["columns"]) == 20, "columns capped at 20")
ok(len(env["sample_rows"]) == 15, "sample rows capped at 15")
env = envelope(kind="table", summary="s",
               stats={"by_x": {str(i): i for i in range(60)}})
ok(len(env["stats"]["by_x"]) == 31, "stats entries capped at 30 (+ overflow marker)")

print("socrata detect():")
rows = [{"payment_id": "10001", "agency_name": "DEPT OF X",
         "vendor_name": "ULINE INC", "amount": "1,234.50",
         "account_descr": "SUPPLIES"} for _ in range(10)]
vcol, bcols, acol, dcol = detect(rows, "ULINE")
ok(vcol == "vendor_name", "vendor col = where ULINE appears")
ok(acol == "amount", "amount col ranked over payment_id (ID_WORDS)")
ok("agency_name" in bcols, "buyer cols include agency_name")
ok(dcol == "account_descr", "desc col found")

print("seamless helpers:")
ok(expand_name("Maine Twp HSD 207") == "Maine Township High School District 207",
   "abbrev expansion")
vs = name_variants("Newark Public Schools", city="Newark")
ok("Newark Board of Education" in vs, "city board-of-ed variant")
em, val, conf = best_email({"email1": "a@x.com", "email1EmailAI": "risky",
                            "email2": "b@x.com", "email2EmailAI": "valid",
                            "email2TotalAI": 91})
ok(em == "b@x.com" and val == "valid", "best_email prefers EmailAI valid")

print("geo_rank helpers:")
d = haversine_miles(40.7357, -74.1724, 40.2206, -74.7597)  # Newark NJ -> Trenton NJ
ok(45 < d < 55, f"Newark->Trenton ~50mi (got {d:.1f})")
ok(haversine_miles(40.0, -74.0, 40.0, -74.0) == 0, "zero distance")
B = [{"lat": 40.74, "lng": -74.17, "weight": 44199, "name": "Newark PS"},
     {"lat": 40.72, "lng": -74.05, "weight": 25951, "name": "Jersey City"},
     {"lat": 40.22, "lng": -74.76, "weight": 14000, "name": "Trenton"}]
stats = nearby_stats([(40.73, -74.17), (None, None)], B, radius_miles=15)
ok(stats[0]["count"] == 2 and stats[0]["weight_sum"] == 44199 + 25951,
   "counts+weights within 15mi (Trenton excluded)")
ok(stats[0]["nearest"] == "Newark PS" and stats[0]["nearest_miles"] < 2,
   "nearest neighbor + distance")
ok(stats[1] is None, "coordinate-less target -> None")

print("serper closed_signal (field names live-verified 2026-08-06):")
from salesagent.integrations.serper import closed_signal
ok(closed_signal({"isPermanentlyClosed": True}) == "closed_permanent",
   "isPermanentlyClosed=true (the REAL serper field)")
ok(closed_signal({"isPermanentlyClosed": None, "rating": 4.2}) is None,
   "null closed flags -> open-ish None")
ok(closed_signal({"description": "Permanently closed - moved"})
   == "closed_permanent", "text fallback")
ok(closed_signal({"isTemporarilyClosed": True}) == "closed_temporary",
   "temporary variant")

print("merge norm:")
ok(norm_org("The Newark Board of Education, Inc.") == norm_org("NEWARK"),
   "norm_org strips stopwords/punct")
ok(norm_org("Rutgers University") == norm_org("Rutgers Univ"),
   "univ stopword")

print("own estate (snapshot runs):")
with tempfile.TemporaryDirectory() as td:
    fake = SimpleNamespace(estate_dir=Path(td))
    ok(estate.current_manifest(fake, "k12") is None, "no estate -> None")
    run = estate.new_run_dir(fake, "k12")
    (run / "k12ref.db").write_bytes(b"")
    estate.set_current(fake, "k12", run,
                       {"domain": "k12", "db_file": "k12ref.db",
                        "built_at": "2026-08-06T00:00:00Z",
                        "scope": "states", "states": ["NJ", "PA"]})
    m = estate.current_manifest(fake, "k12")
    ok(m is not None and m["run"] == run.name, "current pointer round-trip")
    ok(estate.covered_states(m) == {"NJ", "PA"}, "covered_states partial")
    ok(estate.covered_states({"scope": "national"}) is None,
       "covered_states national -> None")

print("state bboxes (vendored):")
from salesagent.tools.geo import _bboxes, state_bbox
bb = _bboxes()
ok(len(bb) == 51, "50 states + DC")
ok(all(v[0] < v[2] and v[1] < v[3] for v in bb.values()),
   "every bbox s<n and w<e")
ok(state_bbox("nj") == bb["NJ"], "state_bbox case-insensitive")
ok(state_bbox("ZZ") is None, "unknown state -> None")

print("k12_local over builder DDL:")
k12 = sqlite3.connect(":memory:")
k12.row_factory = sqlite3.Row
k12.executescript(estate.K12REF_SCHEMA)
k12.executemany(
    "INSERT INTO districts (leaid,name,state,city,county_name,enrollment,"
    "number_of_schools,agency_type,charter,latitude,longitude,year)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    [("3412345", "Newark Public School District", "NJ", "Newark", "Essex",
      44199, 63, 1, 0, 40.73, -74.17, 2024),
     ("3499999", "Tiny Charter", "NJ", "Camden", "Camden", 300, 1, 7, 1,
      39.9, -75.1, 2024)])
k12.execute("INSERT INTO district_finance (leaid,year,rev_title_i) "
            "VALUES ('3412345',2020,8.4e7)")
k12.execute("INSERT INTO district_crdc VALUES ('3412345',2021,412.0,9)")
rows, _ = k12_local.find_districts(k12, states=["NJ"], min_enrollment=1000)
ok(len(rows) == 1 and rows[0]["leaid"] == "3412345", "enrollment filter")
rows, _ = k12_local.find_districts(k12, charter=True)
ok(len(rows) == 1 and rows[0]["leaid"] == "3499999", "charter filter")
rows, _ = k12_local.find_districts(k12, min_rev_title_i=1e7)
ok(rows and rows[0]["rev_title_i"] == 8.4e7, "finance join filter")
ok(k12_local.coerce_leaid(100002) == "0100002", "leaid zero-pad from int")

app = sqlite3.connect(":memory:")
app.row_factory = sqlite3.Row
app.executescript(
    "CREATE TABLE contacts_app (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " leaid TEXT, company TEXT, source TEXT NOT NULL, full_name TEXT NOT NULL,"
    " title TEXT, email TEXT, email_validation TEXT, seamless_confidence TEXT,"
    " phone TEXT, role_bucket TEXT, search_result_id TEXT, researched_at TEXT,"
    " raw_json TEXT, created_at TEXT NOT NULL);")
app.executemany(
    "INSERT INTO contacts_app (leaid,source,full_name,title,email,role_bucket,"
    "created_at) VALUES (?,?,?,?,?,?,'2026-08-06')",
    [("3412345", "manual", "Sam Sci", "Science Supervisor", None, "science"),
     ("3412345", "seamless", "Pat Buyer", "Dir. Purchasing", "p@x.org",
      "purchasing")])
cts = k12_local.contacts(app, k12, states=["NJ"])
ok(len(cts) == 1 and cts[0]["full_name"] == "Pat Buyer",
   "best contact = purchasing/seamless/email-first")
ok(cts[0]["district"].startswith("Newark"), "estate name enrichment")
ok(k12_local.districts_without_contact(app, ["3412345", "3499999"])
   == ["3499999"], "districts_without_contact")

print("CLIA labs estate (integrations/clia.py):")
from salesagent import estate as _estate
from salesagent.integrations import clia

_CLIA_ROW = {
    "PRVDR_NUM": "01D9999999", "FAC_NAME": "DECATUR DIAGNOSTIC LAB INC",
    "ADDTNL_FAC_NAME": "", "ST_ADR": "12 MAIN ST", "ADDTNL_ST_ADR": "",
    "CITY_NAME": "DECATUR", "STATE_CD": "al", "ZIP_CD": "356010000",
    "FIPS_CNTY_CD": "103", "PHNE_NUM": "(256) 350-1234",
    "FAX_PHNE_NUM": "", "GNRL_FAC_TYPE_CD": "15", "CRTFCT_TYPE_CD": "1",
    "GNRL_CNTL_TYPE_CD": "04", "PGM_TRMNTN_CD": "00",
    "TRMNTN_EXPRTN_DT": "20270601", "ORGNL_PRTCPTN_DT": "19950115",
    "CAP_ACRDTD_Y_MATCH_SW": "Y", "COLA_ACRDTD_Y_MATCH_SW": "N",
    "FORM_116_ACRDTD_TEST_VOL_CNT": "1000", "FORM_1557_TEST_VOL_CNT": "500",
    "WVD_TEST_VOL_CNT": "x", "DRCTLY_AFLTD_LAB_CNT": "0",
    "CBSA_URBN_RRL_IND": "U",
}
t = clia.row_to_lab(_CLIA_ROW)
ok(t is not None and t[0] == "01D9999999", "row_to_lab keeps CLIA number")
ok(t[6] == "AL" and t[7] == "35601", "state uppercased, zip trimmed to 5")
ok(t[9] == "2563501234", "phone reduced to 10 digits")
ok(t[14] == 1 and t[16] == "2027-06-01", "active flag + ISO expiry")
ok(t[18] == "CAP" and t[19] == 1500, "accreditor match list + summed volumes")
ok(clia.row_to_lab({"PRVDR_NUM": "", "FAC_NAME": "X"}) is None,
   "rows without CLIA number dropped")
ok(clia.is_chain("QUEST DIAGNOSTICS INC") and clia.is_chain("BioLife Plasma"),
   "chain screen catches Quest + plasma networks")
ok(not clia.is_chain("DECATUR DIAGNOSTIC LAB"), "independents pass the screen")
ok(clia.resolve_fac_types(["independent lab", 14, "21"]) == [15, 14, 21],
   "fac types resolve by name and code")
try:
    clia.resolve_fac_types(["zzz"])
    raise AssertionError("should raise")
except ValueError as e:
    ok("independent lab" in str(e), "unknown fac type error lists options")

labdb = sqlite3.connect(":memory:")
labdb.row_factory = sqlite3.Row
labdb.executescript(_estate.LABSREF_SCHEMA)
def _lab(clia_num, name, state="NY", fac=15, cert=1, active=1, phone="5551234567",
         vol=100, affil=0):
    labdb.execute(clia.INSERT_SQL, (clia_num, name, None, "1 St", None, "City",
                                    state, "10001", None, phone, None, fac,
                                    cert, 4, active, "00" if active else "01",
                                    None, "2001-01-01", None, vol, affil, "U"))
_lab("A1", "SMALL INDIE LAB", vol=900)
_lab("A2", "QUEST DIAGNOSTICS WESTBURY", vol=99999)
_lab("A3", "DEAD LAB", active=0)
_lab("A4", "NO PHONE LAB", phone=None)
_lab("A5", "HOSPITAL LAB", fac=14)
_lab("A6", "WAIVED CORNER CLINIC", cert=2, vol=5)
_lab("A7", "MULTI SITE OPS", affil=9)
rows, warns = clia.query_labs(labdb)
names = {r["name"] for r in rows}
ok("SMALL INDIE LAB" in names and "WAIVED CORNER CLINIC" in names,
   "defaults return active independent labs with phones")
ok("QUEST DIAGNOSTICS WESTBURY" not in names, "default chain screen works")
ok("DEAD LAB" not in names and "NO PHONE LAB" not in names
   and "HOSPITAL LAB" not in names, "active/phone/fac-type defaults hold")
ok(any("chain/franchise" in w for w in warns), "chain exclusion warned")
rows2, _ = clia.query_labs(labdb, exclude_chains=False, require_phone=False,
                           active_only=False, fac_types=[15, 14])
ok(len(rows2) == 7, "filters all relax")
rows3, _ = clia.query_labs(labdb, cert_types=["compliance"])
ok(all(r["cert_type"] == 1 for r in rows3), "cert type name filter")
rows4, _ = clia.query_labs(labdb, max_affiliated_labs=0)
ok(all(r["affiliated_labs"] == 0 for r in rows4), "single-site filter")
ok(rows[0]["test_volume"] >= rows[-1]["test_volume"], "default sort by volume")
ok(rows[0]["phone"].startswith("(") and rows[0]["fac_type_label"] == "independent lab",
   "phone formatted + labels attached")

print("history trim (_trim_history):")
import json as _json
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from salesagent.agent.graph import TRIM_KEEP_RECENT, _trim_history


def _env(n):
    return _json.dumps({"ok": True, "summary": "s", "artifact_id": "art_x",
                        "row_count": n, "sample_rows": [["r"] * 30] * 15,
                        "stats": {"pad": "y" * 900}})


def _ai_tc(tcid):
    return AIMessage(content=[{"type": "thinking", "thinking": "t" * 500},
                              {"type": "text", "text": "calling"}],
                     tool_calls=[{"name": "k12_find_districts",
                                  "args": {}, "id": tcid}])


old = []
for j in range(15):                     # 15 old rounds = 30 msgs before turn 2
    old += [_ai_tc(f"tc{j}"), ToolMessage(content=_env(j), tool_call_id=f"tc{j}",
                                          name="k12_find_districts")]
msgs = [HumanMessage(content="turn 1")] + old + [
    HumanMessage(content="turn 2"), _ai_tc("tcZ"),
    ToolMessage(content=_env(99), tool_call_id="tcZ", name="k12_find_districts")]
out = _trim_history(msgs)
ok(len(out) == len(msgs), "trim never drops messages (pairing intact)")
ok(_json.loads(out[-1].content)["row_count"] == 99
   and "sample_rows" in out[-1].content, "current-turn tool result untouched")
first_tool = next(m for m in out if isinstance(m, ToolMessage))
slim = _json.loads(first_tool.content)
ok("sample_rows" not in slim and slim["artifact_id"] == "art_x"
   and "trimmed" in slim, "old tool result slimmed to summary + artifact_id")
ok(first_tool.tool_call_id == "tc0", "slimmed result keeps tool_call_id")
first_ai = next(m for m in out if isinstance(m, AIMessage))
ok(all(p.get("type") != "thinking" for p in first_ai.content),
   "old AI thinking blocks dropped")
ok(first_ai.tool_calls and first_ai.tool_calls[0]["id"] == "tc0",
   "old AI tool_calls preserved")
last_ai = [m for m in out if isinstance(m, AIMessage)][-1]
ok(any(p.get("type") == "thinking" for p in last_ai.content),
   "current-turn thinking preserved")
grown = _trim_history(msgs + [_ai_tc("tcY"),
                              ToolMessage(content=_env(1), tool_call_id="tcY",
                                          name="k12_find_districts")])
ok([m.content for m in grown[:len(msgs)]] == [m.content for m in out],
   "trim is byte-stable within a turn (cache-safe)")
short = [HumanMessage(content="hi"), AIMessage(content="hello")]
ok(_trim_history(short) == short, "short conversations pass through")

print(f"\nALL {PASS} ASSERTIONS PASSED")
