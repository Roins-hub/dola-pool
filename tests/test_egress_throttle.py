"""2.1.5：出口限流退避。

上游 710022002「当前服务访问频繁」按出口 IP 限流。旧行为是「立刻换号重试」，同出口的号
挨个撞墙、越撞限得越久（2026-10-13 线上：20 个号共用一个出口，6 秒内全被拒）。
新行为：某出口一被拒，整条出口退避一段时间（递增、封顶），期间不派发、不探测，
任务原地排队，成功一次即清零。
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


OK = {"ok": True, "status": "ok", "login_ok": True, "replied": True,
      "reason": "replied", "reply": "你好呀！", "elapsed": 2.0}
THROTTLED = {"ok": False, "status": "error", "login_ok": True, "replied": False,
             "reason": "710022002: 当前服务访问频繁，请稍后重试", "reply": "", "elapsed": 0.3}


def _pool(tmp_path, monkeypatch, egress: dict[str, str], probe=None, calls=None):
    """建号池；egress 指定每个号的出口（不走真实代理表）。"""
    import browser_pool

    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 4)
    monkeypatch.setattr(browser_pool, "DAILY_LIMIT", 4)
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    monkeypatch.setattr(config, "EGRESS_THROTTLE_SECONDS", 60)
    monkeypatch.setattr(config, "EGRESS_THROTTLE_MAX_SECONDS", 900)
    monkeypatch.setattr(config, "EGRESS_THROTTLE_ROTATE", False)
    # 这里只测出口级退避；号级暂停（2.1.6）在 test_account_throttle 单独测
    monkeypatch.setattr(config, "THROTTLE_ACCOUNT_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(config, "HELLO_PROBE_SUBMIT_GAP_SECONDS", 0)
    monkeypatch.setattr(config, "V20_WAIT_SECONDS", 0)
    accounts_dir = tmp_path / "accounts"
    for name in egress:
        d = accounts_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "cookie_state.json").write_text('{"source":"cookie"}', encoding="utf-8")
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir),
                                  db_path=str(tmp_path / "pool.db"))
    for name in egress:
        bp.set_login_status(name, True)
    bp._egress_for = lambda name: egress[name]
    calls = calls if calls is not None else []
    probe_map = probe or {}

    def fake_hello(state, proxy="", prompt=None, timeout_sec=None):
        name = Path(state).parent.name
        calls.append(name)
        return dict(probe_map.get(name, OK))

    monkeypatch.setattr(browser_pool, "hello_probe", fake_hello)
    return browser_pool, bp, calls


def _group(bp, name):
    return next(a for a in bp.list_accounts() if a["name"] == name)["group"]


def test_transient_probe_throttles_whole_egress(tmp_path, monkeypatch):
    """一个号吃了 710022002：同出口的其它号不再发请求，也不会被判风控。"""
    browser_pool, bp, calls = _pool(
        tmp_path, monkeypatch, {"acc1": "jp:1", "acc2": "jp:1", "acc3": "kr:2"},
        probe={"acc1": THROTTLED})

    r1 = bp.probe_hello("acc1", force=True)
    assert r1["status"] == "error"
    assert bp.egress_throttle_remaining("acc1") > 50
    assert bp.egress_throttle_remaining("acc2") > 50       # 同出口一起退避
    assert bp.egress_throttle_remaining("acc3") == 0       # 别的出口不受影响

    r2 = bp.probe_hello("acc2", force=True)
    assert r2.get("throttled") is True
    assert "限流" in r2["reason"]
    assert calls == ["acc1"]                               # acc2 根本没发请求

    bp.probe_hello("acc3", force=True)
    assert calls == ["acc1", "acc3"]
    # 软失败不判风控
    assert _group(bp, "acc1") == browser_pool.GROUP_FULL
    assert _group(bp, "acc2") == browser_pool.GROUP_FULL

    snap = bp.egress_throttle_snapshot()
    assert [s["egress"] for s in snap] == ["jp:1"]
    assert snap[0]["accounts"] == ["acc1", "acc2"]


def test_backoff_doubles_and_caps_then_resets_on_success(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "jp:1"})
    monkeypatch.setattr(config, "EGRESS_THROTTLE_SECONDS", 10)
    monkeypatch.setattr(config, "EGRESS_THROTTLE_MAX_SECONDS", 25)

    def expire():
        bp._egress_throttle["jp:1"]["until"] = time.time() - 1

    assert bp.note_egress_throttle("acc1", "710022002") == 10
    # 退避期内再次被拒（并发任务同时撞上）：不重复加码
    assert bp.note_egress_throttle("acc1", "710022002") <= 10
    expire()
    assert bp.note_egress_throttle("acc1", "710022002") == 20
    expire()
    assert bp.note_egress_throttle("acc1", "710022002") == 25   # 封顶
    expire()
    bp.probe_hello("acc1", force=True)                           # 探测通过 → 清零
    assert bp.egress_throttle_remaining("acc1") == 0
    assert bp.note_egress_throttle("acc1", "710022002") == 10    # 重新从头算


def test_throttle_disabled_keeps_old_behaviour(tmp_path, monkeypatch):
    browser_pool, bp, calls = _pool(
        tmp_path, monkeypatch, {"acc1": "jp:1", "acc2": "jp:1"},
        probe={"acc1": THROTTLED, "acc2": THROTTLED})
    monkeypatch.setattr(config, "EGRESS_THROTTLE_SECONDS", 0)
    bp.probe_hello("acc1", force=True)
    bp.probe_hello("acc2", force=True)
    assert calls == ["acc1", "acc2"]
    assert bp.egress_throttle_snapshot() == []


def test_dispatch_skips_throttled_egress(tmp_path, monkeypatch):
    """出口被限流的号不派发、也不探测；直接用其它出口的号出片。"""
    browser_pool, bp, calls = _pool(
        tmp_path, monkeypatch, {"acc1": "jp:1", "acc2": "kr:2"})
    bp.note_egress_throttle("acc1", "710022002")
    gen = []

    async def fake_gen(account, *a, **k):
        gen.append(account)
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake_gen
    result = _run(bp.generate_video("p", None, 10, "seedance-2.5",
                                    deadline=time.time() + 30))
    assert result["account"] == "acc2"
    assert gen == ["acc2"]
    assert "acc1" not in calls


def test_dispatch_waits_for_egress_then_succeeds(tmp_path, monkeypatch):
    """所有号的出口都在退避：任务原地排队，出口恢复后自动出片（不报错、不硬撞）。"""
    browser_pool, bp, calls = _pool(tmp_path, monkeypatch, {"acc1": "jp:1"})
    bp.note_egress_throttle("acc1", "710022002")
    bp._egress_throttle["jp:1"]["until"] = time.time() + 1.5

    async def fake_gen(account, *a, **k):
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake_gen
    started = time.time()
    result = _run(bp.generate_video("p", None, 10, "seedance-2.5",
                                    deadline=time.time() + 30))
    assert result["account"] == "acc1"
    assert time.time() - started >= 1.0          # 确实等了出口恢复
    assert bp.egress_throttle_remaining("acc1") == 0   # 出片成功 → 退避清零


def test_dispatch_times_out_with_clear_message(tmp_path, monkeypatch):
    browser_pool, bp, calls = _pool(tmp_path, monkeypatch, {"acc1": "jp:1"})
    bp.note_egress_throttle("acc1", "710022002")

    async def fake_gen(account, *a, **k):
        raise AssertionError("出口退避中不应该提交")

    bp._generate_effective = fake_gen
    with pytest.raises(RuntimeError, match="出口 IP 都被上游限流"):
        _run(bp.generate_video("p", None, 10, "seedance-2.5", deadline=time.time() + 1.5))
    assert calls == []


def test_transient_generation_failure_throttles_egress(tmp_path, monkeypatch):
    """出片时吃了 710022002：同出口的下一个号不再接着撞，换到其它出口。"""
    browser_pool, bp, calls = _pool(
        tmp_path, monkeypatch, {"acc1": "jp:1", "acc2": "jp:1", "acc3": "kr:2"})
    gen = []

    async def fake_gen(account, *a, **k):
        gen.append(account)
        if account in ("acc1", "acc2"):
            raise RuntimeError("提交失败: 710022002: 当前服务访问频繁，请稍后重试")
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake_gen
    monkeypatch.setattr(bp, "_ordered_candidates",
                        lambda allowed: sorted(bp.list_accounts(), key=lambda a: a["name"]))
    result = _run(bp.generate_video("p", None, 10, "seedance-2.5",
                                    deadline=time.time() + 30))
    assert result["account"] == "acc3"
    assert gen == ["acc1", "acc3"]               # acc2 与 acc1 同出口，被跳过
    assert bp.egress_throttle_remaining("acc2") > 0


def test_batch_probe_stops_hitting_throttled_egress(tmp_path, monkeypatch):
    """批量探测：第一个号吃了限流后，同出口其余号不再发请求，结果里写明具体原因。"""
    import server as server_mod
    import store as store_mod

    browser_pool, bp, calls = _pool(
        tmp_path, monkeypatch, {"acc1": "jp:1", "acc2": "jp:1", "acc3": "jp:1"},
        probe={"acc1": THROTTLED, "acc2": THROTTLED, "acc3": THROTTLED})
    monkeypatch.setattr(server_mod, "BATCH_PROBE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(server_mod, "BATCH_PROBE_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(server_mod, "BATCH_PROBE_CONCURRENCY", 1)
    server_mod.pool = bp
    server_mod.store = store_mod.TaskStore(str(tmp_path / "tasks.db"))

    job_id = "batch_throttle"
    server_mod.BATCH_JOBS[job_id] = {
        "kind": "probe", "status": "running", "total": 3, "done": 0, "ok_count": 0,
        "failed_count": 0, "skipped_count": 0, "results": [], "current": "",
        "started_at": 0, "finished_at": None, "error": "",
    }
    _run(server_mod._run_batch_probe_job(job_id, ["acc1", "acc2", "acc3"]))
    job = server_mod.BATCH_JOBS[job_id]

    assert job["status"] == "completed"
    assert len(calls) == 1                       # 只有第一个号真的发了请求
    by_name = {r["name"]: r for r in job["results"]}
    first = calls[0]
    assert "710022002" in by_name[first]["message"]
    assert by_name[first]["attempts"] == 1       # 限流不重试（重试也只会被跳过）
    for name in ("acc1", "acc2", "acc3"):
        assert "未完成" in by_name[name]["message"]
        assert by_name[name]["group"] == browser_pool.GROUP_FULL    # 都不判风控
    others = [n for n in ("acc1", "acc2", "acc3") if n != first]
    for name in others:
        assert "限流中" in by_name[name]["message"]
        assert by_name[name]["attempts"] == 0
    assert job["failed_count"] == 0
