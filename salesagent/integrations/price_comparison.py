"""HTTP client for the EXISTING price_comparison app (Tim Montondo project).
SalesAgent delegates price work to it — no engine fork; browser-vendor
(Chrome) constraints stay in that deployment. Endpoints verified against
price_comparison/backend/routes.py."""
from __future__ import annotations

import requests

from ..settings import Settings

TIMEOUT = 30


class PriceComparison:
    def __init__(self, settings: Settings):
        self.base = settings.price_comparison_url

    @property
    def configured(self) -> bool:
        return bool(self.base)

    def _get(self, path: str, **params):
        r = requests.get(f"{self.base}{path}", params=params or None,
                         timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def health(self) -> dict:
        return self._get("/api/health")

    def own_price(self, sku: str, price_class: str | None = None) -> dict:
        params = {"price_class": price_class} if price_class else {}
        return self._get(f"/api/acumatica/price/{sku}", **params)

    def config_matrix(self) -> dict:
        return self._get("/api/config/matrix")

    def estimate(self, n_skus: int, **toggles) -> dict:
        return self._get("/api/estimate", n_skus=n_skus, **toggles)

    def create_job(self, skus: list[str], vendors: list[str],
                   brands: list[str] | None = None) -> dict:
        options: dict = {"vendors": vendors}
        if brands:
            options["brands"] = brands   # competitor brands for the market grid
        r = requests.post(f"{self.base}/api/jobs",
                          json={"skus": skus, "options": options},
                          timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def job(self, job_id: str) -> dict:
        return self._get(f"/api/jobs/{job_id}")

    def result(self, job_id: str) -> dict:
        r = requests.get(f"{self.base}/api/jobs/{job_id}/result", timeout=120)
        r.raise_for_status()
        return r.json()

    def export_url(self, job_id: str) -> str:
        return f"{self.base}/api/jobs/{job_id}/export.xlsx"
