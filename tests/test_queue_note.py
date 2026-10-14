"""2.2.6：排队 / 换号原因（含 dola 原话）写进任务，面板「报错/备注」显示。

线上 10-14：AWS 73 个号共用直连出口被 710022002 限流，任务在面板上只显示「排队中」，看不出在等什么。
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
    sys.modules.setdefault("patchright", types.ModuleType("patchright"))
    pa = types.ModuleType("patchright.async_api")

    def _no(*_a, **_k):
        raise RuntimeError("stub")

    pa.async_playwright = _no
    sys.modules.setdefault("patchright.async_api", pa)
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
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    monkeypatch.setattr(config, "HELLO_PROBE_SUBMIT_GAP_SECONDS", 0)
    monkeypatch.setattr(config, "V20_WAIT_SECONDS", 0)
    accounts_dir = tmp_path / "accounts"
    for name in names:
        d = accounts_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "cookie_state.json").write_text('{"source":"cookie"}', encoding="utf-8")
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir), db_path=str(tmp_path / "pool.db"))
    for name in names:
        bp.set_login_status(name, True)
    monkeypatch.setattr(browser_pool, "hello_probe", lambda *a, **k: {
        "ok": True, "status": "ok", "login_ok": True, "replied": True, "reason": "", "reply": "hi",
        "elapsed": 1.0})
    return browser_pool, bp


def _row(bp, name):
    return next(a for a in bp.list_accounts() if a["name"] == name)



def test_busy_wait_reports_which_accounts(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch, names=("acc1",))
    monkeypatch.setattr(browser_pool, "WAIT_FREE_ACCOUNT_STEP", 0.2)
    notes = []

    async def go():
        await bp._locks.setdefault("acc1", asyncio.Lock()).acquire()
        return await bp.generate_video("p", None, 5, "seedance-2.5", deadline=time.time() + 1.0,
                                       on_wait=notes.append)

    with pytest.raises(RuntimeError):
        _run(go())
    assert any("都在出片中" in n and "acc1" in n for n in notes)
    assert len(notes) == len(set(notes))           # 同一句不重复回传


def test_probe_rejection_reports_dola_words(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch, names=("acc1",))
    monkeypatch.setattr(browser_pool, "hello_probe", lambda *a, **k: {
        "ok": False, "status": "error", "login_ok": True, "replied": False,
        "reason": "710022002: 当前服务访问频繁，请稍后重试", "reply": "", "elapsed": 1.0})
    notes = []

    async def fake_gen(account, *a, **k):
        raise AssertionError("探测没过不该派发")

    bp._generate_effective = fake_gen
    with pytest.raises(RuntimeError):
        _run(bp.generate_video("p", None, 5, "seedance-2.5", deadline=time.time() + 1.5,
                               on_wait=notes.append))
    assert any("acc1" in n and "当前服务访问频繁" in n for n in notes)


def test_queue_note_does_not_reset_queue_clock(tmp_path):
    """看门狗按 updated_at 算排队时长：写排队原因不能把它刷新。"""
    import store as store_mod
    st = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    st.create("t1", "seedance-2.5", "p", None, 5)
    before = st.get("t1")["updated_at"]
    time.sleep(0.05)
    st.set_queue_note("t1", "排队中：能接单的号所在的出口 IP 正被 dola 限流")
    row = st.get("t1")
    assert row["queue_note"].startswith("排队中") and row["updated_at"] == before
