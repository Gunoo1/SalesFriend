"""Haiku A/B instance: same codebase, its own port, database, and env file.

Run: .venv\\Scripts\\python.exe run_haiku.py    -> http://localhost:8512
The main (sonnet) instance keeps running on 8511 — start both and compare
answer quality and cost on the same asks.

Config: .env.haiku (gitignored) is loaded FIRST, so anything in it wins;
keys it doesn't set fall through to the shared .env. That's where the
separate ANTHROPIC_API_KEY lives so Haiku usage tracks on its own key in
the Anthropic console. The setdefault lines below are last-resort defaults
if .env.haiku is missing.

Everything stateful lives under data_haiku/ (users, conversations,
artifacts, checkpoints, estate), fully separate from the main data/ so the
two instances never contend for the same SQLite files. Seed users with:
    set DATA_DIR=data_haiku && .venv\\Scripts\\python.exe -m salesagent.auth add-user <name> --password "..."
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env.haiku")
os.environ.setdefault("ORCHESTRATOR_MODEL", "claude-haiku-4-5")
os.environ.setdefault("PORT", "8512")
os.environ.setdefault("DATA_DIR", "data_haiku")

import uvicorn

from salesagent.settings import load_settings

if __name__ == "__main__":
    settings = load_settings()
    print(f"HAIKU instance: {settings.orchestrator_model} on port {settings.port}, "
          f"data in {settings.data_dir}")
    if not settings.anthropic_api_key:
        print("WARNING: ANTHROPIC_API_KEY is empty — paste the separate key "
              "into .env.haiku (deliberately NOT falling back to the main "
              "key, so usage tracking stays clean)")
    uvicorn.run("salesagent.web.app:app", host="0.0.0.0", port=settings.port,
                workers=1)
