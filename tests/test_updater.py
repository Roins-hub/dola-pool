"""面板一键更新（app 侧）：版本检查、请求校验、排空与放行、超时放弃、取消，以及派单关卡和 owner 权限。"""
import asyncio
import json
import time

import pytest

import config
import updater
from test_task_deadline import _run


@pytest.fixture
def upd(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPDATE_DIR", str(tmp_path / "update"))
    monkeypatch.setattr(config, "DEPLOY_MODE", "source")
    monkeypatch.setattr(config, "UPDATE_IDLE_TIMEOUT", 1800)
    monkeypatch.setattr(config, "UPDATE_AGENT_TIMEOUT", 600)
    updater._LATEST_CACHE.update(at=0.0, data=None, error="")
    return tmp_path / "update"


def install_agent(root, mode="source", **extra):
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.json").write_text(json.dumps({"mode": mode, **extra}), encoding="utf-8")


LATEST = {"version": "2.6.5", "tag": "v2.6.5"}


def test_version_compare():
    assert updater.is_newer("2.6.10", "2.6.9") and not updater.is_newer("2.6.4", "2.6.4")
    assert updater.parse_version("v2.6.4") == (2, 6, 4) and updater.parse_version("2.6") is None


def test_latest_release_is_cached_and_survives_errors(upd, monkeypatch):
    calls = []
    class Resp:
        def __init__(self, status, body): self.status, self.body = status, body
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def json(self): return self.body
    replies = [Resp(200, {"tag_name": "v2.6.5", "name": "v2.6.5 — x", "body": "notes", "html_url": "u"}),
               Resp(503, {})]
    class Session:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, url, **kw):
            calls.append(url)
            return replies.pop(0)
    monkeypatch.setattr(updater.aiohttp, "ClientSession", Session)
    first = _run(updater.latest_release())
    assert first["version"] == "2.6.5" and first["notes"] == "notes" and not first["error"]
    assert _run(updater.latest_release())["version"] == "2.6.5" and len(calls) == 1     # 缓存
    failed = _run(updater.latest_release(force=True))
    assert failed["version"] == "2.6.5" and "503" in failed["error"]                  # 失败保留上次结果
    assert calls[0].endswith("/repos/Roins-hub/dola-pool/releases/latest")


def test_request_requires_agent_newer_and_latest(upd):
    with pytest.raises(updater.UpdateError, match="没有安装更新程序"):
        updater.request_update("2.6.5", "2.6.4", "admin", LATEST)
    install_agent(upd)
    with pytest.raises(updater.UpdateError, match="最新正式版"):
        updater.request_update("2.6.6", "2.6.4", "admin", LATEST)
    with pytest.raises(updater.UpdateError, match="不需要更新"):
        updater.request_update("2.6.5", "2.6.5", "admin", LATEST)
    request = updater.request_update("2.6.5", "2.6.4", "admin", LATEST)
    assert request["state"] == "draining" and request["tag"] == "v2.6.5" and updater.draining()
    with pytest.raises(updater.UpdateError, match="已经有一个更新"):
        updater.request_update("2.6.5", "2.6.4", "admin", LATEST)


def test_docker_agent_needs_a_recent_heartbeat(upd, monkeypatch):
    monkeypatch.setattr(config, "DEPLOY_MODE", "docker")
    install_agent(upd, "docker", heartbeat_at=time.time() - 600)
    assert not updater.agent_info()["ready"]
    install_agent(upd, "docker", heartbeat_at=time.time())
    assert updater.agent_info()["ready"]
    install_agent(upd, "docker", heartbeat_at=time.time(), error="项目目录没有挂进 updater 容器")
    assert "没有挂进" in updater.agent_info()["reason"]
    install_agent(upd, "source")
    assert "source 版" in updater.agent_info()["reason"]


def test_tick_waits_for_running_tasks_then_signals_ready(upd):
    install_agent(upd)
    updater.request_update("2.6.5", "2.6.4", "admin", LATEST)
    updater.tick({"running": 2, "jobs": 1})
    assert updater.current_request()["state"] == "draining" and not (upd / "ready").exists()
    assert "2 个生成中" in updater.current_request()["message"]
    updater.tick({"running": 0, "jobs": 0})
    request = updater.current_request()
    assert request["state"] == "ready" and json.loads((upd / "ready").read_text())["id"] == request["id"]
    assert updater.draining()                       # 放行后、更新程序结束前仍不派新单


