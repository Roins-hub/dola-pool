"""browser_pool 与选号引擎/proxy_store 的集成验证。

注入桩模块（video_worker_ui / dola_client），避免依赖 patchright/fastapi。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _stubs():
    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError", "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))

    async def _gen(*_a, **_k):
        raise RuntimeError("stub")
    async def _resume(*_a, **_k):
        raise RuntimeError("stub")
    v.generate_video = _gen
    v.resume_video = _resume
    sys.modules["video_worker_ui"] = v

    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d
    yield


def _seed_accounts(tmp_path):
    import browser_pool, proxy_store
    import config
    config.POOL_DB_PATH = str(tmp_path / "pool_usage.db")
    proxy_store.DB_PATH = Path(config.POOL_DB_PATH)
    accounts_dir = tmp_path / "accounts"
    for name in ("acc1", "acc2", "acc3"):
        d = accounts_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / ".dola-browser").write_text("")
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir), max_concurrency=2)
    assert bp.accounts == ["acc1", "acc2", "acc3"]
    for name in bp.accounts:
        bp.set_login_status(name, True)
    return bp, proxy_store


def test_list_accounts_has_engine_fields(tmp_path):
    bp, _ = _seed_accounts(tmp_path)
    rows = bp.list_accounts()
    row = rows[0]
    for key in ("status", "weight", "preferred", "effective_egress", "remaining", "used_today"):
        assert key in row
    assert {r["status"] for r in rows} == {"healthy"}


def test_preview_route_and_preferred(tmp_path):
    bp, _ = _seed_accounts(tmp_path)
    preview = bp.preview_route()
    assert preview["next_account_id"] == "acc1"
    bp.set_preferred("acc2")
    preview = bp.preview_route()
    assert preview["strategy"] == "pinned"
    assert preview["next_account_id"] == "acc2"


def test_set_weight(tmp_path):
    bp, _ = _seed_accounts(tmp_path)
    bp.set_weight("acc3", 10)
    assert next(a for a in bp.list_accounts() if a["name"] == "acc3")["weight"] == 10


def test_rebalance_and_bind_proxies(tmp_path):
    bp, proxy_store = _seed_accounts(tmp_path)
    p1 = proxy_store.create_proxy("jp1", "http", "10.0.0.1", 8000)
    p2 = proxy_store.create_proxy("jp2", "http", "10.0.0.2", 8000)
    proxy_store.set_account_proxy("acc1", p1["id"])
    proxy_store.set_account_proxy("acc2", p1["id"])
    result = bp.rebalance_proxy_bindings()
    assert sum(result["distribution"].values()) == 3
    assert set(result["distribution"].values()) == {1, 2}
    assert result["busy_count"] == 0
    bound = bp.bind_proxies(
        ["http://u:p@1.2.3.4:1080", "http://u:p@5.6.7.8:1080", "http://u:p@9.9.9.9:1080"]
    )
    assert len(bound["bound"]) == 3
    egresses = sorted(a["effective_egress"] for a in bp.list_accounts())
    assert egresses == ["http://1.2.3.4:1080", "http://5.6.7.8:1080", "http://9.9.9.9:1080"]


def test_engine_snapshot(tmp_path):
    bp, _ = _seed_accounts(tmp_path)
    meta = bp.engine_snapshot()
    assert meta["healthy_count"] == 3
    assert meta["isolation"]["ok"] is False  # 无代理时同出口(direct)隔离不ok


def test_set_source_and_list(tmp_path):
    bp, _ = _seed_accounts(tmp_path)
    bp.set_source("acc1", "login")
    bp.set_source("acc2", "cookie")
    src = {x["name"]: x["source"] for x in bp.list_accounts()}
    assert src["acc1"] == "login"
    assert src["acc2"] == "cookie"
    assert src["acc3"] == ""
