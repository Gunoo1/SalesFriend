"""Background jobs: thread pool + DB rows as canonical state + in-memory ring
logs (price_comparison jobs.py pattern, simplified). Workers use fresh
connections and commit per unit — never hold a write txn across network calls.

Delegated jobs (price_scrape) just run a runner that polls the external app;
external_ref records the remote job id so /workflows-style UIs can deep-link.
"""
from __future__ import annotations

import json
import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from ..audit import log
from ..db import get_conn, utcnow
from ..settings import Settings

_RING_MAX = 400


class JobCtx:
    def __init__(self, manager: "JobManager", job_id: str, settings: Settings,
                 params: dict, conversation_id: str, user_id: int | None):
        self.manager = manager
        self.job_id = job_id
        self.settings = settings
        self.params = params
        self.conversation_id = conversation_id
        self.user_id = user_id

    def rw(self):
        return get_conn(self.settings.app_db)

    def log(self, msg: str) -> None:
        self.manager._log(self.job_id, msg)

    def progress(self, done: int, total: int | None = None,
                 message: str | None = None) -> None:
        self.manager._progress(self.job_id, done, total, message)

    def external_ref(self, ref: str) -> None:
        conn = self.rw()
        try:
            with conn:
                conn.execute("UPDATE jobs SET external_ref=? WHERE id=?",
                             (ref, self.job_id))
        finally:
            conn.close()


class JobManager:
    _instance: "JobManager | None" = None
    _lock = threading.Lock()

    def __init__(self, settings: Settings):
        self.settings = settings
        self.executor = ThreadPoolExecutor(max_workers=2,
                                           thread_name_prefix="job")
        self.logs: dict[str, deque] = {}

    @classmethod
    def get(cls, settings: Settings) -> "JobManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = JobManager(settings)
            return cls._instance

    # ---- internals ----------------------------------------------------------

    def _log(self, job_id: str, msg: str) -> None:
        self.logs.setdefault(job_id, deque(maxlen=_RING_MAX)) \
            .append({"ts": utcnow(), "msg": str(msg)[:300]})

    def _progress(self, job_id: str, done: int, total: int | None,
                  message: str | None) -> None:
        conn = get_conn(self.settings.app_db)
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET progress_done=?, progress_total="
                    "COALESCE(?, progress_total), message=COALESCE(?, message)"
                    " WHERE id=?", (done, total, message, job_id))
        finally:
            conn.close()

    def _run(self, job_id: str, kind: str, ctx: JobCtx) -> None:
        from . import runners
        conn = get_conn(self.settings.app_db)
        try:
            with conn:
                conn.execute("UPDATE jobs SET status='running', started_at=? "
                             "WHERE id=?", (utcnow(), job_id))
        finally:
            conn.close()
        self._log(job_id, f"job {kind} started")
        try:
            artifact_id = runners.RUNNERS[kind](ctx)
            conn = get_conn(self.settings.app_db)
            try:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET status='done', finished_at=?,"
                        " result_artifact_id=? WHERE id=?",
                        (utcnow(), artifact_id, job_id))
            finally:
                conn.close()
            self._log(job_id, f"done -> artifact {artifact_id}")
        except Exception as e:
            log.exception("job %s (%s) failed", job_id, kind)
            conn = get_conn(self.settings.app_db)
            try:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET status='error', finished_at=?, error=?"
                        " WHERE id=?",
                        (utcnow(), f"{type(e).__name__}: {e}"[:400], job_id))
            finally:
                conn.close()
            self._log(job_id, f"ERROR {type(e).__name__}: {e}")

    # ---- api ----------------------------------------------------------------

    def submit(self, kind: str, params: dict, *, conversation_id: str,
               user_id: int | None, tool_name: str) -> str:
        job_id = "job_" + uuid.uuid4().hex[:10]
        conn = get_conn(self.settings.app_db)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO jobs (id, kind, params_json, conversation_id,"
                    " user_id, tool_name, status, created_at)"
                    " VALUES (?,?,?,?,?,?, 'queued', ?)",
                    (job_id, kind, json.dumps(params, default=str),
                     conversation_id, user_id, tool_name, utcnow()))
        finally:
            conn.close()
        ctx = JobCtx(self, job_id, self.settings, params, conversation_id,
                     user_id)
        self.executor.submit(self._run, job_id, kind, ctx)
        return job_id

    def status(self, job_id: str, log_tail: int = 30) -> dict | None:
        conn = get_conn(self.settings.app_db)
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return {**dict(row),
                "log": list(self.logs.get(job_id, []))[-log_tail:]}
