"""助手请求确认时附带的假设说明不是失败：按线上原话回归。"""
import pytest

from test_work_session import load_agent, Page, bot, session, video, wait
from work_reply import without_conditions

ASKING = [
    "`seedance_2.0_fast` 支持 5–15 秒，15 秒为其支持上限，可原生单段生成；9:16 在支持画幅内。\n"
    "- 免费额度余额无法在调用前由工具直接查询：确认后将以免费额度发起这唯一一次调用；若返回额度不足、权益不可用或模型/时长受限，"
    "将按你的要求直接停止并说明原因，不做任何降级变通。\n\n请确认以上参数：回复\"确认 / 开始生成\"即按此执行。",
    "**额度与中止规则**：仅使用当前免费额度；若调用时模型、15s 时长或免费权益不可用（如额度不足、模型不可用），"
    "我会如实说明具体原因并停止 —— 不购买、不充值、不升级套餐。\n\n请确认是否按以上参数开始生成。",
    "若调用时模型不可用、15 秒时长不可用、或免费额度不足/权益不可用，我会**直接停止**。\n\n回复“**确认 / 开始生成**”即可。",
    "若 7 天免费额度用完，我会停止。请确认参数后回复「确认」。",
    "無料利用分が上限に到達した場合は停止します。この内容でよろしければ「確認」と返信してください。",
    "If the quota exhausted error appears I will stop. 请回复“确认”开始。",
]
REFUSED = [
    "当前额度不足，如果需要继续请升级套餐，确认后再试。",
    "如果想继续可以明天再来，但当前免费额度已用完，请确认。",
    "動画生成の無料利用分の上限に到達しました。確認してください。",
    "7日間の上限に到達しました。続ける場合は、プランの変更を確認してください。",
    "余额不足。",
    "若干素材已处理，余额不足，请确认。",
]


@pytest.mark.parametrize("text", ASKING)
def test_hypothetical_limits_do_not_fail_the_task(monkeypatch, text):
    agent = load_agent(monkeypatch)
    assert agent.failure_from_reply("acc1", text, ran_tools=False) is None


@pytest.mark.parametrize("text", REFUSED)
def test_real_limits_still_fail(monkeypatch, text):
    agent = load_agent(monkeypatch)
    assert agent.failure_from_reply("acc1", text, ran_tools=False) is not None


def test_reply_without_a_confirmation_request_is_judged_as_before(monkeypatch):
    agent = load_agent(monkeypatch)
    assert agent.failure_from_reply("acc1", "如果额度不足", ran_tools=False) is not None


def test_statement_after_the_condition_is_kept():
    assert "余额不足" in without_conditions("如果要继续，但当前余额不足")
    assert "余额不足" not in without_conditions("若余额不足，将停止。")


def test_conditional_note_is_answered_once_and_the_video_is_delivered(monkeypatch):
    """11-25 acc15：助手只是请求确认，应回「确认」继续等原会话，不换号、不锁号。"""
    page = Page([bot(ASKING[1])] * 4 + [video()])
    _, item = session(monkeypatch, page)
    wait(item)
    assert page.typed == ["确认"]
