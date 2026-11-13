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
    ("115 帧的片子", None, "115 帧的片子，15s"),
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


def test_work_video_locks_only_the_work_quota_for_seven_days(tmp_path, monkeypatch):
    # 工作模式额度和每日额度分开：acc13 每日额度用完还出了 15 秒
    import time
    _, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(config, "WORK_15S_ENABLED", True)
    fake = FakeAgent(result={"local_path": "", "account": "acc1"})
    _agent(monkeypatch, fake)
    bp._conn.execute("UPDATE accounts_meta SET source='login' WHERE name='acc1'")
    bp._conn.commit()
    assert _dispatch(bp, "acc1")["account"] == "acc1"
    assert fake.calls == [("acc1", "猫", "9:16", ["ref.png"])]
    row = _row(bp, "acc1")
    assert not row["quota_blocked"] and row["credit_balance"] is None
    assert 6.9 * 86400 < row["work_blocked_until"] - time.time() <= 7 * 86400


def test_failed_work_video_keeps_both_quotas(tmp_path, monkeypatch):
    _, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(config, "WORK_15S_ENABLED", True)
    _agent(monkeypatch, FakeAgent(error=RuntimeError("boom")))
    bp._conn.execute("UPDATE accounts_meta SET source='login' WHERE name='acc1'")
    bp._conn.commit()
    with pytest.raises(RuntimeError, match="boom"):
        _dispatch(bp, "acc1")
    assert not _row(bp, "acc1")["quota_blocked"] and not _row(bp, "acc1")["work_blocked_until"]


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
    bp._conn.execute("UPDATE accounts_meta SET source='cookie' WHERE name='acc1'")
    bp._conn.commit()
    with pytest.raises(browser_pool.PreSubmissionParametersError, match="纯 API"):
        _dispatch(bp, "acc1")
    assert fake.calls == []


@pytest.mark.parametrize("flag,expected", [("1", (5, 10, 15, 30)), ("0", (5, 10))])
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
    assert config.V20_DURATIONS == (5, 10, 15, 30) and wr.uses_work_mode("seedance-2.0", 15)
    assert wr.uses_work_pair("seedance-2.0", 30)
    config.set_work_15s(False)
    assert config.V20_DURATIONS == (5, 10) and not wr.uses_work_mode("seedance-2.0", 15)
    assert not wr.uses_work_pair("seedance-2.0", 30)


@pytest.mark.parametrize("saved,env_default,expected", [
    ("1", False, True), ("0", True, False), (None, True, True), (None, False, False),
])
def test_saved_panel_choice_beats_the_environment_default(switch_state, saved, env_default, expected):
    config.set_work_15s(env_default)
    assert wr.apply_saved(saved)["enabled"] is expected


def test_environment_without_cloak_stays_off_and_says_why(switch_state, monkeypatch):
    monkeypatch.setattr(wr, "unavailable_reason", lambda: "没有安装")
    view = wr.apply_saved("1")
    assert (view["enabled"], view["available"], view["reason"]) == (False, False, "没有安装")
    assert view["v20_durations"] == [5, 10]


@pytest.mark.parametrize("saved,expected", [("3", 3), ("99", 5), ("0", 1), ("x", 2), (None, 2)])
def test_saved_concurrency_stays_in_range(switch_state, monkeypatch, saved, expected):
    monkeypatch.setattr(config, "WORK_15S_CONCURRENCY", 2)
    assert wr.apply_saved(None, saved)["concurrency"] == expected


def test_rotating_a_proxy_reaches_every_account_bound_to_it(tmp_path, monkeypatch):
    # 面板显示 37 个号绑定，点「换IP」却是「已换 0 个」：list_accounts 的行里没有 proxy_id
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a", "acc2": "b", "acc3": "c"})
    bound = {"acc1": {"id": "px_a"}, "acc2": {"id": "px_a"}, "acc3": {"id": "px_b"}}
    monkeypatch.setattr(browser_pool.proxy_store, "get_account_proxy", bound.get)
    calls = []

    def rotate(name, reason="", force=False):
        calls.append((name, force))
        return {"rotated": name == "acc1"}
    monkeypatch.setattr(bp, "rotate_account_ip", rotate)
    assert bp.rotate_proxy_accounts("px_a") == {"rotated": ["acc1"], "skipped": ["acc2"]}
    assert calls == [("acc1", True), ("acc2", True)]


def test_recent_conversation_list_is_parsed():
    data = {"downlink_body": {"pull_recent_conv_chain_downlink_body": {"cells": [
        {"conversation": {"conversation_id": "38", "conv_version": "1794018361839171"}},
        {"conversation": {"conversation_id": "39"}}, {"other": 1}]}}}
    assert wr.recent_conversations(data) == [("38", 1794018361)]
    assert wr.recent_conversations({}) == []


