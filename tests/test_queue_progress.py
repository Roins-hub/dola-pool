"""排队进度：号池回给客户端的「排队中，预计 N 分钟」文案与结构化字段。"""
from __future__ import annotations

import sys
import time
import types

import pytest

import config
import store as store_mod


def test_queue_progress_counts_ahead_and_running(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    st = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    # 两条更早排队 + 一条更晚排队 + 一条在跑
    now = time.time()
    st.create("older1", "seedance-2.5", "p", "16:9", 10)
    st.create("older2", "seedance-2.5", "p", "16:9", 10)
    st.create("mine", "seedance-2.5", "p", "16:9", 10)
    st.create("newer", "seedance-2.5", "p", "16:9", 10)
    st.update("older1", status="processing", started_at=now)
    # 手工把两条 queued 的 created_at 调到 mine 之前/之后
    with store_mod._LOCK:
        st._conn.execute("UPDATE tasks SET created_at=? WHERE id='older2'", (now - 60,))
        st._conn.execute("UPDATE tasks SET created_at=? WHERE id='mine'", (now - 30,))
        st._conn.execute("UPDATE tasks SET created_at=? WHERE id='newer'", (now - 10,))
        st._conn.commit()

    info = st.queue_progress(created_at=now - 30, duration=10)
    assert info["ahead"] == 1          # 只有 older2 比我早且还在排队
    assert info["running"] == 1        # older1 在跑
    assert info["typical_seconds"] >= 30


def test_progress_payload_messages(tmp_path, monkeypatch):
    """文案要能直接显示：排队中带「前面还有 N 个任务」，生成中带预计时长。"""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
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

    queued = server._progress_payload(status="queued", created_at=time.time(), duration=10)
    assert queued["state"] == "queued"
    assert queued["message"].startswith("排队中，预计")
    assert queued["position"] == queued["ahead"] + 1
    assert queued["eta_seconds"] > 0

    running = server._progress_payload(status="processing", created_at=time.time(), duration=30)
    assert running["state"] == "running"
    assert running["message"].startswith("生成中，预计")

    done = server._progress_payload(status="completed", created_at=time.time(), duration=10)
    assert done["state"] == "done" and done["message"] == "已完成"

    failed = server._progress_payload(status="failed", created_at=time.time(), duration=10)
    assert failed["state"] == "failed"


def test_eta_text_rounding():
    import server  # noqa: WPS433

    assert server._eta_text(30) == "不到 1 分钟"
    assert server._eta_text(120) == "约 2 分钟"
    assert server._eta_text(400) == "约 7 分钟"
