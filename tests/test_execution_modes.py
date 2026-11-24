"""execution_mode：chat（普通）/ work（工作）/ auto（默认），以及没绑定代理的号也能用。"""
import json
from pathlib import Path

import pytest

import config
import work_reply
from test_doubao import _pool
from test_task_deadline import _run
from test_work_execution import api_server

FAST = "doubao-seedance-2.0-fast"


def _set_type(bp, name, kind):
    state = Path(bp.accounts_dir) / name / "cookie_state.json"
    state.write_text(json.dumps({"cookies_list": [
        {"name": "sessionid", "value": name, "domain": ".doubao.com"},
        {"name": "has_biz_token", "value": "true" if kind == "work" else "false", "domain": ".doubao.com"},
    ]}), encoding="utf-8")
    bp.set_source(name, "cookie")


def _record(bp, tmp_path):
    output = tmp_path / "clip.mp4"
    output.write_bytes(b"clip")
    calls = []

    async def fake(account, prompt, ratio, duration, model, **kwargs):
        calls.append((account, kwargs["work_mode"], kwargs["execution_mode"]))
        return {"account": account, "local_path": str(output)}
    bp._generate_effective = fake
    return calls


# ---------- 规则 ----------

@pytest.mark.parametrize("duration", [5, 10, 15, 30])
def test_chat_never_uses_work_mode_even_with_the_switch_on(monkeypatch, duration):
    monkeypatch.setattr(config, "WORK_15S_ENABLED", True)
    assert not work_reply.uses_work_mode("seedance-2.0-fast", duration, "chat")
    assert not work_reply.uses_work_pair("seedance-2.0-fast", duration, "chat")
    assert not work_reply.is_work_request("seedance-2.0-fast", duration, "chat")
    assert not work_reply.is_work_request(FAST, duration, "chat")


def test_auto_rules_for_dola_are_unchanged(monkeypatch):
    monkeypatch.setattr(config, "WORK_15S_ENABLED", True)
    assert work_reply.uses_work_mode("seedance-2.0-fast", 15) and work_reply.uses_work_pair("seedance-2.0-fast", 30)
    assert not work_reply.uses_work_mode("seedance-2.0-fast", 10)


@pytest.mark.parametrize("kind,expected", [("work", "work"), ("personal", "chat"), (None, "chat")])
def test_auto_follows_the_doubao_account_type(tmp_path, monkeypatch, kind, expected):
    _module, bp = _pool(tmp_path, monkeypatch, dola=("acc1",), doubao=("db1",))
    if kind:
        _set_type(bp, "db1", kind)
    assert bp.execution_mode_for("db1") == expected
    assert bp.execution_mode_for("db1", "chat") == "chat" and bp.execution_mode_for("db1", "work") == "work"
    assert bp.execution_mode_for("acc1") == "auto"           # Dola 看模型和时长，不在这里展开


# ---------- 派号 ----------

def test_chat_skips_work_only_accounts(tmp_path, monkeypatch):
    _module, bp = _pool(tmp_path, monkeypatch, dola=(), doubao=("db1", "db2"))
    _set_type(bp, "db1", "work")
    _set_type(bp, "db2", "personal")
    calls = _record(bp, tmp_path)
    _run(bp.generate_video("湖面", "16:9", 5, FAST, execution_mode="chat", max_attempts=1))
    assert calls == [("db2", False, "chat")]


def test_chat_with_only_work_accounts_fails_without_sending(tmp_path, monkeypatch):
    _module, bp = _pool(tmp_path, monkeypatch, dola=(), doubao=("db1",))
    _set_type(bp, "db1", "work")
    calls = _record(bp, tmp_path)
    with pytest.raises(RuntimeError, match="没有普通模式"):
        _run(bp.generate_video("湖面", "16:9", 5, FAST, execution_mode="chat", max_attempts=1))
    assert calls == []


