"""回归：写事务不能泄漏，否则 pool_usage.db 会被永久锁死。

历史 bug：browser_pool._clear_expired_rate_limits() 里写成
``if cur.rowcount: self._conn.commit()``。UPDATE 命中 0 行时不提交，
但 Python sqlite3 已经隐式开了写事务 -> 连接一直攥着写锁。
该方法由 list_accounts() 调用，也就是面板一打开就锁库，
之后所有写操作（建/删代理、导入 cookie）全部 database is locked。
"""
from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _stubs():
    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError",
                "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))

    async def _g(*_a, **_k):
        raise RuntimeError("stub")

    async def _r(*_a, **_k):
        raise RuntimeError("stub")

    v.generate_video = _g
    v.resume_video = _r
    sys.modules["video_worker_ui"] = v

    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d
    yield


def _pool(tmp_path):
    import browser_pool
    import config
    db = tmp_path / "pool_usage.db"
    config.POOL_DB_PATH = str(db)
    browser_pool.DAILY_LIMIT = 99
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    return browser_pool.BrowserPool(accounts_dir=str(accounts_dir)), db


def _can_write_elsewhere(db) -> bool:
    """另开一条连接试写：能写说明没有别的事务攥着写锁。"""
    conn = sqlite3.connect(str(db), timeout=1.0)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS _probe (x INTEGER)")
        conn.execute("INSERT INTO _probe VALUES (1)")
        conn.commit()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def test_list_accounts_does_not_leak_write_lock(tmp_path):
    """list_accounts() 走一遍后，别的连接必须还能写。"""
    pool, db = _pool(tmp_path)
    assert _can_write_elsewhere(db), "基线：池刚建好时应该能写"

    for _ in range(3):
        pool.list_accounts()

    assert _can_write_elsewhere(db), \
        "list_accounts() 泄漏了写事务，pool_usage.db 被锁死"


def test_clear_expired_rate_limits_commits_even_with_zero_rows(tmp_path):
    """命中 0 行也必须提交（这正是当初漏掉的分支）。"""
    pool, db = _pool(tmp_path)
    pool._conn.execute("DELETE FROM accounts_meta")
    pool._conn.commit()
    assert pool._conn.execute("SELECT COUNT(*) FROM accounts_meta").fetchone()[0] == 0

    pool._clear_expired_rate_limits()   # 0 行命中

    assert _can_write_elsewhere(db), "0 行命中时没有提交，写锁泄漏了"


def test_write_still_possible_after_full_panel_flow(tmp_path, monkeypatch):
    """面板加载账号页的完整路径（list_accounts + engine 快照）之后仍可写库。"""
    pool, db = _pool(tmp_path)
    import proxy_store
    monkeypatch.setattr(proxy_store, "DB_PATH", Path(db))
    proxy_store._ready_paths.clear()

    pool.list_accounts()
    pool.engine_snapshot()

    created = proxy_store.create_proxy("after-panel", "http", "1.2.3.4", 8080)
    assert created["id"]
    proxy_store.delete_proxy(created["id"])
    assert _can_write_elsewhere(db)
