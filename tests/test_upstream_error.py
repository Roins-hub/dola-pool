"""Issue #24: offline diagnostics -> exception/result -> DB -> public API."""
import asyncio
import json
import sqlite3
import types
from contextlib import asynccontextmanager

import pytest

import upstream_error as diagnostics


RAW = {"ai_creation_res_code": "710082023", "ai_creation_tool_list": json.dumps([
    {"status": 5, "fail_code": 710082023, "failure_reason": "cta_limit",
     "file_type": "video", "tool_name": "text_to_video",
     "cookie": "do-not-store", "download_url": "https://invalid/?token=secret"}
]), "token": "do-not-store", "log_id": "private-trace"}


def message(text="视频生成失败，生成额度未扣除。", ext=None):
    return {"message_id": "synthetic-message", "index_in_conv": 3, "user_type": 2,
            "content_block": [{"content": {"text_block": {"text": text}}, "is_finish": True}],
            "ext": RAW if ext is None else ext}


def test_actual_message_fields_are_retained_and_allowlisted():
    from doubao_gen import _messages
    row = _messages({"messages": [message()]})[0]
    error = diagnostics.attach(RuntimeError(row["text"]), row["upstream_error"])
    fields = diagnostics.failure_fields(error)
    assert fields["failure_code"] == "710082023"
    assert json.loads(fields["upstream_error"])["ai_creation_tool_list"] == [
        {"status": "5", "fail_code": "710082023", "failure_reason": "cta_limit"}]
    assert not any(secret in fields["upstream_error"] for secret in
                   ("do-not-store", "https://", "private-trace", "token", "cookie"))
    assert str(error) == "视频生成失败，生成额度未扣除。"


@pytest.mark.parametrize("value", [None, [], True, "{", "x" * 16385, "[" * 2000])
def test_malformed_diagnostics_never_mask_original_failure(value):
    error = diagnostics.attach(ValueError("original"), value)
    assert str(error) == "original"
    assert diagnostics.failure_fields(error) == {"failure_code": None, "upstream_error": None}


def test_bounded_tools_and_freeform_reason_not_exposed():
    raw = {"ai_creation_res_code": True, "ai_creation_tool_list": [
        {"status": 5, "fail_code": "https://invalid/secret", "failure_reason": "cookie=secret"}
    ] * 100}
    normalized = diagnostics.normalize(raw)
    assert normalized == {"ai_creation_tool_list": [{"status": "5"}] * 16}


@pytest.mark.parametrize("status,code", [("4", "710082023"), ("5", "0"), ("1", "710082041")])
def test_non_failure_codes_do_not_become_failure_code(status, code):
    fields = diagnostics.failure_fields({"upstream_error": {
        "ai_creation_res_code": "710082041", "ai_creation_tool_list": [
            {"status": status, "fail_code": code}]}})
    assert fields["failure_code"] is None
    assert json.loads(fields["upstream_error"])["ai_creation_res_code"] == "710082041"


def test_legacy_sqlite_migration_and_reopen(tmp_path):
    from store import TaskStore
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, model TEXT, prompt TEXT, ratio TEXT, duration INTEGER, status TEXT, video_url TEXT, error TEXT, created_at REAL, updated_at REAL)")
    db.commit(); db.close()
    store = TaskStore(str(path))
    store.create("case", "doubao-seedance-2.0-fast", "synthetic", "16:9", 5)
    store.update("case", status="failed", error="unchanged", **diagnostics.failure_fields({"upstream_error": RAW}))
    store._conn.close()
    reopened = TaskStore(str(path))
    row = reopened.get("case")
    assert row["error"] == "unchanged" and row["failure_code"] == "710082023"
    assert diagnostics.public_fields(row)["upstream_error"]["ai_creation_res_code"] == "710082023"
    assert diagnostics.public_fields({**row, "status": "completed"}) == {"failure_code": None, "upstream_error": None}
    reopened.update("case", status="completed")
    assert reopened.get("case")["failure_code"] is None
    assert reopened.get("case")["upstream_error"] is None
    reopened._conn.close()