def test_chat_reports_locked_accounts_instead_of_waiting_for_a_work_only_one(tmp_path, monkeypatch):
    import time
    _module, bp = _pool(tmp_path, monkeypatch, dola=(), doubao=("db1", "db2"))
    _set_type(bp, "db1", "work")
    _set_type(bp, "db2", "personal")
    bp._mark_daily_limit("db2", "今日额度已用完", until=time.time() + 86400)
    calls = _record(bp, tmp_path)
    with pytest.raises(RuntimeError, match="db2"):
        _run(bp.generate_video("湖面", "16:9", 5, FAST, execution_mode="chat", max_attempts=1,
                               deadline=time.time() + 5))
    assert calls == []


def test_chat_sent_directly_to_a_work_only_account_stops_before_sending(tmp_path, monkeypatch):
    from generation_errors import PreSubmissionParametersError
    _module, bp = _pool(tmp_path, monkeypatch, dola=(), doubao=("db1",))
    _set_type(bp, "db1", "work")
    with pytest.raises(PreSubmissionParametersError, match="没有普通模式"):
        _run(bp._generate_effective("db1", "湖面", "16:9", 5, FAST, on_conversation_id=None, on_poll=None,
                                    on_balance=None, reference_image_paths=[], execution_mode="chat"))


# ---------- 代理 ----------

@pytest.mark.parametrize("isolate", [True, False])
@pytest.mark.parametrize("mode", ["chat", "work"])
def test_accounts_without_a_proxy_are_dispatched(tmp_path, monkeypatch, isolate, mode):
    module, bp = _pool(tmp_path, monkeypatch, dola=(), doubao=("db1",))
    monkeypatch.setattr(config, "ISOLATE_SHARED_EGRESS", isolate)
    monkeypatch.setattr(config, "EGRESS_PRECHECK", False)
    monkeypatch.setattr(module.proxy_store, "get_account_proxy", lambda name: None)
    monkeypatch.setattr(module.proxy_store, "binding_disabled", lambda name: False)
    calls = _record(bp, tmp_path)
    result = _run(bp.generate_video("湖面", "16:9", 5, FAST, execution_mode=mode, max_attempts=1))
    assert result["account"] == "db1" and calls == [("db1", mode == "work", mode)]


def test_strict_isolation_still_serialises_accounts_on_the_direct_egress(tmp_path, monkeypatch):
    import asyncio
    module, bp = _pool(tmp_path, monkeypatch, dola=(), doubao=("db1", "db2"))
    monkeypatch.setattr(config, "ISOLATE_SHARED_EGRESS", True)
    monkeypatch.setattr(config, "PROXY", "")
    monkeypatch.setattr(module.proxy_store, "get_account_proxy", lambda name: None)

    async def scenario():
        lock = bp._locks.setdefault("db1", asyncio.Lock())
        assert not bp._egress_blocked("db2")
        async with lock:
            assert bp._egress_blocked("db2")
    _run(scenario())


@pytest.fixture
def proxy_db(tmp_path, monkeypatch):
    import proxy_store
    monkeypatch.setattr(proxy_store, "DB_PATH", tmp_path / "proxies.db")
    return proxy_store


def test_binding_disabled_separates_no_binding_from_a_disabled_one(proxy_db):
    conn = proxy_db._connect()
    conn.execute("INSERT INTO account_proxy(account, proxy_id) VALUES ('dangling','gone')")
    conn.commit()
    conn.close()
    created = proxy_db.create_proxy(name="p", protocol="http", host="proxy.invalid", port=8080)
    pid = created["id"] if isinstance(created, dict) else created
    proxy_db.set_account_proxy("bound", pid)
    assert not proxy_db.binding_disabled("none") and not proxy_db.binding_disabled("bound")
    assert proxy_db.binding_disabled("dangling")
    proxy_db.update_proxy(pid, enabled=0)
    assert proxy_db.binding_disabled("bound")


