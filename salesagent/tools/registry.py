"""Tool registry: adding a tool = dropping a decorated function in a module
under salesagent.tools (pkgutil discovery — fixes K12Intel's
edit-a-function-to-register pattern). Duplicate names raise at boot.

Handlers are SYNC (SQLite + requests); the graph's tools node runs them via
asyncio.to_thread with a fresh ToolContext whose connections are closed after
the call.
"""
from __future__ import annotations

import importlib
import json
import pkgutil
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ..artifacts.store import ArtifactTooLarge
from ..artifacts.transforms import TransformError
from ..audit import log
from ..db import get_conn
from ..estate import EstateMissing, open_k12
from ..settings import Settings
from .envelope import error_envelope


class CostClass(str, Enum):
    FREE = "free"
    CHEAP = "cheap"
    PAID_CREDITS = "paid_credits"
    SLOW_JOB = "slow_job"


class ToolContext:
    """Per-call context. Lazily opens connections; execute_tool closes them."""

    def __init__(self, settings: Settings, user: dict, conversation_id: str,
                 emit: Callable[[str, dict], None]):
        self.settings = settings
        self.user = user
        self.conversation_id = conversation_id
        self.emit = emit                      # (event, data) -> custom SSE
        self._rw: sqlite3.Connection | None = None
        self._k12: sqlite3.Connection | None = None

    def rw(self) -> sqlite3.Connection:
        if self._rw is None:
            self._rw = get_conn(self.settings.app_db)
        return self._rw

    def k12(self) -> sqlite3.Connection:
        """Read-only conn to the app's OWN k12 estate snapshot (self-built).
        Raises EstateMissing (-> actionable error envelope) if never built."""
        if self._k12 is None:
            self._k12 = open_k12(self.settings)
        return self._k12

    def close(self) -> None:
        for c in (self._rw, self._k12):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
        self._rw = self._k12 = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str                      # written for the model: says WHEN to call
    input_schema: dict                    # JSON Schema (additionalProperties false)
    handler: Callable[..., dict]          # (ctx, **params) -> envelope dict
    cost_class: CostClass = CostClass.FREE
    needs_confirmation: bool = False
    estimate_cost: Callable[[dict], int] | None = None
    enabled: bool = True


def tool_spec(*, name: str, description: str, input_schema: dict,
              cost_class: CostClass = CostClass.FREE,
              needs_confirmation: bool = False,
              estimate_cost: Callable[[dict], int] | None = None,
              enabled: bool = True):
    """Decorator: collect a ToolSpec into the defining module's _SPECS list."""
    input_schema.setdefault("type", "object")
    input_schema.setdefault("additionalProperties", False)

    def wrap(fn: Callable) -> Callable:
        import sys
        mod = sys.modules[fn.__module__]
        specs = getattr(mod, "_SPECS", None)
        if specs is None:
            specs = []
            mod._SPECS = specs
        specs.append(ToolSpec(name=name, description=description,
                              input_schema=input_schema, handler=fn,
                              cost_class=cost_class,
                              needs_confirmation=needs_confirmation,
                              estimate_cost=estimate_cost, enabled=enabled))
        return fn
    return wrap


_MODULES_SKIP = {"registry", "envelope", "__init__"}


def discover() -> dict[str, ToolSpec]:
    """Import every module in salesagent.tools and collect _SPECS."""
    from . import __path__ as pkg_path
    registry: dict[str, ToolSpec] = {}
    for info in pkgutil.iter_modules(pkg_path):
        if info.name in _MODULES_SKIP:
            continue
        mod = importlib.import_module(f"salesagent.tools.{info.name}")
        for spec in getattr(mod, "_SPECS", []):
            if spec.name in registry:
                raise RuntimeError(f"duplicate tool name '{spec.name}' "
                                   f"({info.name} vs earlier module)")
            registry[spec.name] = spec
    return registry


def as_anthropic_tools(registry: dict[str, ToolSpec]) -> list[dict]:
    """bind_tools accepts Anthropic-format dicts directly — no pydantic needed."""
    return [{"name": s.name, "description": s.description,
             "input_schema": s.input_schema}
            for s in registry.values() if s.enabled]


def execute_tool(settings: Settings, registry: dict[str, ToolSpec],
                 user: dict, conversation_id: str,
                 emit: Callable[[str, dict], None],
                 name: str, args: dict) -> dict:
    """Sync execution with a fresh context; every failure becomes an error
    envelope the model can react to (never an exception up the graph)."""
    spec = registry.get(name)
    if spec is None or not spec.enabled:
        return error_envelope(f"unknown or disabled tool '{name}'")
    ctx = ToolContext(settings, user, conversation_id, emit)
    try:
        return spec.handler(ctx, **(args or {}))
    except TypeError as e:
        # bad/missing params — tell the model what the schema wanted
        return error_envelope(
            f"bad parameters for {name}: {e}. Schema: "
            f"{json.dumps(spec.input_schema.get('properties', {}))[:800]}",
            error_type="BadParams")
    except EstateMissing as e:
        return error_envelope(str(e), error_type="EstateMissing")
    except (ArtifactTooLarge, TransformError) as e:
        return error_envelope(str(e), error_type=type(e).__name__)
    except Exception as e:
        log.exception("tool %s failed", name)
        return error_envelope(f"{name} failed: {type(e).__name__}: {e}",
                              error_type=type(e).__name__)
    finally:
        ctx.close()
