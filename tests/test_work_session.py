"""工作模式等待循环：确认只回一次、被拒绝立即停、回复结束却没片要报错（假页面）。"""
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_task_deadline import _run


class Keyboard:
    def __init__(self, page):
        self.page = page

    async def insert_text(self, text):
        self.page.typed.append(text)

    async def press(self, key):
        pass


class Target:
    def __init__(self, count=1):
        self._count, self.first, self.last = count, self, self

    async def count(self):
        return self._count

    async def click(self, **kwargs):
        pass


class Context:
    async def cookies(self, url):
        return [{"name": "msToken", "value": "t"}]


class Page:
    url = "https://www.dola.com/chat/123"

    def __init__(self, chains, paused=False, question="请选择", picker_rounds=1):
        self.chains, self.paused, self.typed = list(chains), paused, []
        self.question, self.picker_rounds = question, picker_rounds
        self.keyboard, self.context = Keyboard(self), Context()

    async def wait_for_timeout(self, ms):
        pass

    def get_by_text(self, pattern):
        return Target(1 if self.paused and len(self.typed) < self.picker_rounds else 0)

    def locator(self, selector):
        return Target()

    async def evaluate(self, script, args):
        if not isinstance(args, dict):
            return self.question
        chain = self.chains.pop(0) if len(self.chains) > 1 else self.chains[0]
        return {"status": 200, "text": json.dumps(chain)}


def bot(text, finished=True, index=2, tool=False):
    blocks = [{"content": {"text_block": {"text": text} if text else {}}}]
    if tool:
        blocks.append({"content": {"generic_tool_block": {"tool_name": "text_to_video"}}})
    return {"messages": [{"index_in_conv": index, "message_id": str(index), "user_type": 2,
                          "ext": {"is_finish": "1"} if finished else {}, "content_block": blocks}]}


def video():
    return {"video_model": json.dumps({"video_duration": 15.1})}


def load_agent(monkeypatch):
    """Other test modules stub these at import time; load the file against our own stubs."""
    class RiskControlError(Exception):
        pass

    class LoginExpiredError(Exception):
        pass

    async def logged_in(_page):
        return False
    noop = lambda *a, **k: None
    stubs = {
        "browser": SimpleNamespace(cookie_value=lambda cookies, name: "t", launch_account_context=noop),
        "video_worker": SimpleNamespace(RiskControlError=RiskControlError, _download=noop),
        "video_worker_ui": SimpleNamespace(
            LoginExpiredError=LoginExpiredError, dismiss_popups=noop, fetch_higher_bitrate_url=noop,
            is_logged_out=logged_in, raise_if_region_restricted=noop),
    }
    for name, stub in stubs.items():
        monkeypatch.setitem(sys.modules, name, stub)
    path = Path(__file__).resolve().parent.parent / "video_worker_agent.py"
    spec = importlib.util.spec_from_file_location("video_worker_agent_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def session(monkeypatch, page, polls=4):
    agent = load_agent(monkeypatch)
    monkeypatch.setattr(agent, "SETTLED_POLLS", polls)
    item = agent._WorkSession("acc1", page, None, ratio="16:9", attachments=1)
    item.page_query = "https://www.dola.com/x?device_id=1&aid=2&a_bogus=z"
    return agent, item


def wait(item):
    return _run(item.wait_for_video("123", time.time() + 60))


def test_picker_pause_is_confirmed_once_then_video_comes_back(monkeypatch):
    # acc148：选择题不在 chain 里，靠「一時停止中」判断，题目从输入框附近读
    page = Page([bot("", False), bot("", False), video()], paused=True, question="请确认后开始")
    _, item = session(monkeypatch, page)
    assert json.loads(wait(item))["video_duration"] == 15.1
    assert page.typed == ["确认"]


def test_plain_reply_is_confirmed_once(monkeypatch):
    page = Page([bot("请确认")] * 4 + [bot("", False, index=4)] * 2 + [video()])
    _, item = session(monkeypatch, page)
    wait(item)
    assert page.typed == ["确认"]


def test_thinking_or_generating_is_never_answered(monkeypatch):
    # 生成途中文字已写出、工具也在跑，但消息没写完：不是在等人回答
    page = Page([bot("", False)] * 3 + [bot("我开始生成", False, tool=True)] * 8 + [video()])
    _, item = session(monkeypatch, page)
    wait(item)
    assert page.typed == []


def test_unknown_question_gets_generic_replies_then_ai(monkeypatch):
    page = Page([bot("你想要什么风格？")] * 50)
    agent, item = session(monkeypatch, page)
    asked = []

    async def ai(question, ratio, duration, attachments):
        asked.append((question, ratio, duration, attachments))
        return "按提示词直接生成"
    monkeypatch.setattr(agent.dola_responder, "enabled", lambda: True)
    monkeypatch.setattr(agent.dola_responder, "ai_reply", ai)
    with pytest.raises(agent.UpstreamReplyError) as caught:
        wait(item)
    assert len(page.typed) == 3
    assert page.typed[-1] == "按提示词直接生成"
    assert asked == [("你想要什么风格？", "16:9", 15, 1)]
    assert caught.value.category == "上游在等待回复"
    assert caught.value.generation_started is False


def test_unknown_picker_also_reaches_ai(monkeypatch):
    page = Page([bot("", False)] * 40, paused=True, question="选哪种风格", picker_rounds=9)
    agent, item = session(monkeypatch, page)

    async def ai(question, ratio, duration, attachments):
        return "选第一个"
    monkeypatch.setattr(agent.dola_responder, "enabled", lambda: True)
    monkeypatch.setattr(agent.dola_responder, "ai_reply", ai)
    with pytest.raises(agent.UpstreamReplyError):
        wait(item)
    assert page.typed[-1] == "选第一个"


def test_ai_refusal_ends_the_task_instead_of_guessing(monkeypatch):
    page = Page([bot("你想要什么风格？")] * 50)
    agent, item = session(monkeypatch, page)

    async def ai(*args):
        return None
    monkeypatch.setattr(agent.dola_responder, "enabled", lambda: True)
    monkeypatch.setattr(agent.dola_responder, "ai_reply", ai)
    with pytest.raises(agent.UpstreamReplyError, match="上游在等待回复"):
        wait(item)
    assert len(page.typed) == 2


def test_blocked_logout_counts_as_rejection_and_keeps_710022002(monkeypatch):
    page = Page([bot("", False)])
    agent, item = session(monkeypatch, page)
    item.logouts.append("/passport/web/logout/")
    with pytest.raises(agent.RiskControlError, match="710022002"):
        wait(item)
    assert page.typed == []


def test_finished_tool_run_without_video_does_not_resubmit(monkeypatch):
    page = Page([bot("生成失败，内容不合规", tool=True)] * 5)
    agent, item = session(monkeypatch, page, polls=2)
    with pytest.raises(agent.UpstreamReplyError) as caught:
        wait(item)
    assert caught.value.generation_started is True
    assert page.typed == []


def test_deadline_reports_a_timeout_without_resubmitting(monkeypatch):
    page = Page([bot("", False)])
    _, item = session(monkeypatch, page)
    with pytest.raises(TimeoutError, match="不换号"):
        _run(item.wait_for_video("123", time.time() - 1))
