"""11-25 线上失败回归：慢代理下编辑器晚到、历史会话标题含技能名、助手等确认被当成失败、发送后迟到的会话。"""
import json

import pytest

from test_task_deadline import _run
from test_work_session import Page, SkillPage, Target, bot, send_session, session, video, wait, PROMPT
from work_reply import waits_for_confirmation

LISTING = {"code": 0, "data": {"featured": [{"name": "dola-creative-video", "display_name": "クリエイティブ動画",
                                             "external_skill_id": "302885379089", "type": 2}]}}
DOUBAO_WAITING = ("按你的要求，这条只使用当前账号已有的免费或会员生成权益；如果 Seedance 2.0 Mini、5 秒或该权益任一不可用，"
                  "我会说明原因并停下。\n你还没确认，我就先停在这里，不开始生成。\n\n方案和参数都已经准备好了："
                  "Seedance 2.0 Mini、5 秒、16:9、1 条。你确认“按要求生成”后，我就直接按这套方案出片")


def allow(item):
    async def allowed():
        pass
    item.raise_if_blocked = allowed


def test_work_mode_waits_for_the_slow_editor(monkeypatch):
    page = SkillPage("クリエイティブ動画")
    _, item = session(monkeypatch, page)
    allow(item)
    states = iter([False] * 40 + [True])
    page.editor_ready = lambda: next(states)
    page.get_by_text = lambda pattern: Target()
    _run(item.enter_work_mode())
    assert next(states, "used") == "used" and not page.keys


def test_other_editor_implementation_still_reaches_the_skill_menu(monkeypatch):
    page = SkillPage("クリエイティブ動画")
    _, item = session(monkeypatch, page)
    allow(item)
    page.editor_ready = lambda: False
    page.get_by_text = lambda pattern: Target()
    page.locator = lambda selector: Target(1 if selector.startswith('[contenteditable') else 0)
    _run(item.enter_work_mode())


def test_missing_editor_stops_before_typing(monkeypatch):
    page = SkillPage("クリエイティブ動画")
    agent, item = session(monkeypatch, page)
    allow(item)
    page.editor_ready = lambda: False
    page.locator = lambda selector: Target(0)
    page.get_by_text = lambda pattern: Target()
    with pytest.raises(agent.PreSubmissionParametersError, match="编辑器 90 秒没有加载出来.*尚未发送"):
        _run(item.enter_work_mode())
    assert not page.keys and not item.sent


def test_dola_attaches_the_official_skill_without_the_menu(monkeypatch):
    page = SkillPage("クリエイティブ動画")
    agent, item = session(monkeypatch, page)
    original, inserted = page.evaluate, []

    async def evaluate(script, args):
        if script == agent.SKILLS_JS:
            return {"status": 200, "text": json.dumps(LISTING)}
        if script == agent.SELECT_SKILL_JS:
            inserted.append(args["reference"])
            page.selected = args["reference"]["label"]
            return True
        return await original(script, args)
    page.evaluate = evaluate
    page.get_by_text = lambda *a, **k: pytest.fail("技能引用可用时不应再按文字查找")
    _run(item.choose_skill())
    assert inserted[0]["id"] == "dola-creative-video" and inserted[0]["external_skill_id"] == "302885379089"
    assert page.selected == "クリエイティブ動画" and not page.keys


def test_history_conversation_with_the_skill_name_is_not_clicked(monkeypatch):
    page = SkillPage("クリエイティブ動画")
    agent, item = session(monkeypatch, page)
    item.skill_labels = ["クリエイティブ動画"]
    original = page.get_by_text

    def get(pattern):
        menu, history = original(pattern), Target()

        async def count():
            return 2

        async def outside(script):
            assert script == agent.OUTSIDE_MENU_JS
            return True

        async def wrong(**kwargs):
            pytest.fail("点到了历史会话")
        history.count, history.evaluate, history.click = count, outside, wrong
        history.nth = lambda index: menu
        return history
    page.get_by_text = get
    _run(item.choose_skill())
    assert page.selected == "クリエイティブ動画"


