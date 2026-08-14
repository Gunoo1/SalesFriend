"""Job runners. Each takes a JobCtx, does the slow work with fresh
connections, creates the result artifact itself, and returns artifact_id.
BlockedError semantics: a blocked/failed unit is SKIPPED and logged, never
recorded as a negative result."""
from __future__ import annotations

import re
import time

from ..artifacts import store
from ..db import utcnow
from ..integrations import overpass
from ..tools.geo import _LOC_COLS
from .manager import JobCtx

FASTENAL_ALL = "https://www.fastenal.com/locations/all"


_SECTION_TYPES = {"Corporate Headquarters": "headquarters",
                  "Distribution Centers": "distribution_center",
                  "Branch Locations": "branch"}


def _cells(row_html: str) -> list[str]:
    import html as htmllib
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
    return [htmllib.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells]


def _fastenal_scrape(ctx: JobCtx, states: list[str]) -> list[dict]:
    """Faithful port of fbi/stage1_branches.py parse_locations_all +
    parse_detail_page: h2-delimited sections, 8-cell branch rows / 7-cell
    HQ-DC rows, detail-page googleMaps.lat/longitude inline JS."""
    from curl_cffi import requests as cr
    sess = cr.Session(impersonate="chrome124")
    ctx.log("fetching fastenal.com/locations/all (master list)")
    r = sess.get(FASTENAL_ALL, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"locator page HTTP {r.status_code}")
    rows = []
    for part in re.split(r"<h2>\s*", r.text)[1:]:
        m = re.match(r"(.*?)\s*</h2>(.*)", part, re.S)
        if not m:
            continue
        loc_type = _SECTION_TYPES.get(m.group(1).strip())
        if not loc_type:
            continue
        body = m.group(2).split("</thead>", 1)[-1]
        for row_html in re.findall(r"<tr>\s*(.*?)\s*</tr>", body, re.S):
            c = _cells(row_html)
            href = re.search(r'/locations/details/([^"]+)"', row_html)
            if not href:
                continue
            if len(c) == 8:    # branch: 4code, 5code, addr, city, st, zip, country, phone
                _c4, code5, street, city, state, _zip, country, phone = c
            elif len(c) == 7:  # HQ/DC: 5code, addr, city, st, zip, phone, fax
                code5, street, city, state, _zip, phone, _fax = c
                country = "USA"
            else:
                continue
            state = state.strip().upper()
            if state in states and country in ("USA", "US"):
                rows.append({"code": code5.upper(),
                             "name": f"Fastenal {city}, {state} ({code5.upper()})",
                             "city": city, "state": state, "phone": phone,
                             "street": street, "location_type": loc_type,
                             "url": f"https://www.fastenal.com/locations/"
                                    f"details/{href.group(1)}"})
    ctx.log(f"{len(rows)} locations in {states} from master list")
    out = []
    cap = min(len(rows), 120)
    for i, b in enumerate(rows[:cap]):
        lat = lng = None
        try:
            d = sess.get(b["url"], timeout=45)
            if d.status_code == 200:
                mm = re.search(r"googleMaps\.lat\s*=\s*(-?[\d.]+)", d.text)
                nn = re.search(r"googleMaps\.longitude\s*=\s*(-?[\d.]+)", d.text)
                lat = float(mm.group(1)) if mm else None
                lng = float(nn.group(1)) if nn else None
                if lat == 0.0 and lng == 0.0:  # placeholder coords
                    lat = lng = None
        except Exception as e:
            ctx.log(f"detail {b['code']} skipped ({type(e).__name__}) — "
                    "blocked/err units are skipped, never negatives")
        out.append({**b, "lat": lat, "lng": lng, "provider": "locator_scrape"})
        if (i + 1) % 10 == 0:
            ctx.progress(i + 1, cap, f"detail pages {i + 1}/{cap}")
        time.sleep(0.7)   # locator throttle (fbi lesson: 1-2 req/s)
    for b in rows[cap:]:
        out.append({**b, "lat": None, "lng": None, "provider": "locator_scrape"})
    if len(rows) > cap:
        ctx.log(f"detail-paged first {cap} of {len(rows)}; rest kept w/o lat/lng")
    return out


