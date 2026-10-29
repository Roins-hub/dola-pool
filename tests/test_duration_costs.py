"""2.1.9：按「模型 + 时长」记账；上游嫌贵拒绝时不锁号；没号付得起直接失败。

依据（2026-10-14 两台线上日志 + 实测，上游回执报价对齐任务时长）：
  seedance-2.5：5 秒 = 2、10 秒 = 4、15 秒 = 12；seedance-2.0：5 秒 = 2、10/15 秒 = 4（2026-10-30 实测：每日 5 秒 2 条、10 秒 1 条）。
pair 测试：一段 15 秒被报 12 点被拒，号还剩 4 点却被锁了一天。
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


# ---------------------------------------------------------------- 扣点表

def test_credit_cost_table_and_extrapolation():
    c = config.credit_cost
    assert c("seedance-2.5", 5) == 2
    assert c("seedance-2.5", 10) == 4
    assert c("seedance-2.5", 4) == 2           # 取比它长的最短一档
    assert c("seedance-2.5", 15) == 12         # 实测：15 秒报价 12（不是线性的 6）
    assert c("seedance-2.5", 12) == 12         # 取比它长的最短一档（15 秒）
    assert c("seedance-2.5", 30) == 2          # 请求 30 秒：上游压成 15 秒、只扣 2 点（网页截图 + 线上报价）
    assert c("seedance-2.0", 20) == 6          # 比表里都长：按最长一档（15 秒 4 点）的每秒单价外推
    assert c("seedance-2.0", 5) == 2
    assert c("seedance-2.0", 10) == 4
    assert c("seedance-2.0", 15) == 4
    assert c("seedance-2.0", 0) == config.MODEL_COSTS["seedance-2.0"]


def test_duration_costs_env_override_parsing():
    t = config._parse_duration_costs("seedance-2.5:5=2,10=4,15=6;seedance-2.0:10=2;bad;x:y=z")
    assert t == {"seedance-2.5": {5: 2, 10: 4, 15: 6}, "seedance-2.0": {10: 2}}


def test_duration_cost_model_names_and_pair(monkeypatch):
    import browser_pool
    assert browser_pool.duration_cost("seedance_v2.5", 10) == 4
    assert browser_pool.duration_cost("seedance-2.0", 10) == 4
    assert browser_pool.duration_cost("???", 5) == config.DEFAULT_CREDIT_COST    # 认不出 → 保守
    monkeypatch.setattr(config, "LONG_VIDEO_MODE", "pair")
    assert browser_pool.duration_cost("seedance-2.5", 30) == 2                # 先原样请求一次 30 秒


# ---------------------------------------------------------------- 受理：付不起的组合直接拒

def test_unaffordable_rejected_at_submit():
    import server
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        server._reject_unaffordable("seedance-2.5", 15)        # 12 点 > 每日 4 点
    assert exc.value.status_code == 422
    assert "12 个额度" in exc.value.detail and "seedance-2.0" in exc.value.detail
    server._reject_unaffordable("seedance-2.5", 10)            # 4 点，满额号付得起
    server._reject_unaffordable("seedance-2.0", 15)            # 4 点，满额号付得起


# ---------------------------------------------------------------- 号池

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


def test_10s_25_goes_to_full_account_only(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    bp._claim("acc1", 2)                           # acc1 半额（剩 2），付不起 10 秒的 4 点
    gen = []

    async def fake_gen(account, *a, **k):
        gen.append(account)
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake_gen
    r = _run(bp.generate_video("p", None, 10, "seedance-2.5", deadline=time.time() + 20))
    assert r["account"] == "acc2" and gen == ["acc2"]
    assert _row(bp, "acc2")["remaining"] == 0      # 按 4 点记账


def test_nobody_can_afford_fails_fast(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    bp._claim("acc1", 2)
    bp._claim("acc2", 3)

    async def fake_gen(account, *a, **k):
        raise AssertionError("付不起的号不应被派发")

    bp._generate_effective = fake_gen
    started = time.time()
    with pytest.raises(RuntimeError, match="没有号付得起"):
        _run(bp.generate_video("p", None, 10, "seedance-2.5", deadline=time.time() + 120))
    assert time.time() - started < 5              # 不再原地等到任务时限


def test_quote_refusal_keeps_account_and_syncs_balance(tmp_path, monkeypatch):
    """上游嫌贵拒绝（没扣点）：余额按回执写回，余额还够便宜单就不锁号。"""
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    msg = ("acc1 第 1 段出片未成功 status=rate_limited: 本次视频生成需要消耗 12 个视频生成额度，"
           "今日剩余 4 个视频生成额度 ，无法生成该视频。请尝试降低配置后重试。")
    assert bp._note_upstream_failure("acc1", msg) == "quota"
    row = _row(bp, "acc1")
    assert not row["quota_blocked"] and row["remaining"] == 4

    msg2 = ("acc2 出片未成功: 本次视频生成需要消耗 4 个视频生成额度，今日剩余 2 个视频生成额度 ，"
            "无法生成该视频。")
    bp._note_upstream_failure("acc2", msg2)
    row2 = _row(bp, "acc2")
    assert not row2["quota_blocked"] and row2["remaining"] == 2     # 还能出 5 秒


def test_quote_refusal_with_almost_nothing_left_blocks(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    msg = "本次视频生成需要消耗 2 个视频生成额度，今日剩余 1 个视频生成额度 ，无法生成该视频。"
    bp._note_upstream_failure("acc1", msg)
    assert _row(bp, "acc1")["quota_blocked"]


def test_parse_quota_quote():
    import browser_pool
    assert browser_pool.parse_quota_quote(
        "本次视频生成需要消耗 12 个视频生成额度，今日剩余 4 个视频生成额度") == (12, 4)
    assert browser_pool.parse_quota_quote("视频生成失败") is None


def test_part_hints_never_mention_30_seconds():
    import pure_api_gen
    assert all("30" not in h for h in pure_api_gen.PART_HINTS)



def test_waits_for_busy_account_until_it_frees(tmp_path, monkeypatch):
    """唯一付得起的号正在出片：排队等它空出来，而不是 15 秒后判「无可用账号」。"""
    browser_pool, bp = _pool(tmp_path, monkeypatch, names=("acc1",))
    monkeypatch.setattr(browser_pool, "WAIT_FREE_ACCOUNT_STEP", 0.2)
    gen = []

    async def fake_gen(account, *a, **k):
        gen.append(account)
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake_gen

    async def go():
        lock = bp._locks.setdefault("acc1", asyncio.Lock())
        await lock.acquire()                       # acc1 正在出片

        async def release_later():
            await asyncio.sleep(1.0)
            lock.release()

        asyncio.get_running_loop().create_task(release_later())
        return await bp.generate_video("p", None, 5, "seedance-2.5", deadline=time.time() + 60)

    started = time.time()
    r = _run(go())
    assert r["account"] == "acc1" and gen == ["acc1"]
    assert 0.8 <= time.time() - started < 10


def test_busy_until_deadline_says_busy_not_throttled(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch, names=("acc1",))
    monkeypatch.setattr(browser_pool, "WAIT_FREE_ACCOUNT_STEP", 0.2)

    async def go():
        await bp._locks.setdefault("acc1", asyncio.Lock()).acquire()
        return await bp.generate_video("p", None, 5, "seedance-2.5", deadline=time.time() + 1.0)

    with pytest.raises(RuntimeError, match="都在出片中"):
        _run(go())



def test_expensive_job_can_use_half_account_with_enough_balance(tmp_path, monkeypatch):
    """2.0 的 5 秒要 2 点：满额号没有时，剩 2 点的半额号也能接（同一个号当天第二条 5 秒）。"""
    browser_pool, bp = _pool(tmp_path, monkeypatch, names=("acc4",))
    bp._claim("acc4", 2)                           # 已出过一条 5 秒，剩 2 点 → 半额组
    gen = []

    async def fake_gen(account, *a, **k):
        gen.append(account)
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake_gen
    r = _run(bp.generate_video("p", None, 5, "seedance-2.0", deadline=time.time() + 20))
    assert r["account"] == "acc4" and gen == ["acc4"]
    assert _row(bp, "acc4")["remaining"] == 0
