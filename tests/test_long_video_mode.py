"""2.1.8：dola 单条最长 15 秒后的 30 秒处理（DOLA_30S_MODE）。

线上：最后一次原生 30 秒成片是 2026-10-11；之后 30 秒请求上游回「支持 4 到 15 秒 / 按默认 5 秒 /
要不要拆两段」（issue #4：官网上选「拆两段」也直接报错）。旧的两段兜底按 30 秒提交、指望上游自己
压到 15 秒，现在也走不通。
"""
from __future__ import annotations

import asyncio
import importlib
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


# ---------------------------------------------------------------- 配置

def test_mode_default_and_legacy_flag(monkeypatch):
    monkeypatch.delenv("DOLA_30S_MODE", raising=False)
    monkeypatch.delenv("DOLA_ALLOW_30S_PAIR", raising=False)
    assert importlib.reload(config).LONG_VIDEO_MODE == "reject"
    monkeypatch.setenv("DOLA_ALLOW_30S_PAIR", "1")
    assert importlib.reload(config).LONG_VIDEO_MODE == "pair"      # 兼容旧变量
    monkeypatch.setenv("DOLA_30S_MODE", "native")
    assert importlib.reload(config).LONG_VIDEO_MODE == "native"    # 显式设置优先
    monkeypatch.delenv("DOLA_30S_MODE", raising=False)
    monkeypatch.delenv("DOLA_ALLOW_30S_PAIR", raising=False)
    importlib.reload(config)


# ---------------------------------------------------------------- 受理：reject 模式直接拒

def test_reject_mode_refuses_30s_at_submit(monkeypatch):
    import server
    from fastapi import HTTPException
    monkeypatch.setattr(config, "LONG_VIDEO_MODE", "reject")
    with pytest.raises(HTTPException) as exc:
        server._reject_unsupported_long(30)
    assert exc.value.status_code == 422
    assert "最长 15 秒" in exc.value.detail and "DOLA_30S_MODE=pair" in exc.value.detail
    server._reject_unsupported_long(15)                  # 15 秒照常
    server._reject_unsupported_long(5)


@pytest.mark.parametrize("mode", ["pair", "native"])
def test_other_modes_accept_30s(monkeypatch, mode):
    import server
    monkeypatch.setattr(config, "LONG_VIDEO_MODE", mode)
    server._reject_unsupported_long(30)                  # 不抛


def test_seedance_25_accepts_15s():
    import server
    assert server._resolve_model_for_duration("seedance-2.5", 15) == "seedance-2.5"


# ---------------------------------------------------------------- 号池：pair 模式算两条额度、只用满额纯 API 号

def _pool(tmp_path, monkeypatch, names=("acc1", "acc2")):
    import browser_pool
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 4)
    monkeypatch.setattr(browser_pool, "DAILY_LIMIT", 4)
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    monkeypatch.setattr(config, "HELLO_PROBE_SUBMIT_GAP_SECONDS", 0)
    monkeypatch.setattr(config, "V20_WAIT_SECONDS", 0)
    accounts_dir = tmp_path / "accounts"
    for name in names:
        d = accounts_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "cookie_state.json").write_text('{"source":"cookie"}', encoding="utf-8")
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir), db_path=str(tmp_path / "pool.db"))
    for name in names:
        bp.set_login_status(name, True)
        bp.set_source(name, "cookie")
    monkeypatch.setattr(browser_pool, "hello_probe", lambda *a, **k: {
        "ok": True, "status": "ok", "login_ok": True, "replied": True, "reason": "", "reply": "hi",
        "elapsed": 1.0})
    return browser_pool, bp


def test_pair_job_costs_two_videos_and_needs_full_account(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "LONG_VIDEO_MODE", "pair")
    monkeypatch.setitem(config.DURATION_COSTS, "seedance-2.5", {5: 2, 10: 2, 15: 2})
    assert bp._duration_cost("seedance-2.5", 30) == 4
    assert bp._duration_cost("seedance-2.5", 15) == 2
    assert bp._allowed_quota_groups("seedance-2.5", 30) == (browser_pool.GROUP_FULL,)
    monkeypatch.setattr(config, "LONG_VIDEO_MODE", "reject")
    assert bp._duration_cost("seedance-2.5", 30) == config.credit_cost("seedance-2.5", 30)


def test_pair_job_skips_half_account(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "LONG_VIDEO_MODE", "pair")
    monkeypatch.setitem(config.DURATION_COSTS, "seedance-2.5", {5: 2, 10: 2, 15: 2})
    bp._claim("acc1", 2)                                  # acc1 只剩 2 点，不够两段
    gen = []

    async def fake_gen(account, *a, **k):
        gen.append(account)
        return {"account": account, "local_path": "x.mp4"}

    bp._generate_effective = fake_gen
    result = _run(bp.generate_video("p", None, 30, "seedance-2.5", deadline=time.time() + 20))
    assert result["account"] == "acc2" and gen == ["acc2"]


# ---------------------------------------------------------------- 出片：每段明确请求 15 秒

class _Submit:
    def __init__(self, cid):
        self.error = None
        self.error_code = None
        self.conversation_id = cid


class _PairDola:
    BASE = "https://www.dola.com"

    def __init__(self):
        self.durations = []
        self.prompts = []

    def submit_chat_completion(self, session, ctx, headers, *, images, audios, prompt, duration,
                               model, ratio, timeout_sec, video_ability):
        self.durations.append(duration)
        self.prompts.append(prompt)
        return _Submit(f"c{len(self.durations)}")

    def get_video_result(self, session, ctx, cid, **k):
        return {"status": "succeeded", "download_url": f"https://cdn/{cid}.mp4", "texts": []}

    def download_video(self, url, path, **k):
        Path(path).write_bytes(b"part")
        return Path(path)

    def is_prompt_echo_text(self, t):
        return False


def test_pair_requests_two_explicit_15s_segments(tmp_path, monkeypatch):
    import pure_api_gen
    dola = _PairDola()
    merged = []
    monkeypatch.setattr(pure_api_gen, "_concat_videos",
                        lambda parts, out: merged.append(list(parts)) or Path(out))
    notes = []
    out = pure_api_gen._generate_capped_pair(
        dola=dola, session=None, ctx={}, headers={}, account="acc1", prompt="一只猫",
        ratio="16:9", model="seedance_v2.5", output_path=tmp_path / "o.mp4",
        remove_watermark=False, proxy="", timeout=60, poll_interval=0,
        on_poll=lambda now, note=None, wait_seconds=None: notes.append(note),
        segment_duration=15)
    assert dola.durations == [15, 15]
    assert "前半段" in dola.prompts[0] and "后半段" in dola.prompts[1]
    assert len(merged) == 1 and len(merged[0]) == 2
    assert any(n and "第 1/2 段" in n for n in notes)
    assert out == tmp_path / "o.mp4"