def _brand_overpass(ctx: JobCtx, company: str, states: list[str]) -> list[dict]:
    from ..tools.geo import state_bbox
    variants = list(dict.fromkeys(
        [company, company.title(), company.upper(), company.capitalize()]))
    out = []
    for i, st in enumerate(states):
        bbox = state_bbox(st)
        if not bbox:
            ctx.log(f"{st}: no bbox — skipped")
            continue
        ctx.progress(i, len(states), f"OSM brand search {st}")
        try:
            pois = overpass.search_brand(bbox, variants)
        except overpass.OverpassError as e:
            ctx.log(f"{st}: {e} — skipped (unknown, not zero)")
            continue
        for p in pois:
            out.append({"code": f"osm-{p['osm_type']}-{p['osm_id']}",
                        "name": p["name"], "city": p.get("city"),
                        "state": p.get("state") or st,
                        "phone": p.get("phone"),
                        "street": " ".join(x for x in
                                           (p.get("housenumber"),
                                            p.get("street")) if x),
                        "lat": p["lat"], "lng": p["lng"],
                        "location_type": "branch",
                        "provider": "osm_brand"})
        ctx.log(f"{st}: {len(pois)} OSM locations")
        time.sleep(12)   # inter-state gap (fbi stage2 pacing)
    return out


def branch_finder(ctx: JobCtx) -> str:
    company = str(ctx.params["company"]).strip()
    states = [s.upper() for s in ctx.params["states"]]
    from ..tools.geo import SCRAPE_DENYLIST
    low = company.lower()
    for dom, why in SCRAPE_DENYLIST.items():
        if dom.split(".")[0] in low:
            ctx.log(f"{why} -> using OSM brand path")
    if low == "fastenal":
        try:
            locations = _fastenal_scrape(ctx, states)
        except Exception as e:
            ctx.log(f"locator scrape failed ({type(e).__name__}: {e}) — "
                    "falling back to OSM brand match")
            locations = _brand_overpass(ctx, company, states)
    else:
        locations = _brand_overpass(ctx, company, states)

    conn = ctx.rw()
    try:
        with conn:
            for b in locations:
                conn.execute(
                    "INSERT OR REPLACE INTO company_locations (company,"
                    " location_id, name, street, city, state, zip, phone, lat,"
                    " lng, location_type, provider, source_url, fetched_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (low, b["code"], b["name"], b.get("street"),
                     b.get("city"), b.get("state"), None, b.get("phone"),
                     b.get("lat"), b.get("lng"), b.get("location_type"),
                     b.get("provider"), b.get("url"), utcnow()))
        rows = [[b["name"], b.get("city"), b.get("state"), b.get("street"),
                 b.get("phone"), b.get("location_type"), b.get("provider"),
                 b.get("lat"), b.get("lng")] for b in locations]
        art = store.create(
            conn, conversation_id=ctx.conversation_id,
            tool="find_company_locations",
            kind="map" if any(b.get("lat") for b in locations) else "table",
            title=f"{company.title()} locations — {', '.join(states)}",
            columns=_LOC_COLS, rows=rows,
            provenance=[{"source": locations[0]["provider"] if locations
                         else "none", "detail": f"{company} in {states}",
                         "url": FASTENAL_ALL if low == "fastenal" else
                         "https://overpass-api.de", "fetched_at": utcnow()}],
            map_spec={"lat": "lat", "lng": "lng", "label": "name",
                      "popup_cols": ["name", "city", "state", "phone"]})
        return art["artifact_id"]
    finally:
        conn.close()


def price_scrape(ctx: JobCtx) -> str:
    """Delegated to the price_comparison app; we mirror progress + build the
    grid artifact from its /result JSON (tolerant flattener)."""
    from ..integrations.price_comparison import PriceComparison
    pc = PriceComparison(ctx.settings)
    if not pc.configured:
        raise RuntimeError("PRICE_COMPARISON_URL not configured")
    skus = ctx.params["skus"]
    vendors = ctx.params.get("vendors") or ["vwr"]
    brands = ctx.params.get("brands")
    job = pc.create_job(skus, vendors, brands=brands)
    remote_id = job["job_id"]
    ctx.external_ref(remote_id)
    ctx.log(f"delegated to price_comparison job {remote_id} "
            f"({job.get('n_skus')} SKUs, vendors={vendors}, "
            f"queue={job.get('queue_position')})")
    while True:
        time.sleep(5)
        snap = pc.job(remote_id)
        status = snap.get("status")
        done = snap.get("done") or snap.get("progress_done") or 0
        total = snap.get("total") or snap.get("progress_total")
        ctx.progress(int(done or 0), int(total) if total else None,
                     f"remote: {status}")
        if status in ("done", "error", "cancelled"):
            break
    if status != "done":
        err = str(snap.get("error") or "")
        if "nodriver" in err.lower():
            # browser stack missing on the deployment — seen live 2026-08-06.
            # market has a browserless serper path: it activates when
            # SERPER_API_KEY is set in THAT app's env (fisher/amazon are
            # browser-only regardless).
            raise RuntimeError(
                "the price app fell back to its browser scraper and that "
                "deployment has no Chrome/nodriver. vendors=['vwr'] works "
                "now; 'market' works browserless once SERPER_API_KEY is "
                "added to the price app's env (container recreate). "
                "fisher/amazon are browser-only — skip them.")
        raise RuntimeError(f"remote job ended {status}: {err}")
    result = pc.result(remote_id)
    cols, rows, warnings = _price_grid(result)
    conn = ctx.rw()
    try:
        art = store.create(
            conn, conversation_id=ctx.conversation_id, tool="price_scrape",
            kind="table",
            title=f"Price scrape — {len(skus)} SKUs ({', '.join(vendors)})",
            columns=cols, rows=rows,
            provenance=[{"source": "price_comparison engine",
                         "detail": f"remote job {remote_id}; flagged-match "
                                   f"detail + xlsx: {pc.export_url(remote_id)}",
                         "url": pc.export_url(remote_id),
                         "fetched_at": utcnow()}]
            + [{"source": "price grid caveat", "detail": w,
                "fetched_at": utcnow()} for w in warnings])
        if warnings:
            ctx.log("; ".join(warnings))
        return art["artifact_id"]
    finally:
        conn.close()


