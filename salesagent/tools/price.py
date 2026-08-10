"""Price + stock tools. price_own and sku_stock hit Acumatica DIRECTLY
(read-only OData) when ACUMATICA_* creds are configured — no dependency on
the price_comparison VM; price_own falls back to that app's proxy otherwise.
price_scrape = full brand x store scrape job, always delegated."""
from __future__ import annotations

from ..artifacts import store as artifact_store
from ..integrations.acumatica import Acumatica, AcumaticaError
from ..integrations.price_comparison import PriceComparison
from .envelope import envelope, error_envelope, prov, table_envelope
from .registry import CostClass, tool_spec


def _resolve_skus(ctx, skus, artifact_id, sku_column) -> list[str] | dict:
    """SKUs come either inline or from an artifact column (uploads) —
    resolved server-side, never through the model's context."""
    if skus:
        return [str(s).strip() for s in skus if str(s).strip()]
    if artifact_id:
        spec = artifact_store.get(ctx.rw(), artifact_id)
        if not spec or spec.get("conversation_id") != ctx.conversation_id:
            return error_envelope(f"artifact {artifact_id} not found here")
        keys = [c["key"] for c in spec["columns"]]
        col = sku_column or ("sku" if "sku" in [k.lower() for k in keys] else None)
        idx = None
        for i, k in enumerate(keys):
            if col and k.lower() == col.lower():
                idx = i
                break
        if idx is None:
            return error_envelope(
                f"no SKU column '{sku_column}' — columns: {keys}")
        return [str(r[idx]).strip() for r in spec["rows"]
                if r[idx] not in (None, "")]
    return error_envelope("pass skus[] or artifact_id + sku_column")


@tool_spec(
    name="price_own",
    description=(
        "Instant authoritative Eisco price for SKUs (SHOPGUEST public price "
        "class + T1/T4 managed tiers) via a DIRECT read-only Acumatica "
        "connection (works even when the price VM is down); falls back to the "
        "price_comparison service if direct creds are absent. Up to 50 SKUs; "
        "not-found rows kept so gaps are visible. Free."),
    input_schema={"properties": {
        "skus": {"type": "array", "items": {"type": "string"}},
        "artifact_id": {"type": "string",
                        "description": "read SKUs from an artifact (e.g. an upload)"},
        "sku_column": {"type": "string"},
        "price_class": {"type": "string"},
    }},
    cost_class=CostClass.FREE,
)
def price_own(ctx, skus: list[str] | None = None, artifact_id: str | None = None,
              sku_column: str | None = None,
              price_class: str | None = None) -> dict:
    resolved = _resolve_skus(ctx, skus, artifact_id, sku_column)
    if isinstance(resolved, dict):   # error envelope
        return resolved
    if len(resolved) > 50:
        return error_envelope(
            f"{len(resolved)} SKUs is too many for instant pricing (max 50) — "
            "use price_scrape for bulk runs.")

    acu = Acumatica(ctx.settings)
    if acu.configured:
        return _price_own_direct(ctx, acu, resolved, price_class)
    return _price_own_via_vm(ctx, resolved, price_class)


def _price_own_direct(ctx, acu: Acumatica, resolved: list[str],
                      price_class: str | None) -> dict:
    rows, errors = [], 0
    for sku in resolved:
        try:
            rec = acu.price(sku, price_class)
        except AcumaticaError as e:
            if not rows and not errors:   # first call failing = systemic
                return error_envelope(str(e), error_type="AcumaticaError")
            errors += 1
            rows.append([sku, None, None, None, None, None,
                         f"error: {str(e)[:60]}"])
            continue
        if rec:
            tiers = rec.get("tiers") or {}
            rows.append([sku, rec["price"], rec["price_class"], rec["uom"],
                         tiers.get("T1"), tiers.get("T4"),
                         rec.get("title") or ""])
        else:
            rows.append([sku, None, None, None, None, None,
                         "not found in price class"])
    cols = [{"key": "sku", "label": "SKU", "type": "string"},
            {"key": "price", "label": "Eisco price", "type": "number", "format": "money"},
            {"key": "price_class", "label": "Class", "type": "string"},
            {"key": "uom", "label": "UOM", "type": "string"},
            {"key": "t1", "label": "T1 (best tier)", "type": "number", "format": "money"},
            {"key": "t4", "label": "T4", "type": "number", "format": "money"},
            {"key": "note", "label": "Product / note", "type": "string"}]
    found = sum(1 for r in rows if r[1] is not None)
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="price_own", title=f"Eisco prices ({found}/{len(rows)} found)",
        columns=cols, rows=rows,
        provenance=[prov("Acumatica (read-only OData, direct)",
                         "EC-MCP-ARSalesPrice; SHOPGUEST + T1/T4 tiers")],
        summary=f"{found}/{len(rows)} SKUs priced (direct Acumatica; T1/T4 "
                "managed tiers included).",
        warnings=[] if found else
        ["No SKUs found — check the SKU format (raw Eisco catalog numbers)."])


