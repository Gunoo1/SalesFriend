"""Two log surfaces:
- rotating app log (logs/app.log) for operational messages
- per-conversation JSONL turn audit (logs/turns/{conversation_id}.jsonl) —
  one line per turn with tool calls, credits, cache hits, token usage.
  This is the replay/debug surface and the cost-attribution record.
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .db import utcnow
from .settings import Settings

log = logging.getLogger("salesagent")


def setup_logging(settings: Settings) -> None:
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fh = RotatingFileHandler(settings.logs_dir / "app.log",
                             maxBytes=10 * 1024 * 1024, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(sh)


def log_turn(settings: Settings, conversation_id: str, record: dict) -> None:
    """Append one JSONL line; never let audit failure break a turn."""
    record = {"ts": utcnow(), **record}
    try:
        path: Path = settings.logs_dir / "turns" / f"{conversation_id}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False,
                               default=str) + "\n")
    except Exception:
        log.exception("turn audit write failed for %s", conversation_id)
