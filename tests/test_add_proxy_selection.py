"""Explicit proxies must be assigned before the very first login/verification request."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import account_proxy_selection as selection


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_invalid_or_disabled_selection_rejected(monkeypatch):
    monkeypatch.setattr(selection.proxy_store, "get_proxy", lambda pid: {"enabled": pid == "p1"} if pid != "missing" else None)
    assert selection.validate_proxy_ids(["p1", "p1"]) == ["p1"]
    for pid in ("missing", "disabled"):
        with pytest.raises(ValueError, match="不存在或已停用"):
            selection.validate_proxy_ids([pid])


def test_empty_node_pool_cannot_fall_back_to_default(monkeypatch):
    monkeypatch.setattr(selection.proxy_store, "get_proxy", lambda _: {"enabled": True})
    monkeypatch.setattr(selection.proxy_store, "set_account_proxy", lambda *a: None)
    monkeypatch.setattr(selection.proxy_store, "proxy_url_for", lambda _: "http://default:80")
    monkeypatch.setattr(selection.proxy_store, "get_account_proxy", lambda _: {"mode": "extract"})
    monkeypatch.setattr(selection.proxy_store, "get_session", lambda _: {})
    with pytest.raises(ValueError, match="没有可用节点"):
        selection.bind_selected_proxy("acc1", "p1")


@pytest.mark.parametrize("proxy_id", ["p1", None])
def test_cookie_first_probe_uses_selected_proxy(tmp_path, monkeypatch, proxy_id):
    import config
    import cookie_login
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "COOKIE_RUNTIME", "pure")
    monkeypatch.setattr(cookie_login, "browser_backend_available", lambda: (False, "test"))
    events = []
    monkeypatch.setattr(selection, "bind_selected_proxy", lambda n, p: events.append(("bind", n, p)))
    expected_proxy = "http://chosen:8080" if proxy_id else "http://default:8080"
    monkeypatch.setattr(selection.proxy_store, "requests_proxy_url", lambda n: expected_proxy)
    def probe(state, proxy=""):
        assert events == ([("bind", "acc1", "p1")] if proxy_id else [])
        assert Path(state).exists()
        events.append(("probe", proxy))
        return True, ""
    monkeypatch.setitem(sys.modules, "doubao_gen", SimpleNamespace(probe_login=probe))
    bundle = {"cookies": [{"name": "sessionid", "value": "test-only", "domain": ".doubao.com"}]}
    result = run(cookie_login.import_cookie_account("acc1", bundle, proxy_id=proxy_id))
    assert result["verified"] and events[-1] == ("probe", expected_proxy)


def test_batch_round_robin_skips_existing_cookie(monkeypatch):
    import cookie_login
    monkeypatch.setattr(selection, "validate_proxy_ids", lambda ids: ids)
    assignments = []
    async def fake_import(name, data, **kwargs):
        assignments.append((name, kwargs["proxy_id"]))
        return {"verified": True}
    monkeypatch.setattr(cookie_login, "import_cookie_account", fake_import)
    pool = SimpleNamespace(accounts=["acc9"], find_by_sessionid=lambda sid: ("acc9",) if sid == "old" else None,
                           ensure_account=lambda *a: None, set_sessionid=lambda *a: None,
                           set_source=lambda *a: None, set_login_status=lambda *a: None,
                           set_email=lambda *a: None)
    result = run(cookie_login.import_cookie_accounts_from_text(pool,
        "sessionid=old\nsessionid=a\nsessionid=b\nsessionid=c", proxy_ids=["p1", "p2"]))
    assert result["updated"] == ["acc9"]
    assert assignments == [("acc1", "p1"), ("acc2", "p2"), ("acc3", "p1")]


def test_google_binding_precedes_login(monkeypatch):
    import server
    events = []
    monkeypatch.setattr(server, "ADD_ACCOUNT_SEM", asyncio.Semaphore(1))
    monkeypatch.setattr(server, "JOBS", {})
    monkeypatch.setattr(server, "ADD_SEM_HOLDER", {"what": ""})
    monkeypatch.setattr(server, "bind_selected_proxy", lambda n, p: events.append(("bind", p)))
    async def login(*args):
        assert events == [("bind", "p2")]
        events.append(("login",))
    monkeypatch.setattr(server, "add_account_flow", login)
    monkeypatch.setattr(server, "pool", SimpleNamespace(ensure_account=lambda *a: None,
        set_email=lambda *a: None, set_source=lambda *a: None, set_login_status=lambda *a: None))
    run(server._run_add_job("acc1", "test@example.com", "test-only", "", proxy_id="p2"))
    assert server.JOBS["acc1"]["status"] == "success"
    assert events == [("bind", "p2"), ("login",)]


def test_single_cookie_job_passes_proxy_to_import(monkeypatch):
    import server
    seen = []
    async def fake_import(name, data, **kwargs):
        seen.append(kwargs)
        return {"verified": False}
    monkeypatch.setattr(server, "import_cookie_account", fake_import)
    monkeypatch.setattr(server, "ADD_ACCOUNT_SEM", asyncio.Semaphore(1))
    monkeypatch.setattr(server, "JOBS", {})
    monkeypatch.setattr(server, "ADD_SEM_HOLDER", {"what": ""})
    monkeypatch.setattr(server, "pool", SimpleNamespace(ensure_account=lambda *a: None,
        set_email=lambda *a: None, set_source=lambda *a: None, set_login_status=lambda *a: None))
    run(server._run_add_job("acc1", "", "", "", cookies="[]", cookie_skip_verify=True, proxy_id="p1"))
    assert seen == [{"require_login": False, "proxy_id": "p1"}]
