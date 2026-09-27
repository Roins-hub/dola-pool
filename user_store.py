"""管理员用户存储：登录、增删改子管理员、改密码。

初始账号：admin / admin123（role=owner，不允许删除/禁用/改名）。
密码使用 PBKDF2-SHA256 + 随机盐存储，绝不明文入库。
"""
import hashlib
import hmac
import os
import sqlite3
import time

import config

DB_PATH = getattr(config, "POOL_DB_PATH", "pool_usage.db")
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 200_000
    ).hex()


def _safe(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("password_hash", None)
    d.pop("salt", None)
    d["enabled"] = bool(d.get("enabled"))
    return d


def _seed():
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT username FROM admin_users WHERE role='owner'"
        ).fetchone()
        if not row:
            salt = os.urandom(16).hex()
            now = time.time()
            conn.execute(
                """INSERT OR IGNORE INTO admin_users
                   (username, password_hash, salt, role, enabled, created_at, updated_at)
                   VALUES (?,?,?,?,1,?,?)""",
                (DEFAULT_ADMIN_USER, _hash_password(DEFAULT_ADMIN_PASSWORD, salt),
                 salt, "owner", now, now),
            )
            conn.commit()
    finally:
        conn.close()


def get_user(username: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM admin_users WHERE username=?", (username,)
        ).fetchone()
        return _safe(row) if row else None
    finally:
        conn.close()


def verify_login(username: str, password: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM admin_users WHERE username=?", (username,)
        ).fetchone()
        if not row or not row["enabled"]:
            return None
        expected = _hash_password(password, row["salt"])
        if not hmac.compare_digest(expected, row["password_hash"]):
            return None
        return _safe(row)
    finally:
        conn.close()


def list_users() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM admin_users ORDER BY role DESC, username ASC"
        ).fetchall()
        return [_safe(r) for r in rows]
    finally:
        conn.close()


def create_user(username: str, password: str, role: str = "admin") -> dict:
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("用户名和密码不能为空")
    if role not in ("owner", "admin"):
        role = "admin"
    conn = _connect()
    try:
        if conn.execute(
            "SELECT 1 FROM admin_users WHERE username=?", (username,)
        ).fetchone():
            raise ValueError(f"用户名已存在: {username}")
        salt = os.urandom(16).hex()
        now = time.time()
        conn.execute(
            """INSERT INTO admin_users
               (username, password_hash, salt, role, enabled, created_at, updated_at)
               VALUES (?,?,?,?,1,?,?)""",
            (username, _hash_password(password, salt), salt, role, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_user(username)


def update_user(username: str, *, new_username: str | None = None,
                password: str | None = None,
                enabled: bool | None = None) -> dict:
    user = get_user(username)
    if not user:
        raise ValueError("用户不存在")
    owner = user["role"] == "owner"
    if new_username is not None and new_username.strip() != username:
        if owner:
            raise ValueError("主管理员 admin 不允许改名")
        new_username = new_username.strip()
        if get_user(new_username):
            raise ValueError(f"用户名已存在: {new_username}")
    if enabled is False and owner:
        raise ValueError("主管理员不允许禁用")

    conn = _connect()
    try:
        if new_username is not None and new_username.strip() != username:
            conn.execute(
                "UPDATE admin_users SET username=? WHERE username=?",
                (new_username.strip(), username),
            )
            username = new_username.strip()
        if password:
            salt = os.urandom(16).hex()
            conn.execute(
                "UPDATE admin_users SET password_hash=?, salt=?, updated_at=? WHERE username=?",
                (_hash_password(password, salt), salt, time.time(), username),
            )
        if enabled is not None:
            conn.execute(
                "UPDATE admin_users SET enabled=?, updated_at=? WHERE username=?",
                (1 if enabled else 0, time.time(), username),
            )
        conn.commit()
    finally:
        conn.close()
    return get_user(username)


def delete_user(username: str):
    user = get_user(username)
    if not user:
        return
    if user["role"] == "owner":
        raise ValueError("主管理员不允许删除")
    conn = _connect()
    try:
        conn.execute("DELETE FROM admin_users WHERE username=?", (username,))
        conn.commit()
    finally:
        conn.close()


_seed()
