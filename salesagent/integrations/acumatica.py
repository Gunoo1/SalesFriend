"""Direct read-only Acumatica access (Eisco's ERP) — prices AND stock levels,
no price_comparison VM in the middle.

Everything goes through the OData Generic-Inquiry feed with Basic Auth:
  EC-MCP-ARSalesPrice   price ladder per SKU (SHOPGUEST = public storefront
                        price; T1/T4 managed tiers ride in the same response)
  EC-CurrentInventory   per-warehouse-bin stock rows (QtyOnHand/QtyAvailable,
                        ABC velocity code, snapshot timestamp)

Both GIs live-verified on the PRODUCTION tenant 2026-08-07 (CH0162C: SHOPGUEST
$18.39; stock 3 on hand / 0 available across 3 bins in 01-MAIN). Price recipe
ported from Tim Montondo/price_comparison/backend/vendor/acumatica_prices.py
(itself proven against this tenant's sandbox).

READ-ONLY GUARANTEE: `_readonly_get` refuses any non-OData URL (the
write-capable contract API lives under /entity/ and is never touched) and only
ever issues GET — a structural guarantee this module cannot write to the ERP.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

import requests

from ..settings import Settings

GI_SALES_PRICE = "EC-MCP-ARSalesPrice"
GI_STOCK = "EC-CurrentInventory"
GI_PRODUCTS = "EC-IPProducts"   # webstore feed: title/brand/price/stock/visible
_PRODUCT_SELECT = "InventoryID,title,brand,price,stock_quantity,visible"
TIER_CLASSES = ("T1", "T4")   # managed-customer tiers, free in the same response
_PRICE_SELECT = ("InventoryID,CustomerPriceClass,Price,UOM,BreakQty,"
                 "EffectiveDate,ExpirationDate,Description_3")


class AcumaticaError(RuntimeError):
    pass


def _fnum(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _resolve_current(rows: list[dict], today: str) -> dict | None:
    """The row effective today (open expiration counts), else the latest-dated
    row — annual re-price versions coexist in the GI."""
    if not rows:
        return None

    def active(r):
        eff = (r.get("EffectiveDate") or "0000")[:10]
        exp = (r.get("ExpirationDate") or "9999")[:10]
        return eff <= today <= exp

    pool = [r for r in rows if active(r)] or rows
    return max(pool, key=lambda r: (r.get("EffectiveDate") or ""))


def aggregate_stock(rows: list[dict]) -> list[dict]:
    """Collapse per-bin GI rows into one row per SKU x warehouse. Pure —
    unit-tested. QtyAvailable = on-hand minus allocations (0 available with
    stock on hand means it's all committed)."""
    agg: dict[tuple, dict] = {}
    for r in rows:
        sku = (r.get("InventoryID") or "").strip()
        wh = (r.get("Warehouse") or "").strip()
        a = agg.setdefault((sku, wh), {
            "sku": sku, "warehouse": wh, "qty_on_hand": 0.0,
            "qty_available": 0.0, "bins": 0,
            "abc_code": (r.get("ABCCode") or "").strip(),
            "as_of": (r.get("LastRun") or "").strip()})
        a["qty_on_hand"] += _fnum(r.get("QtyOnHand")) or 0.0
        a["qty_available"] += _fnum(r.get("QtyAvailable")) or 0.0
        a["bins"] += 1
    return list(agg.values())


class Acumatica:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.acumatica_url and s.acumatica_tenant
                    and s.acumatica_username and s.acumatica_password)

    @property
    def _base(self) -> str:
        return (f"{self.settings.acumatica_url}/OData/"
                f"{quote(self.settings.acumatica_tenant)}")

    def _readonly_get(self, gi: str, params: dict) -> list[dict]:
        """The ONLY way this module touches Acumatica: GET on the OData GI
        feed. Never builds an /entity/ (write-capable) URL."""
        url = f"{self._base}/{quote(gi)}"
        if "/OData/" not in url or "/entity/" in url:
            raise AcumaticaError(f"refusing non-OData URL: {url}")
        r = self.session.get(
            url, params={"$format": "json", **params},
            auth=(self.settings.acumatica_username,
                  self.settings.acumatica_password),
            headers={"Accept": "application/json"}, timeout=60)
        if r.status_code == 401:
            raise AcumaticaError("Acumatica rejected the credentials (401)")
        if r.status_code == 403:
            raise AcumaticaError(f"no access to GI '{gi}' (403)")
        if r.status_code != 200:
            raise AcumaticaError(f"Acumatica HTTP {r.status_code}: {r.text[:160]}")
        return r.json().get("value", [])

    # ---- prices -----------------------------------------------------------

    def price(self, sku: str, price_class: str | None = None) -> dict | None:
        """Current price for one SKU in `price_class` (default SHOPGUEST =
        public storefront), plus T1/T4 tiers. None when not found/$0."""
        cls = (price_class or self.settings.acumatica_price_class or
               "SHOPGUEST").strip()
        rows = self._readonly_get(GI_SALES_PRICE, {
            "$select": _PRICE_SELECT,
            "$filter": f"InventoryID eq '{sku}'"})
        today = dt.date.today().isoformat()

        def class_price(c: str):
            rc = [x for x in rows
                  if (x.get("CustomerPriceClass") or "").strip() == c]
            if rc:   # base price = lowest break-qty rung
                minbrk = min(_fnum(x.get("BreakQty")) or 0 for x in rc)
                rc = [x for x in rc if (_fnum(x.get("BreakQty")) or 0) == minbrk]
            r = _resolve_current(rc, today)
            p = _fnum(r.get("Price")) if r else None
            return (r, p) if r and p and p > 0 else (None, None)

        row, price = class_price(cls)
        if not row:
            return None
        return {"sku": sku, "price": price, "price_class": cls,
                "effective_date": (row.get("EffectiveDate") or "")[:10],
                "uom": (row.get("UOM") or "").strip(),
                "title": (row.get("Description_3") or "").strip(),
                "tiers": {c: class_price(c)[1] for c in TIER_CLASSES}}

    # ---- stock ------------------------------------------------------------

    def stock(self, sku: str) -> list[dict]:
        """Per-warehouse stock for one SKU (bins collapsed). Empty list =
        SKU has no inventory rows at all."""
        rows = self._readonly_get(GI_STOCK, {
            "$filter": f"InventoryID eq '{sku}'"})
        return aggregate_stock(rows)

    # ---- webstore products (title search) ----------------------------------

    def products_by_title(self, keyword: str, top: int = 500) -> list[dict]:
        """Visible webstore products whose title contains `keyword`
        (server-side, case-insensitive — tolower(substringof) live-verified
        2026-08-07). Deduped by SKU; stock = webstore stock_quantity."""
        kw = keyword.replace("'", "''").lower()
        rows = self._readonly_get(GI_PRODUCTS, {
            "$select": _PRODUCT_SELECT, "$top": str(int(top)),
            "$filter": f"substringof('{kw}', tolower(title))"})
        out: dict[str, dict] = {}
        for r in rows:
            sku = (r.get("InventoryID") or "").strip()
            if not sku or str(r.get("visible")).upper() not in ("TRUE", "1"):
                continue
            price = _fnum(r.get("price"))
            rec = {"sku": sku, "title": (r.get("title") or "").strip(),
                   "brand": (r.get("brand") or "").strip(),
                   # the feed carries $0 for many items — that's "no webstore
                   # price", not free (authoritative price = price_own)
                   "price": price if price else None,
                   "stock": _fnum(r.get("stock_quantity")) or 0.0}
            old = out.get(sku)
            if old is None or rec["stock"] > old["stock"]:
                out[sku] = rec
        return list(out.values())