LIMIT_JA = ("7日間の無料利用分の上限に到達しました。2026年11月14日 10:57（GMT+08:00）に再開するか、"
            "チャットモードで私とチャットしてください。 続けるには、プランをアップグレードしてください。")


@pytest.mark.parametrize("text,hit", [
    (LIMIT_JA, True), ("7天免费使用额度已用完", True), ("免费额度已用完，请升级", True),
    ("视频已生成完成，15 秒", False), ("请确认参数", False),
])
def test_work_limit_wording_is_recognised(text, hit):
    assert bool(wr.WORK_LIMIT_RE.search(text)) is hit


def test_resume_time_follows_the_stated_offset():
    import datetime
    want = datetime.datetime(2026, 11, 14, 2, 57, tzinfo=datetime.timezone.utc).timestamp()
    assert wr.work_limit_until(LIMIT_JA) == want
    assert wr.work_limit_until("2026年11月14日 10:57 に再開") == want      # 没写时区按北京时间
    assert wr.work_limit_until("2026年11月14日 10:57 (GMT+09:00)") == want - 3600
    assert wr.work_limit_until("没有时间") is None
    assert wr.work_limit_until("2026年13月40日 10:57") is None


def _work_pool(tmp_path, monkeypatch, names):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {n: n for n in names})
    monkeypatch.setattr(config, "WORK_15S_ENABLED", True)
    monkeypatch.setattr(config, "EGRESS_PRECHECK", False)
    bp._has_bound_proxy = lambda name: True
    for n in names:
        bp._conn.execute("UPDATE accounts_meta SET source='login' WHERE name=?", (n,))
    bp._conn.commit()
    return browser_pool, bp


def test_daily_quota_cooling_does_not_block_work_mode(tmp_path, monkeypatch):
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc1"])
    row = _row(bp, "acc1")
    assert bp._work_ready({**row, "group": "冷却", "remaining": 0})


def test_work_lock_blocks_only_work_mode(tmp_path, monkeypatch):
    import time
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc1"])
    bp._mark_work_blocked("acc1", time.time() + 3600, "7 天上限")
    row = _row(bp, "acc1")
    assert not bp._work_ready(row)
    assert bp._schedulable(row)          # 5/10 秒照常能派


def test_all_accounts_locked_reports_the_earliest_resume(tmp_path, monkeypatch):
    import time
    browser_pool, bp = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    assert bp._work_all_blocked(browser_pool.sites.DOLA) == ""
    soon = time.time() + 3600
    bp._mark_work_blocked("acc1", soon, "")
    assert bp._work_all_blocked(browser_pool.sites.DOLA) == ""      # acc2 还能用
    bp._mark_work_blocked("acc2", soon + 7200, "")
    note = bp._work_all_blocked(browser_pool.sites.DOLA)
    assert "都用完了" in note and browser_pool._beijing_clock(soon) in note


def test_work_quota_error_switches_to_the_next_account(tmp_path, monkeypatch):
    import time
    browser_pool, bp = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    until = time.time() + 5 * 86400
    calls = []

    async def fake(account, prompt, ratio, **kwargs):
        calls.append(account)
        if len(calls) == 1:
            raise browser_pool.WorkQuotaError(f"{account} 工作模式免费额度已用完", until=until)
        return {"local_path": "", "account": account}
    _agent(monkeypatch, fake)
    result = _run(bp.generate_video("猫", "16:9", 15, model="seedance-2.0", max_attempts=3))
    assert len(calls) == 2 and result["account"] == calls[1]
    assert _row(bp, calls[0])["work_blocked_until"] == until
    assert _row(bp, calls[1])["used_today"] == 0          # 工作模式不扣每日额度


def test_used_accounts_are_restored_from_task_history(tmp_path, monkeypatch):
    import time
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2", "acc3"])
    now = time.time()
    bp._mark_work_blocked("acc3", now + 9 * 86400, "Dola 给的时间")
    marked = bp.restore_work_quota([("acc1", now - 3600), ("acc1", now - 86400), ("acc2", now - 8 * 86400),
                                    ("acc3", now - 60), ("ghost", now)])
    assert marked == ["acc1"]                                        # 过期的、未知的、已有更晚锁的不动
    assert abs(_row(bp, "acc1")["work_blocked_until"] - (now - 86400 + 7 * 86400)) < 1   # 取最早一次
    assert _row(bp, "acc3")["work_blocked_until"] == now + 9 * 86400


