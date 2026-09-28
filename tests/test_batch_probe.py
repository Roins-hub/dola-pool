"""批量「你好」探测：结论口径与单号一致（硬信号进风控、软失败不锁号）。"""
from __future__ import annotations

import asyncio
import collections
import sys
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
    v.generate_video = _no
    v.resume_video = _no
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


def _setup(tmp_path, monkeypatch, probe_map):
    """建 3 个账号 + server/browser_pool，把 hello_probe 换成按账号返回的假实现。"""
    import browser_pool
    import server as server_mod
    import store as store_mod

    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 4)
    monkeypatch.setattr(browser_pool, "DAILY_LIMIT", 4)
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)

    accounts_dir = tmp_path / "accounts"
    names = ["acc1", "acc2", "acc3"]
    for name in names:
        d = accounts_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "cookie_state.json").write_text('{"source":"cookie"}', encoding="utf-8")
    pool = browser_pool.BrowserPool(accounts_dir=str(accounts_dir),
                                   db_path=str(tmp_path / "pool.db"))
    for name in names:
        pool.set_login_status(name, True)

    calls = []

    def fake_hello(state, proxy="", prompt=None, timeout_sec=None):
        name = Path(state).parent.name
        calls.append(name)
        value = probe_map[name]
        if isinstance(value, list):
            index = min(calls.count(name) - 1, len(value) - 1)
            return dict(value[index])
        return dict(value)

    monkeypatch.setattr(browser_pool, "hello_probe", fake_hello)
    # 批量探测默认串行 + 间距 + 失败退避，测试里把等待调到 0
    monkeypatch.setattr(server_mod, "BATCH_PROBE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(server_mod, "BATCH_PROBE_RETRY_SECONDS", 0.0)
    server_mod.pool = pool
    server_mod.store = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    return browser_pool, pool, server_mod, calls


OK = {"ok": True, "status": "ok", "login_ok": True, "replied": True,
      "reason": "replied", "reply": "你好呀！", "elapsed": 2.0}
KICKED = {"ok": False, "status": "logged_out", "login_ok": False, "replied": True,
          "reason": "被登出（x-tt-agw-login=0）", "reply": "你好呀！", "elapsed": 2.1}
SOFT = {"ok": False, "status": "error", "login_ok": True, "replied": False,
        "reason": "710022002: 当前服务访问频繁", "reply": "", "elapsed": 0.3}


def test_batch_probe_summary_and_groups(tmp_path, monkeypatch):
    browser_pool, pool, server, calls = _setup(tmp_path, monkeypatch, {
        "acc1": OK, "acc2": KICKED, "acc3": SOFT,
    })

    job_id = "batch_test"
    server.BATCH_JOBS[job_id] = {
        "kind": "probe", "status": "running", "total": 3, "done": 0, "ok_count": 0,
        "failed_count": 0, "skipped_count": 0, "results": [], "current": "",
        "started_at": 0, "finished_at": None, "error": "",
    }
    _run(server._run_batch_probe_job(job_id, ["acc1", "acc2", "acc3"]))

    job = server.BATCH_JOBS[job_id]
    assert job["status"] == "completed"
    assert job["done"] == 3
    assert job["ok_count"] == 1          # acc1
    assert job["failed_count"] == 1      # acc2（被登出）
    assert job["skipped_count"] == 1     # acc3（软失败）
    # acc2（被登出）、acc3（软失败）都会退避重试一次（顺序由调度决定，用计数器比）
    # 被登出是强信号，一次就判（不重试）；"没探成"(error) 才重试一次
    assert collections.Counter(calls) == collections.Counter(
        {"acc1": 1, "acc2": 1, "acc3": 2})

    by_name = {r["name"]: r for r in job["results"]}
    assert by_name["acc1"]["message"] == "通过"
    assert "风控" in by_name["acc2"]["message"]
    assert "未完成" in by_name["acc3"]["message"]
    assert by_name["acc1"]["group"] == "满额"
    assert by_name["acc2"]["group"] == "风控"      # 两次都被登出 → 两振出局，判风控
    assert by_name["acc3"]["group"] == "满额"      # 软失败不锁号
    # 软失败会退避重试一次，所以那个号被探了两次
    assert by_name["acc3"]["attempts"] == 2
    assert by_name["acc1"]["attempts"] == 1


def test_batch_probe_does_not_lock_on_soft_failure(tmp_path, monkeypatch):
    browser_pool, pool, server, calls = _setup(tmp_path, monkeypatch, {
        "acc1": SOFT, "acc2": SOFT, "acc3": SOFT,
    })
    job_id = "batch_soft"
    server.BATCH_JOBS[job_id] = {
        "kind": "probe", "status": "running", "total": 3, "done": 0, "ok_count": 0,
        "failed_count": 0, "skipped_count": 0, "results": [], "current": "",
        "started_at": 0, "finished_at": None, "error": "",
    }
    _run(server._run_batch_probe_job(job_id, ["acc1", "acc2", "acc3"]))

    job = server.BATCH_JOBS[job_id]
    assert job["ok_count"] == 0 and job["failed_count"] == 0
    assert job["skipped_count"] == 3          # 软失败单列"未完成"
    assert all(r["group"] == "满额" for r in job["results"])
    assert pool._meta("acc1")["risk_control"] == 0


SILENT = {"ok": False, "status": "no_reply", "login_ok": True, "replied": False,
          "reason": "60 秒内没有回复", "reply": "", "elapsed": 60.0}


def test_retry_rescues_account_that_was_silent_once(tmp_path, monkeypatch):
    """第一次「没回复」很可能只是上游抖动：重试后正常回复 → 不判风控（这正是线上踩到的误锁）。"""
    browser_pool, pool, server, calls = _setup(tmp_path, monkeypatch, {
        "acc1": OK, "acc2": [SILENT, OK], "acc3": OK,
    })
    job_id = "batch_retry"
    server.BATCH_JOBS[job_id] = {
        "kind": "probe", "status": "running", "total": 3, "done": 0, "ok_count": 0,
        "failed_count": 0, "skipped_count": 0, "results": [], "current": "",
        "started_at": 0, "finished_at": None, "error": "",
    }
    _run(server._run_batch_probe_job(job_id, ["acc1", "acc2", "acc3"]))

    by_name = {r["name"]: r for r in server.BATCH_JOBS[job_id]["results"]}
    assert by_name["acc2"]["attempts"] == 2
    assert by_name["acc2"]["ok"] is True
    assert by_name["acc2"]["group"] == "满额"
    assert pool._meta("acc2")["risk_control"] == 0      # 没被误锁
