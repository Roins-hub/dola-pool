"""工作模式等待循环：确认只回一次、被拒绝立即停、回复结束却没片要报错（假页面）。"""
import asyncio
import importlib.util
import json
import re
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
        if args == "123":            # PAUSED_JS：当前会话那一行有没有暂停标记
            return self.paused and len(self.typed) < self.picker_rounds
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

    class AccountLimitedError(Exception):
        pass

    class CreditInsufficientError(Exception):
        pass


    async def logged_in(_page):
        return False
    noop = lambda *a, **k: None
    stubs = {
        "browser": SimpleNamespace(cookie_value=lambda cookies, name: "t", launch_account_context=noop,
                                   cloak_proxy_settings=lambda url: {"server": url, "bypass": "*.upload"}),
        "video_worker": SimpleNamespace(RiskControlError=RiskControlError, _download=noop),
        "video_worker_ui": SimpleNamespace(
            LoginExpiredError=LoginExpiredError, dismiss_popups=noop, fetch_higher_bitrate_url=noop,
            is_logged_out=logged_in, raise_if_region_restricted=noop, find_captcha_frame=noop,
            solve_slider=noop, AccountLimitedError=AccountLimitedError,
            CreditInsufficientError=CreditInsufficientError,
            DAILY_LIMIT_PATTERN=re.compile("每日生成上限|動画生成の 1日あたりの上限")),
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


def test_old_conversation_paused_in_the_sidebar_is_ignored(monkeypatch):
    # 只看当前会话：别的会话挂着「一時停止中」时不能在生成途中乱回确认
    page = Page([bot("", False), bot("", False), bot("", False), bot("", False), bot("", False), video()])
    _, item = session(monkeypatch, page)
    wait(item)
    assert page.typed == []


def test_paused_check_is_scoped_to_this_conversation(monkeypatch):
    agent = load_agent(monkeypatch)
    assert "a[href*=" in agent.PAUSED_JS and "conversationId" in agent.PAUSED_JS


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
        return "按提示词直接生成", ""
    monkeypatch.setattr(agent.dola_responder, "enabled", lambda: True)
    monkeypatch.setattr(agent.dola_responder, "ai_reply_detail", ai)
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
        return "选第一个", ""
    monkeypatch.setattr(agent.dola_responder, "enabled", lambda: True)
    monkeypatch.setattr(agent.dola_responder, "ai_reply_detail", ai)
    with pytest.raises(TimeoutError):      # 答完还在暂停：不判失败，等到时限
        _run(item.wait_for_video("123", time.time() + 0.3))
    assert page.typed[-1] == "选第一个"


def test_ai_refusal_ends_the_task_instead_of_guessing(monkeypatch):
    page = Page([bot("你想要什么风格？")] * 50)
    agent, item = session(monkeypatch, page)

    async def ai(*args):
        return None, "模型的回复不符合规则（放弃）"
    monkeypatch.setattr(agent.dola_responder, "enabled", lambda: True)
    monkeypatch.setattr(agent.dola_responder, "ai_reply_detail", ai)
    with pytest.raises(agent.UpstreamReplyError, match="AI 代答：模型的回复不符合规则") as caught:
        wait(item)
    assert "上游在等待回复" in str(caught.value)
    assert len(page.typed) == 2


def test_seven_day_limit_switches_accounts_without_nudging(monkeypatch):
    # acc147：这句被当成追问，回了两次通用回复、又请 AI 代答，最后没换号
    limit = ("7日間の無料利用分の上限に到達しました。2026年11月14日 10:57（GMT+08:00）に再開するか、"
             "チャットモードで私とチャットしてください。")
    page = Page([bot(limit)] * 8)
    agent, item = session(monkeypatch, page)
    with pytest.raises(agent.WorkQuotaError) as caught:
        wait(item)
    assert caught.value.until and page.typed == []


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


def test_waiting_for_a_slot_keeps_reporting_progress(monkeypatch):
    # 第二单排队时没有进度，前一单超过 10 分钟就会被看门狗判卡死、重派
    agent = load_agent(monkeypatch)
    monkeypatch.setattr(agent.config, "WORK_15S_CONCURRENCY", 1)
    monkeypatch.setattr(agent, "SLOT_NOTE_SECONDS", 0.01)
    notes = []

    async def scenario():
        async with agent._work_slot(None):
            waiter = asyncio.ensure_future(hold(agent, notes))
            await asyncio.sleep(0.05)
            assert not waiter.done()
        await waiter
    _run(scenario())
    assert notes and notes[-1] == "got slot"
    assert any("排队中" in note for note in notes)


async def hold(agent, notes):
    async with agent._work_slot(lambda now, progress=None, **kw: notes.append(progress)):
        notes.append("got slot")


def test_changing_concurrency_applies_to_new_jobs(monkeypatch):
    agent = load_agent(monkeypatch)
    monkeypatch.setattr(agent.config, "WORK_15S_CONCURRENCY", 2)
    first = agent._work_slots()
    assert agent._work_slots() is first
    monkeypatch.setattr(agent.config, "WORK_15S_CONCURRENCY", 3)
    assert agent._work_slots() is not first


def test_queue_note_appears_at_once_when_every_slot_is_busy(monkeypatch):
    agent = load_agent(monkeypatch)
    monkeypatch.setattr(agent.config, "WORK_15S_CONCURRENCY", 1)
    monkeypatch.setattr(agent, "SLOT_NOTE_SECONDS", 10)
    notes = []

    async def scenario():
        async with agent._work_slot(None):
            waiter = asyncio.ensure_future(hold(agent, notes))
            await asyncio.sleep(0.01)
            assert notes and "排队中" in notes[0]
        await waiter
    _run(scenario())


class SkillPage(Page):
    """菜单只有输入名字筛选后才露出 Creative video。"""
    def __init__(self):
        super().__init__([])
        self.keys = ""

    def get_by_text(self, pattern):
        page, target = self, Target()

        async def count():                # 真实 locator 每次重新查询
            return 1 if "Creative" in page.keys else 0
        target.count = count
        return target

    @property
    def keyboard(self):
        return self

    @keyboard.setter
    def keyboard(self, value):
        pass

    async def type(self, text, delay=0):
        self.keys += text

    async def press(self, key):
        pass


def test_skill_is_found_by_filtering_the_menu(monkeypatch):
    page = SkillPage()
    _, item = session(monkeypatch, page)
    _run(item.choose_skill())
    assert page.keys == "/Creative"


class SendPage(Page):
    """可配置：写入丢失、回车不发送、回车把输入框清空、按钮禁用。"""
    url = "https://www.dola.com/chat"

    def __init__(self, lost_writes=0, enter_sends=True, button_sends=True, wipes=0, disabled=False, jumps=False):
        super().__init__([])
        self.text, self.lost, self.enter_sends, self.button_sends = "", lost_writes, enter_sends, button_sends
        self.wipes, self.disabled, self.jumps = wipes, disabled, jumps
        self.session, self.clicks, self.enters, self.restores = None, 0, 0, 0
        self.keyboard = self

    async def insert_text(self, text):
        if self.lost > 0:
            self.lost -= 1
        else:
            self.text = "Creative video " + text

    def go_out(self):
        self.url = "https://www.dola.com/chat/123"
        self.session.sent.append("completion")

    async def press(self, key):
        if key != "Enter":
            return
        self.enters += 1
        if self.wipes > 0:           # 重绘：内容没了，请求也没发
            self.wipes -= 1
            self.text = ""
        elif self.jumps:             # 看不到请求，但页面已进新会话
            self.url = "https://www.dola.com/chat/local_123"
        elif self.enter_sends:
            self.go_out()

    async def evaluate(self, script, args):
        return self.text

    def locator(self, selector):
        target, page = Target(), self

        async def click(**kwargs):
            if "send_button" in selector:
                if page.disabled:
                    raise AssertionError("点了禁用的按钮")
                page.clicks += 1
                if page.button_sends:
                    page.go_out()

        async def is_enabled():
            return not page.disabled
        target.click, target.is_enabled = click, is_enabled
        return target


def send_session(monkeypatch, **options):
    page = SendPage(**options)
    agent, item = session(monkeypatch, page)
    page.session = item

    async def restored():
        page.restores += 1
        page.text = "Creative video "
    item.enter_work_mode, item.choose_skill = (lambda: asyncio.sleep(0)), restored
    return agent, item, page


PROMPT = "一只橘猫在沙滩上慢慢走，15s"


def test_prompt_is_retyped_when_the_composer_loses_it(monkeypatch):
    _, item, page = send_session(monkeypatch, lost_writes=2)
    _run(item.fill_prompt(PROMPT))
    assert page.text.endswith(PROMPT)


def test_prompt_is_not_written_twice(monkeypatch):
    _, item, page = send_session(monkeypatch)
    page.text = "Creative video " + PROMPT
    _run(item.fill_prompt(PROMPT))
    assert page.text.count(PROMPT) == 1


def test_prompt_that_never_lands_is_reported_before_sending(monkeypatch):
    agent, item, _ = send_session(monkeypatch, lost_writes=9)
    with pytest.raises(agent.PreSubmissionParametersError, match="没有写进输入框"):
        _run(item.fill_prompt(PROMPT))


def test_send_button_is_used_when_enter_does_nothing(monkeypatch):
    # acc22：回车后没有请求，内容还在
    _, item, page = send_session(monkeypatch, enter_sends=False)
    page.text = "Creative video " + PROMPT
    assert _run(item.press_send()) is True
    assert page.clicks == 1


def test_disabled_button_is_never_clicked(monkeypatch):
    # acc46：按钮灰着还去点，报 Locator.click Timeout
    _, item, page = send_session(monkeypatch, enter_sends=False, disabled=True)
    assert _run(item.press_send()) is False
    assert page.clicks == 0


def test_wiped_composer_is_rebuilt_and_sent_once(monkeypatch):
    # 页面重绘清空了输入框：重新选技能、重写提示词，只发一次
    _, item, page = send_session(monkeypatch, wipes=1, disabled=True)
    page.text = "Creative video "
    assert _run(item.send(PROMPT)) == "123"
    assert (page.enters, page.restores, len(item.sent)) == (2, 1, 1)


def test_jump_to_a_new_conversation_counts_as_sent(monkeypatch):
    # 没看到请求但页面已进新会话：再发一次就重复出片了
    _, item, page = send_session(monkeypatch, jumps=True)
    page.text = "Creative video "
    assert _run(item.press_send()) is True
    assert (page.enters, page.clicks) == (1, 0)


def test_failed_request_is_reported_as_not_sent(monkeypatch):
    # acc22：请求卡在代理上，Dola 没有新会话，可以放心换号
    agent, item, page = send_session(monkeypatch, jumps=True)
    page.text = "Creative video "
    item.send_error = "net::ERR_CONNECTION_RESET"
    with pytest.raises(agent.PreSubmissionParametersError, match="ERR_CONNECTION_RESET"):
        _run(item.send(PROMPT))


def waiting_session(monkeypatch, found):
    agent, item, page = send_session(monkeypatch, jumps=True)
    monkeypatch.setattr(agent, "CONVERSATION_WAIT_SECONDS", 2)
    page.text = "Creative video "

    async def lookup(since):
        return found
    item.find_conversation = lookup
    return agent, item


def test_unknown_outcome_after_sending_never_resubmits(monkeypatch):
    agent, item = waiting_session(monkeypatch, (False, None))
    with pytest.raises(TimeoutError, match="不换号") as caught:
        _run(item.send(PROMPT))
    assert not isinstance(caught.value, agent.PreSubmissionParametersError)


def test_no_new_conversation_means_it_is_safe_to_switch_accounts(monkeypatch):
    agent, item = waiting_session(monkeypatch, (True, None))
    with pytest.raises(agent.PreSubmissionParametersError, match="没有新会话"):
        _run(item.send(PROMPT))


def test_conversation_found_in_the_list_is_followed(monkeypatch):
    _, item = waiting_session(monkeypatch, (True, "55"))
    assert _run(item.send(PROMPT)) == "55"


def test_old_conversation_url_is_not_mistaken_for_a_send(monkeypatch):
    # 页面本来就停在某个会话上：地址没变不算发出去
    _, item, page = send_session(monkeypatch, enter_sends=False, disabled=True)
    page.url = "https://www.dola.com/chat/999"
    assert _run(item.press_send()) is False


class PlaywrightLike(Exception):
    pass


PlaywrightLike.__module__ = "playwright._impl._errors"


def test_browser_errors_are_split_by_whether_the_request_went_out(monkeypatch):
    agent = load_agent(monkeypatch)
    error = PlaywrightLike("Locator.click: Timeout 4000ms exceeded.\nCall log: https://www.dola.com/x")
    assert agent.browser_error(error) and not agent.browser_error(ValueError("x"))
    before = agent.translate_browser_error(error, "acc1", False)
    after = agent.translate_browser_error(error, "acc1", True)
    assert isinstance(before, agent.PreSubmissionParametersError) and "Call log" not in str(before)
    assert isinstance(after, TimeoutError) and not isinstance(after, agent.PreSubmissionParametersError)
    assert "不换号" in str(after)


@pytest.mark.parametrize("reply,kind", [
    ("今日の動画生成の 1日あたりの上限に達しました", "AccountLimitedError"),
    ("今天的视频生成次数已用完", "AccountLimitedError"),
    ("クレジットが不足しています", "CreditInsufficientError"),
])
def test_quota_replies_switch_accounts(monkeypatch, reply, kind):
    agent = load_agent(monkeypatch)
    assert type(agent.failure_from_reply("acc1", reply, ran_tools=True)).__name__ == kind


def test_portrait_refusal_can_be_retried_but_other_refusals_cannot(monkeypatch):
    agent = load_agent(monkeypatch)
    monkeypatch.setattr(agent.failure_text, "label_for", lambda text: "肖像保护" if "肖像" in text else "内容违规")
    assert agent.failure_from_reply("acc1", "肖像", ran_tools=True).generation_started is False
    assert agent.failure_from_reply("acc1", "违规", ran_tools=True).generation_started is True
    assert agent.failure_from_reply("acc1", "违规", ran_tools=False).generation_started is False


def test_quota_reply_stops_without_nudging(monkeypatch):
    page = Page([bot("クレジットが不足しています")] * 8)
    agent, item = session(monkeypatch, page)
    with pytest.raises(agent.CreditInsufficientError):
        wait(item)
    assert page.typed == []


def test_slider_is_solved_while_waiting(monkeypatch):
    page = Page([bot("", False)])
    agent, item = session(monkeypatch, page)
    frames, solved = ["frame"], []
    monkeypatch.setattr(agent, "find_captcha_frame", lambda p: frames[0] if frames else None)

    async def solve(p, frame, attempt, account):
        solved.append(attempt)
        frames.clear()
        return True
    monkeypatch.setattr(agent, "solve_slider", solve)
    _run(item.solve_captcha())
    assert solved == [1]


def test_slider_that_never_passes_is_a_risk_error(monkeypatch):
    page = Page([bot("", False)])
    agent, item = session(monkeypatch, page)
    monkeypatch.setattr(agent, "find_captcha_frame", lambda p: "frame")

    async def solve(p, frame, attempt, account):
        return False
    monkeypatch.setattr(agent, "solve_slider", solve)
    with pytest.raises(agent.RiskControlError, match="滑块"):
        _run(item.solve_captcha())


def test_address_lookup_is_retried_after_the_video_exists(monkeypatch):
    agent = load_agent(monkeypatch)
    calls = []

    async def lookup(vm, account):
        calls.append(1)
        return "https://cdn/x.mp4" if len(calls) == 3 else None

    async def download(url, account):
        return "/tmp/x.mp4"

    async def no_wait(seconds):
        pass
    monkeypatch.setattr(agent, "fetch_higher_bitrate_url", lookup)
    monkeypatch.setattr(agent, "_download", download)
    monkeypatch.setattr(agent.asyncio, "sleep", no_wait)
    monkeypatch.setitem(sys.modules, "video_link", SimpleNamespace(enabled=lambda: False))
    assert _run(agent._deliver("acc1", "{}", "1", None))["local_path"] == "/tmp/x.mp4"
    assert len(calls) == 3


def test_proxy_drop_during_send_reports_the_network_not_the_page(monkeypatch):
    # acc57：代理在发送那一刻断了，旧逻辑去找工作模式入口，报「页面上没有工作模式入口」
    agent, item, page = send_session(monkeypatch, enter_sends=False, disabled=True)
    page.text = "Creative video "

    async def press(key):
        item.send_error = "net::ERR_TUNNEL_CONNECTION_FAILED"
    page.press = press
    with pytest.raises(agent.PreSubmissionParametersError, match="请求没连上：net::ERR_TUNNEL"):
        _run(item.send(PROMPT))


def test_restore_without_the_work_tab_just_picks_the_skill_again(monkeypatch):
    agent, item, page = send_session(monkeypatch)
    page.text = ""
    entered = []

    async def enter():
        entered.append(1)
    item.enter_work_mode = enter
    _run(item.restore_composer())                  # 假页面的 get_by_text 找不到入口
    assert entered == [] and page.restores == 1


def test_giving_up_says_what_was_left_in_the_composer(monkeypatch):
    agent, item, page = send_session(monkeypatch, enter_sends=False, button_sends=False)
    page.text = "Creative video "
    with pytest.raises(agent.PreSubmissionParametersError, match="输入框内容：Creative video"):
        _run(item.send(PROMPT))
    assert page.enters == 3


def test_cloak_goes_through_the_local_relay_for_socks_auth(monkeypatch):
    agent = load_agent(monkeypatch)
    seen = {}

    async def launch(profile, **kwargs):
        seen.update(kwargs)
        return "context"
    monkeypatch.setitem(sys.modules, "cloakbrowser", SimpleNamespace(launch_persistent_context_async=launch))
    monkeypatch.setattr(agent, "http_proxy", lambda account: "http://127.0.0.1:9999")
    assert _run(agent._launch_cloak("acc1", "/tmp/x")) == "context"
    assert seen["proxy"] == {"server": "http://127.0.0.1:9999", "bypass": "*.upload"}


def test_failed_chain_read_still_reports_progress(monkeypatch):
    page = Page([bot("", False)])
    _, item = session(monkeypatch, page)
    seen = []
    item.on_poll = lambda now, progress=None, **kw: seen.append(progress)

    async def unreadable(query, conversation_id):
        if len(seen) >= 3:
            item.logouts.append("/passport/web/logout/")
        return None
    item.read_chain = unreadable
    with pytest.raises(Exception, match="710022002"):
        wait(item)
    assert len(seen) >= 3


def test_deadline_reports_a_timeout_without_resubmitting(monkeypatch):
    page = Page([bot("", False)])
    _, item = session(monkeypatch, page)
    with pytest.raises(TimeoutError, match="不换号"):
        _run(item.wait_for_video("123", time.time() - 1))