def test_menu_filter_excludes_history_sidebar_and_editors(monkeypatch):
    agent, _ = session(monkeypatch, SkillPage())
    for part in ('[data-testid^="conversation-"]', '[data-testid^="sidebar"]', 'a[href*="/chat/"]', 'contenteditable'):
        assert part in agent.OUTSIDE_MENU_JS


def test_waiting_for_confirmation_after_reading_the_skill_is_answered(monkeypatch):
    reading = bot(DOUBAO_WAITING, tool=True)
    reading["messages"][0]["content_block"][-1]["content"]["generic_tool_block"]["tool_name"] = "read_file"
    page = Page([reading] * 8 + [video()])
    _, item = session(monkeypatch, page)
    item.duration, item.model = 5, "doubao-seedance-2.0-mini"
    wait(item)
    assert page.typed == ["确认"]


def test_confirmed_task_still_ends_when_nothing_more_happens(monkeypatch):
    reading = bot(DOUBAO_WAITING, tool=True)
    reading["messages"][0]["content_block"][-1]["content"]["generic_tool_block"]["tool_name"] = "read_file"
    page = Page([reading])
    agent, item = session(monkeypatch, page)
    item.duration, item.model = 5, "doubao-seedance-2.0-mini"
    with pytest.raises(agent.UpstreamReplyError):
        wait(item)
    assert page.typed == ["确认"]


def test_started_generation_is_never_confirmed_again(monkeypatch):
    page = Page([bot(DOUBAO_WAITING, tool=True)])          # text_to_video 已调用
    agent, item = session(monkeypatch, page)
    item.duration, item.model = 5, "doubao-seedance-2.0-mini"
    with pytest.raises(agent.UpstreamReplyError) as caught:
        wait(item)
    assert caught.value.generation_started is True and page.typed == []


@pytest.mark.parametrize("text,expected", [
    (DOUBAO_WAITING, True), ("请确认以上参数：回复\"确认 / 开始生成\"即按此执行。", True),
    ("この内容でよろしければ「確認」と返信してください。", True),
    ("视频已开始生成，确认后可查看。", False), ("正在生成，请确认网络。", False),
    ("参数确认无误，视频如下。", False), ("", False),
])
def test_only_an_explicit_request_counts_as_waiting(text, expected):
    assert waits_for_confirmation(text) is expected


CONSENT = ("我将为你生成一条 5 秒视频。你在本功能中上传、使用的素材，均已获充分授权，无侵权违法风险。"
           "违反前述承诺或存在不当使用等违反《豆包使用规范》，你需自行承担责任。请确认")


def consent_page(monkeypatch, chains, *, button=True, enabled=True):
    page = Page(chains)
    agent, item = session(monkeypatch, page)
    item.duration, item.model, item.site = 5, "doubao-seedance-2.0-mini", agent.sites.DOUBAO
    monkeypatch.setattr(agent.config, "DOUBAO_CONFIRM_MATERIAL_AUTH", enabled)
    page.locator = lambda selector: (Target(1 if button else 0, page, "consent")
                                     if "ai-creation-common-button" in selector else Target())
    return agent, item, page


def consent_card(finished=True, tool=False, index=2):
    card = bot(CONSENT, finished, index=index, tool=tool)
    if tool:
        card["messages"][0]["content_block"][-1]["content"]["generic_tool_block"]["tool_name"] = "tool_search"
    return card


@pytest.mark.parametrize("finished,tool", [(True, False), (True, True), (False, False)])
def test_material_consent_card_is_clicked_once_then_the_video_comes_back(monkeypatch, finished, tool):
    agent, item, page = consent_page(monkeypatch, [consent_card(finished, tool)] * 3 + [video()])
    assert json.loads(wait(item))
    assert page.clicked == ["consent"] and page.typed == [] and item.tried == ["授权确认"]


