"""Seamless.ai client — the one justified port (plan §consuming-existing-code).

Base = K12Intel k12/seamless.py (Token auth, 429/422/401 handling, credits
header, async research poll — wire-proven), merged with FastenalBranchIntel
fbi/seamless.py safety mechanisms. Ledger writes go to app.db (org-shared).
Method semantics kept aligned with the k12 variant so fixes port both ways.

Hard-won API facts carried forward (learned with real credits — do not relearn):
- search costs ~1 credit per 10 rows returned; empty searches ~0
- POST /search/contacts returns NO email/phone — research (1 credit/contact)
  reveals them; GET /contacts re-fetches already-researched contacts FREE
- the ONLY working geo filter is locationType="contact" + contactState with
  FULL state names; contactZipCode is null across the index (a zip filter
  excludes nobody and once burned ~178 credits/call)
- locationType="company" resolves multi-branch companies to HQ — useless
- NCES-style abbreviations miss matches (IL: 33/58 districts) → name_variants
"""
from __future__ import annotations

import re
import sqlite3
import time

import requests

from ..db import utcnow
from ..settings import Settings

BASE = "https://api.seamless.ai/api/client/v1"

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# NCES writes "Maine Township HSD 207"; Seamless indexes the expanded legal
# name. Ported from k12/seamless.py _ABBREV.
_ABBREV = [
    (re.compile(r"\bHSD\b", re.I), "High School District"),
    (re.compile(r"\bUSD\b", re.I), "Unified School District"),
    (re.compile(r"\bCUSD\b", re.I), "Community Unit School District"),
    (re.compile(r"\bCCSD\b", re.I), "Consolidated Community School District"),
    (re.compile(r"\bSD\b", re.I), "School District"),
    (re.compile(r"\bTwp\b", re.I), "Township"),
    (re.compile(r"\bDist\b", re.I), "District"),
    (re.compile(r"\bElem\b", re.I), "Elementary"),
    (re.compile(r"\bCo\b", re.I), "County"),
    (re.compile(r"\bPblc\b", re.I), "Public"),
    (re.compile(r"\bSchs\b", re.I), "Schools"),
]


def expand_name(name: str) -> str:
    out = name
    for rx, full in _ABBREV:
        out = rx.sub(full, out)
    return re.sub(r"\s+", " ", out).strip()


def name_variants(name: str, city: str | None = None) -> list[str]:
    """Orderered candidate company names for a Seamless search."""
    out = [name]
    expanded = expand_name(name)
    if expanded.lower() != name.lower():
        out.append(expanded)
    if city:
        out += [f"{city} Board of Education", f"{city} Public Schools"]
    seen: set[str] = set()
    uniq = []
    for n in out:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(n)
    return uniq


class SeamlessError(RuntimeError):
    pass


class BudgetExhausted(SeamlessError):
    pass