@pytest.mark.parametrize("model,duration,expected", [
    ("seedance-2.0", 30, True), ("seedance-2.0-fast", "30", True), ("seedance-2.0", 15, False),
    ("seedance-2.5", 30, False), ("doubao-seedance-2.0-fast", 30, False), ("seedance-2.0", None, False),
])
def test_only_20_at_30_seconds_is_a_work_pair(monkeypatch, model, duration, expected):
    monkeypatch.setattr(config, "WORK_15S_ENABLED", True)
    assert wr.uses_work_pair(model, duration) is expected


def test_continue_prompt_points_at_the_reference_frame():
    import work_pair
    text = work_pair.continue_prompt("  橘猫在沙滩上走  ")
    assert text.startswith("接着参考图这一刻继续：橘猫在沙滩上走") and "镜头运动" in text


@pytest.mark.parametrize("audio", [True, False])
def test_concat_fits_the_second_segment_into_the_first_size(audio):
    import work_pair
    args = work_pair.concat_args("a.mp4", "b.mp4", "o.mp4", 960, 960, audio)
    graph = args[args.index("-filter_complex") + 1]
    assert graph.count("scale=960:960") == 2 and "pad=960:960" in graph
    assert ("concat=n=2:v=1:a=1" in graph) is audio and ("[au]" in args) is audio
    assert args[-1] == "o.mp4" and "+faststart" in args


def _make_clip(path, color, seconds, audio):
    import subprocess
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=24:d={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest", "-c:a", "aac"]
    subprocess.run(args + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="需要 ffmpeg")
def test_real_frame_and_concat(tmp_path):
    import work_pair
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _make_clip(first, "red", 2, True)
    _make_clip(second, "blue", 2, False)            # 第二段没声音、尺寸一样也能拼
    frame = work_pair.last_frame(first, tmp_path / "f.png")
    assert frame.stat().st_size > 0
    out = work_pair.concat(first, second, tmp_path / "o.mp4")
    info = work_pair.probe(out)
    assert (info["width"], info["height"]) == (320, 180)
    import subprocess
    seconds = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                                   capture_output=True, text=True).stdout)
    assert 3.8 < seconds < 4.3


def test_pair_runs_two_segments_on_two_accounts_and_stitches(tmp_path, monkeypatch):
    browser_pool, bp = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    import work_pair
    calls, polls = [], []

    async def fake(account, prompt, ratio, **kwargs):
        calls.append((account, prompt, kwargs.get("reference_image_paths")))
        clip = tmp_path / f"{account}.mp4"
        clip.write_bytes(b"x")
        return {"local_path": str(clip), "account": account, "conversation_id": account + "-conv"}
    _agent(monkeypatch, fake)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(browser_pool, "ensure_web_playable", lambda path: path)
    def made(name):
        def write(*args):
            (tmp_path / name).write_bytes(b"x")
            return tmp_path / name
        return write
    monkeypatch.setattr(work_pair, "last_frame", made("frame.png"))
    monkeypatch.setattr(work_pair, "concat", made("out.mp4"))
    result = _run(bp.generate_video("橘猫", "16:9", 30, model="seedance-2.0", max_attempts=3,
                                    on_poll=lambda now, **f: polls.append(f.get("progress"))))
    assert len(calls) == 2 and calls[0][0] != calls[1][0]           # 两个号
    assert calls[1][1].startswith("接着参考图这一刻继续：橘猫") and calls[1][2] == [str(tmp_path / "frame.png")]
    assert result["merge_mode"] == "work_pair" and result["local_path"] == str(tmp_path / "out.mp4")
    assert sorted(result["accounts"]) == ["acc1", "acc2"]
    for name in result["accounts"]:
        assert _row(bp, name)["work_blocked_until"] and _row(bp, name)["used_today"] == 0
    assert any(str(p).startswith("30 秒") for p in polls if p)


def test_second_segment_failure_says_the_first_quota_is_spent(tmp_path, monkeypatch):
    browser_pool, bp = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    import work_pair
    calls = []

    async def fake(account, prompt, ratio, **kwargs):
        calls.append(account)
        if len(calls) > 1:
            raise RuntimeError("boom")
        clip = tmp_path / f"{account}.mp4"
        clip.write_bytes(b"x")
        return {"local_path": str(clip), "account": account}
    _agent(monkeypatch, fake)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(browser_pool, "ensure_web_playable", lambda path: path)
    monkeypatch.setattr(work_pair, "last_frame", lambda video, out: tmp_path / "frame.png")
    with pytest.raises(RuntimeError, match="第 2 段失败（第 1 段已用掉 acc[12] 的 15 秒额度）"):
        _run(bp.generate_video("橘猫", "16:9", 30, model="seedance-2.0", max_attempts=1))