# A competitor price this far off Eisco's is almost always a pack-size /
# unit-of-measure mismatch in the match, not a real market gap (the scraper
# doesn't normalize pack sizes). Suspect cells are excluded from the grid's
# cheapest/gap math but surfaced with detail so the rep can verify.
PLAUSIBLE_HI = 4.0    # competitor more than 4x Eisco -> suspect
PLAUSIBLE_LO = 0.25   # competitor less than 1/4 of Eisco -> suspect


def price_plausible(eisco, other, hi: float = PLAUSIBLE_HI,
                    lo: float = PLAUSIBLE_LO) -> bool:
    """Pure — unit-tested. Unjudgeable (missing/zero) prices pass through."""
    try:
        e, o = float(eisco), float(other)
    except (TypeError, ValueError):
        return True
    if e <= 0 or o <= 0:
        return True
    return lo <= (o / e) <= hi


def _price_grid(result: dict) -> tuple[list[dict], list[list], list[str]]:
    """Per-SKU price grid from the price_comparison /result JSON.
    Shape live-verified 2026-08-06: meta.cells = [{key,label,...}] defines the
    brand@store grid; scan_view = one dict per SKU with eisco{total,trust,url},
    cells{key -> null | {total,conf,flags[]}}, cheapest/gap/decision_ready.
    Low-confidence competitor matches are blanked (flags carry 'low_conf') —
    the remote app's own export keeps the flagged detail."""
    meta = result.get("meta") or {}
    scan = result.get("scan_view") or []
    if not scan:                      # unknown shape — tolerant fallback
        def biggest_list(obj, depth=0):
            best = None
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                best = obj
            elif isinstance(obj, dict) and depth < 3:
                for v in obj.values():
                    cand = biggest_list(v, depth + 1)
                    if cand is not None and (best is None or
                                             len(cand) > len(best)):
                        best = cand
            return best
        items = biggest_list(result) or []
        keys: list[str] = []
        for it in items[:50]:
            for k, v in it.items():
                if k not in keys and not isinstance(v, (dict, list)):
                    keys.append(k)
        keys = keys[:24]
        return ([{"key": k, "label": k, "type": "string"} for k in keys],
                [[it.get(k) for k in keys] for it in items],
                ["result had no scan_view — generic flatten used"])

    cell_defs = (meta.get("cells") or [])[:10]
    # every price column carries link_col -> a hidden sibling column holding
    # the listing URL; the frontend renders the price as a link (and a bare
    # "verify" link when the price was blanked), Excel writes hyperlinks
    cols = ([{"key": "sku", "label": "SKU", "type": "string"},
             {"key": "title", "label": "Product", "type": "string"},
             {"key": "eisco", "label": "Eisco $", "type": "number",
              "format": "money", "link_col": "eisco__url"},
             {"key": "trust", "label": "Match", "type": "string"}]
            + [{"key": c["key"], "label": c.get("label") or c["key"],
                "type": "number", "format": "money",
                "link_col": f"{c['key']}__url"} for c in cell_defs]
            + [{"key": "cheapest", "label": "Cheapest", "type": "string"},
               {"key": "gap", "label": "Gap $", "type": "number",
                "format": "money"},
               {"key": "gap_pct", "label": "Gap %", "type": "number"},
               {"key": "ready", "label": "Decision-ready", "type": "string"},
               {"key": "suspect", "label": "Filtered (verify)", "type": "string"},
               {"key": "eisco__url", "label": "Eisco URL", "type": "string",
                "hidden": True}]
            + [{"key": f"{c['key']}__url", "label": f"{c.get('label') or c['key']} URL",
                "type": "string", "hidden": True} for c in cell_defs])
    rows, low_conf, implausible = [], 0, 0
    for sv in scan:
        eisco = sv.get("eisco") or {}
        cells = sv.get("cells") or {}
        vals, urls, suspects = [], [], []
        for c in cell_defs:
            cell = cells.get(c["key"])
            urls.append((cell or {}).get("url"))
            if not cell:
                vals.append(None)
            elif "low_conf" in (cell.get("flags") or []):
                low_conf += 1
                vals.append(None)
            elif not price_plausible(eisco.get("total"), cell.get("total")):
                implausible += 1
                ratio = float(cell["total"]) / float(eisco["total"])
                suspects.append(
                    f"{c.get('label') or c['key']} ${cell['total']:g} "
                    f"({ratio:.1f}x Eisco — probably a pack-size mismatch, "
                    "not a real gap"
                    + (f"; verify: {cell['url']}" if cell.get("url") else "")
                    + ")")
                vals.append(None)
            else:
                vals.append(cell.get("total"))
        if suspects:
            # the remote's cheapest/gap/decision math included the implausible
            # cell — recompute locally from surviving cells only
            surv = [(c["key"], v) for c, v in zip(cell_defs, vals)
                    if v is not None]
            e = eisco.get("total")
            if surv and e:
                ck, cv = min(surv, key=lambda kv: kv[1])
                cheapest, gap = ck, round(float(e) - float(cv), 2)
                gap_pct = round(100 * gap / float(e), 1) if float(e) else None
            else:
                cheapest = gap = gap_pct = None
            ready = "no"
        else:
            cheapest, gap, gap_pct = (sv.get("cheapest"), sv.get("gap"),
                                      sv.get("gap_pct"))
            ready = "yes" if sv.get("decision_ready") else "no"
        rows.append([sv.get("sku"), sv.get("title") or eisco.get("matched"),
                     eisco.get("total"), eisco.get("trust"), *vals,
                     cheapest, gap, gap_pct, ready,
                     "; ".join(suspects), eisco.get("url"), *urls])
    warnings = []
    if low_conf:
        warnings.append(f"{low_conf} low-confidence competitor matches "
                        "blanked (remote export keeps flagged detail)")
    if implausible:
        warnings.append(
            f"{implausible} competitor prices filtered as implausible "
            f"(> {PLAUSIBLE_HI:g}x or < {PLAUSIBLE_LO:g}x of Eisco — usually "
            "pack-size mismatches; see the 'Filtered (verify)' column, and "
            "the remote export keeps full match detail)")
    if meta.get("n_skipped"):
        warnings.append(f"{meta['n_skipped']} SKUs skipped by the engine")
    if meta.get("n_discontinued"):
        warnings.append(f"{meta['n_discontinued']} SKUs discontinued")
    return cols, rows, warnings