def test_idle_timeout_and_silent_agent_give_up(upd, monkeypatch):
    install_agent(upd)
    updater.request_update("2.6.5", "2.6.4", "admin", LATEST)
    monkeypatch.setattr(config, "UPDATE_IDLE_TIMEOUT", 60)
    request = updater.current_request()
    request["requested_at"] = time.time() - 120
    (upd / "request.json").write_text(json.dumps(request), encoding="utf-8")
    assert updater.tick({"running": 1, "jobs": 0}) is None
    assert not updater.draining() and updater.status()["progress"]["state"] == "failed"
    updater.request_update("2.6.5", "2.6.4", "admin", LATEST)
    updater.tick({"running": 0, "jobs": 0})
    request = updater.current_request()
    request["ready_at"] = time.time() - 700
    (upd / "request.json").write_text(json.dumps(request), encoding="utf-8")
    assert updater.tick({"running": 0, "jobs": 0}) is None
    assert "没有响应" in updater.status()["progress"]["message"] and not (upd / "ready").exists()


def test_cancel_only_before_the_agent_starts(upd):
    install_agent(upd)
    request = updater.request_update("2.6.5", "2.6.4", "admin", LATEST)
    (upd / "agent-status.json").write_text(json.dumps({"id": request["id"], "state": "downloading"}), encoding="utf-8")
    with pytest.raises(updater.UpdateError, match="已经开始执行"):
        updater.cancel_update()
    assert updater.status()["progress"]["state"] == "downloading"
    (upd / "agent-status.json").unlink()
    updater.cancel_update()
    assert not updater.draining() and updater.status()["progress"]["state"] == "cancelled"


def test_finished_agent_status_is_reported_after_the_request_is_gone(upd):
    install_agent(upd)
    (upd / "agent-status.json").write_text(json.dumps(
        {"id": "x", "state": "rolled_back", "message": "已回滚", "updated_at": time.time()}), encoding="utf-8")
    assert updater.status()["progress"]["state"] == "rolled_back" and not updater.draining()


# ---------------------------------------------------------------- server 集成

def test_new_tasks_wait_while_draining(upd, tmp_path, monkeypatch):
    import server
    from store import TaskStore
    monkeypatch.setattr(server, "store", TaskStore(str(tmp_path / "tasks.db")))
    server.store.create("video_q1", "seedance-2.0-fast", "p", "16:9", 5)
    install_agent(upd)
    updater.request_update("2.6.5", "2.6.4", "admin", LATEST)
    sleeps = []
    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            (upd / "request.json").unlink()        # 更新程序结束
    monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)
    _run(server._wait_update_drain("video_q1"))
    assert len(sleeps) == 3 and server.store.get("video_q1")["queue_note"] == ""


def test_only_owner_can_start_an_update(monkeypatch):
    import server
    monkeypatch.setattr(server, "_admin_auth", lambda key: None)
    monkeypatch.setitem(server.ADMIN_SESSIONS, "k-admin", {"username": "ops", "role": "admin"})
    monkeypatch.setitem(server.ADMIN_SESSIONS, "k-owner", {"username": "admin", "role": "owner"})
    with pytest.raises(server.HTTPException) as denied:
        server._require_owner("k-admin")
    assert denied.value.status_code == 403
    assert server._require_owner("k-owner") == "admin"


def test_update_loop_busy_counts_running_tasks_and_jobs(tmp_path, monkeypatch):
    import server
    from store import TaskStore
    monkeypatch.setattr(server, "store", TaskStore(str(tmp_path / "tasks.db")))
    server.store.create("video_r1", "seedance-2.0-fast", "p", "16:9", 5)
    server.store.update("video_r1", status="processing")
    monkeypatch.setattr(server, "TEST_JOBS", {"t": {"status": "running"}, "u": {"status": "done"}})
    assert server._update_busy() == {"running": 1, "jobs": 1}
