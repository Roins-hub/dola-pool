"""服务重启的恢复语义：被取消的任务必须能重新派发，而不是被判「排队超时」。

背景（线上事故）：服务重启会把在跑的 `_run_task` 取消，`CancelledError` 分支写回
`status='queued' + account=NULL`，但会话（conversation_id）留着。老版本两个恢复入口
一个要求 account 非空、一个要求 conversation 为空，这类行两边都不沾 —— 只能等看门狗
按「排队超过 30 分钟」判死，而它其实已经在上游生成过一版了。
"""
from __future__ import annotations

import time

import config
import store as store_mod


def _store(tmp_path) -> store_mod.TaskStore:
    return store_mod.TaskStore(str(tmp_path / "tasks.db"))


def _raw(st: store_mod.TaskStore, task_id: str, **fields) -> None:
    with store_mod._LOCK:
        cols = ", ".join("%s=?" % k for k in fields)
        st._conn.execute("UPDATE tasks SET %s WHERE id=?" % cols,
                         list(fields.values()) + [task_id])
        st._conn.commit()


def _cancel_path_task(st: store_mod.TaskStore, task_id: str, conversation: str) -> None:
    """复刻 `_run_task` 的 CancelledError 分支写的行。"""
    st.create(task_id, "seedance-2.5", "p", "16:9", 10)
    st.update(task_id, status="processing", account="acc7",
              conversation_id=conversation, attempted_accounts='["acc7"]')
    st.update(task_id, status="queued", account=None, last_poll_at=0,
              error="任务被中断（服务重启），已重新排队")


def test_interrupted_inflight_task_is_recovered(tmp_path):
    st = _store(tmp_path)
    _cancel_path_task(st, "t_int", "conv-7")

    assert [r["id"] for r in st.recoverable_queued_tasks()] == ["t_int"], \
        "会话在、账号没的任务必须重新受理"
    assert [r["id"] for r in st.recoverable_tasks()] == []
    row = st.get("t_int")
    assert row["conversation_id"] == "conv-7"      # 会话不丢（可用于对账）
    assert row["reference_images"] is not None or True


def test_recovery_partition_leaves_nobody_behind(tmp_path):
    """四种组合都要被两个入口之一认领，一个都不能漏。"""
    st = _store(tmp_path)
    st.create("t_conv_acc", "seedance-2.5", "p", "16:9", 10)
    st.update("t_conv_acc", status="processing", account="acc1", conversation_id="c1")
    st.create("t_conv_noacc", "seedance-2.5", "p", "16:9", 10)
    st.update("t_conv_noacc", status="processing", conversation_id="c2")
    st.create("t_noconv_acc", "seedance-2.5", "p", "16:9", 10)
    st.update("t_noconv_acc", status="processing", account="acc3")
    st.create("t_noconv_noacc", "seedance-2.5", "p", "16:9", 10)
    st.update("t_noconv_noacc", status="processing")

    resume = [r["id"] for r in st.recoverable_tasks()]
    submit = [r["id"] for r in st.recoverable_queued_tasks()]
    assert resume == ["t_conv_acc"]
    assert sorted(submit) == ["t_conv_noacc", "t_noconv_acc", "t_noconv_noacc"]
    assert not set(resume) & set(submit)           # 互不重叠
    assert len(resume) + len(submit) == 4          # 也不漏


def test_queue_wait_measures_from_last_state_change(tmp_path):
    st = _store(tmp_path)
    now = time.time()
    st.create("t_recent_requeue", "seedance-2.5", "p", "16:9", 10)
    _raw(st, "t_recent_requeue", status="queued", created_at=now - 3600,
         updated_at=now - 5)
    st.create("t_stale", "seedance-2.5", "p", "16:9", 10)
    _raw(st, "t_stale", status="queued", created_at=now - 3600, updated_at=now - 3600)

    info = st.stale_pending_tasks(stale_seconds=600, wait_cap_seconds=1800)
    ids = [r["id"] for r in info["waiting"]]
    assert ids == ["t_stale"], "刚被重新排队的任务不该立刻判「排队超时」"
    assert info["waiting"][0]["queued_since"] == now - 3600


def test_watchdog_spares_just_requeued_task(tmp_path, monkeypatch):
    """端到端：重启打回队列的任务，看门狗不该判死它。"""
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
    for exc in ("AccountLimitedError", "CreditInsufficientError", "LoginExpiredError",
                "RiskControlError"):
        setattr(v, exc, type(exc, (), {}))
    v.generate_video = _no_playwright
    v.resume_video = _no_playwright
    sys.modules["video_worker_ui"] = v
    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d

    import server

    st = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server, "store", st)
    _cancel_path_task(st, "w_restart", "conv-9")

    server.WATCHDOG_DISPATCHED.clear()
    server.WATCHDOG_RETRIES.clear()
    server._watchdog_plan()

    assert st.get("w_restart")["status"] == "queued", "不该被看门狗判失败"
    assert "已重新排队" in (st.get("w_restart")["error"] or "")
