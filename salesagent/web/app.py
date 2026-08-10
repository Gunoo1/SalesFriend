"""FastAPI factory. Static SPA mounted last so /api routes win."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..audit import setup_logging
from ..db import init_db
from ..settings import load_settings
from . import (routes_admin, routes_artifacts, routes_auth, routes_chat,
               routes_jobs, routes_uploads)


def create_app() -> FastAPI:
    settings = load_settings()
    setup_logging(settings)
    conn = init_db(settings)  # schema + first-boot reference seeding
    # heal restart orphans: a 'running' turn cannot survive a restart, and a
    # wedged status blocks all further messages (409). awaiting_confirmation
    # is durable (checkpointed interrupt) and must NOT be reset.
    with conn:
        conn.execute("UPDATE conversations SET status='idle' "
                     "WHERE status='running'")
    conn.close()

    app = FastAPI(title="SalesAgent", docs_url=None, redoc_url=None)
    app.include_router(routes_auth.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_artifacts.router)
    app.include_router(routes_uploads.router)
    app.include_router(routes_jobs.router)

    @app.get("/api/health")
    def health():
        return {"ok": True, "app": "salesagent"}

    static_dir = settings.root / "static"
    app.mount("/", StaticFiles(directory=str(static_dir), html=True))
    return app


app = create_app()
