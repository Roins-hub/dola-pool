"""2.2.9：代理 / 任务 / 管理员 勾选多个、全选后「删除选中」。"""
from __future__ import annotations

import asyncio
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


@pytest.fixture
def server(tmp_path, monkeypatch):
    import server as server_mod
    import store as store_mod
    monkeypatch.setattr(server_mod, "_admin_auth", lambda key: None)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path / "dl"))
    (tmp_path / "dl").mkdir()
    monkeypatch.setattr(server_mod, "store", store_mod.TaskStore(str(tmp_path / "tasks.db")))
    return server_mod


def test_task_batch_delete_skips_running_and_removes_files(server, tmp_path):
    st = server.store
    for tid, status in (("t_done", "completed"), ("t_fail", "failed"), ("t_run", "processing"),
                        ("t_q", "queued")):
        st.create(tid, "seedance-2.5", "p", None, 5)
        st.update(tid, status=status)
    video = tmp_path / "dl" / "v1.mp4"
    video.write_bytes(b"x")
    st.update("t_done", video_url="https://example.com/videos/v1.mp4")
    r = _run(server.admin_task_delete_batch(server.BatchIds(ids=["t_done", "t_fail", "t_run", "t_q", "nope"]),
                                            x_admin_key="k"))
    assert sorted(r["deleted"]) == ["t_done", "t_fail"]
    assert len(r["skipped"]) == 3
    assert st.get("t_done") is None and st.get("t_run") is not None and st.get("t_q") is not None
    assert not video.exists()                       # 视频文件一起删


def test_user_batch_delete_skips_owner_and_self(server, monkeypatch):
    users = {"admin": {"username": "admin", "role": "owner"}, "me": {"username": "me", "role": "admin"},
             "bob": {"username": "bob", "role": "admin"}}
    deleted = []

    def fake_delete(name):
        if users[name]["role"] == "owner":
            raise ValueError("主管理员不允许删除")
        deleted.append(name)

    monkeypatch.setattr(server, "user_get", lambda name: users.get(name))
    monkeypatch.setattr(server, "user_delete", fake_delete)
    monkeypatch.setitem(server.ADMIN_SESSIONS, "k", {"username": "me"})
    r = _run(server.admin_user_delete_batch(server.BatchUsers(usernames=["admin", "me", "bob", "ghost"]),
                                            x_admin_key="k"))
    assert r["deleted"] == ["bob"] and deleted == ["bob"]
    assert any("admin" in s for s in r["skipped"]) and any("当前登录" in s for s in r["skipped"])


def test_proxy_batch_delete(server, monkeypatch):
    gone = []
    monkeypatch.setattr(server, "list_proxies", lambda: [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}])
    monkeypatch.setattr(server, "delete_proxy", gone.append)
    r = _run(server.admin_proxy_delete_batch(server.BatchIds(ids=["p1", "p3", "p1", "zz"]), x_admin_key="k"))
    assert gone == ["p1", "p3"] and r["deleted"] == ["p1", "p3"] and r["skipped"] == ["zz"]
