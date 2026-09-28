"""看门狗：把「挂着不动」和「排队等太久」的任务收掉，别再让客户端一直等。"""
from __future__ import annotations

import os
import time

import config
import store as store_mod


def _store(tmp_path) -> store_mod.TaskStore:
    return store_mod.TaskStore(str(tmp_path / "tasks.db"))


def _age(st: store_mod.TaskStore, task_id: str, *, created: float | None = None,
         started: float | None = None, last_poll: float | None = None,
         status: str = "processing", account: str | None = None,
         conversation: str | None = None, updated: float | None = None) -> None:
    fields = {"status": status}
    if created is not None:
        fields["created_at"] = created
    if updated is not None:
        # 「排队超时」按最后一次状态变化计时（updated_at），测试要能把它按回过去
        fields["updated_at"] = updated
    if started is not None:
        fields["started_at"] = started
    if last_poll is not None:
        fields["last_poll_at"] = last_poll
    if account is not None:
        fields["account"] = account
    if conversation is not None:
        fields["conversation_id"] = conversation
    with store_mod._LOCK:
        cols = ", ".join("%s=?" % k for k in fields)
        st._conn.execute("UPDATE tasks SET %s WHERE id=?" % cols,
                         list(fields.values()) + [task_id])
        st._conn.commit()


def test_stale_buckets(tmp_path):
    st = _store(tmp_path)
    now = time.time()
    # 1) 有 conversation 但 20 分钟没动静 → stuck
    st.create("t_stuck", "seedance-2.5", "p", "16:9", 10)
    _age(st, "t_stuck", last_poll=now - 1200, conversation="conv-1", account="acc1")
    # 2) 没 conversation、40 分钟前创建且一直没动 → waiting（排队等太久）
    st.create("t_wait", "seedance-2.5", "p", "16:9", 30)
    _age(st, "t_wait", created=now - 2400, updated=now - 2400, last_poll=now - 2400,
         status="queued")
    # 3) processing 但没 conversation、15 分钟没进展 → orphan（重启留下的）
    st.create("t_orphan", "seedance-2.5", "p", "16:9", 10)
    _age(st, "t_orphan", started=now - 900, last_poll=0, account=None)
    # 4) 正常在跑的不该被收
    st.create("t_ok", "seedance-2.5", "p", "16:9", 10)
    _age(st, "t_ok", last_poll=now - 30, conversation="conv-2", account="acc2")
    # 5) 拿到过 conversation 但被重排回 queued、确实等太久的也要收
    st.create("t_wait_conv", "seedance-2.5", "p", "16:9", 30)
    _age(st, "t_wait_conv", created=now - 3600, updated=now - 3600, last_poll=now - 3600,
         status="queued", conversation="conv-3")
    # 6) 跑了 1 小时、刚被服务重启打回 queued 的（updated_at 是刚刚）→ 不该判「排队超时」，
    #    它该被重启恢复流程重新受理（见 tests/test_restart_recovery.py）
    st.create("t_requeued", "seedance-2.5", "p", "16:9", 30)
    _age(st, "t_requeued", created=now - 3600, updated=now - 5, last_poll=0,
         status="queued", conversation="conv-4")

    info = st.stale_pending_tasks(stale_seconds=600, wait_cap_seconds=1800)
    assert [r["id"] for r in info["stuck"]] == ["t_stuck"]
    assert sorted(r["id"] for r in info["waiting"]) == ["t_wait", "t_wait_conv"]
    assert [r["id"] for r in info["orphans"]] == ["t_orphan"]
    assert all(r["id"] != "t_ok" for bucket in info.values() for r in bucket)
    assert all(r["id"] != "t_requeued" for bucket in info.values() for r in bucket)


def test_watchdog_plan_fails_waiting_and_dispatches_stuck(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "STALE_TASK_SECONDS", 600)
    monkeypatch.setattr(config, "MAX_QUEUE_WAIT_SECONDS", 1800)
    import sys
    import types

    sys.modules.setdefault("patchright", types.ModuleType("patchright"))
    pa = types.ModuleType("patchright.async_api")

    def _no_playwright(*_a, **_k):
        raise RuntimeError("stub")

    pa.async_playwright = _no_playwright
    sys.modules.setdefault("patchright.async_api", pa)
    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError", "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))
    v.generate_video = _no_playwright
    v.resume_video = _no_playwright
    sys.modules["video_worker_ui"] = v
    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d

    import server

    # 关键：把 server 的 store 换成临时库，别把测试任务写进线上 tasks.db
    st = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server, "store", st)
    now = time.time()
    st.create("w_wait", "seedance-2.5", "p", "16:9", 30)
    _age(st, "w_wait", created=now - 3000, updated=now - 3000, last_poll=now - 3000,
         status="queued")
    st.create("w_orphan", "seedance-2.5", "p", "16:9", 10)
    _age(st, "w_orphan", started=now - 1200, last_poll=0, status="processing")

    server.WATCHDOG_DISPATCHED.clear()
    server.WATCHDOG_RETRIES.clear()
    actions = server._watchdog_plan()

    assert st.get("w_wait")["status"] == "failed"
    assert "排队等待超过" in (st.get("w_wait")["error"] or "")
    assert [a["row"]["id"] for a in actions] == ["w_orphan"]
    assert actions[0]["kind"] == "submit"        # 没有 conversation → 重新提交
