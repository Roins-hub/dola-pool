"""P4：「你好」风控探测 + 风控组 + 待激活/激活 + 人工恢复。

实测背景（2026-09-23，线上真号）：把 sessionid/sid_tt 等 9 个会话 cookie 换成死值后，
发「你好」照样收到匿名回复 —— 所以判据必须同时看登录标记（x-tt-agw-login），
只看"有没有回复"会把已登出的号判成健康号。
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config


@pytest.fixture(autouse=True)
def _stubs():
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


def _pool(tmp_path, monkeypatch, names=("acc1", "acc2"), verify=True):
    import browser_pool

    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 4)
    monkeypatch.setattr(browser_pool, "DAILY_LIMIT", 4)
    # 纯 API 开关打开 + 造出 cookie_state.json，让这个号走「可探测」路径
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    accounts_dir = tmp_path / "accounts"
    for name in names:
        d = accounts_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "cookie_state.json").write_text('{"source":"cookie"}', encoding="utf-8")
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir),
                                  db_path=str(tmp_path / "pool.db"))
    if verify:
        for name in names:
            bp.set_login_status(name, True)
    else:
        for name in names:
            bp.ensure_account(name)      # 建行但不写登录态 → 新号
    return browser_pool, bp


def _fake_probe(browser_pool, monkeypatch, result, counter=None):
    def fake(state, proxy="", prompt=None, timeout_sec=None):
        if counter is not None:
            counter.append(state)
        return dict(result)

    monkeypatch.setattr(browser_pool, "hello_probe", fake)


OK_PROBE = {"ok": True, "status": "ok", "login_ok": True, "replied": True,
            "reason": "replied", "reply": "你好呀！", "elapsed": 2.9}
KICKED_PROBE = {"ok": False, "status": "logged_out", "login_ok": False, "replied": True,
                "reason": "被登出（x-tt-agw-login=0）", "reply": "你好呀！", "elapsed": 3.1}
SILENT_PROBE = {"ok": False, "status": "no_reply", "login_ok": True, "replied": False,
                "reason": "60 秒内没有回复", "reply": "", "elapsed": 60.0}
# 软失败：上游 710022002 拒绝 / 网络不通 —— 实测健康号也会遇到，绝不能据此判风控
SOFT_ERROR_PROBE = {"ok": False, "status": "error", "login_ok": True, "replied": False,
                    "reason": "710022002: 当前服务访问频繁，请稍后重试",
                    "reply": "", "elapsed": 2.9}


def test_ok_probe_keeps_account_healthy(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    _fake_probe(browser_pool, monkeypatch, OK_PROBE)

    result = bp.probe_hello("acc1")

    assert result["ok"] is True
    row = bp._meta("acc1")
    assert row["probe_ok_at"] > 0
    assert row["activated_at"] > 0
    group = next(a for a in bp.list_accounts() if a["name"] == "acc1")["group"]
    assert group == browser_pool.GROUP_FULL


def test_kicked_account_goes_risk_even_if_it_replies(tmp_path, monkeypatch):
    """能聊但已被登出（cookie 失效）→ 必须进风控组，这是实测踩过的坑。"""
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    _fake_probe(browser_pool, monkeypatch, KICKED_PROBE)

    assert bp.probe_hello("acc1")["ok"] is False

    row = bp._meta("acc1")
    assert row["risk_control"] == 1
    assert row["risk_since"] > 0
    assert "被登出" in row["risk_reason"]
    group = next(a for a in bp.list_accounts() if a["name"] == "acc1")["group"]
    assert group == browser_pool.GROUP_RISK


def test_silent_account_goes_risk(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    _fake_probe(browser_pool, monkeypatch, SILENT_PROBE)

    assert bp.probe_hello("acc1")["ok"] is False
    assert next(a for a in bp.list_accounts() if a["name"] == "acc1")["group"] == browser_pool.GROUP_RISK


def test_soft_error_does_not_lock_account(tmp_path, monkeypatch):
    """探测本身失败（上游限流/网络）→ 只跳过本次派发，绝不判风控。

    2026-09-23 线上实测踩过：健康号 acc110 的探测被上游 710022002 拒绝，
    按软失败判风控会把好号永久锁死。
    """
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    _fake_probe(browser_pool, monkeypatch, SOFT_ERROR_PROBE)

    result = bp.probe_hello("acc1")

    assert result["ok"] is False
    row = bp._meta("acc1")
    assert row["risk_control"] == 0          # 不锁号
    assert row["risk_reason"] == ""
    assert "710022002" in row["probe_result"]
    assert next(a for a in bp.list_accounts() if a["name"] == "acc1")["group"] == browser_pool.GROUP_FULL


def test_probe_result_is_cached_until_forced(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    calls = []
    _fake_probe(browser_pool, monkeypatch, OK_PROBE, counter=calls)
    monkeypatch.setattr(config, "HELLO_PROBE_CACHE_SECONDS", 300)

    bp.probe_hello("acc1")
    cached = bp.probe_hello("acc1")           # 300 秒内复用
    assert len(calls) == 1
    assert cached["cached"] is True
    assert cached["probed"] is False          # 复用不算真探（不占提交通道）

    monkeypatch.setattr(config, "HELLO_PROBE_CACHE_SECONDS", 0)
    bp.probe_hello("acc1")                    # 关掉缓存 → 每次都真探
    assert len(calls) == 2


def test_login_ok_zero_is_risk_group(tmp_path, monkeypatch):
    """验证过的账号掉登录 = 被登出 → 风控组（不是悄悄失效）。"""
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    bp.set_login_status("acc1", False)

    row = next(a for a in bp.list_accounts() if a["name"] == "acc1")
    assert row["group"] == browser_pool.GROUP_RISK


def test_never_verified_account_is_pending(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch, names=("new1",), verify=False)
    # 新号：没有 login_ok 记录 → 待激活，不参与派发
    row = next(a for a in bp.list_accounts() if a["name"] == "new1")
    assert row["group"] == browser_pool.GROUP_PENDING


def test_recover_risk_returns_account_to_pending(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    _fake_probe(browser_pool, monkeypatch, KICKED_PROBE)
    bp.probe_hello("acc1")
    assert next(a for a in bp.list_accounts() if a["name"] == "acc1")["group"] == browser_pool.GROUP_RISK

    out = bp.recover_account("acc1", "risk")

    assert out["ok"] is True
    row = bp._meta("acc1")
    assert row["risk_control"] == 0
    assert row["login_ok"] is None            # 登录态要靠重新探测确认
    assert next(a for a in bp.list_accounts() if a["name"] == "acc1")["group"] == browser_pool.GROUP_PENDING

    # 恢复后探测通过 → 回到满额
    _fake_probe(browser_pool, monkeypatch, OK_PROBE)
    bp.probe_hello("acc1")
    assert next(a for a in bp.list_accounts() if a["name"] == "acc1")["group"] == browser_pool.GROUP_FULL


def test_recover_abnormal(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    bp._mark_abnormal("acc1", "5 分钟无回执")
    assert next(a for a in bp.list_accounts() if a["name"] == "acc1")["group"] == browser_pool.GROUP_ABNORMAL

    bp.recover_account("acc1", "abnormal")

    assert bp._meta("acc1")["abnormal_until"] == 0
    assert next(a for a in bp.list_accounts() if a["name"] == "acc1")["group"] == browser_pool.GROUP_FULL


def test_gate_blocks_kicked_account_from_dispatch(tmp_path, monkeypatch):
    """派发前的探测门：探测不通过的号直接被跳过（不会白提交一次出片）。"""
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    _fake_probe(browser_pool, monkeypatch, KICKED_PROBE)
    calls = []

    async def fake_gen(account, *a, **k):
        calls.append(account)
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake_gen
    monkeypatch.setattr(config, "V20_WAIT_SECONDS", 0)
    monkeypatch.setattr(config, "HELLO_PROBE_SUBMIT_GAP_SECONDS", 0)

    with pytest.raises(RuntimeError):
        asyncio.new_event_loop().run_until_complete(
            bp.generate_video("p", None, 10, "seedance-2.5"))

    assert calls == []          # 一个都没真的提交
    assert next(a for a in bp.list_accounts() if a["name"] == "acc1")["group"] == browser_pool.GROUP_RISK
