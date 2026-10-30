"""2.1.6：吃到 710022002 的号进【异常】暂停派发；30 秒被上游封顶时直接失败；回传上游原话。

线上（2026-10-13）：直连和日本代理两个出口都被拒，出口退避一结束就换同出口的下一个号去撞，
任务在「退避 → 换号 → 再被拒」里循环到超时；30 秒测试任务上游回「按默认 5 秒为你生成」，
我们白等了 30 分钟。
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config


@pytest.fixture(autouse=True)
def _stubs():
    sys.modules.setdefault("patchright", types.ModuleType("patchright"))
    pa = types.ModuleType("patchright.async_api")

    def _no(*_a, **_k):
        raise RuntimeError("stub")

    pa.async_playwright = _no
    sys.modules.setdefault("patchright.async_api", pa)
    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError",
                "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))

    async def _gen(*_a, **_k):
        raise RuntimeError("stub")

    v.generate_video = _gen
    v.resume_video = _gen
    sys.modules["video_worker_ui"] = v
    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d
    yield


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


OK = {"ok": True, "status": "ok", "login_ok": True, "replied": True,
      "reason": "replied", "reply": "你好呀！", "elapsed": 2.0}
THROTTLED = {"ok": False, "status": "error", "login_ok": True, "replied": False,
             "reason": "710022002: 当前服务访问频繁，请稍后重试", "reply": "", "elapsed": 0.3}


def _pool(tmp_path, monkeypatch, egress: dict, probe: dict | None = None):
    import browser_pool
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 4)
    monkeypatch.setattr(browser_pool, "DAILY_LIMIT", 4)
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    monkeypatch.setattr(config, "EGRESS_THROTTLE_SECONDS", 0)       # 只测号级暂停
    monkeypatch.setattr(config, "THROTTLE_ACCOUNT_COOLDOWN_SECONDS", 1800)
    monkeypatch.setattr(config, "HELLO_PROBE_SUBMIT_GAP_SECONDS", 0)
    monkeypatch.setattr(config, "V20_WAIT_SECONDS", 0)
    accounts_dir = tmp_path / "accounts"
    for name in egress:
        d = accounts_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "cookie_state.json").write_text('{"source":"cookie"}', encoding="utf-8")
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir), db_path=str(tmp_path / "pool.db"))
    for name in egress:
        bp.set_login_status(name, True)
    bp._egress_for = lambda name: egress[name]
    calls: list[str] = []
    probe_map = probe or {}

    def fake_hello(state, proxy="", prompt=None, timeout_sec=None):
        name = Path(state).parent.name
        calls.append(name)
        return dict(probe_map.get(name, OK))

    monkeypatch.setattr(browser_pool, "hello_probe", fake_hello)
    return browser_pool, bp, calls


def _row(bp, name):
    return next(a for a in bp.list_accounts() if a["name"] == name)


def test_throttled_account_is_paused_and_skipped(tmp_path, monkeypatch):
    browser_pool, bp, calls = _pool(tmp_path, monkeypatch, {"acc1": "a", "acc2": "b"},
                                    probe={"acc1": THROTTLED})
    bp.probe_hello("acc1", force=True)
    row = _row(bp, "acc1")
    assert row["group"] == browser_pool.GROUP_ABNORMAL        # 不参与派发
    assert "710022002" in row["abnormal_reason"]
    assert row["risk_control"] is False                       # 不是风控，到期自动恢复

    gen = []

    async def fake_gen(account, *a, **k):
        gen.append(account)
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake_gen
    result = _run(bp.generate_video("p", None, 10, "seedance-2.5", deadline=time.time() + 20))
    assert result["account"] == "acc2" and gen == ["acc2"]
    assert calls.count("acc1") == 1                           # acc1 没再被探测


def test_probe_ok_releases_throttle_pause_early(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"}, probe={"acc1": THROTTLED})
    bp.probe_hello("acc1", force=True)
    assert _row(bp, "acc1")["group"] == browser_pool.GROUP_ABNORMAL
    monkeypatch.setattr(browser_pool, "hello_probe", lambda *a, **k: dict(OK))
    bp.probe_hello("acc1", force=True)
    assert _row(bp, "acc1")["group"] == browser_pool.GROUP_FULL


def test_probe_ok_does_not_clear_other_abnormal(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    bp._mark_abnormal("acc1", "派发后 5 分钟无回执")
    bp.probe_hello("acc1", force=True)
    assert _row(bp, "acc1")["group"] == browser_pool.GROUP_ABNORMAL


def test_generation_throttle_pauses_account_and_moves_on(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a", "acc2": "a"})
    gen = []

    async def fake_gen(account, *a, **k):
        gen.append(account)
        if account == "acc1":
            raise RuntimeError("acc1 提交失败: 710022002: 当前服务访问频繁，请稍后重试")
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake_gen
    monkeypatch.setattr(bp, "_ordered_candidates",
                        lambda allowed: sorted(bp.list_accounts(), key=lambda a: a["name"]))
    result = _run(bp.generate_video("p", None, 10, "seedance-2.5", deadline=time.time() + 20))
    assert result["account"] == "acc2"
    assert _row(bp, "acc1")["group"] == browser_pool.GROUP_ABNORMAL


def test_all_paused_fails_fast_with_clear_reason(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a", "acc2": "b"})
    bp.note_account_throttle("acc1", "710022002")
    bp.note_account_throttle("acc2", "710022002")

    async def fake_gen(account, *a, **k):
        raise AssertionError("暂停中的号不应被派发")

    bp._generate_effective = fake_gen
    started = time.time()
    with pytest.raises(RuntimeError, match="2 个号因上游限流"):
        _run(bp.generate_video("p", None, 10, "seedance-2.5", deadline=time.time() + 60))
    assert time.time() - started < 15                         # 不再转圈到超时


def test_cooldown_zero_disables_account_pause(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"}, probe={"acc1": THROTTLED})
    monkeypatch.setattr(config, "THROTTLE_ACCOUNT_COOLDOWN_SECONDS", 0)
    bp.probe_hello("acc1", force=True)
    assert _row(bp, "acc1")["group"] == browser_pool.GROUP_FULL


def test_unsupported_duration_stops_whole_task(tmp_path, monkeypatch):
    """上游说 30 秒不支持：直接失败，不再挨个号重试。"""
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a", "acc2": "b"})
    monkeypatch.setitem(config.DURATION_COSTS, "seedance-2.5", {30: 2})   # 只测「时长不支持」这条路径
    gen = []

    async def fake_gen(account, *a, **k):
        gen.append(account)
        raise browser_pool.UnsupportedDurationError(f"{account} dola 目前只支持 4~15 秒，30 秒无法一次生成")

    bp._generate_effective = fake_gen
    with pytest.raises(RuntimeError, match="只支持 4~15 秒"):
        _run(bp.generate_video("p", None, 30, "seedance-2.5", deadline=time.time() + 60))
    assert len(gen) == 1


# ---------------------------------------------------------------- pure_api_gen

class _Poll:
    def __init__(self, status="pending", texts=None):
        self.status = status
        self.texts = texts or []
        self.failure_reasons = []
        self.vids = []
        self.urls = []
        self.source_data = None


def _fake_dola(texts):
    from protocol import dola_pure_api as real
    return types.SimpleNamespace(
        inspect_video_once=lambda *a, **k: _Poll("pending", texts),
        is_prompt_echo_text=real.is_prompt_echo_text,
        poll_has_duration_split=real.poll_has_duration_split,
        extract_wait_minutes_from_text=real.extract_wait_minutes_from_text,
        find_video_info_objects=lambda data: [],
    )


def test_30s_capped_by_upstream_fails_immediately(monkeypatch):
    import pure_api_gen
    monkeypatch.setattr(config, "ALLOW_30S_PAIR", False)
    dola = _fake_dola(["视频生成目前支持 4 到 15 秒的时长。我将按默认 5 秒为你生成。"])
    started = time.time()
    with pytest.raises(pure_api_gen.UnsupportedDurationError, match="只支持 4~15 秒"):
        pure_api_gen._wait_video_same_conversation(
            dola=dola, session=None, ctx={}, headers={}, conversation_id="c", account="acc24",
            duration=30, deadline=time.time() + 1800, poll_interval=0, on_poll=None)
    assert time.time() - started < 5


def test_first_ack_reports_upstream_note_and_wait():
    import pure_api_gen
    texts = ["视频生成目前支持 4 到 15 秒。我按你指定的 9:16 比例生成 5 秒版本。",
             "本次使用 **Dreamina Seedance 2.5** 生成，将消耗 2 个视频生成额度，预计等待 45 分钟。"]
    dola = _fake_dola(texts)
    got = []

    def on_poll(now, note=None, wait_seconds=None):
        if note:
            got.append((note, wait_seconds))

    pure_api_gen._await_first_ack(dola=dola, session=None, ctx={}, account="acc1",
                                  conversation_id="c", ack_deadline=time.time() + 5,
                                  poll_interval=0, on_poll=on_poll)
    assert got and "预计等待 45 分钟" in got[-1][0]
    assert got[-1][1] == 45 * 60


def test_old_style_on_poll_still_works():
    """只收一个参数的老回调不能被新增的原话回传弄崩。"""
    import pure_api_gen
    dola = _fake_dola(["预计等待 10 分钟"])
    ticks = []
    pure_api_gen._await_first_ack(dola=dola, session=None, ctx={}, account="acc1",
                                  conversation_id="c", ack_deadline=time.time() + 5,
                                  poll_interval=0, on_poll=lambda now: ticks.append(now))
    assert ticks


def test_30s_cap_to_15s_waits_but_not_forever(monkeypatch):
    """「按最接近的 15 秒生成」上游可能真会出片（或出两段可拼接），先等；等到宽限期仍没片才失败。"""
    import pure_api_gen
    monkeypatch.setattr(config, "ALLOW_30S_PAIR", False)
    monkeypatch.setattr(config, "CAP_30S_GRACE_SECONDS", 1)
    dola = _fake_dola(["视频生成目前支持 4 到 15 秒。我将按最接近的支持时长 15 秒生成。"])
    started = time.time()
    with pytest.raises(pure_api_gen.UnsupportedDurationError, match="1 分钟|0 分钟|仍未出片"):
        pure_api_gen._wait_video_same_conversation(
            dola=dola, session=None, ctx={}, headers={}, conversation_id="c", account="acc1",
            duration=30, deadline=time.time() + 1800, poll_interval=0, on_poll=None)
    assert 1 <= time.time() - started < 5


# ---------------------------------------------------------------- 2.1.7：连续限流暂停递增

def _expire(bp, name):
    bp._conn.execute("UPDATE accounts_meta SET abnormal_until=? WHERE name=?", (time.time() - 1, name))


def test_pause_doubles_on_consecutive_throttles_and_caps(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(config, "THROTTLE_ACCOUNT_COOLDOWN_MAX_SECONDS", 28800)
    got = []
    for _ in range(6):
        got.append(bp.note_account_throttle("acc1", "710022002"))
        _expire(bp, "acc1")
    assert got == [1800, 3600, 7200, 14400, 28800, 28800]      # 30 分 → 1 → 2 → 4 → 8 小时封顶
    assert bp._meta("acc1")["throttle_streak"] == 6


def test_reason_is_readable_and_not_duplicated(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    bp.note_account_throttle("acc1", "710022002: 当前服务访问频繁，请稍后重试")
    _expire(bp, "acc1")
    bp.note_account_throttle("acc1", "710022002: 当前服务访问频繁，请稍后重试")
    reason = bp._meta("acc1")["abnormal_reason"]
    assert reason.count("710022002") == 1
    assert "连续第 2 次" in reason and "暂停 1 小时" in reason


def test_no_escalation_while_still_paused(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    assert bp.note_account_throttle("acc1", "710022002") == 1800
    assert bp.note_account_throttle("acc1", "710022002") <= 1800    # 暂停期间再被拒不加码
    assert bp._meta("acc1")["throttle_streak"] == 1


def test_probe_ok_resets_streak(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    bp.note_account_throttle("acc1", "710022002")
    _expire(bp, "acc1")
    bp.note_account_throttle("acc1", "710022002")
    bp.probe_hello("acc1", force=True)                               # 默认假探测 = 通过
    assert bp._meta("acc1")["throttle_streak"] == 0
    assert _row(bp, "acc1")["group"] == browser_pool.GROUP_FULL
    assert bp.note_account_throttle("acc1", "710022002") == 1800    # 重新从 30 分钟算


def test_generation_success_resets_streak(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    bp.note_account_throttle("acc1", "710022002")
    _expire(bp, "acc1")

    async def fake_gen(account, *a, **k):
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake_gen
    _run(bp.generate_video("p", None, 10, "seedance-2.5", deadline=time.time() + 20))
    assert bp._meta("acc1")["throttle_streak"] == 0


def test_streak_survives_restart(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    bp.note_account_throttle("acc1", "710022002")
    _expire(bp, "acc1")
    bp2 = browser_pool.BrowserPool(accounts_dir=str(tmp_path / "accounts"),
                                   db_path=str(tmp_path / "pool.db"))
    assert bp2.note_account_throttle("acc1", "710022002") == 3600   # 重启后接着第 2 次


def test_old_streak_expires(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    bp.note_account_throttle("acc1", "710022002")
    _expire(bp, "acc1")
    bp._conn.execute("UPDATE accounts_meta SET throttle_last=? WHERE name='acc1'",
                     (time.time() - 2 * 86400,))
    assert bp.note_account_throttle("acc1", "710022002") == 1800    # 太久以前的旧账不算


def test_human_secs():
    import browser_pool
    assert browser_pool._human_secs(1800) == "30 分钟"
    assert browser_pool._human_secs(7200) == "2 小时"
    assert browser_pool._human_secs(5400) == "1.5 小时"


# ---------------------------------------------------------------- 测试生成的保护提醒

def test_test_generate_warnings(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a", "acc2": "b"})
    monkeypatch.setattr(bp, "_proxy_url_for", lambda name: "http://u:p@1.2.3.4:8080")
    assert bp.test_generate_warnings("acc2", "seedance-2.5", 5) == []          # 健康号不打扰

    bp.note_account_throttle("acc1", "710022002")
    w = bp.test_generate_warnings("acc1", "seedance-2.5", 5)
    assert any("限流" in x and "暂停中" in x for x in w)

    monkeypatch.setattr(bp, "_proxy_url_for", lambda name: "")
    assert any("没绑代理" in x for x in bp.test_generate_warnings("acc2", "seedance-2.5", 5))

    bp._set_risk("acc2", "被登出")
    assert any("风控" in x for x in bp.test_generate_warnings("acc2", "seedance-2.5", 5))


def test_test_generate_warns_when_quota_short(tmp_path, monkeypatch):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(bp, "_proxy_url_for", lambda name: "http://1.2.3.4:8080")
    bp._claim("acc1", 3)                                                        # 剩 1 点
    w = bp.test_generate_warnings("acc1", "seedance-2.5", 5)
    assert any("额度不够" in x for x in w)


def test_test_generate_endpoint_requires_confirmation(tmp_path, monkeypatch):
    import server as server_mod
    import store as store_mod
    from fastapi import HTTPException
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(bp, "_proxy_url_for", lambda name: "")                  # 没绑代理 → 会提醒
    started = []

    async def fake_test_generate(*a, **k):
        started.append(a[0])
        return {"ok": True, "video_url": "x"}

    monkeypatch.setattr(bp, "test_generate", fake_test_generate)
    server_mod.pool = bp
    server_mod.store = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server_mod, "_admin_auth", lambda key: None)

    async def go():
        body = server_mod.TestGenRequest(prompt="p", model="seedance-2.5", duration=5)
        with pytest.raises(HTTPException) as exc:
            await server_mod.admin_test_generate("acc1", body, None)
        assert exc.value.status_code == 409
        assert str(exc.value.detail).startswith("TEST_GEN_WARN:") and "没绑代理" in exc.value.detail
        body.force = True
        r = await server_mod.admin_test_generate("acc1", body, None)
        await asyncio.sleep(0.2)
        return r

    r = _run(go())
    assert r["account"] == "acc1"
    assert started == ["acc1"]                                                  # 确认后才真的发



# ---------------------------------------------------------------- 上游宣布的秒数 / 按号记录回执

def test_announced_seconds_parsing():
    import pure_api_gen
    from protocol import dola_pure_api as real
    f = lambda t: pure_api_gen._announced_seconds(real, [t])  # noqa: E731
    assert f("我将为您生成一个5秒的视频。") == 5
    assert f("视频生成目前支持 4 到 15 秒的时长。我将按默认 5 秒为你生成。") == 5
    assert f("视频生成目前支持 4 到 15 秒。我将按最接近的支持时长 15 秒生成。") == 15
    assert f("视频生成目前支持 4 到 15 秒。我按你指定的 9:16 比例生成 5 秒版本。") == 5
    assert f("视频生成目前支持 4 到 15 秒。") == 0                 # 只是范围说明
    assert f("本次使用 Seedance 2.5 生成，大约需要 1-3 分钟。") == 0


def test_30s_announced_5s_fails_even_without_cap_wording(monkeypatch):
    """线上 2026-10-14：「我将为您生成一个5秒的视频…预计等待 45 分钟」——没有「4~15 秒」字样也要立即失败。"""
    import pure_api_gen
    monkeypatch.setattr(config, "ALLOW_30S_PAIR", False)
    dola = _fake_dola(["我将为您生成一个5秒的视频。",
                       "本次使用 **Dreamina Seedance 2.5** 生成，将消耗 2 个视频生成额度，预计等待 45 分钟。"])
    with pytest.raises(pure_api_gen.UnsupportedDurationError, match="只答应生成 5 秒"):
        pure_api_gen._wait_video_same_conversation(
            dola=dola, session=None, ctx={}, headers={}, conversation_id="c", account="acc5",
            duration=30, deadline=time.time() + 1800, poll_interval=0, on_poll=None)


def test_run_task_keeps_each_accounts_note(tmp_path, monkeypatch):
    """换号时每个号的 dola 原话都留着（面板折叠显示），预计等待按当前号重置。"""
    import server as server_mod
    import store as store_mod
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    out = tmp_path / "x.mp4"
    out.write_bytes(b"fake")

    class FakePool:
        async def generate_video(self, prompt, ratio, duration, model, *, on_conversation_id=None,
                                 on_poll=None, on_account_try=None, **k):
            on_account_try("acc3", 1)
            on_poll(time.time(), note="今日视频生成免费数量用完了", wait_seconds=0)
            on_account_try("acc5", 2)
            on_poll(time.time(), note="本次使用 Seedance 2.5 生成，预计等待 45 分钟。", wait_seconds=2700)
            return {"local_path": str(out), "account": "acc5"}

    server_mod.pool = FakePool()
    server_mod.store = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    server_mod.store.create("video_n1", "seedance-2.5", "p", "9:16", 5)
    _run(server_mod._run_task("video_n1", "seedance-2.5", "p", "9:16", 5, [],
                              {"api_key_hash": None, "concurrency_limit": 0}))
    row = server_mod.store.get("video_n1")
    notes = __import__("json").loads(row["upstream_notes"])
    assert [n["account"] for n in notes] == ["acc3", "acc5"]
    assert "免费数量用完" in notes[0]["note"] and "45 分钟" in notes[1]["note"]
    assert row["upstream_wait"] == 2700
    assert row["attempt_started_at"] and row["attempt_started_at"] >= row["started_at"]


def test_test_generate_rejects_15s_on_seedance_20(tmp_path, monkeypatch):
    """2.0-fast 网页只给 4～10 秒，纯协议直接请求 15 秒被 dola 拒绝（2026-10-30 实测）：测试生成也要先拦下。"""
    import server as server_mod
    import store as store_mod
    from fastapi import HTTPException
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    started = []

    async def fake_test_generate(*a, **k):
        started.append(a[0])
        return {"ok": True}

    monkeypatch.setattr(bp, "test_generate", fake_test_generate)
    server_mod.pool = bp
    server_mod.store = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server_mod, "_admin_auth", lambda key: None)

    async def go():
        body = server_mod.TestGenRequest(prompt="p", model="seedance-2.0-fast", duration=15, force=True)
        with pytest.raises(HTTPException) as exc:
            await server_mod.admin_test_generate("acc1", body, None)
        assert exc.value.status_code == 422 and "5/10" in exc.value.detail and "15" in exc.value.detail
        ok = server_mod.TestGenRequest(prompt="p", model="seedance-2.0-fast", duration=10, force=True)
        await server_mod.admin_test_generate("acc1", ok, None)
        await asyncio.sleep(0.2)

    _run(go())
    assert started == ["acc1"]                                                   # 只有 10 秒那条真的发了