@pytest.mark.parametrize("isolate", [True, False])
def test_browser_proxy_uses_the_default_route_only_when_nothing_is_bound(monkeypatch, isolate):
    import account_transport as t
    monkeypatch.setattr(t.config, "ISOLATE_SHARED_EGRESS", isolate)
    monkeypatch.setattr(t.proxy_store, "get_account_proxy", lambda name: None)
    monkeypatch.setattr(t.proxy_store, "binding_disabled", lambda name: False)
    monkeypatch.setattr(t.proxy_store, "account_proxy_url", lambda name: None)
    assert t.browser_proxy("acc1") is None and t.requests_proxy("acc1") is None
    monkeypatch.setattr(t.proxy_store, "account_proxy_url", lambda name: "http://default.invalid:1")
    assert t.browser_proxy("acc1") == "http://default.invalid:1"
    monkeypatch.setattr(t.proxy_store, "binding_disabled", lambda name: True)
    with pytest.raises(RuntimeError, match="已停用"):
        t.browser_proxy("acc1")
    monkeypatch.setattr(t.proxy_store, "binding_disabled", lambda name: False)
    monkeypatch.setattr(t.proxy_store, "get_account_proxy", lambda name: {"mode": "static"})
    monkeypatch.setattr(t.proxy_store, "account_proxy_url", lambda name: "")
    with pytest.raises(RuntimeError, match="禁止直连"):
        t.browser_proxy("acc1")


def test_unreadable_binding_is_treated_as_disabled(tmp_path, monkeypatch):
    module, bp = _pool(tmp_path, monkeypatch, dola=("acc1",), doubao=())

    def broken(name):
        raise OSError("db")
    monkeypatch.setattr(module.proxy_store, "binding_disabled", broken)
    assert bp._proxy_disabled("acc1") is True


def test_test_generate_warns_about_a_disabled_proxy_only(tmp_path, monkeypatch):
    module, bp = _pool(tmp_path, monkeypatch, dola=(), doubao=("db1",))
    monkeypatch.setattr(config, "ISOLATE_SHARED_EGRESS", True)
    monkeypatch.setattr(config, "PROXY", "")
    monkeypatch.setattr(module.proxy_store, "get_account_proxy", lambda name: None)
    monkeypatch.setattr(module.proxy_store, "binding_disabled", lambda name: False)
    assert not any("代理" in w for w in bp.test_generate_warnings("db1", FAST, 5, execution_mode="work"))
    monkeypatch.setattr(module.proxy_store, "binding_disabled", lambda name: True)
    assert any("已停用" in w for w in bp.test_generate_warnings("db1", FAST, 5, execution_mode="work"))


# ---------- 接口 ----------

def _client(server):
    return server._auth(None)


@pytest.mark.parametrize("mode", ["auto", "chat", "work"])
def test_submission_stores_the_requested_mode(tmp_path, monkeypatch, mode):
    server = api_server(tmp_path, monkeypatch)
    response = _run(server._submit_task(_client(server), FAST, "湖面", None, 5, [], execution_mode=mode))
    row = server.store.get(response.id)
    assert row["requested_mode"] == row["execution_mode"] == mode and response.execution_mode == mode


def test_omitted_mode_means_auto_and_unknown_mode_is_rejected(tmp_path, monkeypatch):
    server = api_server(tmp_path, monkeypatch)
    assert server.VideoGenRequest(prompt="湖面").execution_mode == "auto"
    response = _run(server._submit_task(_client(server), FAST, "湖面", None, 5, []))
    assert server.store.get(response.id)["requested_mode"] == "auto"
    with pytest.raises(server.HTTPException) as failure:
        _run(server._submit_task(_client(server), FAST, "湖面", None, 5, [], execution_mode="normal"))
    assert failure.value.status_code == 422 and "chat" in failure.value.detail


@pytest.mark.parametrize("duration", [15, 30])
def test_chat_rejects_the_work_only_durations_of_dola_20(tmp_path, monkeypatch, duration):
    server = api_server(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "WORK_15S_ENABLED", True)
    monkeypatch.setattr(config, "V20_DURATIONS", (5, 10, 15, 30))
    _run(server._submit_task(_client(server), "seedance-2.0-fast", "湖面", None, duration, []))      # auto 照收
    with pytest.raises(server.HTTPException) as failure:
        _run(server._submit_task(_client(server), "seedance-2.0-fast", "湖面", None, duration, [],
                                 execution_mode="chat"))
    assert failure.value.status_code == 422
    response = _run(server._submit_task(_client(server), "seedance-2.0-fast", "湖面", None, 10, [],
                                        execution_mode="chat"))
    assert server.store.get(response.id)["execution_mode"] == "chat"


