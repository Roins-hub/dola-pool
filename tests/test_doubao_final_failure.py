"""Issue #24：豆包普通模式的失败卡片（工具 status=5 + fail_code）要当场结束任务，不能等到超时。"""
import json
import types

import pytest

import doubao_gen
from generation_errors import UpstreamGenerationFailed
from test_doubao import CONFIRM, FREE_ACCEPTED, _FakeDP, _gen

VOIDED = "视频生成失败，生成额度未扣除。"


def failed_ext(code=710082023, status=5, *, as_json=True, **extra):
    tools = [{"status": status, "fail_code": code, **extra}]
    return {"agent_name": "Agent-Creation", "has_video_gen": "1", "ai_creation_res_code": str(code),
            "ai_creation_tool_list": json.dumps(tools) if as_json else tools}


def short_timeout(monkeypatch):
    """没有识别到终态时让轮询很快到点，避免测试空转。"""
    now = [0.0]
    def tick():
        now[0] += 1
        return now[0]
    monkeypatch.setattr(doubao_gen.time, "time", tick)


@pytest.mark.parametrize("as_json", [True, False])
def test_reported_voided_reply_ends_the_task_with_its_code(monkeypatch, tmp_path, as_json):
    short_timeout(monkeypatch)
    dp = _FakeDP([(0, FREE_ACCEPTED, []), (0, VOIDED, [], failed_ext(as_json=as_json))])
    with pytest.raises(UpstreamGenerationFailed) as failure:
        _gen(monkeypatch, tmp_path, dp)
    error = failure.value
    assert error.code == "710082023" and error.not_charged and error.conversation_id == "conv1"
    assert "710082023" in str(error) and VOIDED in str(error)
    assert not error.retryable and not error.allow_account_retry     # 不自动换号重提
    assert dp.replies == [] and dp.downloaded == []


def test_failed_card_is_never_confirmed_or_answered(monkeypatch, tmp_path):
    short_timeout(monkeypatch)
    dp = _FakeDP([(0, CONFIRM + VOIDED, [], failed_ext())])
    with pytest.raises(UpstreamGenerationFailed):
        _gen(monkeypatch, tmp_path, dp)
    assert dp.replies == []


def test_failure_must_be_read_twice(monkeypatch, tmp_path):
    """第一次读到失败、下一次已经有视频：以视频为准。"""
    dp = _FakeDP([(0, FREE_ACCEPTED, []), (0, VOIDED, [], failed_ext())])
    reads, real = [], dp.fetch_single_chain
    def fetch(*a, **k):
        reads.append(1)
        dp.vid_after = 0 if len(reads) > 1 else None
        return real(*a, **k)
    dp.fetch_single_chain = fetch
    assert _gen(monkeypatch, tmp_path, dp)["unwatermarked"] is True


@pytest.mark.parametrize("ext", [
    failed_ext(status=4), failed_ext(code=0), failed_ext(status=2),
    failed_ext(file_type="image", tool_name="text_to_image"),
    {"ai_creation_res_code": "710082041"},                  # 前置确认的码，不是失败
    {"ai_creation_tool_list": "{"}, {"ai_creation_tool_list": "[" * 5000},
    {"ai_creation_tool_list": [None, "x"]}, {},
])
def test_unconfirmed_states_are_not_final(ext):
    chain = {"messages": [{"message_id": "m3", "index_in_conv": 3, "user_type": 2, "ext": ext, "content_block": [
        {"content": {"text_block": {"text": VOIDED}}, "is_finish": True}]}]}
    msgs = doubao_gen._messages(chain)
    assert [m["text"] for m in msgs] == [VOIDED]
    assert doubao_gen.final_failure(msgs) is None


def test_mixed_tools_and_later_user_reply_are_not_final():
    def rows(*extra, tools):
        return doubao_gen._messages({"messages": [
            {"message_id": "m3", "index_in_conv": 3, "user_type": 2, "ext": {"ai_creation_tool_list": tools}, "content_block": [
                {"content": {"text_block": {"text": VOIDED}}, "is_finish": True}]}, *extra]})
    failed = {"status": 5, "fail_code": 710082023}
    assert doubao_gen.final_failure(rows(tools=[failed]))["code"] == "710082023"
    assert doubao_gen.final_failure(rows(tools=[failed, {"status": 2, "fail_code": 0}])) is None
    # 用户随后在官方 App 里重新确认（issue 案例 E）：不能拿旧失败判定新的尝试
    retry = {"message_id": "m4", "index_in_conv": 4, "user_type": 1, "content_block": [
        {"content": {"text_block": {"text": "确认生成"}}, "is_finish": True}]}
    assert doubao_gen.final_failure(rows(retry, tools=[failed])) is None


def test_reason_enum_is_kept_but_free_text_is_not():
    chain = lambda reason: doubao_gen._messages({"messages": [
        {"message_id": "m3", "index_in_conv": 3, "user_type": 2, "ext": failed_ext(failure_reason=reason), "content_block": []}]})
    assert doubao_gen.final_failure(chain("internal_error"))["reason"] == "internal_error"
    assert doubao_gen.final_failure(chain("cookie=secret https://x"))["reason"] == ""


def test_background_retrieval_stops_on_failed_card(monkeypatch, tmp_path):
    dp = _FakeDP([(0, FREE_ACCEPTED, []), (0, VOIDED, [], failed_ext())])
    state = tmp_path / "cookie_state.json"
    state.write_text("{}")
    monkeypatch.setattr(doubao_gen, "_site", lambda: dp)
    monkeypatch.setattr(doubao_gen, "_load_state", lambda _p: {})
    monkeypatch.setattr(doubao_gen, "_session", lambda *_a, **_k: (types.SimpleNamespace(close=lambda: None), {}, {}))
    result = doubao_gen.fetch_conversation_video(state, "conv1", output_dir=tmp_path)
    assert result["final"] is True and result["status"] == "failed"
    assert "710082023" in result["detail"][:80]          # 任务报错只取前 80 字
    dp.script = [(0, FREE_ACCEPTED, []), (0, VOIDED, [])]        # 只有文案、没有结构化状态：继续盯
    assert doubao_gen.fetch_conversation_video(state, "conv1", output_dir=tmp_path)["final"] is False


def test_failure_text_does_not_lock_the_account():
    from browser_pool import classify_upstream_failure
    error = UpstreamGenerationFailed("上游生成失败", VOIDED, code="710082023", conversation_id="c",
                                     retryable=False, not_charged=True)
    assert classify_upstream_failure(str(error)) == "other"
