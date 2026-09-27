"""提取型动态IP（mode=extract）：解析提取结果、节点池分配、轮换、限流。

参考真实接口（ipdeep）：
    https://api.ipdeep.com/api/Pro/DynamicIp/GetIpByGenerateLink?id=xxx
返回 50 行，每行：
    gate.ipdeep.com:8080:d8449846000-res-country-JP-session-9686648000-sessiontime-5:Rv3G01Xw
即 host:port:username:password；username 里已编码 session 与国家，sessiontime 单位是分钟。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

import config
import proxy_store


IPDEEP_BODY = (
    "gate.ipdeep.com:8080:d8449846000-res-country-JP-session-9686648000-sessiontime-5:Rv3G01Xw\r\n"
    "gate.ipdeep.com:8080:d8449846000-res-country-JP-session-9686648001-sessiontime-5:Rv3G01Xw\r\n"
    "gate.ipdeep.com:8080:d8449846000-res-country-JP-session-9686648002-sessiontime-5:Rv3G01Xw\r\n"
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "pool_usage.db"
    monkeypatch.setattr(proxy_store, "DB_PATH", db)
    monkeypatch.setattr(config, "PROXY", "")
    proxy_store._ready_paths.clear()
    yield db


def _mk_extract(name="ipdeep-jp", url="https://api.ipdeep.com/GetIp?id=abc", **kw):
    return proxy_store.create_proxy(name, "http", "gate.ipdeep.com", 8080,
                                    mode="extract", extract_url=url, **kw)


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _stub_extract(monkeypatch, body=IPDEEP_BODY, calls=None, batches=False):
    import requests

    def fake_get(url, timeout=None, **kw):
        if calls is not None:
            calls.append(url)
        if batches:
            n = len(calls) if calls is not None else 1
            text = "".join(
                "gate.ipdeep.com:8080:u-batch%d-session-%d-sessiontime-5:pw\r\n" % (n, i)
                for i in range(2)
            )
            return _FakeResp(text)
        return _FakeResp(body)

    monkeypatch.setattr(requests, "get", fake_get)


# ===== 解析 =====

def test_parse_ipdeep_line():
    nodes = proxy_store.parse_extract_lines(IPDEEP_BODY)
    assert len(nodes) == 3
    n = nodes[0]
    assert n["host"] == "gate.ipdeep.com"
    assert n["port"] == 8080
    assert n["username"] == "d8449846000-res-country-JP-session-9686648000-sessiontime-5"
    assert n["password"] == "Rv3G01Xw"
    assert n["ttl"] == 300          # sessiontime-5 -> 5 分钟


def test_parse_handles_crlf_blank_and_comments():
    body = "\r\n# comment\r\n1.2.3.4:8080:u:p\r\n\r\n5.6.7.8:3128\r\n"
    nodes = proxy_store.parse_extract_lines(body)
    assert [n["host"] for n in nodes] == ["1.2.3.4", "5.6.7.8"]
    assert nodes[0]["username"] == "u" and nodes[0]["password"] == "p"
    assert nodes[1]["username"] == ""        # 无鉴权节点
    assert nodes[1]["port"] == 3128


def test_parse_other_common_shapes():
    n = proxy_store.parse_extract_lines("u1:p1@9.9.9.9:1080")[0]
    assert (n["host"], n["port"], n["username"], n["password"]) == ("9.9.9.9", 1080, "u1", "p1")
    n2 = proxy_store.parse_extract_lines("9.9.9.9:1080@u2:p2")[0]
    assert (n2["host"], n2["port"], n2["username"], n2["password"]) == ("9.9.9.9", 1080, "u2", "p2")


def test_parse_ignores_garbage():
    assert proxy_store.parse_extract_lines("") == []
    assert proxy_store.parse_extract_lines("not-a-proxy") == []
    assert proxy_store.parse_extract_lines("1.2.3.4:notaport:u:p") == []


def test_ttl_without_sessiontime_uses_default(monkeypatch):
    monkeypatch.setenv("DOLA_EXTRACT_NODE_TTL", "120")
    nodes = proxy_store.parse_extract_lines("1.2.3.4:8080:user:pass")
    assert nodes[0]["ttl"] == 120


# ===== 建记录 =====

def test_extract_requires_url():
    with pytest.raises(ValueError):
        proxy_store.create_proxy("bad", "http", "gate.x.com", 8080, mode="extract")


def test_extract_mode_stored():
    pid = _mk_extract()["id"]
    info = proxy_store.get_proxy(pid)
    assert info["mode"] == "extract"
    assert info["extract_url"].startswith("https://api.ipdeep.com")


# ===== 提取与分配 =====

def test_fetch_adds_nodes_and_dedupes(tmp_path, monkeypatch):
    pid = _mk_extract()["id"]
    _stub_extract(monkeypatch)
    r1 = proxy_store.fetch_extract_nodes(pid)
    assert r1["ok"] and r1["added"] == 3
    r2 = proxy_store.fetch_extract_nodes(pid)      # 同一批，不该重复入库
    assert r2["ok"] and r2["added"] == 0
    assert len(proxy_store.list_nodes(pid)) == 3


def test_fetch_records_error_on_failure(tmp_path, monkeypatch):
    pid = _mk_extract()["id"]
    import requests

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(requests, "get", boom)
    r = proxy_store.fetch_extract_nodes(pid)
    assert r["ok"] is False and "network down" in r["error"]
    assert proxy_store.get_proxy(pid)["extract_last_error"]


def test_accounts_get_distinct_nodes(tmp_path, monkeypatch):
    pid = _mk_extract()["id"]
    _stub_extract(monkeypatch)
    for name in ("acc1", "acc2"):
        proxy_store.set_account_proxy(name, pid)
    u1 = proxy_store.proxy_url_for("acc1")
    u2 = proxy_store.proxy_url_for("acc2")
    assert u1 != u2
    assert u1.startswith("http://d8449846000-") and "@gate.ipdeep.com:8080" in u1
    e1, e2 = proxy_store.egress_key_for("acc1"), proxy_store.egress_key_for("acc2")
    assert e1 != e2 and "#node" in e1


def test_node_is_sticky_per_account(tmp_path, monkeypatch):
    pid = _mk_extract()["id"]
    _stub_extract(monkeypatch)
    proxy_store.set_account_proxy("acc1", pid)
    first = proxy_store.proxy_url_for("acc1")
    assert proxy_store.proxy_url_for("acc1") == first


def test_nodes_not_shared_between_accounts(tmp_path, monkeypatch):
    pid = _mk_extract()["id"]
    _stub_extract(monkeypatch)
    names = ["acc1", "acc2", "acc3"]
    for n in names:
        proxy_store.set_account_proxy(n, pid)
    urls = {proxy_store.proxy_url_for(n) for n in names}
    assert len(urls) == 3
    node_ids = {proxy_store.get_session(n)["node_id"] for n in names}
    assert len(node_ids) == 3


def test_rotate_moves_to_another_node(tmp_path, monkeypatch):
    pid = _mk_extract()["id"]
    _stub_extract(monkeypatch)
    proxy_store.set_account_proxy("acc1", pid)
    before = proxy_store.proxy_url_for("acc1")
    # 刚绑定就被「自动轮换间隔」挡住，是预期行为
    assert proxy_store.rotate_account_session("acc1") is None
    # 面板手动换 IP 不受该间隔限制
    assert proxy_store.rotate_account_session("acc1", force=True)
    assert proxy_store.proxy_url_for("acc1") != before


def test_pool_exhausted_triggers_new_extract(tmp_path, monkeypatch):
    """节点用完了要自动再调一次提取（真实厂商每次返回新 session）。"""
    pid = _mk_extract()["id"]
    calls = []
    _stub_extract(monkeypatch, calls=calls, batches=True)
    for n in ("acc1", "acc2", "acc3"):
        proxy_store.set_account_proxy(n, pid)
        proxy_store.proxy_url_for(n)
    assert len(calls) >= 2
    assert proxy_store.get_session("acc3")["node_id"]


def test_rotating_quickly_does_not_starve_pool(tmp_path, monkeypatch):
    """提取间隔内不该反复调接口（保护按次计费的配额）。"""
    pid = _mk_extract(extract_interval=99999)["id"]
    calls = []
    _stub_extract(monkeypatch, calls=calls)
    proxy_store.set_account_proxy("acc1", pid)
    proxy_store.proxy_url_for("acc1")
    n_after_first = len(calls)
    for _ in range(3):
        proxy_store.rotate_account_session("acc1", force=True)
    assert len(calls) == n_after_first


def test_expired_nodes_are_skipped(tmp_path, monkeypatch):
    pid = _mk_extract()["id"]
    _stub_extract(monkeypatch)
    proxy_store.set_account_proxy("acc1", pid)
    proxy_store.proxy_url_for("acc1")
    conn = sqlite3.connect(str(proxy_store.DB_PATH))
    conn.execute("UPDATE proxy_nodes SET expires_at=?", (time.time() - 10,))
    conn.commit()
    conn.close()
    assert "9686648001" in proxy_store.proxy_url_for("acc1")


def test_dead_nodes_are_skipped(tmp_path, monkeypatch):
    pid = _mk_extract()["id"]
    _stub_extract(monkeypatch)
    proxy_store.set_account_proxy("acc1", pid)
    first = proxy_store.proxy_url_for("acc1")
    node_id = proxy_store.get_session("acc1")["node_id"]
    proxy_store.mark_node_dead(node_id)
    after = proxy_store.proxy_url_for("acc1")
    assert after != first
    assert "9686648000" not in after


def test_rotate_on_risk_disabled_for_extract(tmp_path, monkeypatch):
    pid = _mk_extract(rotate_on_risk=False)["id"]
    _stub_extract(monkeypatch)
    proxy_store.set_account_proxy("acc1", pid)
    proxy_store.proxy_url_for("acc1")
    assert proxy_store.rotate_account_session("acc1", force=True) is None


def test_rotate_min_interval_blocks_auto_but_not_manual(tmp_path, monkeypatch):
    pid = _mk_extract(rotate_min_interval=3600)["id"]
    _stub_extract(monkeypatch)
    proxy_store.set_account_proxy("acc1", pid)
    proxy_store.proxy_url_for("acc1")
    assert proxy_store.rotate_account_session("acc1", force=True)
    assert proxy_store.rotate_account_session("acc1") is None          # 自动被挡
    assert proxy_store.rotate_account_session("acc1", force=True)      # 手动可以


def test_prune_removes_expired_and_broken(tmp_path, monkeypatch):
    pid = _mk_extract()["id"]
    _stub_extract(monkeypatch)
    proxy_store.fetch_extract_nodes(pid)
    nodes = proxy_store.list_nodes(pid)
    assert len(nodes) == 3
    proxy_store.mark_node_dead(nodes[0]["id"])
    conn = sqlite3.connect(str(proxy_store.DB_PATH))
    conn.execute("UPDATE proxy_nodes SET expires_at=? WHERE id=?",
                 (time.time() - 1, nodes[1]["id"]))
    conn.commit()
    conn.close()
    assert proxy_store.prune_nodes(pid) >= 2
    assert len(proxy_store.list_nodes(pid)) == 1


def test_mark_node_ok_records_exit_ip(tmp_path, monkeypatch):
    pid = _mk_extract()["id"]
    _stub_extract(monkeypatch)
    proxy_store.fetch_extract_nodes(pid)
    nid = proxy_store.list_nodes(pid)[0]["id"]
    proxy_store.mark_node_ok(nid, "118.18.64.245")
    row = [n for n in proxy_store.list_nodes(pid) if n["id"] == nid][0]
    assert row["last_exit_ip"] == "118.18.64.245"
    assert row["fail_count"] == 0


def test_fetch_without_url_reports_clearly(tmp_path):
    pid = _mk_extract()["id"]
    proxy_store.update_proxy(pid, extract_url="")
    r = proxy_store.fetch_extract_nodes(pid)
    assert r["ok"] is False and "未配置提取链接" in r["error"]


def test_extract_falls_back_to_default_proxy_when_empty(tmp_path, monkeypatch):
    """提取接口拿不到节点时，回落到默认代理，而不是把号挂空。"""
    monkeypatch.setattr(config, "PROXY", "http://default.example.com:3128")
    pid = _mk_extract()["id"]
    _stub_extract(monkeypatch, "提取失败/没有可用节点")
    proxy_store.set_account_proxy("acc1", pid)
    assert proxy_store.egress_key_for("acc1") == "http://default.example.com:3128"
    assert proxy_store.proxy_url_for("acc1") == "http://default.example.com:3128"


def test_node_marked_dead_when_egress_unhealthy(tmp_path, monkeypatch):
    """出口被风控 -> 当前节点必须标废，轮换后才不会又挑回它。"""
    import sys
    import types as _types

    v = _types.ModuleType("video_worker_ui")
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
    d = _types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d

    import browser_pool
    pid = _mk_extract()["id"]
    _stub_extract(monkeypatch)
    accounts_dir = tmp_path / "accounts" / "acc1"
    accounts_dir.mkdir(parents=True)
    bp = browser_pool.BrowserPool(accounts_dir=str(tmp_path / "accounts"))
    proxy_store.set_account_proxy("acc1", pid)
    bp.list_accounts()

    used_node = proxy_store.get_session("acc1")["node_id"]
    engine = bp._engine or bp._sync_engine()
    engine.on_egress_unhealthy = bp._on_egress_unhealthy
    engine.mark_egress_unhealthy(proxy_store.egress_key_for("acc1"), "风控", 120)

    rows = {n["id"]: n for n in proxy_store.list_nodes(pid)}
    assert rows[used_node]["dead"] == 1, "坏节点没被标废"
    assert proxy_store.get_session("acc1")["node_id"] != used_node, "没有换到新节点"


def test_prune_is_scoped_to_one_proxy(tmp_path):
    """prune 只能清指定代理的节点。

    历史 bug：SQL 写成 `WHERE dead=1 OR ... OR fail_count>=3 AND proxy_id=?`，
    AND 优先级更高，导致 prune 一个代理会把别的代理的节点池一起删掉。
    """
    p1 = _mk_extract(name="scope-a", url="https://a/b")["id"]
    p2 = _mk_extract(name="scope-b", url="https://c/d")["id"]
    proxy_store._insert_nodes(
        p1, [{"host": "1.1.1.1", "port": 80, "username": "u1",
              "password": "", "exit_hint": "", "ttl": 300}], "http")
    proxy_store._insert_nodes(
        p2, [{"host": "2.2.2.2", "port": 80, "username": "u2",
              "password": "", "exit_hint": "", "ttl": 300}], "http")
    proxy_store.mark_node_dead(proxy_store.list_nodes(p2)[0]["id"])

    removed = proxy_store.prune_nodes(p1)

    assert len(proxy_store.list_nodes(p1)) == 1, "自己没废节点，不该被清理"
    assert len(proxy_store.list_nodes(p2)) == 1, "别的代理的节点被误删了"
