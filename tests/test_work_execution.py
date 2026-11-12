"""显式工作模式：站点隔离、原生时长、固定账号和只读恢复。"""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import sites
import work_reply as wr
from test_account_throttle import _row
from test_task_deadline import _run
from test_work_mode import _agent, _work_pool
from test_work_followups import work_server
from test_work_session import Page, load_agent


@pytest.mark.parametrize("model,seconds,valid", [
    ("seedance-2.0-fast", 30, True), ("seedance-2.0-fast", 15, True),
    ("seedance-2.0-fast", 5, True), ("seedance-2.5", 15, False),
    ("doubao-seedance-2.0-fast", 15, True), ("doubao-seedance-2.0-mini", 4, True),
    ("doubao-seedance-2.0-fast", 30, False), ("doubao-seedance-2.5", 30, True),
])
def test_explicit_mode_does_not_depend_on_dola_15s_switch(monkeypatch, model, seconds, valid):
    monkeypatch.setattr(config, "WORK_15S_ENABLED", False)
    assert wr.uses_work_mode(model, seconds, "work") is valid
    assert wr.is_work_request(model, seconds, "work") is valid
    assert not wr.uses_work_pair(model, seconds, "work")


def test_native_thirty_prompt_never_inserts_fifteen_seconds():
    text = wr.build_prompt("白鹭飞过湖面", "16:9", 30, "seedance-2.0-fast")
    assert "30s" in text and "原生 30 秒" in text and "15" not in text
    assert "Seedance 2.0 Fast" in text and "不拆段" in text and "免费额度" in text


def test_skills_and_cookies_are_selected_for_the_correct_site():
    data = [{"name": "dola-creative-video", "display_name": "Creative Videos"},
            {"name": "doubao-creative-video", "display_name": "创意视频"},
            {"name": "unrelated-video", "display_name": "视频智能处理"}]
    assert wr.video_skill_labels(data, sites.DOUBAO) == ["创意视频"]
    assert wr.video_skill_labels(data) == ["Creative Videos"]
    cookies = [{"domain": ".doubao.com", "name": "sessionid"},
               {"domain": ".dola.com", "name": "sessionid"},
               {"domain": ".notdoubao.com", "name": "sessionid"}]
    assert wr.session_cookies(cookies, sites.DOUBAO) == cookies[:1]


def test_doubao_uses_its_own_page_query_and_requested_ai_parameters(monkeypatch):
    agent = load_agent(monkeypatch)
    page = Page([])
    item = agent._WorkSession("db", page, None, duration=10, model="doubao-seedance-2.0-fast")
    assert item.base == "https://www.doubao.com"
    assert item.responder.context["duration"] == 10 and item.responder.context["site"] == "豆包"
    calls = {}
    page.on = lambda name, callback: calls.setdefault(name, []).append(callback)
    item.watch()
    calls["request"][0](SimpleNamespace(url="https://www.dola.com/x?device_id=wrong&aid=2"))
    assert not item.page_query
    calls["request"][0](SimpleNamespace(url="https://www.doubao.com/x?device_id=1&aid=2"))
    assert item.page_query.startswith(item.base)


def test_native_thirty_dispatch_uses_one_account_without_pair(tmp_path, monkeypatch):
    bp_module, pool = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    monkeypatch.setattr(bp_module, "ensure_web_playable", lambda p: p)
    calls = []

    async def generate(account, prompt, ratio, **kwargs):
        calls.append((account, kwargs["duration"], kwargs["model"]))
        output = tmp_path / (account + ".mp4")
        output.write_bytes(b"clip")
        return {"local_path": str(output), "account": account}

    _agent(monkeypatch, generate)
    result = _run(pool.generate_video("湖面", "16:9", 30, model="seedance-2.0-fast",
                                      execution_mode="work", max_attempts=1))
    assert calls == [(result["account"], 30, "seedance-2.0-fast")]
    assert not result.get("merge_mode")
    assert _row(pool, result["account"])["work_blocked_until"] > time.time()
    assert sum(_row(pool, name)["used_today"] for name in pool.accounts) == 0


def test_doubao_work_cookie_account_uses_browser_and_its_own_quota(tmp_path, monkeypatch):
    bp_module, pool = _work_pool(tmp_path, monkeypatch, ["db"])
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    state = Path(pool.accounts_dir) / "db" / "cookie_state.json"
    state.write_text(json.dumps({"cookies_list": [{"domain": ".doubao.com", "name": "sessionid", "value": "test"}]}), encoding="utf-8")
    pool._conn.execute("UPDATE accounts_meta SET source='cookie' WHERE name='db'")
    monkeypatch.setattr(bp_module, "ensure_web_playable", lambda p: p)
    calls = []

    async def generate(account, prompt, ratio, **kwargs):
        calls.append(kwargs)
        output = tmp_path / "doubao.mp4"
        output.write_bytes(b"clip")
        return {"local_path": str(output), "account": account}

    _agent(monkeypatch, generate)
    result = _run(pool.test_generate("db", "湖面", "16:9", 15, "doubao-seedance-2.0-fast", execution_mode="work"))
    assert result["ok"] and len(calls) == 1
    assert calls[0]["model"] == "doubao-seedance-2.0-fast"
    assert not _row(pool, "db")["work_blocked_until"]
    assert _row(pool, "db")["used_today"] > 0


