"""Admin-editable integration settings, stored in app.db (app_config table)
and layered OVER .env inside load_settings(). .env stays the base; a row here
wins; clearing a field deletes the row and reverts to .env. Only names in
FIELDS are readable/writable from the web — nothing else in Settings is
reachable. Values are stored plaintext in app.db (same trust level as the
.env sitting next to it); secrets are masked to last-4 in every API response.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

from .db import utcnow

# name -> (label, group, is_secret). Order = display order in the admin tab.
# Every name is BOTH a Settings field and (uppercased) its .env variable.
FIELDS: dict[str, tuple[str, str, bool]] = {
    "seamless_api_key":      ("Seamless API key", "Seamless.AI", True),
    "serper_api_key":        ("Serper API key (web search + verification)", "Search", True),
    "google_places_api_key": ("Google Places API key", "Search", True),
    "acumatica_url":         ("Instance URL (https://...)", "Acumatica", False),
    "acumatica_tenant":      ("Tenant", "Acumatica", False),
    "acumatica_username":    ("Username", "Acumatica", False),
    "acumatica_password":    ("Password", "Acumatica", True),
    "acumatica_price_class": ("Price class", "Acumatica", False),
    "price_comparison_url":  ("Price-comparison app URL", "Pricing", False),
    "anthropic_api_key":     ("Anthropic API key", "Models", True),
    "orchestrator_model":    ("Orchestrator model (claude-* or an Ollama tag)", "Models", False),
    "extract_model":         ("Extraction model", "Models", False),
    "ollama_base_url":       ("Ollama base URL", "Models", False),
}


def read_overrides(app_db: Path) -> dict[str, str]:
    """DB rows for known fields; {} on any failure (first boot, missing
    table) so settings loading can never be broken by this layer."""
    try:
        conn = sqlite3.connect(f"file:{Path(app_db).as_posix()}?mode=ro",
                               uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT name, value FROM app_config").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {name: value for name, value in rows
            if name in FIELDS and value}


def apply_overrides(settings):
    over = read_overrides(settings.app_db)
    # .rstrip("/") mirrors what load_settings does to the env versions
    for k in ("acumatica_url", "price_comparison_url", "ollama_base_url"):
        if k in over:
            over[k] = over[k].rstrip("/")
    return dataclasses.replace(settings, **over) if over else settings


def set_values(conn: sqlite3.Connection, values: dict[str, str],
               updated_by: str) -> None:
    """Empty string deletes the override (revert to .env); anything else
    upserts. Unknown names must be rejected by the caller."""
    now = utcnow()
    with conn:
        for name, value in values.items():
            value = (value or "").strip()
            if not value:
                conn.execute("DELETE FROM app_config WHERE name=?", (name,))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO app_config"
                    " (name, value, updated_by, updated_at) VALUES (?,?,?,?)",
                    (name, value, updated_by, now))
