"""工作模式额度：号池要知道哪些号已经用掉了，别再派出去白等一轮「7 天上限」（2026-11-09 排查）。

当天三条 30 秒任务里撞到 4 个已用完的号：
- acc21 / acc24 是同一个 Google 账号加了两遍，acc24 出过片后 acc21 没跟着锁；
- acc147 / acc20 早先失败记录里就有 Dola 写明的恢复时间，旧版本没把它记成锁；
- acc27 发出去后等超时，片子一小时后才做出来，额度已用但号池不知道。
"""
import json
import time

import pytest

import config
import work_pair
import work_reply as wr
from store import TaskStore
from test_account_throttle import _row
from test_task_deadline import _run
from test_work_mode import _agent, _work_pool

LIMIT = ("{acc} 工作模式免费额度已用完：7日間の無料利用分の上限に到達しました。"
         "2030年11月14日 10:57（GMT+08:00）に再開するか、チャットモードで私とチャットしてください。")


def _email(bp, name, email):
    bp._conn.execute("UPDATE accounts_meta SET email=? WHERE name=?", (email, name))
    bp._conn.commit()


# ---------- 同一个邮箱加了两遍 ----------

def test_same_email_accounts_share_the_work_lock(tmp_path, monkeypatch):
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc21", "acc24", "acc30"])
    _email(bp, "acc21", "Same@Example.com")
    _email(bp, "acc24", " same@example.com ")
    _email(bp, "acc30", "other@example.com")
    until = time.time() + 5 * 86400
    bp._mark_work_blocked("acc24", until, "工作模式免费额度已用")
    assert bp.same_login("acc24") == ["acc21"]
    assert _row(bp, "acc21")["work_blocked_until"] == until
    assert "acc24" in _row(bp, "acc21")["work_block_reason"]
    assert not bp._work_ready(_row(bp, "acc21"))
    assert bp._work_ready(_row(bp, "acc30"))


def test_twin_lock_is_never_shortened(tmp_path, monkeypatch):
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    _email(bp, "acc1", "a@example.com")
    _email(bp, "acc2", "a@example.com")
    later = time.time() + 6 * 86400
    bp._mark_work_blocked("acc2", later, "Dola 给的时间")
    bp._mark_work_blocked("acc1", later - 86400, "")
    assert _row(bp, "acc2")["work_blocked_until"] == later


def test_accounts_without_email_are_not_twins(tmp_path, monkeypatch):
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    bp._mark_work_blocked("acc1", time.time() + 3600, "")
    assert bp.same_login("acc1") == []
    assert bp._work_ready(_row(bp, "acc2"))


def test_existing_locks_are_shared_with_twins_at_startup(tmp_path, monkeypatch):
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc21", "acc24"])
    until = time.time() + 3 * 86400
    bp._mark_work_blocked("acc24", until, "")          # 旧版本只锁了出片的这一个
    _email(bp, "acc21", "same@example.com")
    _email(bp, "acc24", "same@example.com")
    assert bp.share_work_locks() == ["acc21"]
    assert _row(bp, "acc21")["work_blocked_until"] == until
    assert bp.share_work_locks() == []


def test_account_list_names_the_duplicate_login(tmp_path, monkeypatch):
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc21", "acc24", "acc30"])
    _email(bp, "acc21", "same@example.com")
    _email(bp, "acc24", "same@example.com")
    assert _row(bp, "acc21")["same_login"] == ["acc24"]
    assert _row(bp, "acc30")["same_login"] == []


# ---------- 失败记录里写明了恢复时间 ----------

