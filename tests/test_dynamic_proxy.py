"""动态IP（隧道/旋转网关）支持：sticky session、出口身份、轮换。

关键行为：
- 静态代理：出口身份仍是 host:port，同出口隔离照旧生效（不能回归）。
- 动态代理：同一个 host:port 上每个号有自己的 sticky session，出口身份互不相同，
  所以共用网关也不会被同出口隔离误伤；被风控时可换 session（=换 IP）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import config
import pool as pool_mod
import proxy_store
from pool import AccountConfig, AccountPool


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "pool_usage.db"
    monkeypatch.setattr(proxy_store, "DB_PATH", db)
    monkeypatch.setattr(config, "PROXY", "")
    proxy_store._ready_paths.clear()
    yield db


def _mk_static(name="jp-static", host="10.0.0.1"):
    return proxy_store.create_proxy(name, "http", host, 8080,
                                    username="user", password="pw")


def _mk_dynamic(name="jp-tunnel", host="gate.example.com", **kw):
    kw.setdefault("rotate_min_interval", 0)
    return proxy_store.create_proxy(name, "http", host, 8000,
                                    username="baseuser", password="pw",
                                    mode="dynamic", **kw)


def _healthy(engine):
    for st in engine.states.values():
        st.status = pool_mod.STATUS_HEALTHY
        st.cooldown_until = 0


def _engine(tmp_path, names, daily=99, isolate=True):
    cfgs = []
    for name in names:
        state = tmp_path / f"{name}.json"
        state.write_text('{"cookies": true}', encoding="utf-8")
        cfgs.append(AccountConfig(
            id=name, state_file=state,
            proxy=proxy_store.proxy_url_for(name),
            egress_key=proxy_store.egress_key_for(name),
        ))
    engine = AccountPool(cfgs, daily_success_limit=daily,
                         isolate_shared_egress=isolate)
    _healthy(engine)
    return engine


def _blocked(engine):
    preview = engine.preview_route()
    return {row["id"]: row["blocked"] for row in preview["candidates"]}


# ===== 静态代理：历史行为不能变 =====

def test_static_proxy_url_has_no_session():
    pid = _mk_static()["id"]
    proxy_store.set_account_proxy("acc1", pid)
    assert proxy_store.proxy_url_for("acc1") == "http://user:pw@10.0.0.1:8080"
    assert proxy_store.egress_key_for("acc1") == "http://10.0.0.1:8080"
    assert proxy_store.get_session("acc1") is None


def test_static_proxy_same_egress_is_still_isolated(tmp_path):
    pid = _mk_static()["id"]
    for name in ("acc1", "acc2"):
        proxy_store.set_account_proxy(name, pid)
    engine = _engine(tmp_path, ["acc1", "acc2"])
    assert engine.effective_egress("acc1") == engine.effective_egress("acc2")
    assert engine.try_assign() == "acc1"
    assert engine.try_assign() is None
    assert _blocked(engine)["acc2"] == "shared_egress"


# ===== 动态代理：每号独立出口 =====

def test_dynamic_assigns_distinct_sessions_and_egress():
    pid = _mk_dynamic()["id"]
    for name in ("acc1", "acc2"):
        proxy_store.set_account_proxy(name, pid)

    url1, url2 = proxy_store.proxy_url_for("acc1"), proxy_store.proxy_url_for("acc2")
    assert url1 != url2
    assert url1.startswith("http://baseuser-") and "@gate.example.com:8000" in url1

    e1, e2 = proxy_store.egress_key_for("acc1"), proxy_store.egress_key_for("acc2")
    assert e1 != e2
    assert e1.startswith("http://gate.example.com:8000#")


def test_dynamic_session_is_sticky():
    pid = _mk_dynamic()["id"]
    proxy_store.set_account_proxy("acc1", pid)
    first = proxy_store.proxy_url_for("acc1")
    assert proxy_store.proxy_url_for("acc1") == first
    assert proxy_store.egress_key_for("acc1") == proxy_store.egress_key_for("acc1")


def test_dynamic_gateway_not_blocked_as_shared_egress(tmp_path):
    """核心收益：共用同一个旋转网关，多个号可以并发。"""
    pid = _mk_dynamic()["id"]
    for name in ("acc1", "acc2"):
        proxy_store.set_account_proxy(name, pid)
    engine = _engine(tmp_path, ["acc1", "acc2"])
    assert engine.effective_egress("acc1") != engine.effective_egress("acc2")
    assert [engine.try_assign(), engine.try_assign()] == ["acc1", "acc2"]


def test_rotate_session_changes_egress():
    pid = _mk_dynamic()["id"]
    proxy_store.set_account_proxy("acc1", pid)
    before_url = proxy_store.proxy_url_for("acc1")
    before_egress = proxy_store.egress_key_for("acc1")
    assert proxy_store.rotate_account_session("acc1")
    assert proxy_store.proxy_url_for("acc1") != before_url
    assert proxy_store.egress_key_for("acc1") != before_egress


def test_rotate_respects_min_interval():
    pid = _mk_dynamic(rotate_min_interval=3600)["id"]
    proxy_store.set_account_proxy("acc1", pid)
    assert proxy_store.rotate_account_session("acc1")
    assert proxy_store.rotate_account_session("acc1") is None


def test_rotate_on_risk_can_be_disabled():
    pid = _mk_dynamic(rotate_min_interval=0, rotate_on_risk=False)["id"]
    proxy_store.set_account_proxy("acc1", pid)
    assert proxy_store.rotate_account_session("acc1") is None


def test_rotate_is_noop_for_static_proxy():
    pid = _mk_static()["id"]
    proxy_store.set_account_proxy("acc1", pid)
    assert proxy_store.rotate_account_session("acc1") is None


def test_rotate_is_noop_when_no_proxy():
    assert proxy_store.rotate_account_session("acc1") is None


def test_sticky_ttl_expires_session():
    pid = _mk_dynamic(sticky_ttl=0)["id"]
    proxy_store.set_account_proxy("acc1", pid)
    fixed = proxy_store.proxy_url_for("acc1")
    assert proxy_store.proxy_url_for("acc1") == fixed

    proxy_store.update_proxy(pid, sticky_ttl=1)
    conn = sqlite3.connect(str(proxy_store.DB_PATH))
    conn.execute("UPDATE proxy_sessions SET rotated_at=?", (0.0,))
    conn.commit()
    conn.close()
    assert proxy_store.proxy_url_for("acc1") != fixed


def test_session_template_rendering():
    pid = _mk_dynamic(name="tpl", session_template="{session}")["id"]
    proxy_store.set_account_proxy("acc1", pid)
    url = proxy_store.proxy_url_for("acc1")
    assert "@gate.example.com:8000" in url
    assert "baseuser" not in url

    pid2 = _mk_dynamic(name="tpl2", host="g2.example.com",
                       session_template="acct-{user}-s-{session}")["id"]
    proxy_store.set_account_proxy("acc2", pid2)
    assert proxy_store.proxy_url_for("acc2").startswith("http://acct-baseuser-s-")


def test_unknown_template_falls_back():
    assert proxy_store.render_session_username("{nope}", "u", "s") == "u-s"
    assert proxy_store.render_session_username("", "", "s") == "s"
    assert proxy_store.render_session_username("", "u", "s") == "u-s"
    assert proxy_store.render_session_username("{session}", "u", "s") == "s"


def test_engine_uses_explicit_egress_key():
    cfg = AccountConfig(id="a", state_file=Path("nope.json"),
                        proxy="http://u:p@1.1.1.1:8080",
                        egress_key="http://1.1.1.1:8080#sess1")
    engine = AccountPool([cfg], account_proxy_enabled=True)
    assert engine.effective_egress("a") == "http://1.1.1.1:8080#sess1"
    engine.set_account_egress_key("a", "")
    assert engine.effective_egress("a") == "http://1.1.1.1:8080"


def test_engine_egress_key_ignored_when_proxy_disabled():
    cfg = AccountConfig(id="a", state_file=Path("nope.json"),
                        proxy="http://u:p@1.1.1.1:8080",
                        egress_key="http://1.1.1.1:8080#sess1")
    engine = AccountPool([cfg], account_proxy_enabled=False)
    assert engine.effective_egress("a") == "direct"


def test_egress_unhealthy_triggers_rotation_callback(tmp_path):
    pid = _mk_dynamic()["id"]
    proxy_store.set_account_proxy("acc1", pid)
    cfg = AccountConfig(id="acc1", state_file=tmp_path / "a.json",
                        proxy=proxy_store.proxy_url_for("acc1"),
                        egress_key=proxy_store.egress_key_for("acc1"))
    engine = AccountPool([cfg])
    seen = []
    engine.on_egress_unhealthy = lambda egress, reason: seen.append((egress, reason))
    engine.mark_egress_unhealthy(engine.effective_egress("acc1"), "风控", 120)
    assert seen and seen[0][1] == "风控"


def test_callback_exception_does_not_break_marking():
    cfg = AccountConfig(id="a", state_file=Path("nope.json"), proxy="http://1.1.1.1:80")
    engine = AccountPool([cfg])

    def boom(_e, _r):
        raise RuntimeError("boom")

    engine.on_egress_unhealthy = boom
    engine.mark_egress_unhealthy("http://1.1.1.1:80", "x", 60)
    assert engine.egress_is_unhealthy("http://1.1.1.1:80")


def test_rebinding_proxy_drops_old_session():
    dyn = _mk_dynamic()["id"]
    static = _mk_static()["id"]
    proxy_store.set_account_proxy("acc1", dyn)
    proxy_store.proxy_url_for("acc1")
    assert proxy_store.get_session("acc1") is not None
    proxy_store.set_account_proxy("acc1", static)
    assert proxy_store.get_session("acc1") is None


def test_unbinding_proxy_drops_session():
    dyn = _mk_dynamic()["id"]
    proxy_store.set_account_proxy("acc1", dyn)
    proxy_store.proxy_url_for("acc1")
    proxy_store.set_account_proxy("acc1", None)
    assert proxy_store.get_session("acc1") is None


def test_no_proxy_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(config, "PROXY", "http://default.example.com:3128")
    assert proxy_store.proxy_url_for("acc-nobody") == "http://default.example.com:3128"
    assert proxy_store.egress_key_for("acc-nobody") == "http://default.example.com:3128"


def test_schema_migration_adds_columns(tmp_path, monkeypatch):
    """老库（没有动态代理列）要能自动升级。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE proxies ("
        " id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,"
        " protocol TEXT NOT NULL DEFAULT 'http', host TEXT NOT NULL,"
        " port INTEGER NOT NULL, username TEXT DEFAULT '',"
        " password TEXT DEFAULT '', remark TEXT DEFAULT '',"
        " enabled INTEGER NOT NULL DEFAULT 1, created_at REAL)"
    )
    conn.execute("INSERT INTO proxies(id,name,protocol,host,port,enabled,created_at) "
                 "VALUES('px_old','old','http','1.2.3.4',8080,1,0)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(proxy_store, "DB_PATH", db)
    rows = proxy_store.list_proxies()
    assert rows and rows[0]["mode"] == "static"
    assert rows[0]["is_dynamic"] is False
    proxy_store.set_account_proxy("acc1", "px_old")
    assert proxy_store.egress_key_for("acc1") == "http://1.2.3.4:8080"


def test_probe_unknown_proxy():
    assert proxy_store.probe_proxy_record("px_nope")["ok"] is False


def test_probe_direct_returns_error():
    assert proxy_store.probe_exit_ip("")["ok"] is False


# ===== 回归：热路径不能做 DDL =====

def test_schema_setup_runs_once_per_process(tmp_path, monkeypatch):
    """建表/迁移每个库路径只跑一次。

    这是踩过的坑：把 ALTER TABLE 放进每次取连接的路径，DDL 需要写锁，
    撞上后台长连接就抛 database is locked；而抛异常的那条连接不会释放，
    锁不还、后续每次取连接都失败 —— 一次抖动放大成整片 500。
    """
    db = tmp_path / "once.db"
    monkeypatch.setattr(proxy_store, "DB_PATH", db)
    proxy_store._ready_paths.clear()
    calls = []
    real = proxy_store._init_schema
    monkeypatch.setattr(proxy_store, "_init_schema",
                        lambda c: (calls.append(1), real(c))[1])
    for _ in range(5):
        proxy_store.list_proxies()
    assert calls == [1]
    assert str(db) in proxy_store._ready_paths


def test_reads_survive_competing_writer(tmp_path, monkeypatch):
    """已有别处占着写锁时，读操作仍然要能拿到连接。

    修复前这条会抛 database is locked（取连接时做 DDL 要写锁）。
    """
    db = tmp_path / "contended.db"
    monkeypatch.setattr(proxy_store, "DB_PATH", db)
    proxy_store._ready_paths.clear()
    proxy_store.list_proxies()      # 首次建表

    holder = sqlite3.connect(str(db), timeout=0.5)
    holder.execute("BEGIN IMMEDIATE")   # 模拟后台长连接占住写锁
    try:
        for _ in range(3):
            assert proxy_store.list_proxies() == []
    finally:
        holder.rollback()
        holder.close()


def test_failed_init_closes_connection(tmp_path, monkeypatch):
    """初始化失败必须关掉连接，不能把写事务悬在那儿。"""
    db = tmp_path / "boom.db"
    monkeypatch.setattr(proxy_store, "DB_PATH", db)
    proxy_store._ready_paths.clear()

    def boom(_conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(proxy_store, "_init_schema", boom)
    with pytest.raises(sqlite3.OperationalError):
        proxy_store.list_proxies()
    assert str(db) not in proxy_store._ready_paths