def test_short_work_result_preserves_video_and_claims_only_one_quota(tmp_path, monkeypatch):
    bp_module, pool = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    monkeypatch.setattr(bp_module, "ensure_web_playable", lambda p: p)
    output = tmp_path / "short.mp4"
    output.write_bytes(b"clip")
    calls = []

    async def generate(account, *args, **kwargs):
        calls.append(account)
        return {"account": account, "local_path": str(output), "video_duration": 15,
                "duration_error": "请求30秒，实际15秒"}

    _agent(monkeypatch, generate)
    result = _run(pool.test_generate("acc1", "湖面", "16:9", 30, "seedance-2.0-fast", execution_mode="work"))
    assert result["ok"] is False and result["video_url"].endswith("short.mp4")
    assert result["video_duration"] == 15 and output.is_file() and calls == ["acc1"]
    assert _row(pool, "acc1")["work_blocked_until"] > time.time()
    assert not _row(pool, "acc2")["work_blocked_until"]


@pytest.mark.parametrize("model,seconds", [("seedance-2.0-fast", 30), ("doubao-seedance-2.0-fast", 15)])
def test_explicit_work_recovery_reads_original_without_pair_or_resubmission(tmp_path, monkeypatch, model, seconds):
    server, pool = work_server(tmp_path, monkeypatch, [{"local_path": str(tmp_path / "late.mp4")}])
    server.store.update("video_t1", model=model, duration=seconds, execution_mode="work")
    _run(server._recover_loop("video_t1"))
    row = server.store.get("video_t1")
    assert row["status"] == "completed" and row["recover_state"] == "recovered"
    assert not pool.merged and not pool.continued and pool.calls == 1
    assert server.store.get("video_t1")["execution_mode"] == "work"


def test_paid_or_wrong_duration_confirmation_stops_before_reply(monkeypatch):
    agent = load_agent(monkeypatch)
    item = agent._WorkSession("acc", Page([]), None, duration=30, model="seedance-2.0-fast")
    from generation_errors import UpstreamReplyError
    for text in ("请确认：时长：15秒，模型：Seedance 2.0 Fast", "是否继续使用付费额度？"):
        with pytest.raises(UpstreamReplyError) as caught:
            _run(item.next_answer(text))
        assert caught.value.allow_account_retry is False
    assert not item.page.typed


def test_no_matching_native_choice_stops_without_accepting_split(monkeypatch):
    agent = load_agent(monkeypatch)
    item = agent._WorkSession("acc", Page([]), None, duration=30, model="seedance-2.0-fast")
    from generation_errors import UpstreamReplyError
    with pytest.raises(UpstreamReplyError):
        _run(item.choice_labels([{"title": "请选择时长", "options": ["两段15秒", "单条15秒（推荐）"]}]))


def test_doubao_cookie_copy_never_opens_dola_profile(tmp_path, monkeypatch):
    agent = load_agent(monkeypatch)
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "accounts" / "db"
    root.mkdir(parents=True)
    (root / "cookie_state.json").write_text(json.dumps({"cookies_list": [
        {"domain": ".doubao.com", "name": "sessionid", "value": "test"},
        {"domain": ".dola.com", "name": "sessionid", "value": "other"}]}), encoding="utf-8")
    result = _run(agent._copied_session(None, "db"))
    assert len(result) == 1 and result[0]["domain"] == ".doubao.com"


def api_server(tmp_path, monkeypatch):
    import server
    from store import TaskStore
    monkeypatch.setattr(server, "store", TaskStore(str(tmp_path / "tasks.db")))
    monkeypatch.setattr(server, "pool", SimpleNamespace(available=True, accounts=["acc1"], site_of=lambda account: sites.DOLA))
    monkeypatch.setattr(config, "WORK_15S_ENABLED", False)
    monkeypatch.setattr(config, "LONG_VIDEO_MODE", "reject")
    client = {"allowed_durations": [5, 10, 15, 30], "api_key_hash": "test", "api_key_name": "test",
              "daily_limit": 0, "concurrency_limit": 0}
    monkeypatch.setattr(server, "_auth", lambda key: client)
    monkeypatch.setattr(server.asyncio, "create_task", lambda coro: coro.close())
    return server


def test_public_json_endpoint_accepts_and_persists_native_thirty_with_auto_switch_off(tmp_path, monkeypatch):
    server = api_server(tmp_path, monkeypatch)

    class Request:
        headers = {"content-type": "application/json"}

        async def json(self):
            return {"prompt": "湖面白鹭", "model": "seedance-2.0-fast", "duration": 30, "execution_mode": "work"}

    response = _run(server.create_video(Request(), "test"))
    row = server.store.get(response.id)
    assert row["execution_mode"] == "work" and row["duration"] == 30
    assert row["model"] == "seedance-2.0-fast" and row["status"] == "queued"


def test_ark_keeps_explicit_native_thirty_instead_of_rounding_to_normal_chat_duration(tmp_path, monkeypatch):
    server = api_server(tmp_path, monkeypatch)
    response = _run(server.ark_create_task({"model": "seedance-2.0-fast", "duration": 30, "execution_mode": "work",
                                            "content": [{"type": "text", "text": "湖面白鹭"}]}, "test"))
    row = server.store.get(response["id"])
    assert row["duration"] == 30 and row["execution_mode"] == "work"


def test_account_endpoint_accepts_native_thirty_and_preserves_fixed_account(tmp_path, monkeypatch):
    server = api_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_admin_auth", lambda key: "owner")
    monkeypatch.setattr(server, "TEST_JOBS", {})
    body = server.TestGenRequest(prompt="湖面白鹭", model="seedance-2.0-fast", duration=30,
                                 execution_mode="work", force=True)
    response = _run(server.admin_test_generate("acc1", body, None))
    job = server.TEST_JOBS[response["task_id"]]
    assert job["account"] == "acc1" and job["execution_mode"] == "work" and job["duration"] == 30
