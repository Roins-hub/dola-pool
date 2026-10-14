"""2.2.8：服务重启 / 看门狗接手时，已经提交过 dola 的 cookie 号任务接着等原会话，不重新提交。

线上 2026-10-14：AWS 升级重启，acc48 的 30 秒单、acc49 的 10 秒单被「走纯 API 重新受理」——
同一单再提交一次，又扣一次点、多出一条片；原来那条 dola 照样出片，号池却不管了。
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


class FakePool:
    def __init__(self, results, pure=True):
        self.results = list(results)
        self.calls = 0
        self.claimed = []
        self.pure = pure

    def is_pure_account(self, name):
        return self.pure

    def egress_throttle_remaining(self, name):
        return 0

    def fetch_conversation_video(self, account, conv):
        self.calls += 1
        r = self.results.pop(0) if self.results else {"status": "timeout", "local_path": "", "final": False}
        if r.get("local_path"):
            Path(r["local_path"]).write_bytes(b"fake-mp4")
        return r

    def claim_recovered(self, account, model, duration, result=None):
        self.claimed.append((account, model, duration))
        return 2




def _server(tmp_path, monkeypatch, pool, conversation="conv48", account="acc48", duration=30):
    import server as server_mod
    import store as store_mod
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path / "dl"))
    monkeypatch.setattr(config, "PUBLIC_BASE", "https://example.com")
    monkeypatch.setattr(config, "RECOVER_TIMEOUT_SECONDS", 600)
    monkeypatch.setattr(config, "RESUME_POLL_SECONDS", 0)
    (tmp_path / "dl").mkdir(exist_ok=True)
    monkeypatch.setattr(server_mod, "pool", pool)
    monkeypatch.setattr(server_mod, "store", store_mod.TaskStore(str(tmp_path / "tasks.db")))
    server_mod.store.create("video_r", "seedance-2.5", "p", "16:9", duration)
    server_mod.store.update("video_r", status="queued", account=account, conversation_id=conversation,
                            deadline_at=time.time() + 3600)
    rerun = []

    async def fake_run_task(*a, **k):
        rerun.append(a[0])

    monkeypatch.setattr(server_mod, "_run_task", fake_run_task)
    monkeypatch.setattr(server_mod, "_video_metadata",
                        lambda path, d, r: {"video_duration": 30.08, "video_width": 1280, "video_height": 720})
    return server_mod, rerun


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_restart_waits_on_original_conversation_instead_of_resubmitting(tmp_path, monkeypatch):
    ready = str(tmp_path / "dl" / "recovered_r.mp4")
    pool = FakePool([{"status": "timeout", "local_path": "", "final": False},
                     {"status": "succeeded", "local_path": ready, "final": False}])
    server, rerun = _server(tmp_path, monkeypatch, pool)
    _run(server._resume_task(server.store.get("video_r")))
    row = server.store.get("video_r")
    assert rerun == []                               # 没有重新提交
    assert pool.calls == 2                           # 查了原会话两次，第二次出片
    assert row["status"] == "completed" and row["video_url"].endswith("recovered_r.mp4")
    assert pool.claimed == [("acc48", "seedance-2.5", 30)]
    assert not row["queue_note"]


def test_restart_marks_failed_when_dola_says_final(tmp_path, monkeypatch):
    pool = FakePool([{"status": "failed", "local_path": "", "final": True,
                      "detail": "生成的音频可能涉及版权限制，请更换输入内容后重试"}])
    server, rerun = _server(tmp_path, monkeypatch, pool)
    _run(server._resume_task(server.store.get("video_r")))
    row = server.store.get("video_r")
    assert rerun == [] and row["status"] == "failed" and "版权" in row["error"]
    assert pool.claimed == []


def test_restart_without_conversation_still_resubmits(tmp_path, monkeypatch):
    """还没提交过 dola 的（没有会话）照旧重新派号——这种不会重复扣点。"""
    pool = FakePool([])
    server, rerun = _server(tmp_path, monkeypatch, pool, conversation=None)
    _run(server._resume_task(server.store.get("video_r")))
    assert rerun == ["video_r"] and pool.calls == 0


def test_same_task_is_not_watched_twice(tmp_path, monkeypatch):
    pool = FakePool([])
    server, rerun = _server(tmp_path, monkeypatch, pool)
    server.RESUMING.add("video_r")
    try:
        _run(server._resume_task(server.store.get("video_r")))
    finally:
        server.RESUMING.discard("video_r")
    assert pool.calls == 0 and rerun == []


def test_watch_hands_over_to_background_recovery_at_deadline(tmp_path, monkeypatch):
    pool = FakePool([])
    server, rerun = _server(tmp_path, monkeypatch, pool)
    server.store.update("video_r", deadline_at=time.time() - 10)
    clock = {"t": time.time()}

    def fake_time():                                 # 每读一次时间就过去 400 秒：很快走到时限
        clock["t"] += 400
        return clock["t"]

    monkeypatch.setattr(time, "time", fake_time)
    started = []
    monkeypatch.setattr(server, "_maybe_start_recovery", lambda tid, exc: started.append(tid) or True)
    _run(server._resume_task(server.store.get("video_r")))
    row = server.store.get("video_r")
    assert row["status"] == "failed" and "超时" in row["error"]
    assert started == ["video_r"] and rerun == []


def test_30s_first_segment_timeout_is_a_wait_timeout(tmp_path, monkeypatch):
    """30 秒第 1 段等到上限 dola 还没出片：抛 TimeoutError（号池不换号重提，服务端转后台取回）。"""
    import pure_api_gen

    class D:
        BASE = "https://www.dola.com"

        def submit_chat_completion(self, *a, **k):
            return types.SimpleNamespace(error=None, conversation_id="c1")

        def inspect_video_once(self, *a, **k):
            return types.SimpleNamespace(status="pending", failure_reasons=[], urls=[], vids=[],
                                         texts=["本次使用 Seedance 2.5 生成，将消耗 2 个视频生成额度，预计等待 2 小时。"])

        def get_video_result(self, *a, **k):
            return {"status": "timeout", "texts": ["预计等待 2 小时"], "failure_reasons": []}

        def is_prompt_echo_text(self, t):
            return False

        def extract_wait_minutes_from_text(self, t):
            return ""

    with pytest.raises(TimeoutError, match="等待出片超时"):
        pure_api_gen._generate_capped_pair(
            dola=D(), session=None, ctx={}, headers={}, account="acc48", prompt="p", ratio=None,
            model="seedance_v2.5", output_path=tmp_path / "o.mp4", remove_watermark=False, proxy="",
            timeout=10800, poll_interval=0)
    import server
    assert server._is_wait_timeout(TimeoutError("x"))
