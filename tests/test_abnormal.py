"""P5：派发后 5 分钟无任何回执 → 账号进【异常】组，任务提示「请重试」。"""
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
    monkeypatch.setattr(config, "DAILY_LIMIT", 4)
    monkeypatch.setattr(browser_pool, "DAILY_LIMIT", 4)
    monkeypatch.setattr(config, "V20_WAIT_SECONDS", 0)
    monkeypatch.setattr(config, "HELLO_PROBE_SUBMIT_GAP_SECONDS", 0)
    accounts_dir = tmp_path / "accounts"
    for name in names:
        (accounts_dir / name).mkdir(parents=True, exist_ok=True)
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir),
                                  db_path=str(tmp_path / "pool.db"))
    for name in names:
        bp.set_login_status(name, True)
    return browser_pool, bp


def test_no_ack_marks_account_abnormal_and_switches(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    from pure_api_gen import AbnormalNoAckError

    calls = []

    async def fake(account, *a, **k):
        calls.append(account)
        raise AbnormalNoAckError("生视频过程中出现异常情况，请重试（提交后 300 秒内上游没有任何回执）")

    bp._generate_effective = fake

    with pytest.raises(RuntimeError) as exc:
        _run(bp.generate_video("p", None, 10, "seedance-2.5"))

    # 两个号都被换了一遍，且都进了【异常】组
    assert calls == ["acc1", "acc2"]
    for name in ("acc1", "acc2"):
        row = bp._meta(name)
        assert row["abnormal_until"] > 0
        assert "请重试" in row["abnormal_reason"]
    assert "请重试" in str(exc.value)
    groups = {a["name"]: a["group"] for a in bp.list_accounts()}
    assert groups == {"acc1": browser_pool.GROUP_ABNORMAL, "acc2": browser_pool.GROUP_ABNORMAL}


def test_abnormal_account_is_not_dispatched(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    bp._mark_abnormal("acc1", "5 分钟无回执")
    calls = []

    async def fake(account, *a, **k):
        calls.append(account)
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake

    result = _run(bp.generate_video("p", None, 10, "seedance-2.5"))

    assert result["account"] == "acc2"      # 异常号被跳过
    assert calls == ["acc2"]


def test_no_ack_seconds_default_is_5_minutes():
    import pure_api_gen
    assert config.NO_ACK_SECONDS == 300
    assert pure_api_gen.NO_ACK_SECONDS == config.NO_ACK_SECONDS
    assert "请重试" in pure_api_gen.ABNORMAL_RETRY_HINT
