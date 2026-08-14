"""Rurality classification — the "middle of nowhere" machinery.

Two complementary codings, both vendored/derived from federal data:

- RUCA (USDA ERS Rural-Urban Commuting Area, 2010 vintage — current release)
  at the ZIP level, seeded into app.db as ref_zip_ruca (41k zips). Codes 1-10:
  1-3 metro, 4-6 micropolitan (10-50k town), 7-9 small town, 10 rural/remote.
  Sales reading: RUCA >= 7 = places distributor reps rarely visit.

- NCES urban-centric locale codes carried natively by the estates
  (districts.locale, colleges.locale, private_schools.locale): 11-13 city,
  21-23 suburb, 31-33 town, 41-43 rural (3rd digit 1 fringe/large ->
  3 remote/small).
"""
from __future__ import annotations

import sqlite3

RUCA_CLASSES = [
    (range(1, 4), "metro"),
    (range(4, 7), "micropolitan"),
    (range(7, 10), "small town"),
    (range(10, 11), "rural remote"),
]

# RUCA >= REMOTE_MIN counts as "middle of nowhere" for sales purposes
REMOTE_MIN = 7


def ruca_class(ruca: int | None) -> str | None:
    if ruca is None:
        return None
    for rng, label in RUCA_CLASSES:
        if ruca in rng:
            return label
    return None


LOCALE_LABELS = {
    11: "City — large", 12: "City — midsize", 13: "City — small",
    21: "Suburb — large", 22: "Suburb — midsize", 23: "Suburb — small",
    31: "Town — fringe", 32: "Town — distant", 33: "Town — remote",
    41: "Rural — fringe", 42: "Rural — distant", 43: "Rural — remote",
}

LOCALE_GROUPS = {"city": (11, 13), "suburb": (21, 23),
                 "town": (31, 33), "rural": (41, 43)}


def locale_label(code) -> str | None:
    try:
        return LOCALE_LABELS.get(int(code))
    except (TypeError, ValueError):
        return None


def locale_where(column: str, *, rural_only: bool = False,
                 locale_groups: list[str] | None = None
                 ) -> tuple[str | None, list]:
    """WHERE fragment for a locale-coded column. rural_only wins; otherwise
    locale_groups is any of city|suburb|town|rural. Raises ValueError on an
    unknown group name (so tools surface the valid options)."""
    if rural_only:
        lo, hi = LOCALE_GROUPS["rural"]
        return f"{column} BETWEEN ? AND ?", [lo, hi]
    if locale_groups:
        parts, args = [], []
        for g in locale_groups:
            rng = LOCALE_GROUPS.get(str(g).strip().lower())
            if not rng:
                raise ValueError(
                    f"unknown locale group {g!r}; use any of: "
                    + ", ".join(LOCALE_GROUPS))
            parts.append(f"{column} BETWEEN ? AND ?")
            args += [rng[0], rng[1]]
        return "(" + " OR ".join(parts) + ")", args
    return None, []


def zip5(value) -> str | None:
    """Normalize any zip-ish value to a 5-digit string (handles ZIP+4,
    ints that lost leading zeros, whitespace)."""
    if value is None:
        return None
    s = str(value).strip().split("-")[0]
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(5)[:5] if len(digits) <= 5 else digits[:5]


def lookup_ruca(conn: sqlite3.Connection, zips: list[str | None]
                ) -> dict[str, int]:
    """Batch lookup zip -> RUCA1 from ref_zip_ruca in app.db."""
    want = sorted({z for z in zips if z})
    out: dict[str, int] = {}
    for i in range(0, len(want), 500):
        chunk = want[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT zip, ruca FROM ref_zip_ruca WHERE zip IN ({ph})",
                chunk):
            out[r["zip"]] = r["ruca"]
    return out
