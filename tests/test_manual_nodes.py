"""动态IP·粘贴节点：在面板里直接把 host:port:user:pass 批量粘进节点池。

对应面板「添加代理 → 出口类型：动态IP → 直接粘贴节点」。
"""
from __future__ import annotations

import time

import pytest

import config
import proxy_store


IPWEB_LINES = (
    "gate1.ipweb.cc:7778:100378695217-qRhvVmkl:f761d19a7fdb8498f69e43de6bd1de31\r\n"
    "gate1.ipweb.cc:7778:100378695217-rF4EnjPm:c021f9128d837d95409f3d5c6f9522c2\n"
    "gate1.ipweb.cc:7778:100378695217-xRfS0bv8:5be1c0017d6a14a78570d7ac32663f29\n"
)

# ipdeep 那种带 sessiontime 的形态：有效期应该按 sessiontime 算（分钟）
IPDEEP_STYLE = ("gate.ipdeep.com:8080:u-res-country-JP-session-1-sessiontime-5:pw\n")


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy_store, "DB_PATH", tmp_path / "pool_usage.db")
    monkeypatch.setattr(config, "PROXY", "")
    proxy_store._ready_paths.clear()
    yield


def _mk_paste(name="ipweb-jp", text=IPWEB_LINES, **kw):
    return proxy_store.create_proxy(name, "http", "", 0, mode="extract",
                                    nodes_text=text, **kw)


def test_create_with_pasted_nodes_derives_gate_address():
    p = _mk_paste()
    assert p["mode"] == "extract"
    # 面板不填 IP/端口，记录的网关地址取粘贴的第一行
    assert (p["host"], p["port"]) == ("gate1.ipweb.cc", 7778)
    nodes = proxy_store.list_nodes(p["id"])
    assert len(nodes) == 3
    assert nodes[0]["username"].startswith("100378695217-")
    assert nodes[0]["password"]
    assert nodes[0]["dead"] == 0


def test_pasted_credentials_do_not_expire():
    """手工粘贴的固定凭据默认不过期，否则 5 分钟后池子就空了。"""
    p = _mk_paste()
    assert all(n["expires_at"] == 0 for n in proxy_store.list_nodes(p["id"]))


def test_sessiontime_line_still_expires():
    p = _mk_paste(name="ipdeep-paste", text=IPDEEP_STYLE)
    node = proxy_store.list_nodes(p["id"])[0]
    assert 0 < node["expires_at"] - time.time() <= 300


def test_extract_requires_url_or_nodes():
    with pytest.raises(ValueError):
        proxy_store.create_proxy("empty", "http", "", 0, mode="extract")


def test_extract_rejects_unparsable_nodes():
    with pytest.raises(ValueError):
        proxy_store.create_proxy("bad", "http", "", 0, mode="extract",
                                 nodes_text="这不是节点\n随便写点什么")


def test_static_still_requires_host_and_port():
    with pytest.raises(ValueError):
        proxy_store.create_proxy("nohost", "http", "", 0)
    p = proxy_store.create_proxy("ok", "http", "1.2.3.4", 3128)
    assert (p["mode"], p["host"], p["port"]) == ("static", "1.2.3.4", 3128)


def test_add_nodes_appends_and_dedups():
    p = _mk_paste(text=IPWEB_LINES.splitlines()[0] + "\n")
    assert len(proxy_store.list_nodes(p["id"])) == 1
    r = proxy_store.add_nodes_from_text(p["id"], IPWEB_LINES)
    assert r == {"ok": True, "added": 2, "parsed": 3}
    assert len(proxy_store.list_nodes(p["id"])) == 3


def test_add_nodes_rejects_empty_text():
    p = _mk_paste()
    r = proxy_store.add_nodes_from_text(p["id"], "  \n\n# 注释\n")
    assert r["ok"] is False and r["added"] == 0


def test_paste_pool_never_calls_extract_interface():
    """没有提取链接的池子不该去调接口。"""
    p = _mk_paste()
    assert proxy_store._should_extract(proxy_store.get_proxy(p["id"])) is False


def test_each_account_gets_its_own_node():
    p = _mk_paste()
    proxy_store.set_account_proxy("ck10", p["id"])
    proxy_store.set_account_proxy("ck11", p["id"])
    u1 = proxy_store.proxy_url_for("ck10")
    u2 = proxy_store.proxy_url_for("ck11")
    assert "gate1.ipweb.cc:7778" in u1 and u1 != u2      # 一个节点只给一个号
    assert proxy_store.proxy_url_for("ck10") == u1       # 已绑定的号不会漂
    proxy_store.rotate_account_session("ck10", force=True)
    assert proxy_store.proxy_url_for("ck10") != u1       # 换 IP = 换节点


def test_dead_pool_falls_back_to_default_proxy(monkeypatch):
    monkeypatch.setattr(config, "PROXY", "http://default:8888")
    p = _mk_paste()
    proxy_store.set_account_proxy("ck12", p["id"])
    for n in proxy_store.list_nodes(p["id"]):
        proxy_store.mark_node_dead(n["id"])
    assert proxy_store.proxy_url_for("ck12") == "http://default:8888"