def test_limit_notes_in_failed_attempts_restore_the_lock(tmp_path, monkeypatch):
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc147", "acc20", "acc22"])
    marked = bp.restore_work_limits([
        ("acc147", LIMIT.format(acc="acc147")),
        ("acc20", "acc20 工作模式参考图 60 秒内没有传完"),                 # 不是额度问题
        ("acc22", "7日間の無料利用分の上限に到達しました。2020年1月1日 10:00（GMT+08:00）に再開"),   # 早就过期
        ("ghost", LIMIT.format(acc="ghost")),
    ])
    assert marked == ["acc147"]
    assert _row(bp, "acc147")["work_blocked_until"] == wr.work_limit_until(LIMIT)
    assert bp._work_ready(_row(bp, "acc20")) and bp._work_ready(_row(bp, "acc22"))


def test_limit_restore_keeps_a_later_lock(tmp_path, monkeypatch):
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc1"])
    later = wr.work_limit_until(LIMIT) + 86400
    bp._mark_work_blocked("acc1", later, "更晚的锁")
    assert bp.restore_work_limits([("acc1", LIMIT.format(acc="acc1"))]) == []
    assert _row(bp, "acc1")["work_blocked_until"] == later


def _task(store, task_id, model, duration, status, account=None, error=None, history=None):
    store.create(task_id, model, "猫", "16:9", duration, account=account)
    fields = {"status": status}
    if error:
        fields["error"] = error
    if history is not None:
        fields["attempt_history"] = json.dumps(history, ensure_ascii=False)
    store.update(task_id, **fields)


