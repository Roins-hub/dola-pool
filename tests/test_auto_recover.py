"""2.1.5：超时任务自动取回。

线上（2026-10-13）：上游接单后报「预计等待 45 分钟」，任务按上限判失败；
53 分钟后上游其实出片了（额度照扣），是人工去会话里取回来的。
现在：等待超时 + 有会话 + 纯 API 号 → 判失败后后台继续盯会话（只查询不提交），
出片就下载并把任务改成「完成」；上游明确失败或到期就停。
"""
from __future__ import annotations

import asyncio
import json
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


# ---------------------------------------------------------------- pure_api_gen 单次查询

class FakeDola:
    BASE = "https://www.dola.com"

    def __init__(self, result):
        self.result = result
        self.submitted = 0
        self.downloaded = []

    def set_native_duration_max(self, _v):
        pass

    def setup_session(self, **_k):
        return object(), {}, {}

    def get_video_result(self, session, ctx, conversation_id, **_k):
        return dict(self.result)

    def download_video(self, url, path, **_k):
        Path(path).write_bytes(b"fake-mp4")
        self.downloaded.append(url)
        return Path(path)


def _fetch(tmp_path, monkeypatch, result):
    import pure_api_gen
    fake = FakeDola(result)
    monkeypatch.setattr(pure_api_gen, "_dola", lambda: fake)
    state = tmp_path / "cookie_state.json"
    state.write_text(json.dumps({"source": "cookie", "cookies": {"sessionid": "x"}}), encoding="utf-8")
    out = pure_api_gen.fetch_conversation_video(state, "conv1", output_dir=tmp_path / "dl")
    return out, fake


def test_fetch_downloads_when_ready(tmp_path, monkeypatch):
    out, fake = _fetch(tmp_path, monkeypatch, {"status": "succeeded", "download_url": "https://cdn/x.mp4"})
    assert out["local_path"] and Path(out["local_path"]).exists()
    assert out["final"] is False
    assert fake.downloaded == ["https://cdn/x.mp4"]


def test_fetch_still_pending(tmp_path, monkeypatch):
    out, fake = _fetch(tmp_path, monkeypatch, {"status": "timeout", "texts": ["预计等待 45 分钟"]})
    assert out["local_path"] == "" and out["final"] is False
    assert fake.downloaded == []


def test_fetch_final_failure(tmp_path, monkeypatch):
    out, _ = _fetch(tmp_path, monkeypatch, {"status": "generation_voided", "texts": ["视频生成失败"]})
    assert out["final"] is True and out["local_path"] == ""


# ---------------------------------------------------------------- server 取回循环

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


def _server(tmp_path, monkeypatch, pool):
    import server as server_mod
    import store as store_mod
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path / "dl"))
    monkeypatch.setattr(config, "PUBLIC_BASE", "https://example.com")
    monkeypatch.setattr(config, "RECOVER_TIMEOUT_SECONDS", 600)
    monkeypatch.setattr(config, "RECOVER_INTERVAL_SECONDS", 0)
    (tmp_path / "dl").mkdir(exist_ok=True)
    server_mod.pool = pool
    server_mod.store = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    server_mod.store.create("video_t1", "seedance-2.5", "p", "9:16", 5)
    server_mod.store.update("video_t1", status="failed", account="acc1",
                            conversation_id="conv1", error="等待出片超时：已等 59 分钟仍未出片")
    return server_mod


def test_timeout_failure_recovers_video(tmp_path, monkeypatch):
    ready = str(tmp_path / "dl" / "recovered_abc.mp4")
    pool = FakePool([{"status": "timeout", "local_path": "", "final": False},
                     {"status": "succeeded", "local_path": ready, "final": False}])
    server = _server(tmp_path, monkeypatch, pool)

    async def go():
        assert server._maybe_start_recovery("video_t1", TimeoutError("等待出片超时")) is True
        for _ in range(60):
            await asyncio.sleep(0.1)
            if server.store.get("video_t1")["recover_state"] != "watching":
                break

    _run(go())
    row = server.store.get("video_t1")
    assert row["status"] == "completed"
    assert row["recover_state"] == "recovered"
    assert row["video_url"] == "https://example.com/videos/recovered_abc.mp4"
    assert not row["error"]
    assert pool.calls == 2                          # 第一次还没好，第二次取回
    assert pool.claimed == [("acc1", "seedance-2.5", 5)]   # 按正常出片记账


def test_stops_when_upstream_says_final(tmp_path, monkeypatch):
    pool = FakePool([{"status": "generation_voided", "local_path": "", "final": True,
                      "detail": "视频生成失败，生成额度未扣除"}])
    server = _server(tmp_path, monkeypatch, pool)
    server.store.update("video_t1", recover_state="watching", recover_until=time.time() + 600)
    _run(server._recover_loop("video_t1"))
    row = server.store.get("video_t1")
    assert row["status"] == "failed"
    assert row["recover_state"] == "gave_up"
    assert "generation_voided" in row["error"]
    assert pool.claimed == []


def test_gives_up_after_deadline(tmp_path, monkeypatch):
    pool = FakePool([])
    server = _server(tmp_path, monkeypatch, pool)
    server.store.update("video_t1", recover_state="watching", recover_until=time.time() - 1)
    _run(server._recover_loop("video_t1"))
    assert server.store.get("video_t1")["recover_state"] == "gave_up"
    assert pool.calls == 0


def test_only_wait_timeouts_on_pure_accounts_are_recovered(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch, FakePool([]))
    # 额度用完 / 内容拒稿这类失败：上游不会再出片，不盯
    assert server._maybe_start_recovery("video_t1", RuntimeError("status=quota_exceeded")) is False
    # 浏览器号（非纯 API）不盯
    server.pool = FakePool([], pure=False)
    assert server._maybe_start_recovery("video_t1", TimeoutError("等待出片超时")) is False
    # 开关关掉不盯
    server.pool = FakePool([])
    monkeypatch.setattr(config, "RECOVER_TIMEOUT_SECONDS", 0)
    assert server._maybe_start_recovery("video_t1", TimeoutError("等待出片超时")) is False
    assert server.store.get("video_t1")["recover_state"] in ("", None)


def test_watching_tasks_are_listed_for_restart(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch, FakePool([]))
    assert server.store.recovering_tasks() == []
    server.store.update("video_t1", recover_state="watching", recover_until=time.time() + 600)
    assert [r["id"] for r in server.store.recovering_tasks()] == ["video_t1"]