def _price_own_via_vm(ctx, resolved: list[str], price_class: str | None) -> dict:
    pc = PriceComparison(ctx.settings)
    if not pc.configured:
        return error_envelope(
            "no Acumatica access configured: set ACUMATICA_URL/TENANT/USERNAME/"
            "PASSWORD in .env (direct) or PRICE_COMPARISON_URL (proxy)",
            error_type="NotConfigured")
    # the remote deployment may not have Acumatica credentials at all —
    # say that ONCE instead of emitting a bare HTTPError per SKU
    try:
        if not (pc.health().get("acumatica_enabled", True)):
            return error_envelope(
                "the price_comparison deployment at "
                f"{ctx.settings.price_comparison_url} has Acumatica DISABLED "
                "(no credentials configured there) — price_own can't work "
                "until that app gets its Acumatica env. price_scrape (VWR/"
                "Amazon/market shelves) still works.",
                error_type="RemoteNotConfigured")
    except Exception:
        pass  # health probe failure -> let per-SKU calls speak
    rows = []
    for sku in resolved:
        try:
            rec = pc.own_price(sku, price_class)
        except Exception as e:
            rows.append([sku, None, None, f"error: {type(e).__name__}"])
            continue
        if rec.get("found"):
            rows.append([sku, rec.get("price"), rec.get("price_class"),
                         rec.get("description") or ""])
        else:
            rows.append([sku, None, None, "not found in price class"])
    cols = [{"key": "sku", "label": "SKU", "type": "string"},
            {"key": "price", "label": "Eisco price", "type": "number", "format": "money"},
            {"key": "price_class", "label": "Class", "type": "string"},
            {"key": "note", "label": "Note", "type": "string"}]
    found = sum(1 for r in rows if r[1] is not None)
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="price_own", title=f"Eisco prices ({found}/{len(rows)} found)",
        columns=cols, rows=rows,
        provenance=[prov("Acumatica (read-only OData via price_comparison)",
                         "EC-MCP-ARSalesPrice, SHOPGUEST class")],
        summary=f"{found}/{len(rows)} SKUs priced.",
        warnings=[] if found else
        ["No SKUs found — check the SKU format (raw Eisco catalog numbers)."])


@tool_spec(
    name="sku_stock",
    description=(
        "LIVE Eisco warehouse stock for SKUs, direct from Acumatica "
        "(read-only): qty on hand, qty AVAILABLE (on-hand minus allocations — "
        "0 available with stock on hand means it's all committed to orders), "
        "bin count, ABC velocity class (A=fast mover … D=slow), snapshot "
        "time. Check before promising delivery or pushing a SKU in a pitch. "
        "Up to 100 SKUs, inline or from an artifact column. Free."),
    input_schema={"properties": {
        "skus": {"type": "array", "items": {"type": "string"}},
        "artifact_id": {"type": "string"},
        "sku_column": {"type": "string"},
    }},
    cost_class=CostClass.FREE,
)
def sku_stock(ctx, skus: list[str] | None = None, artifact_id: str | None = None,
              sku_column: str | None = None) -> dict:
    acu = Acumatica(ctx.settings)
    if not acu.configured:
        return error_envelope(
            "direct Acumatica not configured (ACUMATICA_URL/TENANT/USERNAME/"
            "PASSWORD in .env)", error_type="NotConfigured")
    resolved = _resolve_skus(ctx, skus, artifact_id, sku_column)
    if isinstance(resolved, dict):
        return resolved
    if len(resolved) > 100:
        return error_envelope(f"{len(resolved)} SKUs is too many (max 100)")
    rows, no_rows, allocated = [], 0, 0
    for i, sku in enumerate(resolved):
        try:
            whs = acu.stock(sku)
        except AcumaticaError as e:
            if i == 0:   # first call failing = systemic (auth / GI access)
                return error_envelope(str(e), error_type="AcumaticaError")
            rows.append([sku, None, None, None, None, None,
                         f"error: {str(e)[:60]}"])
            continue
        if not whs:
            no_rows += 1
            rows.append([sku, None, None, None, None, None,
                         "no inventory rows (unknown SKU?)"])
            continue
        for w in whs:
            if w["qty_on_hand"] > 0 and w["qty_available"] <= 0:
                allocated += 1
            rows.append([w["sku"], w["warehouse"], w["qty_on_hand"],
                         w["qty_available"], w["bins"],
                         w["abc_code"], w["as_of"]])
    cols = [{"key": "sku", "label": "SKU", "type": "string"},
            {"key": "warehouse", "label": "Warehouse", "type": "string"},
            {"key": "qty_on_hand", "label": "On hand", "type": "number"},
            {"key": "qty_available", "label": "Available", "type": "number"},
            {"key": "bins", "label": "Bins", "type": "number", "format": "int"},
            {"key": "abc_code", "label": "ABC", "type": "string"},
            {"key": "as_of", "label": "As of / note", "type": "string"}]
    in_stock = len({r[0] for r in rows if r[3] is not None and r[3] > 0})
    warnings = []
    if allocated:
        warnings.append(f"{allocated} SKU-warehouse rows have stock on hand "
                        "but 0 available (fully committed to orders)")
    if no_rows:
        warnings.append(f"{no_rows} SKUs had no inventory rows at all")
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="sku_stock",
        title=f"Stock levels — {len(resolved)} SKUs",
        columns=cols, rows=rows,
        provenance=[prov("Acumatica (read-only OData, direct)",
                         "EC-CurrentInventory; Available = on-hand minus "
                         "allocations; ABC = velocity class")],
        summary=f"{in_stock}/{len(resolved)} SKUs have available stock. "
                + " ".join(warnings),
        warnings=warnings)


