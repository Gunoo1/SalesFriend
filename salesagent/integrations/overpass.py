"""Overpass (OpenStreetMap) client — guardrails carried from
FastenalBranchIntel/fbi/places/overpass.py, learned live:
- 406 for browser User-Agents: identify as a tool with a contact address
- name-REGEX queries time out at every batching level: tag/exact-value only
- 429 = wait 20s, do NOT rotate to the slow mirror
- an HTTP 200 can still contain {"remark": "...timed out..."} — check it
- chemical is badly undertagged in OSM: counts are a floor, not a census
"""
from __future__ import annotations

import time

import requests

ENDPOINT = "https://overpass-api.de/api/interpreter"
UA = {"User-Agent": "SalesAgent/1.0 (Enalas; gunoo.shin@enalasconsulting.com)"}

CATEGORY_CLAUSES = {
    "academic": ['nwr["amenity"~"^(university|college|research_institute)$"]'],
    "lab": ['nwr["healthcare"="laboratory"]'],
    "chemical": ['nwr["industrial"="chemical"]'],
    "research_office": ['nwr["office"="research"]'],
}


class OverpassError(RuntimeError):
    pass


def _post(query: str, timeout: int = 120, attempts: int = 4) -> dict:
    for attempt in range(attempts):
        r = requests.post(ENDPOINT, data={"data": query}, headers=UA,
                          timeout=timeout)
        if r.status_code == 429:
            time.sleep(20)
            continue
        if r.status_code >= 500:
            time.sleep(min(90, 5 * 2 ** attempt))
            continue
        if r.status_code != 200:
            raise OverpassError(f"overpass HTTP {r.status_code}")
        data = r.json()
        remark = str(data.get("remark") or "")
        if "timed out" in remark.lower():
            raise OverpassError(f"overpass query timed out server-side: {remark[:120]}")
        return data
    raise OverpassError("overpass retries exhausted")


def _elements_to_pois(data: dict) -> list[dict]:
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lng = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lng is None:
            continue
        out.append({"name": tags.get("name") or tags.get("brand") or "(unnamed)",
                    "lat": lat, "lng": lng,
                    "city": tags.get("addr:city"),
                    "street": tags.get("addr:street"),
                    "housenumber": tags.get("addr:housenumber"),
                    "state": tags.get("addr:state"),
                    "phone": tags.get("phone") or tags.get("contact:phone"),
                    "website": tags.get("website") or tags.get("contact:website"),
                    "osm_type": el.get("type"), "osm_id": el.get("id"),
                    "tags": {k: v for k, v in tags.items()
                             if k in ("amenity", "healthcare", "industrial",
                                      "office", "shop", "brand", "operator")}})
    # dedupe by (name, rounded coords)
    seen = set()
    uniq = []
    for p in out:
        k = (p["name"].lower(), round(p["lat"], 4), round(p["lng"], 4))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def search_categories(bbox: tuple[float, float, float, float],
                      categories: list[str], timeout_s: int = 90) -> list[dict]:
    s, w, n, e = bbox
    clauses = []
    for cat in categories:
        clauses += CATEGORY_CLAUSES.get(cat, [])
    if not clauses:
        raise OverpassError(f"no clauses for categories {categories}")
    body = "".join(f"{c}({s},{w},{n},{e});" for c in clauses)
    query = f"[out:json][timeout:{timeout_s}];({body});out center tags;"
    return _elements_to_pois(_post(query, timeout=timeout_s + 30))


def search_brand(bbox: tuple[float, float, float, float], brand_variants:
                 list[str], timeout_s: int = 90) -> list[dict]:
    """EXACT-value brand/name/operator match (indexed & fast) — never regex
    (the proven timeout). Try each capitalization variant the caller gives."""
    s, w, n, e = bbox
    body = ""
    for v in brand_variants:
        vq = v.replace('"', "")
        for tag in ("brand", "name", "operator"):
            body += f'nwr["{tag}"="{vq}"]({s},{w},{n},{e});'
    query = f"[out:json][timeout:{timeout_s}];({body});out center tags;"
    return _elements_to_pois(_post(query, timeout=timeout_s + 30))
