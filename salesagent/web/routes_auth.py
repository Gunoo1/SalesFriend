from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response, Cookie
from pydantic import BaseModel

from ..auth import COOKIE_NAME, current_user, db_conn, login, logout
from ..settings import load_settings

router = APIRouter(prefix="/api")


class LoginReq(BaseModel):
    username: str
    password: str


@router.post("/login")
def do_login(req: LoginReq, response: Response,
             conn: sqlite3.Connection = Depends(db_conn)):
    settings = load_settings()
    token = login(conn, req.username, req.password, settings.session_ttl_hours)
    if not token:
        raise HTTPException(401, "bad username or password")
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                        max_age=settings.session_ttl_hours * 3600)
    user = conn.execute("SELECT id, username, is_admin FROM users WHERE username=?",
                        (req.username.strip().lower(),)).fetchone()
    return {"ok": True, "user": {"id": user["id"], "username": user["username"],
                                 "is_admin": bool(user["is_admin"])}}


@router.post("/logout")
def do_logout(response: Response,
              sa_session: str | None = Cookie(default=None),
              conn: sqlite3.Connection = Depends(db_conn)):
    if sa_session:
        logout(conn, sa_session)
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    return {"id": user["id"], "username": user["username"],
            "is_admin": user["is_admin"]}
