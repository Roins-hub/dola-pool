"""2.0 的 15 秒走工作模式：什么时候走、提示词、会话解析、派发和记账（2026-11-07）。"""
import json
import runpy

import pytest

import config
import work_reply as wr
from test_account_throttle import _pool, _row
from test_task_deadline import _run


def message(index, user_type, *texts, finished=True):
    return {"index_in_conv": index, "message_id": str(index), "user_type": user_type,
            "ext": {"is_finish": "1"} if finished else {},
            "content_block": [{"content": {"text_block": {"text": t}}} for t in texts]}


@pytest.mark.parametrize("model,duration,enabled,expected", [
    ("seedance-2.0", 15, True, True),
    ("seedance_v2.0", "15", True, True),
    ("seedance-2.0-fast", 15, True, True),
    ("seedance-2.0", 15, False, False),
    ("seedance-2.0", 10, True, False),
    ("seedance-2.5", 15, True, False),
    ("doubao-seedance-2.0-fast", 15, True, False),
    ("seedance-2.0", None, True, False),
    ("seedance-2.0", "x", True, False),
])
def test_only_dola_20_15s_uses_work_mode(monkeypatch, model, duration, enabled, expected):
    monkeypatch.setattr(config, "WORK_15S_ENABLED", enabled)
    assert wr.uses_work_mode(model, duration) is expected


@pytest.mark.parametrize("prompt,ratio,expected", [
    ("一只猫", "9:16", "一只猫，画面比例 9:16，15s"),
    ("一只猫。", None, "一只猫，15s"),
    ("一只猫，15秒", None, "一只猫，15秒"),
    ("一只猫 15s 慢镜头", None, "一只猫 15s 慢镜头"),
    ("第一行\n第二行", None, "第一行 第二行，15s"),
    ("115s 的片子", None, "115s 的片子，15s"),
])
def test_prompt_carries_duration_and_ratio_on_one_line(prompt, ratio, expected):
    assert wr.build_prompt(prompt, ratio) == expected


def test_session_copy_keeps_only_dola_cookies():
    cookies = [{"name": "sessionid", "domain": ".dola.com"}, {"name": "a", "domain": "www.dola.com"},
               {"name": "b", "domain": ".notdola.com"}, {"name": "c", "domain": ".doubao.com"}]
    assert [c["name"] for c in wr.session_cookies(cookies)] == ["sessionid", "a"]


def test_chain_messages_are_ordered():
    chain = {"body": {"messages": [message(7, 2, "b"), message(1, 1, "a")]}}
    assert [m["index_in_conv"] for m in wr.chain_messages(chain)] == [1, 7]


def test_video_model_is_found_inside_nested_json():
    model = json.dumps({"video_duration": 15.1})
    chain = {"content": json.dumps({"tool": {"video_model": model}})}
    assert wr.video_models(chain) == [model]
    assert wr.video_models({"content": "还在生成"}) == []


def test_reply_counts_as_finished_only_with_the_message_flag():
    # acc147：思考中只有 thinking 块；生成途中文字已出现，但消息还没写完
    thinking = {"content_block": [{"content": {"thinking_block": {}}}]}
    assert not wr.reply_finished(thinking)
    assert not wr.reply_finished(message(2, 2, "正在生成", finished=False))
    assert wr.reply_finished(message(2, 2, "视频已生成"))


def test_tool_card_means_the_agent_is_not_waiting_for_an_answer():
    tool = {"content_block": [{"content": {"generic_tool_block": {"tool_name": "text_to_video"}}}]}
    assert wr.tools_ran(tool)
    assert not wr.tools_ran(message(2, 2, "请确认"))


@pytest.mark.parametrize("text,asked", [("请你确认后我再开始生成", True), ("ご確認ください", True),
                                        ("你想要什么风格", False), ("", False)])
def test_confirmation_is_recognised_in_either_language(text, asked):
    assert wr.asks_to_confirm(text) is asked


def test_every_reference_must_be_committed():
    actions = ["ApplyImageUpload", "CommitImageUpload", "ApplyImageUpload"]
    assert wr.uploads_committed(actions) == 1


class FakeAgent:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def __call__(self, account, prompt, ratio, **kwargs):
        self.calls.append((account, prompt, ratio, kwargs.get("reference_image_paths")))
        if self.error:
            raise self.error
        return self.result


def _dispatch(bp, account, model="seedance-2.0", duration=15):
    return _run(bp._generate_effective(account, "猫", "9:16", duration, model, on_conversation_id=None,
                                       on_poll=None, on_balance=None, reference_image_paths=["ref.png"]))


