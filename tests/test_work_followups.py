"""工作模式迟到取回、分段重试、重复账号和上传失败的服务回归。"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from test_auto_recover import FakePool, _run, _server, _stubs
from test_account_throttle import _row
from test_work_mode import _agent, _work_pool
from test_work_session import Page, bot, load_agent, session, video


def test_duplicate_accounts_share_one_dispatch_entry_and_can_hand_over(tmp_path, monkeypatch):
    _, pool = _work_pool(tmp_path, monkeypatch, ["acc21", "acc24", "acc30"])
    for name, created in (("acc21", 1), ("acc24", 2)):
        pool._conn.execute("UPDATE accounts_meta SET email=?, created_at=? WHERE name=?",
                           (" SAME@example.com " if name == "acc24" else "same@example.com", created, name))
    assert _row(pool, "acc24")["duplicate_of"] == "acc21"
    assert not pool._work_ready(_row(pool, "acc24"))
    assert pool._work_ready(_row(pool, "acc21"))
    assert not pool._schedulable(_row(pool, "acc24"))
    pool.set_scheduling("acc21", False)
    assert _row(pool, "acc21")["duplicate_of"] == "acc24"
    assert pool._work_ready(_row(pool, "acc24"))
    assert all((Path(pool.accounts_dir) / name).exists() for name in ("acc21", "acc24"))


def test_same_email_on_different_sites_is_not_a_duplicate(tmp_path, monkeypatch):
    _, pool = _work_pool(tmp_path, monkeypatch, ["dola", "doubao"])
    pool._conn.execute("UPDATE accounts_meta SET email='same@example.com'")
    (Path(pool.accounts_dir) / "doubao" / "cookie_state.json").write_text(
        '{"cookies_list":[{"name":"sessionid","value":"test","domain":".doubao.com"}]}', encoding="utf-8")
    assert pool.same_login("dola") == []
    assert not any(row["duplicate_of"] for row in pool.list_accounts())


@pytest.mark.parametrize("ready", [True, False])
def test_second_segment_timeout_reuses_first_and_retries_only_once(tmp_path, monkeypatch, ready):
    browser_pool, pool = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2", "acc3", "acc4"])
    import work_pair
    calls, snapshots = [], []
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(browser_pool, "ensure_web_playable", lambda path: path)

    def frame(source, output):
        Path(output).write_bytes(b"frame")
        return Path(output)

    def concat(first, second, output):
        assert Path(first).read_bytes() == b"first"
        Path(output).write_bytes(b"30 seconds")
        return Path(output)

    monkeypatch.setattr(work_pair, "last_frame", frame)
    monkeypatch.setattr(work_pair, "concat", concat)

    async def generate(account, prompt, ratio, **kwargs):
        calls.append(account)
        kwargs["on_conversation_id"](account, account + "-conv", time.time() + 60)
        if len(calls) == 2 or len(calls) == 3 and not ready:
            raise TimeoutError("工作模式等待超时，可能仍在生成，不换号重提")
        path = tmp_path / (account + ".mp4")
        path.write_bytes(b"first" if len(calls) == 1 else b"second")
        return {"account": account, "conversation_id": account + "-conv", "local_path": str(path)}

    _agent(monkeypatch, generate)
    coroutine = pool.generate_video("猫", "16:9", 30, model="seedance-2.0", max_attempts=1,
                                    on_pair_state=lambda state: snapshots.append(json.loads(json.dumps(state))))
    if ready:
        result = _run(coroutine)
        assert Path(result["local_path"]).read_bytes() == b"30 seconds"
        assert result["accounts"] == [calls[0], calls[2]]
    else:
        with pytest.raises(RuntimeError, match="第 2 段失败"):
            _run(coroutine)
        assert Path(snapshots[-1]["first"]["local_path"]).read_bytes() == b"first"
        assert Path(snapshots[-1]["frame"]).is_file()
    assert len(calls) == len(set(calls)) == 3
    assert snapshots[-1]["second_retries"] == 1
    assert len(snapshots[-1]["pending"]) == (1 if ready else 2)
    assert calls[1] in pool._work_suspect


def test_recovered_work_video_locks_weekly_quota_without_daily_charge(tmp_path, monkeypatch):
    _, pool = _work_pool(tmp_path, monkeypatch, ["acc1"])
    monkeypatch.setattr(config, "WORK_15S_ENABLED", False)
    submitted = time.time() - 3600
    pool.note_work_suspect("acc1")
    assert pool.claim_recovered("acc1", "seedance-2.0", 15, {"submitted_at": submitted}) == 0
    row = _row(pool, "acc1")
    assert row["work_blocked_until"] == pytest.approx(submitted + 7 * 86400)
    assert row["used_today"] == 0 and "acc1" not in pool._work_suspect


def test_daily_limit_does_not_block_dispatch_of_unused_work_quota(tmp_path, monkeypatch):
    _, pool = _work_pool(tmp_path, monkeypatch, ["acc1"])
    pool._claim("acc1", 4)
    pool._mark_daily_limit("acc1", "每日额度用完")

    async def generate(account, *args, **kwargs):
        return {"account": account, "local_path": ""}

    _agent(monkeypatch, generate)
    assert _run(pool.generate_video("猫", "16:9", 15, model="seedance-2.0", max_attempts=1))["account"] == "acc1"


@pytest.mark.parametrize("attempts", [1, 2])
def test_pair_restart_reuses_first_and_retains_second_attempt_limit(tmp_path, monkeypatch, attempts):
    browser_pool, pool = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2", "acc3"])
    import work_pair
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "WORK_15S_ENABLED", False)
    monkeypatch.setattr(browser_pool, "ensure_web_playable", lambda path: path)
    first = tmp_path / "already-generated.mp4"
    first.write_bytes(b"first")
    state = {"first": {"account": "acc1", "local_path": str(first)}, "second_attempts": attempts,
             "second_retries": attempts - 1, "pending": [], "stem": str(tmp_path / "pair_restored")}
    calls = []

    def made(*args):
        Path(args[-1]).write_bytes(b"out")
        return Path(args[-1])

    monkeypatch.setattr(work_pair, "last_frame", made)
    monkeypatch.setattr(work_pair, "concat", made)

    async def generate(account, *args, **kwargs):
        calls.append(account)
        path = tmp_path / "second.mp4"
        path.write_bytes(b"second")
        return {"account": account, "local_path": str(path)}

    _agent(monkeypatch, generate)
    job = pool.generate_video("猫", "16:9", 30, model="seedance-2.0", pair_state=state, max_attempts=1)
    if attempts == 1:
        assert _run(job)["merge_mode"] == "work_pair"
        assert len(calls) == 1 and calls[0] != "acc1"
    else:
        with pytest.raises(RuntimeError, match="最多两次"):
            _run(job)
        assert calls == []
    assert state["second_attempts"] == 2


class WorkPool(FakePool):
    def __init__(self, results):
        super().__init__(results, pure=False)
        self.cleared, self.discarded, self.continued, self.merged = [], [], [], []

    async def fetch_work_conversation_video(self, account, conv, **kwargs):
        return self.fetch_conversation_video(account, conv)

    def clear_work_suspect(self, account, **kwargs):
        self.cleared.append(account)

    def discard_work_pair(self, state):
        self.discarded.append(state)

    async def _local_video(self, result):
        return result["local_path"]

    async def finish_work_pair(self, state, second):
        assert Path(state["first"]["local_path"]).is_file()
        self.merged.append(second["account"])
        output = Path(state["stem"] + ".mp4")
        output.write_bytes(b"30 seconds")
        state["active"], state["delivered"] = None, True
        return {"local_path": str(output), "account": second["account"]}

    async def generate_work_pair(self, prompt, ratio, model, **kwargs):
        state = kwargs["pair_state"]
        self.continued.append(state["first"]["account"])
        output = Path(state["stem"] + ".mp4")
        output.write_bytes(b"30 seconds")
        state["delivered"], state["active"] = True, None
        kwargs["on_pair_state"](state)
        return {"local_path": str(output), "account": "second"}


def work_server(tmp_path, monkeypatch, results, *, pair=None, status="failed"):
    pool = WorkPool(results)
    server = _server(tmp_path, monkeypatch, pool)
    server.store.update("video_t1", model="seedance-2.0", duration=30 if pair is not None else 15,
                        status=status, recover_state="watching", recover_until=time.time() + 60,
                        work_pair_state=json.dumps(pair) if pair is not None else "")

    async def completion(task_id, result, duration, ratio):
        return {"video_url": "https://example.com/videos/" + Path(result["local_path"]).name,
                "video_duration": duration}

    monkeypatch.setattr(server, "_completion_fields", completion)
    return server, pool


def pending(account="acc1", segment=2):
    return {"account": account, "conversation_id": account + "-conv", "segment": segment,
            "submitted_at": time.time() - 100, "suspect_at": time.time()}


def test_work_timeout_recovers_with_browser_even_when_work_switch_is_off(tmp_path, monkeypatch):
    server, pool = work_server(tmp_path, monkeypatch, [
        {"local_path": str(tmp_path / "dl" / "late.mp4"), "status": "succeeded"}])
    monkeypatch.setattr(config, "WORK_15S_ENABLED", False)
    _run(server._recover_loop("video_t1"))
    row = server.store.get("video_t1")
    assert row["status"] == "completed" and row["recover_state"] == "recovered"
    assert pool.claimed == [("acc1", "seedance-2.0", 15)]
    assert dict(server.store.work_videos_since(0))["acc1"] > 0


def test_admin_recovery_delivers_the_original_work_task_and_claims_once(tmp_path, monkeypatch):
    server, pool = work_server(tmp_path, monkeypatch, [
        {'local_path': str(tmp_path/'dl'/'late.mp4'), 'status': 'succeeded'}])
    monkeypatch.setattr(server, '_admin_auth', lambda key: 'owner')
    server.store.update('video_t1', recover_state='', recover_until=None)

    async def check():
        first = await server.admin_work_task_recover('video_t1', None)
        second = await server.admin_work_task_recover('video_t1', None)
        assert first['recover_state'] == second['recover_state'] == 'watching'
        for _ in range(30):
            await asyncio.sleep(.01)
            if server.store.get('video_t1')['status'] == 'completed':
                break
        assert server.store.get('video_t1')['recover_state'] == 'recovered'
        await server.admin_work_task_recover('video_t1', None)

    _run(check())
    assert pool.calls == 1 and pool.claimed == [('acc1', 'seedance-2.0', 15)]
    assert not pool.continued and not pool.merged


@pytest.mark.parametrize('updates', [{'status': 'processing'}, {'conversation_id': ''},
                                   {'model': 'doubao-seedance-2.0-mini', 'duration': 5, 'execution_mode': 'auto'},
                                   {'duration': 30}])
def test_admin_recovery_rejects_jobs_that_would_require_submission(tmp_path, monkeypatch, updates):
    server, pool = work_server(tmp_path, monkeypatch, [])
    monkeypatch.setattr(server, '_admin_auth', lambda key: 'owner')
    server.store.update('video_t1', **updates)
    with pytest.raises(server.HTTPException) as caught:
        _run(server.admin_work_task_recover('video_t1', None))
    assert caught.value.status_code == 409 and pool.calls == 0 and not pool.continued


def test_account_test_persists_conversation_and_recovers_without_resubmission(tmp_path, monkeypatch):
    server, pool = work_server(tmp_path, monkeypatch, [
        {"local_path": str(tmp_path / "dl" / "late.mp4"), "status": "succeeded"}])
    monkeypatch.setattr(server, "TEST_JOBS", {"test_late": {}})
    monkeypatch.setattr(server, "_admin_auth", lambda key: "owner")
    submitted = []

    async def generate(account, *args, on_conversation_id, on_poll, **kwargs):
        submitted.append(account)
        on_poll(time.time(), progress="工作模式：发送请求")
        on_conversation_id(account, "original-conv", time.time() + 60)
        return {"ok": False, "account": account, "error": "生成超时", "recoverable": True}
    monkeypatch.setattr(pool, "test_generate", generate, raising=False)

    async def check():
        await server._run_test_generate("test_late", "acc1", "猫", "16:9", 15, "seedance-2.0")
        assert server.store.get("test_late")["conversation_id"] == "original-conv"
        for _ in range(30):
            await asyncio.sleep(.01)
            if server.store.get("test_late")["status"] == "completed":
                break
        job = await server.admin_test_gen_status("test_late", None)
        assert job["status"] == "done" and job["result"]["recovered"]
        assert not job["error"]
    _run(check())
    assert submitted == ["acc1"]
    history = json.loads(server.store.get("test_late")["attempt_history"])
    assert history[0]["conversation_id"] == "original-conv"
    assert history[0]["stages"][0]["stage"] == "工作模式：发送请求"


def test_account_test_pool_passes_original_conversation_and_marks_only_ambiguous_timeouts(tmp_path, monkeypatch):
    _, pool = _work_pool(tmp_path, monkeypatch, ["acc1"])
    from generation_errors import PreSubmissionParametersError
    conversations = []
    error = TimeoutError("发送后等待超时")

    async def generate(account, *args, **kwargs):
        kwargs["on_conversation_id"](account, "conv1", time.time() + 60)
        raise error
    monkeypatch.setattr(pool, "_generate_effective", generate)
    result = _run(pool.test_generate("acc1", "猫", "16:9", 15, "seedance-2.0",
                                   on_conversation_id=lambda *args: conversations.append(args)))
    assert result["recoverable"] and conversations[0][1] == "conv1"
    assert "acc1" in pool._work_suspect
    pool.clear_work_suspect("acc1")
    error = PreSubmissionParametersError("HTTP 403")
    result = _run(pool.test_generate("acc1", "猫", "16:9", 15, "seedance-2.0",
                                   on_conversation_id=lambda *args: None))
    assert not result.get("recoverable") and "acc1" not in pool._work_suspect


def test_final_work_failure_clears_suspect_and_survives_restart(tmp_path, monkeypatch):
    server, pool = work_server(tmp_path, monkeypatch, [
        {"local_path": "", "final": True, "status": "failed", "detail": "内容被拒绝，未生成"}])
    server.store.update("video_t1", error="等待超时，不换号重提")
    _run(server._recover_loop("video_t1"))
    assert pool.cleared == ["acc1"] and not pool.claimed
    assert server.store.get("video_t1")["recover_state"] == "gave_up"
    assert all("不换号重提" not in error for _, error in server.store.work_failures_since(0))


@pytest.mark.parametrize("segment", [1, 2])
def test_late_pair_segment_delivers_thirty_seconds(tmp_path, monkeypatch, segment):
    first = tmp_path / "first.mp4"
    first.write_bytes(b"first")
    state = {"pending": [pending(segment=segment)], "stem": str(tmp_path / "merged")}
    if segment == 2:
        state["first"] = {"account": "original", "local_path": str(first)}
    server, pool = work_server(tmp_path, monkeypatch, [
        {"local_path": str(tmp_path / "dl" / "late.mp4"), "status": "succeeded"}], pair=state)
    _run(server._recover_loop("video_t1"))
    row = server.store.get("video_t1")
    assert row["status"] == "completed" and row["video_duration"] == 30
    assert row["recover_state"] == "recovered" and row["video_url"].endswith("merged.mp4")
    assert pool.continued == (["acc1"] if segment == 1 else [])
    assert pool.merged == (["acc1"] if segment == 2 else [])


def test_delivered_pair_only_checks_late_accounts_and_keeps_original_delivery(tmp_path, monkeypatch):
    state = {"delivered": True, "pending": [pending("acc1"), pending("acc2")]}
    server, pool = work_server(tmp_path, monkeypatch, [
        {"local_path": str(tmp_path / "dl" / "late1.mp4")},
        {"local_path": str(tmp_path / "dl" / "late2.mp4")}], pair=state, status="completed")
    server.store.update("video_t1", video_url="https://example.com/original.mp4")
    _run(server._recover_loop("video_t1"))
    row = server.store.get("video_t1")
    assert row["status"] == "completed" and row["video_url"].endswith("original.mp4")
    assert row["recover_state"] == "checked" and len(pool.claimed) == 2
    assert not pool.merged and not pool.continued


def test_pair_recovery_keeps_earlier_conversation_after_last_presubmission_failure(tmp_path, monkeypatch):
    server, pool = work_server(tmp_path, monkeypatch, [], pair={"pending": [pending()]})
    server.store.update("video_t1", conversation_id=None, recover_state="")

    async def check():
        assert server._maybe_start_recovery("video_t1", RuntimeError("最后一个号上传失败"))
        assert server.store.get("video_t1")["conversation_id"] == "acc1-conv"

    _run(check())


def test_pair_restart_tracks_active_and_earlier_timed_out_conversations(tmp_path, monkeypatch):
    state = {"pending": [pending("acc1")], "active": pending("acc2")}
    server, pool = work_server(tmp_path, monkeypatch, [], pair=state)

    async def check():
        assert server._maybe_start_recovery("video_t1", TimeoutError("工作模式等待超时"))

    _run(check())
    saved = json.loads(server.store.get("video_t1")["work_pair_state"])
    assert {item["account"] for item in saved["pending"]} == {"acc1", "acc2"}


def test_running_service_persists_pair_context_and_recovers_earlier_attempt(tmp_path, monkeypatch):
    server, pool = work_server(tmp_path, monkeypatch, [])
    server.store.update("video_t1", duration=30, recover_state="")
    state = {"pending": [pending("acc1")], "first": {"account": "original", "local_path": "first.mp4"}}

    async def generate(*args, **kwargs):
        kwargs["on_account_try"]("acc1", 1)
        kwargs["on_conversation_id"]("acc1", "acc1-conv", time.time() + 60)
        kwargs["on_attempt_end"]("acc1", 1, time.time(), TimeoutError("工作模式等待超时"))
        kwargs["on_pair_state"](state)
        kwargs["on_account_try"]("acc2", 2)
        from generation_errors import PreSubmissionUploadError
        raise PreSubmissionUploadError("参考图上传失败")

    pool.generate_video = generate
    _run(server._run_task("video_t1", "seedance-2.0", "猫", "16:9", 30, [], {}))
    row = server.store.get("video_t1")
    assert row["status"] == "failed" and row["recover_state"] == "watching"
    assert row["conversation_id"] == "acc1-conv"
    assert json.loads(row["work_pair_state"])["pending"] == state["pending"]
    assert json.loads(row["attempt_history"])[0]["conversation_id"] == "acc1-conv"


def test_restart_reads_original_work_conversation_without_resubmitting(tmp_path, monkeypatch):
    server, pool = work_server(tmp_path, monkeypatch, [
        {"local_path": str(tmp_path / "dl" / "late.mp4"), "status": "succeeded"}])
    server.store.update("video_t1", status="queued", recover_state="")

    async def resumed():
        await server._resume_task(server.store.get("video_t1"))
        for _ in range(50):
            await asyncio.sleep(.01)
            if server.store.get("video_t1")["status"] == "completed":
                break

    _run(resumed())
    assert pool.calls == 1 and server.store.get("video_t1")["status"] == "completed"


def test_restart_before_second_submission_keeps_completed_first_in_service_entrypoint(tmp_path, monkeypatch):
    first = tmp_path / "first.mp4"
    first.write_bytes(b"first")
    state = {"first": {"account": "original", "local_path": str(first)}, "pending": [], "active": None}
    server, pool = work_server(tmp_path, monkeypatch, [], pair=state, status="queued")
    server.store.update("video_t1", recover_state="", reference_images="[]")
    received = []

    async def generate(*args, **kwargs):
        received.append(kwargs["pair_state"])
        output = tmp_path / "dl" / "complete.mp4"
        output.write_bytes(b"30 seconds")
        return {"local_path": str(output), "account": "second"}

    pool.generate_video = generate
    _run(server._resume_task(server.store.get("video_t1")))
    assert received == [state] and pool.calls == 0
    assert server.store.get("video_t1")["status"] == "completed"


def test_recovery_expiry_keeps_uncertain_quota_and_discards_pair_files(tmp_path, monkeypatch):
    state = {"pending": [pending()], "first": {"local_path": "first.mp4"}}
    server, pool = work_server(tmp_path, monkeypatch, [], pair=state)
    server.store.update("video_t1", recover_until=time.time() - 1)
    _run(server._recover_loop("video_t1"))
    assert server.store.get("video_t1")["recover_state"] == "gave_up"
    assert not pool.calls and not pool.claimed and not pool.cleared
    assert len(pool.discarded) == 1


def test_pool_recovery_respects_global_and_account_locks(tmp_path, monkeypatch):
    import sys
    _, pool = _work_pool(tmp_path, monkeypatch, ["acc1"])
    pool.set_max_concurrency(1)
    calls = []

    async def fetch(account, conv, **kwargs):
        calls.append(account)
        assert pool._activities[account] == "recovering"
        return {"local_path": "", "final": False}

    monkeypatch.setitem(sys.modules, "video_worker_agent", SimpleNamespace(fetch_work_conversation_video=fetch))

    async def check():
        lock = pool._locks.setdefault("acc1", asyncio.Lock())
        await lock.acquire()
        await pool.semaphore.acquire()
        task = asyncio.create_task(pool.fetch_work_conversation_video("acc1", "conv"))
        await asyncio.sleep(0)
        assert not calls
        pool.semaphore.release()
        await asyncio.sleep(0)
        assert not calls
        lock.release()
        await asyncio.wait_for(task, 1)
        assert not lock.locked() and pool.semaphore.active == 0

    _run(check())
    assert calls == ["acc1"]


def test_upload_transport_failure_stops_without_waiting_for_upload_deadline(tmp_path, monkeypatch):
    page = Page([])
    handlers = {}
    page.on = lambda event, callback: handlers.setdefault(event, callback)
    agent, item = session(monkeypatch, page)
    item.watch()
    path = tmp_path / "ref.png"
    path.write_bytes(b"image")

    async def picked(paths):
        handlers["requestfailed"](SimpleNamespace(
            url="https://imagex-ap-southeast-1.bytevcloudapi.com/upload?token=private",
            failure="net::ERR_CONNECTION_RESET"))
        return True

    monkeypatch.setattr(item, "_pick_files", picked)
    with pytest.raises(agent.PreSubmissionUploadError, match="ERR_CONNECTION_RESET"):
        _run(item.attach_references([str(path)]))
    assert not item.uploading and not item.sent


@pytest.mark.parametrize("host,active,failed", [
    ("video.vodupload.com", True, True), ("evilbytevcloudapi.com", True, False),
    ("bytevcloudapi.com.evil.test", True, False), ("video.vodupload.com", False, False)])
def test_upload_failure_is_scoped_to_real_upload_hosts(monkeypatch, host, active, failed):
    page, handlers = Page([]), {}
    page.on = lambda event, callback: handlers.setdefault(event, callback)
    _, item = session(monkeypatch, page)
    item.watch()
    item.uploading = active
    handlers["requestfailed"](SimpleNamespace(url="https://" + host + "/x", failure="failed"))
    assert bool(item.upload_error) is failed


@pytest.mark.parametrize("reply,expected", [("第二方案", "第二方案"), ("不存在的方案", "第一方案（推荐）"),
                                           (None, "第一方案（推荐）")])
def test_ai_chooses_only_existing_options_and_caches_answer(monkeypatch, reply, expected):
    _, item = session(monkeypatch, Page([]))
    import dola_responder
    calls = []
    monkeypatch.setattr(dola_responder, "enabled", lambda: True)

    async def answer(text, *args):
        calls.append(text)
        return reply, ""

    monkeypatch.setattr(dola_responder, "ai_reply_detail", answer)
    questions = [{"title": "方案", "options": ["第一方案（推荐）", "第二方案"]}]
    assert _run(item.choice_labels(questions)) == [expected]
    assert _run(item.choice_labels(questions)) == [expected]
    assert len(calls) == 1


def test_readonly_worker_fetch_never_sends_or_answers(monkeypatch):
    agent = load_agent(monkeypatch)
    calls = []

    @asynccontextmanager
    async def browser(account, on_poll=None, **kwargs):
        async def query():
            return "signed-query"

        async def read(query, conv):
            calls.append(conv)
            return video()

        yield SimpleNamespace(chain_query=query, read_chain=read, find_videos=agent.video_models,
                              check_references=lambda *args, **kwargs: None, thread_messages=lambda: [],
                              deliver_all=lambda found, conv: deliver(account, found[0], conv, on_poll))

    async def deliver(account, vm, conv, poll):
        return {"account": account, "conversation_id": conv, "local_path": "late.mp4"}

    monkeypatch.setattr(agent, "_work_browser", browser)
    monkeypatch.setattr(agent, "_deliver", deliver)
    result = _run(agent.fetch_work_conversation_video("acc1", "old-conv"))
    assert result["status"] == "succeeded" and calls == ["old-conv"]


def test_work_browser_cancellation_closes_profile_and_releases_browser_slot(tmp_path, monkeypatch):
    agent = load_agent(monkeypatch)
    monkeypatch.setattr(config, "WORK_15S_CONCURRENCY", 1)
    monkeypatch.setattr(agent, "_slots", None)
    profiles, closed = [], []

    @asynccontextmanager
    async def playwright():
        yield object()

    async def copied(*args):
        return []

    class Context:
        pages = [SimpleNamespace()]

        async def add_cookies(self, cookies):
            pass

        async def route(self, *args):
            pass

        async def close(self):
            closed.append(True)

    async def launched(account, profile):
        profiles.append(profile)
        return Context()

    async def opened(self):
        pass

    monkeypatch.setattr(agent, "async_playwright", playwright)
    monkeypatch.setattr(agent, "_copied_session", copied)
    monkeypatch.setattr(agent, "_launch_cloak", launched)
    monkeypatch.setattr(agent._WorkSession, "watch", lambda self: None)
    monkeypatch.setattr(agent._WorkSession, "open_chat", opened)

    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def first():
            async with agent._work_browser("acc1"):
                entered.set()
                await release.wait()

        async def second():
            async with agent._work_browser("acc2"):
                assert len(closed) == 1

        task = asyncio.create_task(first())
        await asyncio.wait_for(entered.wait(), 1)
        other = asyncio.create_task(second())
        await asyncio.sleep(0)
        assert len(profiles) == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(other, 1)

    _run(check())
    assert len(closed) == 2 and all(not Path(profile).exists() for profile in profiles)
