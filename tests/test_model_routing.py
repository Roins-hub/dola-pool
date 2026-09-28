"""P2：按模型分流选号（2.0 只吃满额；2.5 先半额、后满额；满额空了则排队）。"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config


@pytest.fixture(autouse=True)
def _stubs():
    """与 test_concurrency / test_quota_reset 同一套桩，避免依赖 patchright 与真实上游。"""
    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError",
                "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))

    async def _gen(*_a, **_k):
        raise RuntimeError("stub")

    v.generate_video = _gen
    v.resume_video = _gen
    sys.modules["video_worker_ui"] = v

    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d
    yield


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _pool(tmp_path, monkeypatch, names=("acc1", "acc2")):
    import browser_pool

    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    # 固定每日上限 4 点：别的用例会改全局 DAILY_LIMIT（test_db_lock_leak 设 99），
    # 不固定住，这些额度分组断言就会随执行顺序变化。
    monkeypatch.setattr(config, "DAILY_LIMIT", 4)
    monkeypatch.setattr(browser_pool, "DAILY_LIMIT", 4)
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(exist_ok=True)
    for name in names:
        (accounts_dir / name).mkdir(exist_ok=True)
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir),
                                  db_path=str(tmp_path / "pool.db"))
    for name in names:
        bp.set_login_status(name, True)
    return browser_pool, bp


def _record(bp, calls):
    async def fake(account, prompt, ratio, duration, model, *, on_conversation_id,
                   on_poll, on_balance, reference_image_paths):
        calls.append((account, model))
        return {"account": account, "local_path": "x.mp4", "conversation_id": "conv"}

    bp._generate_effective = fake


def test_allowed_groups_per_model(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)

    assert bp._allowed_quota_groups("seedance-2.0") == (browser_pool.GROUP_FULL,)
    assert bp._allowed_quota_groups("seedance_v2.0") == (browser_pool.GROUP_FULL,)
    assert bp._allowed_quota_groups("seedance-2.5") == (browser_pool.GROUP_HALF,
                                                       browser_pool.GROUP_FULL)


def test_quota_group_is_about_points_only(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)

    assert bp._quota_group({"remaining": 4, "limit": 4}) == browser_pool.GROUP_FULL
    assert bp._quota_group({"remaining": 2, "limit": 4}) == browser_pool.GROUP_HALF
    assert bp._quota_group({"remaining": 1, "limit": 4}) == browser_pool.GROUP_COOLING
    assert bp._quota_group({"remaining": 0, "limit": 4}) == browser_pool.GROUP_COOLING


def test_v25_prefers_half_group(tmp_path, monkeypatch):
    """2.5 优先吃只有 2 点的半额号，把满额号留给 2.0。"""
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    bp._claim("acc2", 2)          # acc2 变半额（剩 2 点）
    calls = []
    _record(bp, calls)

    result = _run(bp.generate_video("p", None, 10, "seedance-2.5"))

    assert result["account"] == "acc2", calls
    assert bp.used_today("acc2") == 4          # 2 + 2 = 4，正好用完
    assert bp.used_today("acc1") == 0          # 满额号没被动


def test_v25_falls_back_to_full_when_no_half(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)   # 两个都是满额
    calls = []
    _record(bp, calls)

    result = _run(bp.generate_video("p", None, 10, "seedance-2.5"))

    assert result["account"] in ("acc1", "acc2")
    assert bp.used_today(result["account"]) == 2      # 4 → 2，留了半额


def test_v20_only_takes_full_group(tmp_path, monkeypatch):
    """只有半额号时，2.0 不能拿它凑数（2 点 < 3 点）。"""
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    bp._claim("acc1", 2)          # acc1 半额
    bp._claim("acc2", 2)          # acc2 半额
    calls = []
    _record(bp, calls)
    monkeypatch.setattr(config, "V20_WAIT_SECONDS", 0)   # 不排队，立刻失败便于断言

    with pytest.raises(browser_pool.AllAccountsGroupEmptyError) as exc:
        _run(bp.generate_video("p", None, 10, "seedance-2.0"))

    assert "满额" in str(exc.value)
    assert calls == []


def test_v20_picks_full_and_leaves_cooling(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    calls = []
    _record(bp, calls)

    result = _run(bp.generate_video("p", None, 10, "seedance-2.0"))

    assert result["account"] in ("acc1", "acc2")
    # 3 点用掉后只剩 1 点 → 冷却组，当天不再参与派发
    row = next(a for a in bp.list_accounts() if a["name"] == result["account"])
    assert row["remaining"] == 1
    assert row["group"] == browser_pool.GROUP_COOLING


def test_blocked_groups_never_dispatched(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    bp._set_risk("acc1", "你好无回复")          # 风控：永久不派发
    bp.set_login_status("acc2", False)          # 登录失效 → 风控口径之外，但也不该出片
    calls = []
    _record(bp, calls)
    monkeypatch.setattr(config, "V20_WAIT_SECONDS", 0)
    # 一个风控（组级阻断）、一个登录失效（_schedulable 阻断）→ 都不许出片
    with pytest.raises(RuntimeError):
        _run(bp.generate_video("p", None, 10, "seedance-2.5"))
    assert calls == []
