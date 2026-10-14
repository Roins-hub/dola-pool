"""2.2.1：加号任务在号进池前就能在面板上看到（阶段 / 排在谁后面 / 完成时间）。

线上：Cookie 导入要开浏览器写 profile + 在线验证，Google 登录走 OAuth，且与自动重登共用一把锁，
号要等整个任务跑完才进列表，面板上半天看不到新号。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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


def test_add_job_reports_queue_holder_stage_and_finish(monkeypatch):
    import server
    seen = {}

    async def fake_import(name, cookies, require_login=True):
        seen["stage"] = server.JOBS[name]["stage"]
        seen["holder"] = server.ADD_SEM_HOLDER["what"]
        return {"verified": True}

    monkeypatch.setattr(server, "import_cookie_account", fake_import)
    noop = lambda *a, **k: None                                # 别的测试可能把 server.pool 换成了假号池
    monkeypatch.setattr(server, "pool", types.SimpleNamespace(
        ensure_account=noop, set_email=noop, set_source=noop, set_login_status=noop))

    async def go():
        sem = asyncio.Semaphore(1)
        monkeypatch.setattr(server, "ADD_ACCOUNT_SEM", sem)
        await sem.acquire()                                    # 别的号在自动重登
        server.ADD_SEM_HOLDER["what"] = "acc6 自动重登"
        task = asyncio.get_running_loop().create_task(
            server._run_add_job("zz_new", "", "", "", cookies='[{"name":"sessionid","value":"x"}]'))
        await asyncio.sleep(0.05)
        queued = server.JOBS["zz_new"]["stage"]
        server.ADD_SEM_HOLDER["what"] = ""
        sem.release()
        await task
        return queued

    loop = asyncio.new_event_loop()
    try:
        queued = loop.run_until_complete(go())
    finally:
        loop.close()
    assert "acc6 自动重登" in queued
    assert seen["stage"].startswith("导入 Cookie") and seen["holder"] == "zz_new 加号"
    job = server.JOBS.pop("zz_new")
    assert job["status"] == "success" and job["source"] == "cookie" and job["finished_at"]
    assert server.ADD_SEM_HOLDER["what"] == ""
