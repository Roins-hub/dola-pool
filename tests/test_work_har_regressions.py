"""脱敏 HAR 回归：后台线程出片、业务额度拒绝与工具卡片的完成文案。"""
import json
import time
from contextlib import asynccontextmanager

import pytest

import work_reply
from test_task_deadline import _run
from test_work_session import Page, bot, load_agent
from test_work_session import send_session, PROMPT


QUOTA_REPLY = "近 7 天免费额度已用完，预计11月19日 23:09 恢复。开通订阅畅享更多额度。"


def creation(status, *, reason="", text="", finished=False):
    chain = bot(text, finished=finished, tool=True)
    message = chain["messages"][0]
    message["content_block"][-1]["content"]["generic_tool_block"]["title"] = "已生成视频"
    record = {"task_id": "task", "tool_name": "text_to_video", "file_type": "video", "status": status}
    if reason:
        record["failure_reason"] = reason
    message["ext"]["ai_creation_tool_list"] = json.dumps([record])
    return chain


def delegated():
    chain = bot("已开始后台生成，完成后交付视频。", finished=True)
    chain["messages"][0]["content_block"].append({"content": {
        "complex_task_block": {"thread_id": "thread", "status": 4}}})
    return chain


def item(monkeypatch, page):
    agent = load_agent(monkeypatch)
    session = agent._WorkSession("account", page, None, model="doubao-seedance-2.0-fast", duration=15)
    session.page_query = "https://www.doubao.com/x?aid=1&device_id=2"
    return agent, session


class ThreadPage(Page):
    def __init__(self, responses):
        super().__init__([delegated()])
        self.thread_responses = list(responses)
        self.thread_reads = []

    async def evaluate(self, script, args):
        if isinstance(args, dict) and "threadId" in args:
            self.thread_reads.append(args["threadId"])
            response = self.thread_responses.pop(0) if len(self.thread_responses) > 1 else self.thread_responses[0]
            return response
        return await super().evaluate(script, args)


def test_cta_limit_wins_over_completed_tool_card_and_unfinished_message(monkeypatch):
    from doubao_gen import DoubaoQuotaError, quota_recover_at

    page = Page([creation(5, reason="cta_limit", text=QUOTA_REPLY)])
    _, session = item(monkeypatch, page)
    with pytest.raises(DoubaoQuotaError, match="11月19日 23:09") as caught:
        _run(session.wait_for_video("123", time.time() + 10))
    assert quota_recover_at(str(caught.value)) is not None
    assert not page.typed and not session.found_videos


def test_metadata_only_quota_refusal_does_not_wait_for_text(monkeypatch):
    from doubao_gen import DoubaoQuotaError

    _, session = item(monkeypatch, Page([]))
    failure = session.creation_failure(creation(5, reason="cta_limit")["messages"])
    assert isinstance(failure, DoubaoQuotaError) and "cta_limit" in str(failure)


def test_previous_failure_does_not_override_a_newer_video_attempt(monkeypatch):
    _, session = item(monkeypatch, Page([]))
    messages = creation(5, reason="cta_limit")["messages"] + creation(4)["messages"]
    assert session.creation_failure(messages) is None


def test_backend_thread_video_is_delivered_while_parent_still_says_running(monkeypatch):
    model = json.dumps({"vid": "thread-video", "video_duration": 15.1})
    page = ThreadPage([{"status": 200, "text": json.dumps({"status_code": 0, "video_model": model})}])
    _, session = item(monkeypatch, page)
    assert _run(session.wait_for_video("123", time.time() + 10)) == model
    assert page.thread_reads == ["thread"] and not page.typed


def test_backend_thread_quota_refusal_ends_a_running_parent(monkeypatch):
    from doubao_gen import DoubaoQuotaError

    page = ThreadPage([{"status": 200, "text": json.dumps(creation(5, reason="cta_limit", text=QUOTA_REPLY))}])
    _, session = item(monkeypatch, page)
    with pytest.raises(DoubaoQuotaError, match="近 7 天"):
        _run(session.wait_for_video("123", time.time() + 10))
    assert len(page.thread_reads) == 1 and not page.typed


def test_temporary_thread_read_failure_keeps_waiting_without_a_new_submission(monkeypatch):
    model = json.dumps({"vid": "late-video", "video_duration": 15.1})
    page = ThreadPage([{"status": 0, "text": "network error"},
                       {"status": 200, "text": json.dumps({"video_model": model})}])
    _, session = item(monkeypatch, page)
    assert _run(session.wait_for_video("123", time.time() + 10)) == model
    assert len(page.thread_reads) == 2 and not page.typed


def test_readonly_recovery_observes_thread_quota_failure_without_reply(monkeypatch):
    from doubao_gen import DoubaoQuotaError

    page = ThreadPage([{"status": 200, "text": json.dumps(creation(5, reason="cta_limit", text=QUOTA_REPLY))}])
    agent, session = item(monkeypatch, page)

    @asynccontextmanager
    async def browser(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(agent, "_work_browser", browser)
    result = _run(agent.fetch_work_conversation_video("account", "123", model=session.model))
    assert result["status"] == "failed" and result["final"] and result["doubao_quota_blocked"]
    assert QUOTA_REPLY in result["detail"] and not page.typed


@pytest.mark.parametrize("records", ["not-json", "{}", "null", 123, [{"tool_name": "image_generate", "file_type": "image", "status": 5}]])
def test_invalid_or_nonvideo_creation_metadata_is_ignored(records):
    assert work_reply.video_creation_records({"ext": {"ai_creation_tool_list": records}}) == []


def preparing_session(monkeypatch, sent_after=None):
    agent, session, page = send_session(monkeypatch, enter_sends=False, button_sends=False)
    session.site = agent.sites.DOUBAO
    original = page.locator
    waited = []

    def locator(selector):
        target = original(selector)
        click = target.click

        async def clicked(**kwargs):
            await click(**kwargs)
            if 'send_button' in selector:
                page.disabled = True
        target.click = clicked
        return target

    async def pause(ms):
        if session.submit_clicked and ms == 1000:
            waited.append(ms)
            if sent_after is not None and len(waited) == sent_after:
                page.go_out()

    async def no_conversation(_since):
        return True, None

    page.locator, page.wait_for_timeout = locator, pause
    session.find_conversation = no_conversation
    return agent, session, page, waited


def test_delayed_submit_preparation_waits_for_the_original_click(monkeypatch):
    _, session, page, waits = preparing_session(monkeypatch, sent_after=30)
    assert _run(session.send(PROMPT)) == '123'
    assert len(waits) == 30 and page.clicks == 1 and page.enters == 0


def test_unfinished_submit_preparation_keeps_the_outcome_ambiguous(monkeypatch):
    _, session, page, waits = preparing_session(monkeypatch)
    with pytest.raises(TimeoutError, match='仍在准备提交'):
        _run(session.send(PROMPT))
    assert page.clicks == 1 and page.enters == 0 and not session.sent