def verify_status_bulk(ctx: JobCtx) -> str:
    """>50-target open/closed verification, reusing the sync tool's logic
    through a ToolContext shim."""
    from ..tools.geo import verify_business_status
    from ..tools.registry import ToolContext
    targets = ctx.params["targets"]
    shim = ToolContext(ctx.settings, {"id": ctx.user_id, "username": "job"},
                       ctx.conversation_id, lambda e, d: None)
    all_rows: list[list] = []
    cols = None
    try:
        for i in range(0, len(targets), 50):
            batch = targets[i:i + 50]
            ctx.progress(i, len(targets), f"verifying {i}-{i + len(batch)}")
            env = verify_business_status(shim, targets=batch)
            if not env.get("ok"):
                raise RuntimeError(env.get("summary"))
            spec = store.get(shim.rw(), env["artifact_id"])
            cols = spec["columns"]
            all_rows.extend(spec["rows"])
        art = store.create(
            shim.rw(), conversation_id=ctx.conversation_id,
            tool="verify_business_status", kind="table",
            title=f"Open/closed — {len(all_rows)} locations",
            columns=cols or [], rows=all_rows,
            provenance=[{"source": "Serper Maps+KG (bulk job)",
                         "detail": f"{len(targets)} targets",
                         "fetched_at": utcnow()}])
        return art["artifact_id"]
    finally:
        shim.close()


from .k12_build import k12_build  # noqa: E402  (runner lives in its own module)

from .labs_build import labs_build  # noqa: E402  (runner lives in its own module)

RUNNERS = {
    "branch_finder": branch_finder,
    "price_scrape": price_scrape,
    "verify_status_bulk": verify_status_bulk,
    "k12_build": k12_build,
    "labs_build": labs_build,
}
