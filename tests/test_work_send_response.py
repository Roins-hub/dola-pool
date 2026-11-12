"""提交响应明确拒绝时立即结束；结果不明时保留原请求。"""
from types import SimpleNamespace

import pytest

from test_task_deadline import _run
from test_work_session import load_agent, send_session, PROMPT, PlaywrightLike


class Response:
    url = "https://www.dola.com/chat/completion?token=private"

    def __init__(self, status=200, data=None, content_type="application/json"):
        self.status, self.data = status, data
        self.headers = {"content-type": content_type}
        self.reads = 0

    async def json(self):
        self.reads += 1
        return self.data


@pytest.mark.parametrize("status,error_name", [(401, "LoginExpiredError"), (403, "PreSubmissionParametersError"),
                                               (429, "RiskControlError"), (422, "PreSubmissionParametersError")])
def test_http_rejection_ends_send_without_waiting_or_retrying(monkeypatch, status, error_name):
    agent, item, page = send_session(monkeypatch)

    async def press(key):
        page.enters += 1
        item.sent.append("completion")
        await item.read_send_response(Response(status, content_type="text/html"))
    page.press = press
    with pytest.raises(getattr(agent, error_name), match=f"HTTP {status}"):
        _run(item.send(PROMPT))
    assert page.enters == 1 and page.clicks == 0


def test_server_error_keeps_ambiguous_result_and_does_not_read_body(monkeypatch):
    agent, item, _ = send_session(monkeypatch)
    response = Response(503, {"code": 710022002})
    _run(item.read_send_response(response))
    with pytest.raises(TimeoutError, match="不换号重提") as caught:
        item.raise_if_send_failed()
    assert not isinstance(caught.value, agent.PreSubmissionParametersError)
    assert response.reads == 0


@pytest.mark.parametrize("code", [710022002, "710022002"])
def test_json_throttle_is_reported_with_redacted_detail(monkeypatch, code):
    agent, item, _ = send_session(monkeypatch)
    _run(item.read_send_response(Response(data={"code": code,
        "msg": "token=private cookie=private password=private https://example.com/private"})))
    with pytest.raises(agent.RiskControlError, match="710022002") as caught:
        item.raise_if_send_failed()
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("code", [None, 0, "0", 2000, "2000", {}, True, "token=private"])
def test_success_and_unknown_codes_do_not_create_errors(monkeypatch, code):
    _, item, _ = send_session(monkeypatch)
    _run(item.read_send_response(Response(data={"code": code})))
    item.raise_if_send_failed()


def test_streaming_and_unrelated_responses_are_not_consumed(monkeypatch):
    _, item, _ = send_session(monkeypatch)
    stream = Response(content_type="text/event-stream")
    unrelated = Response(401)
    unrelated.url = "https://www.dola.com/im/chain/single"
    _run(item.read_send_response(stream))
    _run(item.read_send_response(unrelated))
    assert stream.reads == unrelated.reads == 0
    item.raise_if_send_failed()


def test_response_listener_receives_real_completion_events(monkeypatch):
    agent = load_agent(monkeypatch)
    handlers = {}
    item = agent._WorkSession("acc1", SimpleNamespace(on=lambda event, fn: handlers.update({event: fn})), None)
    item.watch()
    _run(handlers["response"](Response(data={"code": 710022002, "msg": "请求过于频繁"})))
    with pytest.raises(agent.RiskControlError):
        item.raise_if_send_failed()


def test_late_send_during_composer_rebuild_keeps_original_conversation(monkeypatch):
    _, item, page = send_session(monkeypatch, enter_sends=False, button_sends=False)
    rebuilt = []

    async def restore():
        rebuilt.append(True)
        if len(rebuilt) == 2:
            page.go_out()
            raise PlaywrightLike('Locator.click: Timeout 8000ms exceeded.')
        page.text = 'Creative video '
    item.restore_composer = restore
    assert _run(item.send(PROMPT)) == '123'
    assert page.enters == 1 and len(item.sent) == 1


def test_delayed_request_is_checked_before_rebuilding_composer(monkeypatch):
    _, item, page = send_session(monkeypatch, enter_sends=False, button_sends=False)
    waits = []

    async def waited(ms):
        waits.append(ms)
        if ms == 3000:
            page.go_out()
    page.wait_for_timeout = waited
    assert _run(item.send(PROMPT)) == '123'
    assert page.enters == 1 and len(item.sent) == 1


def test_recent_lookup_uses_time_before_the_first_send(monkeypatch):
    agent, item, page = send_session(monkeypatch, jumps=True)
    monkeypatch.setattr(agent, 'CONVERSATION_WAIT_SECONDS', 1)
    clock = [100.0]
    monkeypatch.setattr(agent, 'time', SimpleNamespace(time=lambda: clock[0]))
    original = page.press

    async def press(key):
        await original(key)
        clock[0] = 130.0

    async def lookup(since):
        assert since == 100.0
        return True, '123'
    page.press, item.find_conversation = press, lookup
    assert _run(item.send(PROMPT)) == '123'


def test_late_send_while_filling_does_not_press_enter_again(monkeypatch):
    _, item, page = send_session(monkeypatch)

    async def fill(text):
        page.go_out()
    item.fill_prompt = fill
    assert _run(item.send(PROMPT)) == '123'
    assert page.enters == 0 and len(item.sent) == 1


def test_doubao_button_click_without_request_is_not_mistaken_for_submission(monkeypatch):
    agent, item, page = send_session(monkeypatch, enter_sends=False, button_sends=False)
    item.site = agent.sites.DOUBAO
    page.text = 'Creative video ' + PROMPT

    async def no_new_conversation(_since):
        return True, None

    item.find_conversation = no_new_conversation
    with pytest.raises(agent.PreSubmissionParametersError, match='尚未提交生成'):
        _run(item.send(PROMPT))
    assert page.clicks == 1 and not item.sent and not item.went_out()
