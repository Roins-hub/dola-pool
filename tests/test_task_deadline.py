"""任务总时限：max_attempts=0 时不限次数，在 deadline 之前一轮轮换号重试。"""
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
    # 失败后的内存冷却(120 秒)会挡住下一轮;测试里只验证「时限内再来一轮」的调度本身
    bp._recently_failed = lambda *_a, **_k: False
    return browser_pool, bp


def test_unlimited_attempts_retry_another_round_before_deadline(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds, *a, **k):
        await real_sleep(0)

    monkeypatch.setattr(browser_pool.asyncio, "sleep", fast_sleep)
    calls = []

    async def fake(account, *a, **k):
        calls.append(account)
        if len(calls) <= 2:
            raise RuntimeError("上游临时失败")
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake

    result = _run(bp.generate_video("p", None, 10, "seedance-2.5",
                                    max_attempts=0, deadline=time.time() + 60))

    # 第一轮两个号都失败,时限未到 → 第二轮换号出片成功(超过默认 3 次也不受限)
    assert len(calls) == 3
    assert result["account"] == calls[-1]


def test_expired_deadline_dispatches_nothing(tmp_path, monkeypatch):
    _, bp = _pool(tmp_path, monkeypatch)
    calls = []

    async def fake(account, *a, **k):
        calls.append(account)
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake

    with pytest.raises(RuntimeError):
        _run(bp.generate_video("p", None, 10, "seedance-2.5",
                               max_attempts=0, deadline=time.time() - 1))
    assert calls == []


def test_config_defaults():
    assert config.TASK_DEADLINE == 600
    assert config.VIDEO_MAX_ATTEMPTS == 0
