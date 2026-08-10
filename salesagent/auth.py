"""Simplest real auth: stdlib pbkdf2 + opaque session tokens in an httponly
cookie. No OAuth. Admin CLI:  python -m salesagent.auth add-user <name>
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, Depends, HTTPException

from .db import get_conn, init_db, utcnow
from .settings import load_settings

PBKDF2_ITERS = 600_000
COOKIE_NAME = "sa_session"


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return h, salt


def verify_password(password: str, pw_hash: bytes, salt: bytes) -> bool:
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return hmac.compare_digest(h, pw_hash)


def create_user(conn: sqlite3.Connection, username: str, password: str,
                is_admin: bool = False) -> int:
    pw_hash, salt = hash_password(password)
    with conn:
        cur = conn.execute(
            "INSERT INTO users (username, pw_hash, salt, is_admin, created_at) "
            "VALUES (?,?,?,?,?)",
            (username.strip().lower(), pw_hash, salt, int(is_admin), utcnow()))
    return cur.lastrowid


def login(conn: sqlite3.Connection, username: str, password: str,
          ttl_hours: int) -> str | None:
    row = conn.execute("SELECT * FROM users WHERE username=?",
                       (username.strip().lower(),)).fetchone()
    if not row or not verify_password(password, row["pw_hash"], row["salt"]):
        return None
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) "
                     "VALUES (?,?,?,?)", (token, row["id"], utcnow(), expires))
    return token


def logout(conn: sqlite3.Connection, token: str) -> None:
    with conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))


def user_for_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT u.id, u.username, u.is_admin, s.expires_at FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token=?", (token,)).fetchone()
    if not row:
        return None
    if row["expires_at"] < utcnow():
        logout(conn, token)
        return None
    return row


# ---- FastAPI dependency ----------------------------------------------------
# Per-request connections (FastAPI caches a dependency within one request, so
# current_user and the route handler share a single conn; it closes after the
# response — including after an SSE stream finishes).

def db_conn():
    conn = get_conn(load_settings().app_db)
    try:
        yield conn
    finally:
        conn.close()


def current_user(sa_session: str | None = Cookie(default=None),
                 conn: sqlite3.Connection = Depends(db_conn)) -> dict:
    if not sa_session:
        raise HTTPException(401, "not logged in")
    row = user_for_token(conn, sa_session)
    if not row:
        raise HTTPException(401, "session expired")
    return {"id": row["id"], "username": row["username"],
            "is_admin": bool(row["is_admin"]), "token": sa_session}


# ---- CLI --------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(prog="salesagent.auth")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add-user")
    p_add.add_argument("username")
    p_add.add_argument("--admin", action="store_true")
    p_add.add_argument("--password", help="omit to be prompted")
    p_pw = sub.add_parser("passwd")
    p_pw.add_argument("username")
    p_pw.add_argument("--password")
    sub.add_parser("list")
    args = ap.parse_args()

    settings = load_settings()
    conn = init_db(settings)
    if args.cmd == "add-user":
        pw = args.password or getpass.getpass("password: ")
        uid = create_user(conn, args.username, pw, is_admin=args.admin)
        print(f"created user #{uid} {args.username}")
    elif args.cmd == "passwd":
        pw = args.password or getpass.getpass("new password: ")
        pw_hash, salt = hash_password(pw)
        with conn:
            n = conn.execute("UPDATE users SET pw_hash=?, salt=? WHERE username=?",
                             (pw_hash, salt, args.username.strip().lower())).rowcount
        print("updated" if n else "no such user")
    elif args.cmd == "list":
        for r in conn.execute("SELECT id, username, is_admin, created_at FROM users"):
            print(f"#{r['id']} {r['username']} admin={bool(r['is_admin'])} "
                  f"since {r['created_at']}")


if __name__ == "__main__":
    main()