@tool_spec(
    name="price_scrape",
    description=(
        "Competitor price comparison for a SKU list via the price_comparison "
        "engine. PREFERRED vendors: vwr (curl-based, always safe) and market "
        "(Google Shopping via the serper API — browserless; also the one that "
        "compares COMPETITOR BRANDS: pass brands=[...] e.g. Corning, DWK, "
        "Fisherbrand to build the brand x store grid). fisher/amazon need a "
        "Chrome worker the deployment usually lacks — avoid unless asked. "
        "Runs as a background job (minutes); result grid carries trust flags "
        "and implausible-gap filtering. Slow job."),
    input_schema={"properties": {
        "skus": {"type": "array", "items": {"type": "string"}},
        "artifact_id": {"type": "string"},
        "sku_column": {"type": "string"},
        "vendors": {"type": "array",
                    "items": {"type": "string",
                              "enum": ["vwr", "fisher", "amazon", "market"]},
                    "default": ["vwr", "market"]},
        "brands": {"type": "array", "items": {"type": "string"},
                   "description": "competitor brands for the market grid "
                                  "(default = the engine's configured set)"},
    }},
    cost_class=CostClass.SLOW_JOB,
)
def price_scrape(ctx, skus: list[str] | None = None,
                 artifact_id: str | None = None, sku_column: str | None = None,
                 vendors: list[str] | None = None,
                 brands: list[str] | None = None) -> dict:
    pc = PriceComparison(ctx.settings)
    if not pc.configured:
        return error_envelope(
            "price_comparison service not configured (set PRICE_COMPARISON_URL)",
            error_type="NotConfigured")
    resolved = _resolve_skus(ctx, skus, artifact_id, sku_column)
    if isinstance(resolved, dict):
        return resolved
    vendors = [v for v in (vendors or ["vwr", "market"])
               if v in ("vwr", "fisher", "amazon", "market")] or ["vwr"]
    warnings = []
    try:
        matrix = pc.config_matrix()
        avail = {v["key"]: v["available"] for v in matrix.get("vendors", [])}
        dropped = [v for v in vendors if not avail.get(v, v == "vwr")]
        if dropped:
            warnings.append(f"vendors unavailable on that deployment "
                            f"(no Chrome worker): {dropped} — dropped")
            vendors = [v for v in vendors if v not in dropped] or ["vwr"]
        est = pc.estimate(len(resolved),
                          include_fisher="fisher" in vendors,
                          include_market="market" in vendors,
                          include_amazon="amazon" in vendors)
        if est.get("eta_seconds"):
            warnings.append(f"remote ETA ~{est['eta_seconds'] // 60} min"
                            + (" (upper bound)" if est.get("upper_bound") else ""))
    except Exception as e:
        warnings.append(f"preflight failed ({type(e).__name__}) — submitting anyway")

    from ..jobs.manager import JobManager
    job_id = JobManager.get(ctx.settings).submit(
        "price_scrape", {"skus": resolved, "vendors": vendors,
                         "brands": brands or None},
        conversation_id=ctx.conversation_id, user_id=ctx.user.get("id"),
        tool_name="price_scrape")
    ctx.emit("job_started", {"job_id": job_id, "tool": "price_scrape",
                             "title": f"{len(resolved)} SKUs on "
                                      f"{', '.join(vendors)}"})
    return envelope(
        kind="job_ref", job_id=job_id,
        summary=f"price scrape job {job_id} started: {len(resolved)} SKUs on "
                f"{', '.join(vendors)}. " + " ".join(warnings),
        provenance=[])