def test_material_consent_is_not_clicked_when_disabled(monkeypatch):
    agent, item, page = consent_page(monkeypatch, [consent_card()], enabled=False)
    with pytest.raises(agent.UpstreamReplyError, match="素材授权待确认.*DOUBAO_CONFIRM_MATERIAL_AUTH=0") as caught:
        wait(item)
    assert page.clicked == [] and page.typed == [] and caught.value.generation_started is False


def test_second_material_consent_card_is_not_confirmed_again(monkeypatch):
    agent, item, page = consent_page(monkeypatch, [consent_card(index=2)] * 2 + [consent_card(index=4)])
    with pytest.raises(agent.UpstreamReplyError, match="已确认过一次") as caught:
        wait(item)
    assert page.clicked == ["consent"] and caught.value.allow_account_retry is False


def test_material_consent_without_a_button_ends_before_generation(monkeypatch):
    agent, item, page = consent_page(monkeypatch, [consent_card()], button=False)
    with pytest.raises(agent.UpstreamReplyError, match="没有找到确认按钮") as caught:
        wait(item)
    assert page.typed == [] and caught.value.generation_started is False


def test_dola_never_clicks_the_doubao_consent_button(monkeypatch):
    agent, item, page = consent_page(monkeypatch, [consent_card()])
    item.site = agent.sites.DOLA
    with pytest.raises(agent.UpstreamReplyError, match="素材授权待确认"):
        wait(item)
    assert page.clicked == []


@pytest.mark.parametrize("enabled,clicked", [(True, ["send"]), (False, [])])
def test_doubao_reply_uses_the_send_button_when_available(monkeypatch, enabled, clicked):
    page = Page([video()])
    agent, item = session(monkeypatch, page)
    item.site, pressed = agent.sites.DOUBAO, []
    button = Target(1, page, "send")

    async def is_enabled():
        return enabled

    async def press(key):
        pressed.append(key)
    button.is_enabled, page.keyboard.press = is_enabled, press
    page.locator = lambda selector: button if "send_button" in selector else Target()
    _run(item.type_reply("确认"))
    assert page.typed == ["确认"] and page.clicked == clicked and pressed == ([] if enabled else ["Enter"])


def test_late_conversation_is_found_before_switching_accounts(monkeypatch):
    agent, item, page = send_session(monkeypatch, jumps=True)
    monkeypatch.setattr(agent, "CONVERSATION_WAIT_SECONDS", 1)
    page.text = "Creative video "
    results = iter([(True, None), (True, "456")])

    async def lookup(since):
        return next(results)
    item.find_conversation = lookup
    assert _run(item.send(PROMPT)) == "456"
    assert page.enters == 1


@pytest.mark.parametrize("sent,detail", [(True, "提交请求已发出"), (False, "没有产生提交请求")])
def test_no_reply_reports_whether_the_request_left_the_page(monkeypatch, sent, detail):
    agent, item, page = send_session(monkeypatch, jumps=True)
    monkeypatch.setattr(agent, "CONVERSATION_WAIT_SECONDS", 1)
    page.text = "Creative video "
    if sent:
        item.sent.append("completion")

    async def lookup(since):
        return True, None
    item.find_conversation = lookup
    with pytest.raises(agent.PreSubmissionParametersError, match=detail):
        _run(item.send(PROMPT))


def test_unreadable_second_check_never_switches_accounts(monkeypatch):
    agent, item, page = send_session(monkeypatch, jumps=True)
    monkeypatch.setattr(agent, "CONVERSATION_WAIT_SECONDS", 1)
    page.text = "Creative video "
    results = iter([(True, None), (False, None)])

    async def lookup(since):
        return next(results)
    item.find_conversation = lookup
    with pytest.raises(TimeoutError, match="不换号重提"):
        _run(item.send(PROMPT))
