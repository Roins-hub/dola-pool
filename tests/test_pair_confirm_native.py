"""2.2.2：两段拼接移植 stable-dola-pool 的经验。

- dola 对「请求 30 秒」有时反问「只支持 4~15 秒，是否按 15 秒生成？请回复：确认生成」，
  不回话会一直等 → 同会话回「确认生成」；
- 回执没说压时长 → 先等第 1 段，原样出了 30 秒就直接交付（只扣一次额度，号池只记一半）；
- pair 单只剩浏览器登录号时直接失败（以前静默空转到任务时限）。
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


ACCEPTED = "本次使用 Dreamina Seedance 2.5 生成，将消耗 2 个视频生成额度，预计等待 1 小时。今日剩余 2 个视频生成额度。"
CAPPED = "视频生成当前支持 4 到 15 秒的时长，我将按最接近的支持时长 15 秒生成。"
ASKING = "视频生成支持 4–15 秒，你要的 30 秒超出支持范围。是否按 15 秒生成？请回复：确认生成"


class _Submit:
    def __init__(self, cid):
        self.error = None
        self.error_code = None
        self.conversation_id = cid


class _Poll:
    def __init__(self, texts):
        self.status = "pending"
        self.texts = texts
        self.failure_reasons = []
        self.urls = []
        self.vids = []


class _Dola:
    """按会话返回回执；回了「确认生成」的会话改为受理。用真实的话术识别函数。"""
    BASE = "https://www.dola.com"

    def __init__(self, first_texts):
        from protocol import dola_pure_api as real
        self.real = real
        self.first_texts = first_texts
        self.video_submits = []
        self.replies = []
        self.downloaded = []

    def is_prompt_echo_text(self, t):
        return False

    def looks_like_generation_accepted(self, t):
        return self.real.looks_like_generation_accepted(t)

    def looks_like_duration_confirm(self, t):
        return self.real.looks_like_duration_confirm(t)

    def looks_like_duration_capped(self, t):
        return self.real.looks_like_duration_capped(t)

    def extract_wait_minutes_from_text(self, t):
        return ""

    def submit_chat_completion(self, session, ctx, headers, *, images, audios, prompt, duration,
                               model, ratio, timeout_sec, video_ability, conversation_id="",
                               section_id="", last_message_index=None):
        if not video_ability:
            self.replies.append((conversation_id, prompt))
            return _Submit(conversation_id)
        self.video_submits.append(duration)
        return _Submit(f"c{len(self.video_submits)}")

    def fetch_single_chain(self, session, ctx, cid):
        return {}

    def find_latest_section_id(self, chain):
        return ""

    def find_latest_message_index(self, chain):
        return None

    def inspect_video_once(self, session, ctx, cid):
        if any(c == cid for c, _ in self.replies):
            return _Poll(list(self.first_texts) + ["确认生成", ACCEPTED])
        return _Poll(list(self.first_texts))

    def get_video_result(self, session, ctx, cid, **k):
        return {"status": "succeeded", "download_url": f"https://cdn/{cid}.mp4", "texts": []}

    def download_video(self, url, path, **k):
        self.downloaded.append(url)
        Path(path).write_bytes(b"part")
        return Path(path)


def _pair(pure_api_gen, dola, tmp_path, stats=None):
    return pure_api_gen._generate_capped_pair(
        dola=dola, session=None, ctx={}, headers={}, account="acc1", prompt="一只猫",
        ratio="16:9", model="seedance_v2.5", output_path=tmp_path / "o.mp4",
        remove_watermark=False, proxy="", timeout=60, poll_interval=0, stats=stats)


def test_screenshot_reply_is_accepted_not_a_question():
    """10-14 网页截图的回执：已受理（扣 2 点）+ 说了压到 15 秒 → 不回话、按压时长处理。"""
    import pure_api_gen
    from protocol import dola_pure_api as real
    assert not pure_api_gen._awaiting_confirmation(real, [CAPPED, ACCEPTED])
    assert real.looks_like_duration_capped(CAPPED)
    assert pure_api_gen._awaiting_confirmation(real, [ASKING])


def test_english_cap_wording_detected():
    from protocol import dola_pure_api as real
    text = ("视频 generation currently supports durations from 4 to 15 seconds. I can generate it at "
            "the nearest supported duration of 15 seconds. Do you want me to proceed with a 15-second version?")
    assert real.looks_like_duration_capped(text)


def test_question_gets_confirm_reply_in_same_conversation(tmp_path, monkeypatch):
    import pure_api_gen
    dola = _Dola([ASKING])
    merged = []
    monkeypatch.setattr(pure_api_gen, "_concat_videos",
                        lambda parts, out: merged.append(list(parts)) or Path(out))
    monkeypatch.setattr(pure_api_gen, "_media_duration", lambda p: 15.04)
    stats = {}
    _pair(pure_api_gen, dola, tmp_path, stats)
    assert dola.replies == [("c1", "确认生成"), ("c2", "确认生成")]
    assert dola.video_submits == [30, 30]
    assert len(merged) == 1 and stats == {"segments": 2, "native": False}


def test_accepted_capped_reply_does_not_get_confirm(tmp_path, monkeypatch):
    """已经播报扣点的回执绝不能再回「确认生成」（可能被当成新请求再扣一次）。"""
    import pure_api_gen
    dola = _Dola([CAPPED, ACCEPTED])
    monkeypatch.setattr(pure_api_gen, "_concat_videos", lambda parts, out: Path(out))
    monkeypatch.setattr(pure_api_gen, "_media_duration", lambda p: 15.04)
    _pair(pure_api_gen, dola, tmp_path)
    assert dola.replies == [] and dola.video_submits == [30, 30]


def test_native_30s_first_segment_delivered_alone(tmp_path, monkeypatch):
    import pure_api_gen
    dola = _Dola([ACCEPTED])                       # 没说压时长
    monkeypatch.setattr(pure_api_gen, "_concat_videos",
                        lambda parts, out: (_ for _ in ()).throw(AssertionError("不该拼接")))
    monkeypatch.setattr(pure_api_gen, "_media_duration", lambda p: 30.04)
    stats = {}
    out = _pair(pure_api_gen, dola, tmp_path, stats)
    assert dola.video_submits == [30]              # 只提交了一次 = 只扣一次
    assert out == tmp_path / "o.mp4" and out.exists()
    assert stats == {"segments": 1, "native": True}


def test_silent_cap_then_second_segment(tmp_path, monkeypatch):
    """回执没说压时长、落地却只有 15 秒：再补第 2 段并拼接。"""
    import pure_api_gen
    dola = _Dola([ACCEPTED])
    merged = []
    monkeypatch.setattr(pure_api_gen, "_concat_videos",
                        lambda parts, out: merged.append(list(parts)) or Path(out))
    monkeypatch.setattr(pure_api_gen, "_media_duration", lambda p: 15.04)
    stats = {}
    _pair(pure_api_gen, dola, tmp_path, stats)
    assert dola.video_submits == [30, 30] and len(merged[0]) == 2
    assert stats["segments"] == 2


def test_silent_cap_without_credits_for_second_segment(tmp_path, monkeypatch):
    import pure_api_gen
    dola = _Dola(["本次使用 Seedance 2.5 生成，将消耗 2 个视频生成额度，预计等待 1 小时。今日剩余 0 个视频生成额度。"])
    monkeypatch.setattr(pure_api_gen, "_media_duration", lambda p: 15.04)
    credits = pure_api_gen._CreditTracker()
    with pytest.raises(pure_api_gen.PureApiError, match="补第 2 段要 2 点"):
        pure_api_gen._generate_capped_pair(
            dola=dola, session=None, ctx={}, headers={}, account="acc1", prompt="p",
            ratio=None, model="seedance_v2.5", output_path=tmp_path / "o.mp4",
            remove_watermark=False, proxy="", timeout=60, poll_interval=0, credits=credits)
    assert dola.video_submits == [30]


# ---------------------------------------------------------------- 号池

def _pool(tmp_path, monkeypatch, cookie=("acc1",), browser=("acc7",)):
    import browser_pool
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 4)
    monkeypatch.setattr(browser_pool, "DAILY_LIMIT", 4)
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    monkeypatch.setattr(config, "HELLO_PROBE_SUBMIT_GAP_SECONDS", 0)
    monkeypatch.setattr(config, "V20_WAIT_SECONDS", 0)
    monkeypatch.setattr(config, "LONG_VIDEO_MODE", "pair")
    accounts_dir = tmp_path / "accounts"
    for name in cookie + browser:
        d = accounts_dir / name
        d.mkdir(parents=True, exist_ok=True)
        if name in cookie:
            (d / "cookie_state.json").write_text('{"source":"cookie"}', encoding="utf-8")
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir), db_path=str(tmp_path / "pool.db"))
    for name in cookie + browser:
        bp.set_login_status(name, True)
        if name in cookie:
            bp.set_source(name, "cookie")
    monkeypatch.setattr(browser_pool, "hello_probe", lambda *a, **k: {
        "ok": True, "status": "ok", "login_ok": True, "replied": True, "reason": "", "reply": "hi",
        "elapsed": 1.0})
    return browser_pool, bp


def _row(bp, name):
    return next(a for a in bp.list_accounts() if a["name"] == name)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_pair_with_only_browser_accounts_fails_fast(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    bp._claim("acc1", 3)                           # 唯一的 Cookie 号只剩 1 点

    async def fake_gen(account, *a, **k):
        raise AssertionError("不该派发")

    bp._generate_effective = fake_gen
    started = time.time()
    with pytest.raises(RuntimeError, match="只能用 Cookie 导入的号"):
        _run(bp.generate_video("p", None, 30, "seedance-2.5", deadline=time.time() + 120,
                               max_attempts=0))
    assert time.time() - started < 5


def test_native_30s_charges_one_segment(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch, browser=())

    async def fake_gen(account, *a, **k):
        return {"account": account, "local_path": "x.mp4", "pair_segments_charged": 1}

    bp._generate_effective = fake_gen
    r = _run(bp.generate_video("p", None, 30, "seedance-2.5", deadline=time.time() + 20))
    assert r["account"] == "acc1"
    assert _row(bp, "acc1")["remaining"] == 2      # 原样 30 秒只扣 2 点，不是 4