def test_generation_exception_keeps_diagnostics(monkeypatch, tmp_path):
    from test_doubao import _FakeDP, _gen, FREE_ACCEPTED
    from generation_errors import UpstreamReplyError
    ext = {**RAW, "agent_name": "Agent-Creation", "has_video_gen": "1"}
    dp = _FakeDP([(0, FREE_ACCEPTED, []), (0, "视频生成失败", [], ext)])
    with pytest.raises(UpstreamReplyError) as failure:
        _gen(monkeypatch, tmp_path, dp)
    assert diagnostics.failure_fields(failure.value)["failure_code"] == "710082023"
    assert dp.replies == [] and dp.downloaded == []


def test_work_failure_keeps_code_without_changing_exception(monkeypatch):
    from test_work_session import load_agent
    _WorkSession = load_agent(monkeypatch)._WorkSession
    from doubao_gen import DoubaoQuotaError
    session = object.__new__(_WorkSession)
    session.site = "doubao"
    error = session.creation_failure([message()])
    assert isinstance(error, DoubaoQuotaError)
    assert diagnostics.failure_fields(error)["failure_code"] == "710082023"


@asynccontextmanager
async def idle_account(*args):
    yield


def fake_pool():
    from browser_pool import BrowserPool
    from generation_errors import UpstreamReplyError
    class TestPool(BrowserPool):
        @property
        def accounts(self):
            return ["synthetic"]
    pool = object.__new__(TestPool)
    pool._locks = {}
    pool.semaphore = asyncio.Semaphore(1)
    pool.site_of = lambda _: "doubao"
    pool.execution_mode_for = lambda *args: "auto"
    pool._duration_cost = lambda *args: 1
    pool._use_account = idle_account
    pool._note_upstream_failure = lambda *args: None
    async def fail(*args, **kwargs):
        raise diagnostics.attach(UpstreamReplyError("上游生成失败", "视频生成失败，生成额度未扣除。"), RAW)
    pool._generate_effective = fail
    return pool


def test_pool_admin_db_and_public_api_roundtrip(monkeypatch, tmp_path):
    import server
    from store import TaskStore
    from fastapi.testclient import TestClient
    store = TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "pool", fake_pool())
    monkeypatch.setattr(server, "TEST_JOBS", {"case": {}})
    monkeypatch.setattr(server, "_auth", lambda _: {"api_key_hash": None})
    asyncio.run(server._run_test_generate("case", "synthetic", "x", "16:9", 5, "doubao-seedance-2.0-fast"))
    row = store.get("case")
    assert row["status"] == "failed" and row["failure_code"] == "710082023"
    assert server.TEST_JOBS["case"]["result"]["failure_code"] == "710082023"
    # No lifespan: no workers, real network, account cookies or video generation.
    client = TestClient(server.app)
    for path in ("/v1/videos/case", "/api/v3/contents/generations/tasks/case"):
        response = client.get(path)
        assert response.status_code == 200
        body = response.json()
        assert body["failure_code"] == "710082023"
        assert body["error"] == row["error"]
        assert isinstance(body["error"], str)
        assert body["upstream_error"]["ai_creation_tool_list"][0]["fail_code"] == "710082023"
    monkeypatch.setattr(server, "_auth", lambda _: {"api_key_hash": "another-client"})
    assert client.get("/v1/videos/case").status_code == 404
    client.close(); store._conn.close()


def test_later_success_does_not_reuse_earlier_tool_failure():
    fields = diagnostics.failure_fields({"upstream_error": {"ai_creation_tool_list": [
        {"status": 5, "fail_code": 710082023}, {"status": 4, "fail_code": 0}]}})
    assert fields["failure_code"] is None


def test_non_video_tool_failure_is_not_reported_as_video_failure():
    assert diagnostics.normalize({"ai_creation_tool_list": [
        {"file_type": "image", "tool_name": "text_to_image", "status": 5, "fail_code": 123}]}) == {}


def test_existing_typed_failure_preserves_retry_policy():
    from generation_errors import UpstreamGenerationFailed
    error = UpstreamGenerationFailed("上游生成失败", "failed", code="710082023",
                                    conversation_id="synthetic", retryable=False, not_charged=True)
    assert diagnostics.failure_fields(error)["failure_code"] == "710082023"
    assert error.retryable is False and error.not_charged is True
