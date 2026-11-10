"""管理员登录会话存库：服务重启（面板一键更新）后不用重新登录。

2026-11-09：会话只在内存里，更新重启后接口全部 401，更新进度卡停在半路，要手动刷新再登录。
"""
import secrets
import sqlite3
import time

import pytest
from fastapi import HTTPException

import user_store
from test_task_deadline import _run


@pytest.fixture
def server(tmp_path, monkeypatch):
    import server as server_mod
    monkeypatch.setattr(user_store, "DB_PATH", str(tmp_path / "users.db"))
    user_store._seed()
    monkeypatch.setattr(server_mod, "ADMIN_SESSIONS", {})
    return server_mod


# 测试里的密码都是现生成的随机串，主管理员用 user_store 里的初始值
OPS_KEY = secrets.token_hex(8)
NEW_KEY = secrets.token_hex(8)


def _login(server, username="admin", password=None):
    password = password or user_store.DEFAULT_ADMIN_PASSWORD
    return _run(server.admin_login(server.AdminLogin(username=username, password=password)))["token"]


def _restart(server, monkeypatch):
    monkeypatch.setattr(server, "ADMIN_SESSIONS", {})


def test_login_survives_a_restart(server, monkeypatch):
    token = _login(server)
    _restart(server, monkeypatch)
    server._admin_auth(token)
    assert server.ADMIN_SESSIONS[token]["username"] == "admin"
    assert server.ADMIN_SESSIONS[token]["role"] == "owner"


def test_token_is_not_stored_in_plain_text(server, tmp_path):
    token = _login(server)
    rows = sqlite3.connect(str(tmp_path / "users.db")).execute("SELECT * FROM admin_sessions").fetchall()
    assert len(rows) == 1 and token not in repr(rows)


def test_unknown_or_missing_token_is_rejected(server):
    for token in ("never-issued", "", None):
        with pytest.raises(HTTPException) as caught:
            server._admin_auth(token)
        assert caught.value.status_code == 401


def test_expired_session_is_not_restored(server, monkeypatch):
    user_store.save_session("old", "admin", "owner", time.time() - 1)
    assert user_store.load_session("old") is None
    with pytest.raises(HTTPException):
        server._admin_auth("old")


def test_disabled_user_cannot_come_back_after_restart(server, monkeypatch):
    user_store.create_user("ops", OPS_KEY)
    token = _login(server, "ops", OPS_KEY)
    user_store.update_user("ops", enabled=False)
    _restart(server, monkeypatch)
    with pytest.raises(HTTPException):
        server._admin_auth(token)


def test_password_change_signs_out_other_devices_but_not_this_one(server, monkeypatch):
    here, elsewhere = _login(server), _login(server)
    _run(server.admin_user_update("admin", server.AdminUserUpdate(password=NEW_KEY), x_admin_key=here))
    server._admin_auth(here)
    with pytest.raises(HTTPException):
        server._admin_auth(elsewhere)
    _restart(server, monkeypatch)
    server._admin_auth(here)
    with pytest.raises(HTTPException):
        server._admin_auth(elsewhere)


def test_deleted_user_sessions_are_removed(server, monkeypatch):
    user_store.create_user("ops", OPS_KEY)
    admin, ops = _login(server), _login(server, "ops", OPS_KEY)
    _run(server.admin_user_delete("ops", x_admin_key=admin))
    assert user_store.load_session(ops) is None