def _agent(monkeypatch, fake):
    import types
    import sys
    monkeypatch.setitem(sys.modules, "video_worker_agent", types.SimpleNamespace(generate_work_video=fake))


def test_work_video_uses_up_the_account_for_the_day(tmp_path, monkeypatch):
    _, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(config, "WORK_15S_ENABLED", True)
    fake = FakeAgent(result={"local_path": "", "account": "acc1"})
    _agent(monkeypatch, fake)
    bp._conn.execute("UPDATE accounts_meta SET source='login' WHERE name='acc1'")
    bp._conn.commit()
    assert _dispatch(bp, "acc1")["account"] == "acc1"
    assert fake.calls == [("acc1", "猫", "9:16", ["ref.png"])]
    row = _row(bp, "acc1")
    assert row["quota_blocked"] and row["credit_balance"] == 0


def test_failed_work_video_keeps_the_day_quota(tmp_path, monkeypatch):
    _, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(config, "WORK_15S_ENABLED", True)
    _agent(monkeypatch, FakeAgent(error=RuntimeError("boom")))
    bp._conn.execute("UPDATE accounts_meta SET source='login' WHERE name='acc1'")
    bp._conn.commit()
    with pytest.raises(RuntimeError, match="boom"):
        _dispatch(bp, "acc1")
    assert not _row(bp, "acc1")["quota_blocked"]


def test_switch_off_leaves_the_normal_route_alone(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(config, "WORK_15S_ENABLED", False)
    fake = FakeAgent(result={"local_path": ""})
    _agent(monkeypatch, fake)
    seen = []

    async def normal(*args, **kwargs):
        seen.append(args[0])
        return {"local_path": ""}
    monkeypatch.setattr(browser_pool, "generate_video", normal)
    bp._conn.execute("UPDATE accounts_meta SET source='login' WHERE name='acc1'")
    bp._conn.commit()
    _dispatch(bp, "acc1")
    assert seen == ["acc1"] and fake.calls == []


def test_pure_cookie_account_never_opens_work_mode(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(config, "WORK_15S_ENABLED", True)
    fake = FakeAgent(result={"local_path": ""})
    _agent(monkeypatch, fake)
    monkeypatch.setattr(browser_pool, "is_cloak_cookie_account", lambda name: False)
    monkeypatch.setattr(browser_pool, "generate_pure_video", lambda **kwargs: {"local_path": ""})
    bp._conn.execute("UPDATE accounts_meta SET source='cookie' WHERE name='acc1'")
    bp._conn.commit()
    _dispatch(bp, "acc1")
    assert fake.calls == []


@pytest.mark.parametrize("flag,expected", [("1", (5, 10, 15)), ("0", (5, 10))])
def test_switch_decides_whether_20_offers_15_seconds(monkeypatch, flag, expected):
    monkeypatch.setenv("DOLA_WORK_15S", flag)
    monkeypatch.delenv("DOLA_V20_DURATIONS", raising=False)
    assert runpy.run_path(config.__file__)["V20_DURATIONS"] == expected


@pytest.fixture
def switch_state(monkeypatch):
    monkeypatch.setattr(config, "V20_BASE_DURATIONS", (5, 10))
    monkeypatch.setattr(config, "V20_DURATIONS", (5, 10))
    monkeypatch.setattr(config, "WORK_15S_ENABLED", False)
    monkeypatch.setattr(wr, "unavailable_reason", lambda: "")


def test_panel_switch_takes_effect_without_restart(switch_state):
    config.set_work_15s(True)
    assert config.V20_DURATIONS == (5, 10, 15) and wr.uses_work_mode("seedance-2.0", 15)
    config.set_work_15s(False)
    assert config.V20_DURATIONS == (5, 10) and not wr.uses_work_mode("seedance-2.0", 15)


@pytest.mark.parametrize("saved,env_default,expected", [
    ("1", False, True), ("0", True, False), (None, True, True), (None, False, False),
])
def test_saved_panel_choice_beats_the_environment_default(switch_state, saved, env_default, expected):
    config.set_work_15s(env_default)
    assert wr.apply_saved(saved)["enabled"] is expected


def test_environment_without_cloak_stays_off_and_says_why(switch_state, monkeypatch):
    monkeypatch.setattr(wr, "unavailable_reason", lambda: "没有安装")
    view = wr.apply_saved("1")
    assert view == {"enabled": False, "available": False, "reason": "没有安装", "v20_durations": [5, 10]}