def test_store_reads_work_usage_from_every_attempt(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    now = time.time()
    # 30 秒：第一段 acc23 出片，第二段 acc27 超时 → 任务失败，但 acc23 的额度用掉了
    _task(store, "pair", "seedance-2.0", 30, "failed", account="acc27", history=[
        {"account": "acc23", "outcome": "completed", "error": "", "started_at": now - 100},
        {"account": "acc27", "outcome": "failed", "error": "acc27 工作模式等待超时", "started_at": now - 50}])
    # 15 秒：换了两个号才出片，第一个号报了上限
    _task(store, "single", "seedance-2.0-fast", 15, "completed", account="acc26", history=[
        {"account": "acc148", "outcome": "failed", "error": LIMIT.format(acc="acc148"), "started_at": now - 30},
        {"account": "acc26", "outcome": "completed", "error": "", "started_at": now - 20}])
    # 旧任务没有尝试记录：看任务本身
    _task(store, "old-ok", "seedance_v2.0", 15, "completed", account="acc40")
    _task(store, "old-limit", "seedance-2.0", 15, "failed", account="acc20", error=LIMIT.format(acc="acc20"))
    # 不是工作模式的不算：豆包的 15 秒、2.5、2.0 的 10 秒
    _task(store, "doubao", "doubao-seedance-2.0-fast", 15, "completed", account="db1")
    _task(store, "v25", "seedance-2.5", 15, "completed", account="acc50")
    _task(store, "short", "seedance-2.0", 10, "completed", account="acc51")

    used = dict(store.work_videos_since(now - 3600))
    assert set(used) == {"acc23", "acc26", "acc40"}
    assert abs(used["acc23"] - (now - 100)) < 1
    failures = store.work_failures_since(now - 3600)
    assert {a for a, _ in failures} == {"acc27", "acc148", "acc20"}
    assert store.work_videos_since(now + 60) == []


def test_store_survives_broken_attempt_history(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    _task(store, "bad", "seedance-2.0", 15, "completed", account="acc1")
    store.update("bad", attempt_history="{not json")
    assert [a for a, _ in store.work_videos_since(0)] == ["acc1"]


# ---------- 发出去后结果不明的号排到最后 ----------

def test_account_that_timed_out_after_sending_is_tried_last(tmp_path, monkeypatch):
    browser_pool, bp = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2", "acc3"])
    calls = []

    async def fake(account, prompt, ratio, **kwargs):
        calls.append(account)
        if len(calls) == 1:
            raise TimeoutError(f"{account} 工作模式等待超时，可能仍在生成，不换号重提")
        return {"local_path": "", "account": account}
    _agent(monkeypatch, fake)
    with pytest.raises(TimeoutError):
        _run(bp.generate_video("猫", "16:9", 15, model="seedance-2.0", max_attempts=3))
    stuck = calls[0]
    assert bp._work_ready(_row(bp, stuck))               # 不锁：也许根本没出片
    order = [a["name"] for a in bp._work_order(bp._ordered_candidates(("满额", "半额")))]
    assert order[-1] == stuck
    result = _run(bp.generate_video("猫", "16:9", 15, model="seedance-2.0", max_attempts=3))
    assert result["account"] != stuck


def test_suspects_are_marked_again_after_a_restart(tmp_path, monkeypatch):
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc27", "acc22", "acc30"])
    marked = bp.restore_work_suspects([
        ("acc27", "acc27 工作模式等待超时，可能仍在生成，不换号重提"),
        ("acc22", "acc22 工作模式请求 120 秒没有回应，账号上也没有新会话"),      # 没发出去
        ("ghost", "ghost 工作模式等待超时，可能仍在生成，不换号重提")])
    assert marked == ["acc27"]
    order = [a["name"] for a in bp._work_order(bp._ordered_candidates(("满额", "半额")))]
    assert order[-1] == "acc27"


def test_confirmed_limit_clears_the_suspect_mark(tmp_path, monkeypatch):
    _, bp = _work_pool(tmp_path, monkeypatch, ["acc1"])
    bp.note_work_suspect("acc1")
    bp._mark_work_blocked("acc1", time.time() + 3600, "7 天上限")
    assert "acc1" not in bp._work_suspect


# ---------- 提示词：别让助手停下来问 ----------

@pytest.mark.parametrize("prompt", [
    "《借胎》第一场 时长：30秒｜9个Cut｜16:9", "全片 30s，分 3 批", "总长 60 秒的短片",
])
def test_prompt_with_a_longer_duration_is_pinned_to_one_15s_clip(prompt):
    text = wr.build_prompt(prompt, "16:9")
    assert text.endswith(wr.SINGLE_CLIP_NOTE) and "一条 15 秒" in text
    assert not text.endswith("，15s")


@pytest.mark.parametrize("prompt", ["一只猫 5.5s 起跳", "一只猫，15秒", "00:26.0—00:30.0 拉远", "24fps 的猫"])
def test_short_or_matching_durations_are_left_alone(prompt):
    assert wr.SINGLE_CLIP_NOTE not in wr.build_prompt(prompt, None)


def test_pair_segments_say_which_half_they_are():
    first, second = work_pair.first_prompt(" 猫 "), work_pair.continue_prompt(" 猫 ")
    assert first.startswith("猫") and "前半段" in first
    assert second.startswith("接着参考图这一刻继续：猫") and "后半段" in second


def test_pair_sends_the_half_notes_to_each_account(tmp_path, monkeypatch):
    browser_pool, bp = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    prompts = []

    async def fake(account, prompt, ratio, **kwargs):
        prompts.append(prompt)
        return {"local_path": str(tmp_path / f"{account}.mp4"), "account": account, "conversation_id": "1"}
    _agent(monkeypatch, fake)
    for name in ("acc1", "acc2"):
        (tmp_path / f"{name}.mp4").write_bytes(b"x")
    monkeypatch.setattr(work_pair, "last_frame", lambda src, dst: dst)
    monkeypatch.setattr(work_pair, "concat", lambda a, b, out: out)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    _run(bp.generate_video("全片 30 秒的猫", "16:9", 30, model="seedance-2.0", max_attempts=3))
    assert "前半段" in prompts[0] and "后半段" in prompts[1]


# ---------- 选择题的解析 ----------

def _ask_message(answer=None, after=(), finished=False):
    ask = {"questions": [{"question_id": "plan", "title": "采用哪种方案？", "options": [
        {"label": "两条"}, {"label": "单条（推荐）"}], "answer": answer or {"status": 1, "selected_option_ids": []}}]}
    blocks = [{"parent_id": "t", "content": {"interaction_ask_block": ask}},
              {"parent_id": "t", "content": {"text_block": {"text": "等待确认"}}}]
    blocks += [{"parent_id": "", "content": {"text_block": {"text": t}}} for t in after]
    return {"content_block": blocks, "ext": {"is_finish": "1"} if finished else {}}


def test_pending_ask_lists_questions_and_options():
    assert wr.pending_ask(_ask_message()) == [{"title": "采用哪种方案？", "options": ["两条", "单条（推荐）"]}]


@pytest.mark.parametrize("message", [
    _ask_message(answer={"selected_option_ids": ["a"]}),
    _ask_message(answer={"text": "按推荐的来"}),
    _ask_message(after=["用户暂未响应，我按推荐方案继续生成"]),
    {"content_block": [{"content": {"text_block": {"text": "请确认"}}}]},
    {},
])
def test_no_pending_ask_once_answered_or_moved_on(message):
    assert wr.pending_ask(message) is None


def test_choose_options_prefers_recommended_then_first():
    assert wr.choose_options([{"title": "a", "options": ["两条", "单条（推荐）"]},
                              {"title": "b", "options": ["我是会员", "確認：2.0_fast"]},
                              {"title": "c", "options": ["先出第1批", "全部"]},
                              {"title": "d", "options": []}]) == ["单条（推荐）", "確認：2.0_fast", "先出第1批"]


@pytest.mark.parametrize("tool,expected", [
    ("image_to_video", True), ("text_to_video", True), ("FileBatchUpload", False), ("", False),
])
def test_only_video_tools_count_as_generation_started(tool, expected):
    message = {"content_block": [{"content": {"generic_tool_block": {"tool_name": tool}}}]}
    assert wr.generation_started(message) is expected


# ---------- 换号重试的时间窗口 ----------

def test_work_job_keeps_switching_past_the_short_task_deadline(tmp_path, monkeypatch):
    # 9be3460c31：三个用完额度的号 + 两次发送前失败，12 分钟就判「超过任务时限」
    import asyncio
    browser_pool, bp = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    monkeypatch.setattr(config, "WORK_RETRY_SECONDS", 60)
    calls = []

    async def fake(account, prompt, ratio, **kwargs):
        calls.append(account)
        if len(calls) == 1:
            await asyncio.sleep(0.4)
            raise browser_pool.WorkQuotaError(f"{account} 工作模式免费额度已用完", until=time.time() + 86400)
        return {"local_path": "", "account": account}
    _agent(monkeypatch, fake)
    result = _run(bp.generate_video("猫", "16:9", 15, model="seedance-2.0", max_attempts=0, attempt_timeout=0.2))
    assert len(calls) == 2 and result["account"] == calls[1]


# ---------- 来源一栏：老的登录号显示成「—」 ----------

def test_blank_source_of_browser_login_accounts_is_backfilled(tmp_path, monkeypatch):
    import browser_pool
    accounts = tmp_path / "accounts"
    (accounts / "old-login" / "Default").mkdir(parents=True)
    (accounts / "cookie-only").mkdir(parents=True)
    (accounts / "cookie-only" / "cookie_state.json").write_text("{}", encoding="utf-8")
    (accounts / "both" / "Default").mkdir(parents=True)
    (accounts / "both" / "cookie_state.json").write_text("{}", encoding="utf-8")
    (accounts / "marked" / "Default").mkdir(parents=True)
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts), db_path=str(tmp_path / "pool.db"))
    assert bp.accounts and _row(bp, "old-login")["source"] == ""        # 第一次扫描才有记录
    bp.set_source("marked", "cookie")
    assert bp.backfill_sources() == ["old-login"]
    assert _row(bp, "old-login")["source"] == "login"
    assert _row(bp, "cookie-only")["source"] == "" and _row(bp, "both")["source"] == ""
    assert _row(bp, "marked")["source"] == "cookie"
    again = browser_pool.BrowserPool(accounts_dir=str(accounts), db_path=str(tmp_path / "pool.db"))
    assert _row(again, "old-login")["source"] == "login"