@pytest.mark.parametrize("value,expected", [("chat", "chat"), (None, "auto"), ("", "auto")])
@pytest.mark.parametrize("model", [FAST, "seedance-2.0-fast"])
def test_ark_endpoint_accepts_chat_and_empty(tmp_path, monkeypatch, value, expected, model):
    server = api_server(tmp_path, monkeypatch)
    response = _run(server.ark_create_task({"model": model, "duration": 5, "execution_mode": value,
                                            "content": [{"type": "text", "text": "湖面"}]}, "Bearer x"))
    assert server.store.get(response["id"])["requested_mode"] == expected


def test_ark_chat_picks_a_chat_duration(tmp_path, monkeypatch):
    server = api_server(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "WORK_15S_ENABLED", True)
    monkeypatch.setattr(config, "V20_DURATIONS", (5, 10, 15, 30))
    response = _run(server.ark_create_task({"model": "seedance-2.0-fast", "duration": 15, "execution_mode": "chat",
                                            "content": [{"type": "text", "text": "湖面"}]}, "Bearer x"))
    assert server.store.get(response["id"])["duration"] == 10


def test_restarted_task_is_dispatched_with_the_requested_mode(tmp_path, monkeypatch):
    """auto 任务派过工作号后存的是 work；服务重启重新受理时仍按 auto 派号。"""
    server = api_server(tmp_path, monkeypatch)
    seen = []

    class Pool:
        accounts = ["db1"]

        async def generate_video(self, *args, **kwargs):
            seen.append(kwargs["execution_mode"])
            raise RuntimeError("stop")

        def __getattr__(self, name):
            return lambda *a, **k: None
    response = _run(server._submit_task(_client(server), FAST, "湖面", None, 5, []))
    server.store.update(response.id, execution_mode="work")
    monkeypatch.setattr(server, "pool", Pool())
    _run(server._run_task(response.id, FAST, "湖面", "16:9", 5, [], _client(server)))
    assert seen == ["auto"]


@pytest.mark.parametrize("row,expected", [
    ({"model": FAST, "execution_mode": "auto"}, "auto"),                                  # 还没派到号
    ({"model": FAST, "execution_mode": "auto", "account": "db1"}, "chat"),                # 老任务
    ({"model": FAST, "execution_mode": "chat", "account": "db1", "duration": 5}, "chat"),
    ({"model": FAST, "execution_mode": "work", "account": "db1", "duration": 5}, "work"),
    ({"model": "seedance-2.0-fast", "execution_mode": "auto", "duration": 10}, "chat"),
    ({"model": "seedance-2.0-fast", "execution_mode": "auto", "duration": 15}, "work"),
    ({"model": "seedance-2.5", "execution_mode": None, "duration": 10}, "chat"),
])
def test_reported_mode_is_the_one_actually_used(tmp_path, monkeypatch, row, expected):
    assert api_server(tmp_path, monkeypatch)._task_mode(row) == expected


def test_fixed_account_test_rejects_chat_on_a_work_account(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import sites
    server = api_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_admin_auth", lambda key: "owner")
    monkeypatch.setattr(server, "TEST_JOBS", {})
    monkeypatch.setattr(server, "pool", SimpleNamespace(
        accounts=["db"], site_of=lambda _n: sites.DOUBAO,
        execution_mode_for=lambda _n, requested="auto": "work" if requested == "auto" else requested,
        test_generate_warnings=lambda *a, **k: []))
    body = server.TestGenRequest(prompt="湖面白鹭", model=FAST, duration=5, execution_mode="chat", force=True)
    with pytest.raises(server.HTTPException) as failure:
        _run(server.admin_test_generate("db", body, None))
    assert failure.value.status_code == 422 and server.TEST_JOBS == {}
