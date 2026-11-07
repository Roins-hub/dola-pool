"""删除账号不能和后台重登抢目录，提示要说清账号在做什么（2026-11-07 acc144）。"""
import shutil

import pytest

import browser_pool
from test_account_throttle import _pool
from test_task_deadline import _run


@pytest.mark.parametrize("activity,word", [("generating", "正在出片"), ("verifying", "正在验证或重新登录"),
                                           ("probing", "正在探测")])
def test_delete_says_what_the_account_is_doing(tmp_path, monkeypatch, activity, word):
    _, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})

    async def scenario():
        async with bp._use_account("acc1", activity):
            with pytest.raises(RuntimeError, match=word):
                bp.delete_account("acc1")
    _run(scenario())


def test_delete_retries_and_keeps_the_record_when_the_folder_is_in_use(tmp_path, monkeypatch):
    _, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    (bp.accounts_dir / "acc1").mkdir(parents=True, exist_ok=True)
    calls = []

    def busy(path):
        calls.append(path)
        raise OSError(39, "Directory not empty")

    monkeypatch.setattr(shutil, "rmtree", busy)
    monkeypatch.setattr(browser_pool.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="稍后再点一次删除"):
        bp.delete_account("acc1")
    assert len(calls) == 3 and bp._meta("acc1")           # 记录还在，可以再删一次


def test_background_relogin_holds_the_account_lock(tmp_path, monkeypatch):
    import server
    _, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a", "acc2": "b"})
    bp.set_login_status("acc1", False)
    bp.set_login_status("acc2", False)
    monkeypatch.setattr(server, "pool", bp)
    monkeypatch.setattr(server, "LAST_CRED_REFRESH", {})
    monkeypatch.setattr(server.cred_store, "all_names", lambda: ["acc1", "acc2"])
    monkeypatch.setattr(server.cred_store, "load", lambda n: {"email": f"{n}@x", "password": "p", "totp": ""})
    seen = {}

    async def flow(name, *a, **k):
        seen[name] = (bp._locks[name].locked(), bp._activities.get(name))
        with pytest.raises(RuntimeError, match="正在验证或重新登录"):
            bp.delete_account(name)

    monkeypatch.setattr(server, "add_account_flow", flow)

    async def scenario():
        async with bp._use_account("acc2", "generating"):      # 正在出片的号不抢
            await server._cred_try_refresh_once()
    _run(scenario())
    assert seen == {"acc1": (True, "verifying")}
