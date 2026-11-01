"""加号失败的记录：可以重试（用保存的凭据）或删除（连带清掉残留 profile / 代理绑定 / 凭据）。"""
import asyncio
import time

import pytest
from fastapi import HTTPException

import cred_store
from test_account_throttle import _pool
from test_task_deadline import _run


@pytest.fixture
def srv(tmp_path, monkeypatch):
    import server
    _, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(server, "pool", bp)
    monkeypatch.setattr(server, "_admin_auth", lambda key: None)
    monkeypatch.setattr(cred_store, "_CREDFILE", tmp_path / "creds.json")
    monkeypatch.setattr(server, "JOBS", {})
    return server, bp, tmp_path


def _failed(server, name, source="login"):
    server.JOBS[name] = {"kind": "add", "status": "failed", "email": "x@example.test", "source": source,
                         "error": "Locator.click: Timeout", "started_at": time.time(), "finished_at": time.time()}


def test_delete_removes_the_leftovers(srv, monkeypatch):
    """失败时没留下 profile 目录（否则它会作为普通账号出现在号池里），但凭据和代理绑定还在。"""
    server, bp, tmp = srv
    _failed(server, "acc63")
    cred_store.store("acc63", "x@example.test", "pw", "")
    forgot = []
    monkeypatch.setattr(server.proxy_store if hasattr(server, "proxy_store") else __import__("proxy_store"),
                        "forget_account", lambda name: forgot.append(name))
    assert _run(server.admin_add_job_delete("acc63", None)) == {"ok": True}
    assert forgot == ["acc63"] and cred_store.load("acc63") is None and "acc63" not in server.JOBS
    assert "acc63" not in server.pool.accounts


def test_only_failed_records_outside_the_pool_can_be_deleted(srv):
    server, bp, _ = srv
    server.JOBS["acc70"] = {"kind": "add", "status": "running", "email": "y@example.test", "source": "login"}
    with pytest.raises(HTTPException) as running:
        _run(server.admin_add_job_delete("acc70", None))
    assert running.value.status_code == 409
    _failed(server, "acc1")                                                    # 已在号池
    with pytest.raises(HTTPException) as in_pool:
        _run(server.admin_add_job_delete("acc1", None))
    assert in_pool.value.status_code == 409 and "acc1" in server.pool.accounts


def test_retry_uses_the_saved_credentials(srv, monkeypatch):
    server, bp, _ = srv
    _failed(server, "acc63")
    cred_store.store("acc63", "x@example.test", "pw", "123456")
    started = []

    async def fake_job(name, email, password, totp, **kw):
        started.append((name, email, password, totp))

    monkeypatch.setattr(server, "_run_add_job", fake_job)

    async def go():
        r = await server.admin_add_job_retry("acc63", None)
        await asyncio.sleep(0)
        return r

    assert _run(go())["job"] == "running"
    assert started == [("acc63", "x@example.test", "pw", "123456")]
    assert server.JOBS["acc63"]["status"] == "running"


def test_cookie_import_failures_cannot_be_retried(srv):
    server, bp, _ = srv
    _failed(server, "acc64", source="cookie")
    with pytest.raises(HTTPException) as exc:
        _run(server.admin_add_job_retry("acc64", None))
    assert exc.value.status_code == 409 and "Cookie" in exc.value.detail
