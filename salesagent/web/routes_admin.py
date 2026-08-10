"""Admin API: user management + org activity. Every route requires is_admin.

Removal semantics: deleting a user kills their sessions immediately (instant
lockout) and removes the account. Their conversations/messages/artifacts stay
in the DB attributed to the old user_id — org history is never destroyed —
but become unreachable in the UI (ownership checks need a live user).
Guards: you can't delete yourself, and you can't delete the last admin.
"""
from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import create_user, current_user, db_conn, hash_password
from ..settings import load_settings

router = APIRouter(prefix="/api/admin")

_USERNAME_RE = re.compile(r"^[a-z0-9._-]{2,32}$")


def current_admin(user: dict = Depends(current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(403, "admin only")
    return user


@router.get("/users")
def list_users(admin: dict = Depends(current_admin),
               conn: sqlite3.Connection = Depends(db_conn)):
    rows = conn.execute("""
        SELECT u.id, u.username, u.is_admin, u.created_at,
               (SELECT MAX(s.created_at) FROM sessions s
                 WHERE s.user_id = u.id)                          AS last_login,
               (SELECT MAX(c.updated_at) FROM conversations c
                 WHERE c.user_id = u.id)                          AS last_active,
               (SELECT COUNT(*) FROM conversations c
                 WHERE c.user_id = u.id)                          AS conversations,
               (SELECT COUNT(*) FROM messages m
                 JOIN conversations c ON c.id = m.conversation_id
                 WHERE c.user_id = u.id AND m.role = 'user')      AS messages,
               (SELECT COALESCE(SUM(l.credits_spent), 0)
                  FROM seamless_ledger l WHERE l.user_id = u.id)  AS credits_spent,
               (SELECT COUNT(*) FROM jobs j WHERE j.user_id = u.id) AS jobs,
               (SELECT COUNT(*) FROM uploads up
                 WHERE up.user_id = u.id)                         AS uploads
        FROM users u ORDER BY u.username""").fetchall()
    return {"users": [dict(r) | {"is_admin": bool(r["is_admin"])}
                      for r in rows]}


class NewUser(BaseModel):
    username: str
    password: str
    is_admin: bool = False


@router.post("/users")
def add_user(req: NewUser, admin: dict = Depends(current_admin),
             conn: sqlite3.Connection = Depends(db_conn)):
    uname = req.username.strip().lower()
    if not _USERNAME_RE.match(uname):
        raise HTTPException(400, "username: 2-32 chars, a-z 0-9 . _ -")
    if len(req.password) < 6:
        raise HTTPException(400, "password must be at least 6 characters")
    try:
        uid = create_user(conn, uname, req.password, is_admin=req.is_admin)
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"user '{uname}' already exists")
    return {"ok": True, "id": uid, "username": uname}


class NewPassword(BaseModel):
    password: str


@router.post("/users/{uid}/password")
def reset_password(uid: int, req: NewPassword,
                   admin: dict = Depends(current_admin),
                   conn: sqlite3.Connection = Depends(db_conn)):
    if len(req.password) < 6:
        raise HTTPException(400, "password must be at least 6 characters")
    pw_hash, salt = hash_password(req.password)
    with conn:
        n = conn.execute("UPDATE users SET pw_hash=?, salt=? WHERE id=?",
                         (pw_hash, salt, uid)).rowcount
        # force re-login everywhere with the new password
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    if not n:
        raise HTTPException(404, "no such user")
    return {"ok": True}


@router.delete("/users/{uid}")
def remove_user(uid: int, admin: dict = Depends(current_admin),
                conn: sqlite3.Connection = Depends(db_conn)):
    if uid == admin["id"]:
        raise HTTPException(400, "you can't delete your own account")
    row = conn.execute("SELECT id, username, is_admin FROM users WHERE id=?",
                       (uid,)).fetchone()
    if not row:
        raise HTTPException(404, "no such user")
    if row["is_admin"]:
        admins = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin=1").fetchone()["n"]
        if admins <= 1:
            raise HTTPException(400, "can't delete the last admin")
    with conn:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
    return {"ok": True, "removed": row["username"]}


@router.get("/activity")
def activity(limit: int = 60, admin: dict = Depends(current_admin),
             conn: sqlite3.Connection = Depends(db_conn)):
    settings = load_settings()
    limit = max(10, min(int(limit or 60), 200))
    stats = conn.execute("""
        SELECT (SELECT COUNT(*) FROM users)                       AS users,
               (SELECT COUNT(*) FROM conversations)               AS conversations,
               (SELECT COUNT(*) FROM messages WHERE role='user')  AS messages,
               (SELECT COUNT(*) FROM artifacts)                   AS artifacts,
               (SELECT COUNT(*) FROM jobs)                        AS jobs,
               (SELECT COALESCE(SUM(credits_spent),0)
                  FROM seamless_ledger)                           AS credits_total,
               (SELECT COALESCE(SUM(credits_spent),0) FROM seamless_ledger
                 WHERE ts >= strftime('%Y-%m-%dT00:00:00Z','now')) AS credits_today
        """).fetchone()
    # one merged recent-events feed: user messages, jobs, uploads, spends
    events = conn.execute("""
        SELECT * FROM (
          SELECT m.created_at AS ts, u.username, 'message' AS kind,
                 COALESCE(c.title, c.id) AS context,
                 substr(COALESCE(json_extract(m.content_json,'$.text'),''),1,140)
                   AS detail
          FROM messages m
          JOIN conversations c ON c.id = m.conversation_id
          LEFT JOIN users u ON u.id = c.user_id
          WHERE m.role = 'user'
          UNION ALL
          SELECT j.created_at, u.username, 'job',
                 COALESCE(j.tool_name, j.kind),
                 j.kind || ' — ' || j.status
          FROM jobs j LEFT JOIN users u ON u.id = j.user_id
          UNION ALL
          SELECT up.created_at, u.username, 'upload', up.filename,
                 'file upload'
          FROM uploads up LEFT JOIN users u ON u.id = up.user_id
          UNION ALL
          SELECT l.ts, u.username, 'seamless',
                 COALESCE(l.target,''),
                 l.action || ' — ' || l.credits_spent || ' credits'
          FROM seamless_ledger l LEFT JOIN users u ON u.id = l.user_id
        ) ORDER BY ts DESC LIMIT ?""", (limit,)).fetchall()
    return {"stats": dict(stats) | {"seamless_day_cap": settings.seamless_day_cap},
            "events": [dict(r) for r in events]}
