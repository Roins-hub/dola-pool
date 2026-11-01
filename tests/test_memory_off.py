"""关闭 dola「记忆」：结果判定、批量任务、加号成功后自动关闭。"""
import asyncio
import time

import pytest

from test_account_throttle import _pool
from test_task_deadline import _run
from dola_memory import memory_outcome


def _res(switch, *, upd=True, upd_code=0, after_status=200):
    out = {"before": {"status": 200, "code": 0, "memory": {"switch_status": True}},
           "after": {"status": after_status, "code": 0, "memory": None if switch is None else {"switch_status": switch}}}
    if upd:
        out["update"] = {"status": 200, "body": {"code": upd_code, "msg": "success", "data": {"res": upd_code == 0}}}
    return out


@pytest.mark.parametrize("res,ok", [
    (_res(False), True),
    (_res(0), True),
    (_res("off"), True),
    (_res(True), False),                      # 回读仍开着
    (_res(1), False),
    (_res(False, upd_code=710022002), False),  # 接口报错
    (_res(False, after_status=403), False),
    (_res(None), True),                        # 设置里不回开关值，但接口确认 res=true
    (_res(None, upd=False), False),
])
def test_memory_outcome(res, ok):
    assert memory_outcome(res, want=False)[0] is ok


def test_memory_outcome_names_silent_ignore():
    # 2026-11-01 acc20 / acc148：update_bot 回 res=true，回读仍是开启
    ok, msg = memory_outcome(_res(True), want=False)
    assert not ok and "未生效" in msg


def test_batch_memory_off_runs_each_account(tmp_path, monkeypatch):
    import server
    _, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a", "acc2": "b"})
    monkeypatch.setattr(server, "pool", bp)
    monkeypatch.setattr(server, "_admin_auth", lambda key: None)
    monkeypatch.setattr(server, "BATCH_JOBS", {})
    seen = []

    async def fake(name):
        seen.append(name)
        if name == "acc2":
            raise RuntimeError("账号正在生成、验证或探测，稍后再试")
        return {"ok": True, "message": "记忆已关闭"}

    monkeypatch.setattr(bp, "set_memory_off", fake)

    async def go():
        r = await server.admin_batch_memory_off(server.BatchAccountsBody(names=["acc1", "acc2", "acc9"]), None)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if server.BATCH_JOBS[r["job_id"]]["status"] != "running":
                break
        return server.BATCH_JOBS[r["job_id"]]

    job = _run(go())
    assert job["kind"] == "memory" and job["status"] == "completed" and seen == ["acc1", "acc2"]
    assert [(x["name"], x["ok"]) for x in job["results"]] == [("acc1", True), ("acc2", False), ("acc9", None)]
    assert job["ok_count"] == 1 and job["failed_count"] == 1


def test_add_success_turns_memory_off(tmp_path, monkeypatch):
    import server
    _, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(server, "pool", bp)
    monkeypatch.setattr(server, "JOBS", {})
    calls = []

    async def fake_flow(name, email, password, totp):
        (tmp_path / "accounts" / name).mkdir(parents=True, exist_ok=True)

    async def fake_off(name):
        calls.append(name)
        return {"ok": True, "message": "记忆已关闭"}

    async def no_sleep(*a):
        pass

    monkeypatch.setattr(server, "add_account_flow", fake_flow)
    monkeypatch.setattr(bp, "set_memory_off", fake_off)
    monkeypatch.setattr(server.config, "AUTO_MEMORY_OFF", True)

    async def go():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(server.asyncio, "sleep", no_sleep)
        await server._run_add_job("acc5", "x@example.test", "pw", "")
        for _ in range(5):
            await real_sleep(0)

    _run(go())
    assert server.JOBS["acc5"]["status"] == "success" and calls == ["acc5"]