class SeamlessClient:
    def __init__(self, settings: Settings, conn: sqlite3.Connection,
                 user_id: int | None = None, conversation_id: str | None = None):
        self.settings = settings
        self.conn = conn                      # app.db (ledger lives here)
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.dry_run = settings.seamless_dry_run
        self.credits_remaining: int | None = None
        self.session = requests.Session()
        self.session.headers.update({"Token": settings.seamless_api_key,
                                     "Content-Type": "application/json"})

    # ---- rails -------------------------------------------------------------

    def spent_today(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(credits_spent),0) AS s FROM seamless_ledger "
            "WHERE ts >= date('now')").fetchone()
        return int(row["s"])

    def check_day_cap(self, about_to_spend: int = 0) -> None:
        cap = self.settings.seamless_day_cap
        if self.spent_today() + about_to_spend > cap:
            raise BudgetExhausted(
                f"org daily Seamless budget exhausted ({self.spent_today()}/"
                f"{cap} credits today; resets at midnight UTC)")

    def _ledger(self, action: str, target: str, credits_spent: int,
                status: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO seamless_ledger (ts, user_id, conversation_id,"
                " action, target, credits_spent, credits_remaining,"
                " result_status) VALUES (?,?,?,?,?,?,?,?)",
                (utcnow(), self.user_id, self.conversation_id, action,
                 target[:200], credits_spent, self.credits_remaining, status))

    # ---- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body: dict | None = None,
                 params: dict | None = None, attempts: int = 4) -> dict:
        url = f"{BASE}{path}"
        for attempt in range(attempts):
            r = self.session.request(method, url, json=json_body,
                                     params=params, timeout=60)
            hdr = r.headers.get("X-PublicAPI-Credits")
            if hdr is not None:
                try:
                    self.credits_remaining = int(float(hdr))
                except ValueError:
                    pass
            if r.status_code == 429:
                reset = r.headers.get("X-RateLimit-Reset")
                try:
                    wait = min(max(5, int(float(reset)) - int(time.time())), 90)
                except (TypeError, ValueError):
                    wait = 15
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 422:
                raise SeamlessError("Seamless says insufficient credits/license (422)")
            if r.status_code == 401:
                raise SeamlessError("Seamless API key rejected (401)")
            if r.status_code not in (200, 202):
                raise SeamlessError(f"Seamless HTTP {r.status_code}: {r.text[:200]}")
            try:
                return r.json()
            except ValueError:
                return {}
        raise SeamlessError("Seamless retries exhausted")

    # ---- operations --------------------------------------------------------

    def search(self, company: str, state: str, titles: list[str] | None = None,
               search_type: str = "exact", limit: int = 10) -> list[dict]:
        """Preview people (~1 credit/10 rows). No email/phone in results."""
        state_name = STATE_NAMES.get(state.upper())
        if not state_name:
            raise SeamlessError(f"unknown state '{state}'")
        if self.dry_run:
            self._ledger("search", f"[dry] {company} {state}", 0, "dry_run")
            return [{"name": f"Dry Run Contact {i+1}", "title": t,
                     "company": company, "city": "Testville", "state": state,
                     "searchResultId": f"dry-{company[:8]}-{i}"}
                    for i, t in enumerate((titles or ["Purchasing Manager"])[:3])]
        self.check_day_cap(about_to_spend=max(1, limit // 10))
        before = self.credits_remaining
        body: dict = {"companyName": [company],
                      "companyNameSearchType": search_type,
                      "contactCountry": ["United States"],
                      "contactState": [state_name],
                      "locationType": "contact",
                      "limit": int(limit)}
        if titles:
            body["jobTitle"] = titles
        data = self._request("POST", "/search/contacts", json_body=body)
        rows = data.get("data") or []
        spent = 0
        if before is not None and self.credits_remaining is not None:
            spent = max(0, before - self.credits_remaining)
        self._ledger("search", f"{company} {state} ({len(rows)} rows)",
                     spent, "ok")
        return rows

    def research(self, search_result_ids: list[str]) -> dict[str, dict | None]:
        """1 credit per contact; async — poll until terminal. Returns
        {search_result_id: contact | None}."""
        if self.dry_run:
            self._ledger("research", f"[dry] {len(search_result_ids)} ids",
                         0, "dry_run")
            return {i: {"fullName": f"Dry Contact {n+1}",
                        "title": "Purchasing Manager",
                        "email1": f"dry{n+1}@example.org",
                        "email1EmailAI": "valid", "email1TotalAI": 93,
                        "contactPhone1": "555-0100"}
                    for n, i in enumerate(search_result_ids)}
        self.check_day_cap(about_to_spend=len(search_result_ids))
        before = self.credits_remaining
        resp = self._request("POST", "/contacts/research",
                             json_body={"searchResultIds": search_result_ids})
        request_ids = resp.get("requestIds") or []
        out: dict[str, dict | None] = {i: None for i in search_result_ids}
        if request_ids:
            deadline = time.time() + 120
            pending = list(request_ids)
            while pending and time.time() < deadline:
                time.sleep(4)
                poll = self._request("GET", "/contacts/research/poll",
                                     params={"requestIds": ",".join(pending)})
                items = poll.get("data") or []
                still = []
                for item in items:
                    status = str(item.get("status") or "").lower()
                    rid = item.get("requestId") or item.get("id")
                    srid = item.get("searchResultId") or rid
                    if status in ("done", "duplicate",
                                  "contact-already-researched"):
                        out[srid] = item.get("contact") or item
                    elif status in ("missing", "error", "not found"):
                        out[srid] = None
                    else:
                        if rid:
                            still.append(str(rid))
                pending = still
        spent = 0
        if before is not None and self.credits_remaining is not None:
            spent = max(0, before - self.credits_remaining)
        got = sum(1 for v in out.values() if v)
        self._ledger("research", f"{len(search_result_ids)} ids -> {got} contacts",
                     spent, "ok")
        return out

    def prime_credits(self) -> int | None:
        """Free call that populates the credits header. GET /contacts requires
        ISO8601 startDate/endDate (400 without them — live-verified 2026-08-07)."""
        if self.dry_run:
            return None
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        try:
            self._request("GET", "/contacts", params={
                "page": 1, "limit": 1,
                "startDate": (now - timedelta(days=30)).strftime(fmt),
                "endDate": now.strftime(fmt)})
        except SeamlessError:
            pass
        return self.credits_remaining


# ---- researched-contact field extraction (port of best_contact_fields) -----

def best_email(contact: dict) -> tuple[str | None, str | None, str | None]:
    """(email, validation, confidence) preferring EmailAI == 'valid'."""
    candidates = []
    for i in (1, 2, 3):
        em = contact.get(f"email{i}")
        if em:
            candidates.append((em, contact.get(f"email{i}EmailAI"),
                               contact.get(f"email{i}TotalAI")))
    for em, val, conf in candidates:
        if str(val).lower() == "valid":
            return em, val, str(conf) if conf is not None else None
    if candidates:
        em, val, conf = candidates[0]
        return em, val, str(conf) if conf is not None else None
    return None, None, None


def best_phone(contact: dict) -> str | None:
    for k in ("contactPhone1", "contactPhone2", "companyPhone1",
              "companyPhone2", "companyPhone3"):
        v = contact.get(k)
        if v:
            return re.sub(r"\s*(x|ext\.?)\s*\d+$", "", str(v)).strip()
    return None
