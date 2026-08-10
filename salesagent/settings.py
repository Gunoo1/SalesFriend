"""Single source of configuration. Loads SalesAgent/.env exactly once.

Deliberately replaces the workspace's cross-project fallback chain
(K12Intel -> ../FastenalBranchIntel/.env): this app never reads another
project's .env. Every tunable is a named field so "where does that number
come from" is always answerable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # SalesAgent/


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    app_db: Path
    checkpoint_db: Path      # separate file: checkpointer writes every superstep,
    uploads_dir: Path        # must not queue behind artifact/job writes
    files_dir: Path
    logs_dir: Path
    prompts_dir: Path
    estate_dir: Path         # the app's OWN reference snapshots (self-built
                             # from public sources — no other project's files)

    anthropic_api_key: str
    seamless_api_key: str
    serper_api_key: str
    google_places_api_key: str
    price_comparison_url: str  # base URL of the running price_comparison app; "" = off

    orchestrator_model: str
    extract_model: str        # bulk in-tool extraction stays on Haiku (workspace rule)

    seamless_day_cap: int        # org credits/day, enforced in the client not the prompt
    seamless_confirm_threshold: int  # credits; spends above this require user confirm
    convo_budget_default: int
    seamless_dry_run: bool

    acumatica_url: str          # direct read-only OData (prices + stock); empty = off
    acumatica_tenant: str
    acumatica_username: str
    acumatica_password: str
    acumatica_price_class: str
    category_xlsx: Path         # TEMP trends source (Category.xlsx in app root)

    port: int
    session_ttl_hours: int
    agent_recursion_limit: int   # LangGraph supersteps per turn (~2 per tool round);
                                 # default is effectively unlimited — Stop button +
                                 # step warnings are the runaway controls
    agent_step_warn_every: int   # chat warning every N steps (0 disables)


def _path(env_key: str, default: Path) -> Path:
    raw = os.environ.get(env_key, "")
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p).resolve()


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    data_dir = _path("DATA_DIR", ROOT / "data")
    s = Settings(
        root=ROOT,
        data_dir=data_dir,
        app_db=data_dir / "app.db",
        checkpoint_db=data_dir / "checkpoints.db",
        uploads_dir=data_dir / "uploads",
        files_dir=data_dir / "files",
        logs_dir=ROOT / "logs",
        prompts_dir=_path("PROMPTS_DIR", ROOT / "salesagent" / "agent" / "prompts"),
        estate_dir=data_dir / "estate",
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        seamless_api_key=os.environ.get("SEAMLESS_API_KEY", ""),
        serper_api_key=os.environ.get("SERPER_API_KEY", ""),
        google_places_api_key=os.environ.get("GOOGLE_PLACES_API_KEY", ""),
        price_comparison_url=os.environ.get("PRICE_COMPARISON_URL", "").rstrip("/"),
        orchestrator_model=os.environ.get("ORCHESTRATOR_MODEL", "claude-sonnet-5"),
        extract_model=os.environ.get("EXTRACT_MODEL", "claude-haiku-4-5"),
        seamless_day_cap=int(os.environ.get("SEAMLESS_DAY_CAP", "200")),
        seamless_confirm_threshold=int(os.environ.get("SEAMLESS_CONFIRM_THRESHOLD", "5")),
        convo_budget_default=int(os.environ.get("SEAMLESS_CONVO_BUDGET", "25")),
        seamless_dry_run=os.environ.get("SEAMLESS_DRY_RUN", "0") == "1",
        acumatica_url=os.environ.get("ACUMATICA_URL", "").rstrip("/"),
        acumatica_tenant=os.environ.get("ACUMATICA_TENANT", ""),
        acumatica_username=os.environ.get("ACUMATICA_USERNAME", ""),
        acumatica_password=os.environ.get("ACUMATICA_PASSWORD", ""),
        acumatica_price_class=os.environ.get("ACUMATICA_PRICE_CLASS", "SHOPGUEST"),
        category_xlsx=_path("CATEGORY_XLSX", ROOT / "Category.xlsx"),
        port=int(os.environ.get("PORT", "8511")),
        session_ttl_hours=int(os.environ.get("SESSION_TTL_HOURS", "72")),
        agent_recursion_limit=int(os.environ.get("AGENT_RECURSION_LIMIT", "1000000")),
        agent_step_warn_every=int(os.environ.get("AGENT_STEP_WARN_EVERY", "50")),
    )
    for d in (s.data_dir, s.uploads_dir, s.files_dir, s.estate_dir,
              s.logs_dir, s.logs_dir / "turns"):
        d.mkdir(parents=True, exist_ok=True)
    return s
