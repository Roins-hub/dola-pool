"""2.3.0：豆包（doubao.com）号接入号池。

话术样本均为 2026-10-18 实测原文（会员号 + 免费号各一个）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import sites

CONFIRM = ("我将为你生成一条橘猫在窗台上晒太阳的治愈系短视频，请先确认参数：\n\n**视频生成参数确认**\n\n"
           "- **模型：** Seedance 2.0 Mini\n- **时长：** 5 秒\n- **比例：** 16:9 横屏\n\n确认后我再开始生成视频。")
PAID = ("我将为你生成一条 5 秒治愈系写实短视频。\n今日视频生成免费额度已用完，本次将消耗付费额度。是否继续生成？")
PAID_ACCEPTED = ("本次使用 **Seedance 2.0 Mini** 生成，大约需要 1-3 分钟。视频生成好后，我会主动发送给你。"
                 "今日视频生成免费额度已用完，本次将消耗付费额度，并使用优先生成通道。")
FREE_ACCEPTED = ("本次使用 **Seedance 2.0 Mini** 生成，大约需要 1-3 分钟。视频生成好后，我会主动发送给你。"
                 "本次生成将消耗每日免费额度。")
LOCKED = ("我将为你生成一条 5 秒温馨写实短视频。\n当前无法使用 Seedance 2.5 模型。可改用 **Seedance 2.0 Mini** "
          "继续生成，效果同样温馨自然。  是否改用 Seedance 2.0 Mini 生成？")


@pytest.fixture(autouse=True)
def _stubs():
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


# ---------------------------------------------------------------- 站点 / 模型识别

def test_doubao_model_names_are_exact():
    assert sites.is_doubao_model("doubao-seedance-2.0-mini")
    assert sites.is_doubao_model("DOUBAO_SEEDANCE_2.5")
    assert sites.doubao_spec("doubao-seedance-2.5")[:3] == ("seedance_v2.5", 30, True)
    assert sites.doubao_spec("doubao-seedance-2.0-fast")[:3] == ("seedance_v2.0", 15, False)
    # new-api Doubao 任务插件的官方名照旧走 dola（README 里的老接法）
    assert not sites.is_doubao_model("doubao-seedance-2-0-260128")
    assert not sites.is_doubao_model("doubao-seedance-2-5-260628")
    assert not sites.is_doubao_model("seedance-2.5")
    assert sites.site_for_model("seedance-2.0") == sites.DOLA


def test_site_of_cookies_and_state_file(tmp_path):
    assert sites.site_of_cookies([{"domain": ".doubao.com", "name": "sessionid"}]) == sites.DOUBAO
    assert sites.site_of_cookies([{"domain": "www.dola.com", "name": "sessionid"}]) == sites.DOLA
    p = tmp_path / "cookie_state.json"
    p.write_text(json.dumps({"cookies_list": [{"name": "sessionid", "domain": ".doubao.com"}]}))
    assert sites.site_of_state_file(p) == sites.DOUBAO
    assert sites.site_of_state_file(tmp_path / "missing.json") == sites.DOLA


def test_cookie_import_keeps_doubao_cookies():
    import cookie_import
    raw = json.dumps([{"domain": ".doubao.com", "name": "sessionid", "value": "s1", "path": "/"},
                      {"domain": ".example.com", "name": "x", "value": "y"}])
    states = cookie_import.parse_cookie_header_text(raw)
    assert len(states) == 1 and [c["name"] for c in states[0]["cookies_list"]] == ["sessionid"]
    header = cookie_import.parse_cookie_header_text("sessionid=abc; sid_tt=abc", site="doubao")
    assert header[0]["cookies_list"][0]["domain"] == ".doubao.com"
    assert cookie_import.parse_cookie_header_text("sessionid=abc")[0]["cookies_list"][0]["domain"] == ".dola.com"


# ---------------------------------------------------------------- 协议：同一份代码两个站点

def test_doubao_protocol_instance_is_isolated():
    from protocol import dola_pure_api as dola
    from protocol import doubao_site
    db = doubao_site.load()
    assert db is not dola and db.BASE == "https://www.doubao.com" and dola.BASE == "https://www.dola.com"
    assert db.AID == "497858" and dola.AID == "495671"
    ctx = {"fp": "verify_x"}
    payload, _, _ = db.build_payload([], "一只橘猫", ctx, 7, "seedance_v2.0_mini", "16:9")
    text = payload["messages"][-1]["content_block"][0]["content"]["text_block"]["text"]
    assert text == "生成视频：一只橘猫，16:9，7s"
    ability = json.loads(payload["chat_ability"]["ability_param"])
    assert ability["model"] == "seedance_v2.0_mini" and ability["duration"] == 7
    assert ability["ratio"] == "16:9"
    assert ability["input_box_content"]["user_input_content"] == "一只橘猫"
    # 比例「自动」不写进文字
    payload, _, _ = db.build_payload([], "一只橘猫", ctx, 5, "seedance_v2.0_mini", "auto")
    assert payload["messages"][-1]["content_block"][0]["content"]["text_block"]["text"] == "生成视频：一只橘猫，5s"
    # dola 不受影响：30 秒照旧强制 2.5，提示词照旧带比例
    payload, _, _ = dola.build_payload([], "一只橘猫", ctx, 30, "seedance_v2.0", "16:9")
    assert json.loads(payload["chat_ability"]["ability_param"])["model"] == "seedance_v2.5"
    assert payload["messages"][-1]["content_block"][0]["content"]["text_block"]["text"] == "生成视频：一只橘猫，16:9"


# ---------------------------------------------------------------- 话术识别

def test_doubao_text_classifiers():
    import doubao_gen as g
    assert g.looks_like_confirm_request(CONFIRM) and not g.looks_like_accepted(CONFIRM)
    assert g.looks_like_paid_prompt(PAID) and not g.looks_like_accepted(PAID)
    assert not g.looks_like_paid_prompt(PAID_ACCEPTED) and g.looks_like_accepted(PAID_ACCEPTED)
    assert g.looks_like_accepted(FREE_ACCEPTED) and not g.looks_like_quota_exhausted(FREE_ACCEPTED)
    assert g.looks_like_model_locked(LOCKED)
    assert not g.looks_like_quota_exhausted(PAID)
    assert g.confirm_mismatch(CONFIRM, duration=5, model_display="Seedance 2.0 Mini") == ""
    assert "15" in g.confirm_mismatch(CONFIRM.replace("5 秒", "15 秒"), duration=5, model_display="Seedance 2.0 Mini")
    assert "模型" in g.confirm_mismatch(CONFIRM, duration=5, model_display="Seedance 2.5")
    # 「Seedance 2.0」（标准版）不能被「Seedance 2.0 Mini」误判成一致
    assert g.confirm_mismatch(CONFIRM, duration=5, model_display="Seedance 2.0")


# ---------------------------------------------------------------- 出片流程（假上游）

class _FakeDP:
    """按剧本吐会话：bot 的每条消息在我们回过 N 次之后才出现。"""

    def __init__(self, script, *, vid_after: int | None = None):
        from protocol import doubao_site
        self._real = doubao_site.load()
        self.script = script               # [(需要先回几次, 文本, buttons)]
        self.vid_after = vid_after
        self.replies: list[str] = []
        self.BASE = self._real.BASE
        self.DolaNoDataError = self._real.DolaNoDataError
        self.downloaded = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def submit_chat_completion(self, *a, **k):
        if k.get("video_ability"):
            return types.SimpleNamespace(error="", error_code="", conversation_id="conv1")
        self.replies.append(k.get("prompt"))
        return types.SimpleNamespace(error="", error_code="", conversation_id="conv1")

    def fetch_single_chain(self, *a, **k):
        msgs = [{"message_id": "u1", "index_in_conv": 1, "user_type": 1,
                 "content_block": [{"content": {"text_block": {"text": "生成视频：x，5s"}}, "is_finish": True}]}]
        idx = 3
        for need, text, buttons in self.script:
            if len(self.replies) < need:
                break
            blocks = [{"content": {"text_block": {"text": text}}, "is_finish": True}]
            for b in buttons or []:
                blocks.append({"content": {"button_block": b}, "is_finish": True})
            msgs.append({"message_id": f"b{idx}", "index_in_conv": idx, "user_type": 2, "content_block": blocks})
            idx += 2
        if self.vid_after is not None and len(self.replies) >= self.vid_after:
            msgs.append({"message_id": "v", "index_in_conv": idx, "user_type": 2, "content_block": [
                {"content": {"creation_block": {"creations": [{"video": {"vid": "v0abc"}}]}}, "is_finish": True}]})
        return {"downlink_body": {"pull_singe_chain_downlink_body": {"messages": msgs}}}

    def find_latest_section_id(self, chain):
        return "s1"

    def find_latest_message_index(self, chain):
        return 9

    def dola_api_post_json(self, session, ctx, endpoint, body, **k):
        assert endpoint == "/creativity/resource/get_without_watermark"
        return {"code": 0, "data": {"download_video": {"v0abc": {"download_url": "https://cdn/x.mp4", "duration": 5.05}}}}

    def download_video(self, url, path, **k):
        Path(path).write_bytes(b"mp4")
        self.downloaded.append(url)
        return Path(path)


def _gen(monkeypatch, tmp_path, dp, model="doubao-seedance-2.0-mini", duration=5):
    import doubao_gen
    state = tmp_path / "cookie_state.json"
    state.write_text(json.dumps({"cookies_list": [{"name": "sessionid", "value": "s", "domain": ".doubao.com"}],
                                 "cookies": {"sessionid": {"value": "s", "domain": ".doubao.com", "path": "/"}}}))
    monkeypatch.setattr(doubao_gen, "_site", lambda: dp)
    monkeypatch.setattr(doubao_gen, "_session", lambda *_a, **_k: (types.SimpleNamespace(close=lambda: None), {}, {}))
    monkeypatch.setattr(doubao_gen.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(doubao_gen, "_media_duration", lambda _p: 5.05)
    return doubao_gen.generate_doubao_video(
        account="d1", prompt="x", ratio="16:9", duration=duration, model=model,
        cookie_state_path=state, output_dir=tmp_path)


def test_flow_confirm_then_paid_then_video(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOUBAO_ALLOW_PAID", True)
    dp = _FakeDP([(0, CONFIRM, []), (1, PAID, [{"display_text": "确认生成", "scene": "gen_video_by_downgraded_model"}]),
                  (2, PAID_ACCEPTED, [])], vid_after=2)
    res = _gen(monkeypatch, tmp_path, dp)
    assert dp.replies == ["确认生成", "确认生成"]
    assert res["doubao_paid"] is True and res["unwatermarked"] is True and res["site"] == "doubao"
    assert dp.downloaded == ["https://cdn/x.mp4"]


def test_flow_paid_disabled_raises_quota_without_reply(monkeypatch, tmp_path):
    import doubao_gen
    monkeypatch.setattr(config, "DOUBAO_ALLOW_PAID", False)
    dp = _FakeDP([(0, CONFIRM, []), (1, PAID, [])])
    with pytest.raises(doubao_gen.DoubaoQuotaError):
        _gen(monkeypatch, tmp_path, dp)
    assert dp.replies == ["确认生成"]          # 付费追问没回


def test_flow_free_account_model_locked_never_confirms_downgrade(monkeypatch, tmp_path):
    import doubao_gen
    confirm25 = CONFIRM.replace("Seedance 2.0 Mini", "Seedance 2.5")
    dp = _FakeDP([(0, confirm25, []), (1, LOCKED, [])])
    with pytest.raises(doubao_gen.DoubaoModelLockedError):
        _gen(monkeypatch, tmp_path, dp, model="doubao-seedance-2.5")
    assert dp.replies == ["确认生成"]          # 「是否改用 Mini」没回


def test_flow_duration_changed_by_upstream_is_not_confirmed(monkeypatch, tmp_path):
    import pure_api_gen
    dp = _FakeDP([(0, CONFIRM.replace("5 秒", "15 秒"), [])])
    with pytest.raises(pure_api_gen.UnsupportedDurationError):
        _gen(monkeypatch, tmp_path, dp)
    assert dp.replies == []


def test_duration_range_checked_before_submit(monkeypatch, tmp_path):
    import pure_api_gen
    with pytest.raises(pure_api_gen.UnsupportedDurationError):
        _gen(monkeypatch, tmp_path, _FakeDP([]), model="doubao-seedance-2.0-mini", duration=20)


# ---------------------------------------------------------------- 号池派单

def _pool(tmp_path, monkeypatch, dola=("acc1",), doubao=("db1", "db2")):
    import browser_pool
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 4)
    monkeypatch.setattr(browser_pool, "DAILY_LIMIT", 4)
    monkeypatch.setattr(config, "HELLO_PROBE_SUBMIT_GAP_SECONDS", 0)
    accounts_dir = tmp_path / "accounts"
    for name in list(dola) + list(doubao):
        (accounts_dir / name).mkdir(parents=True, exist_ok=True)
    for name in doubao:
        (accounts_dir / name / "cookie_state.json").write_text(json.dumps(
            {"cookies_list": [{"name": "sessionid", "value": name, "domain": ".doubao.com"}]}))
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir), db_path=str(tmp_path / "pool.db"))
    for name in list(dola) + list(doubao):
        bp.set_login_status(name, True)

    async def ok_probe(name, **_k):
        return {"ok": True, "status": "ok", "probed": False}

    bp.probe_hello_async = ok_probe
    return browser_pool, bp


def test_list_accounts_marks_doubao_and_groups(tmp_path, monkeypatch):
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    rows = {a["name"]: a for a in bp.list_accounts()}
    assert rows["db1"]["site"] == "doubao" and rows["acc1"]["site"] == "dola"
    assert rows["db1"]["limit"] == config.DOUBAO_DAILY_LIMIT
    assert rows["db1"]["group"] == browser_pool.GROUP_FULL
    bp._claim("db1", 3)
    rows = {a["name"]: a for a in bp.list_accounts()}
    assert rows["db1"]["group"] == browser_pool.GROUP_FULL      # 豆包号不按点数降到「半额」


def test_dispatch_routes_by_site_and_tier(tmp_path, monkeypatch):
    import doubao_gen
    browser_pool, bp = _pool(tmp_path, monkeypatch)
    calls = []

    async def fake(account, prompt, ratio, duration, model, **_k):
        calls.append((account, model))
        if account == "db1" and model == "doubao-seedance-2.5":
            raise doubao_gen.DoubaoModelLockedError("db1 用不了 Seedance 2.5")
        return {"account": account, "local_path": "x.mp4", "conversation_id": "c"}

    bp._generate_effective = fake
    res = _run(bp.generate_video("p", "16:9", 5, "doubao-seedance-2.0-mini", max_attempts=3))
    assert res["account"] in ("db1", "db2") and calls[-1][1] == "doubao-seedance-2.0-mini"
    calls.clear()
    res = _run(bp.generate_video("p", "16:9", 30, "doubao-seedance-2.5", max_attempts=3))
    assert res["account"] == "db2"
    assert [c[0] for c in calls] == ["db1", "db2"]
    rows = {a["name"]: a for a in bp.list_accounts()}
    assert rows["db1"]["tier"] == "free" and rows["db2"]["tier"] == "member"
    # 记住了 db1 是免费号：下一单 2.5 不再派给它
    calls.clear()
    _run(bp.generate_video("p", "16:9", 10, "doubao-seedance-2.5", max_attempts=3))
    assert [c[0] for c in calls] == ["db2"]
    # dola 模型绝不派给豆包号
    calls.clear()
    res = _run(bp.generate_video("p", "16:9", 5, "seedance-2.5", max_attempts=3))
    assert res["account"] == "acc1"


def test_doubao_model_without_doubao_accounts_fails_fast(tmp_path, monkeypatch):
    _browser_pool, bp = _pool(tmp_path, monkeypatch, doubao=())
    with pytest.raises(RuntimeError, match="没有豆包号"):
        _run(bp.generate_video("p", "16:9", 5, "doubao-seedance-2.0-mini", max_attempts=1))


def test_doubao_quota_error_marks_daily_limit(tmp_path, monkeypatch):
    import doubao_gen
    _browser_pool, bp = _pool(tmp_path, monkeypatch, doubao=("db1", "db2"))

    async def fake(account, prompt, ratio, duration, model, **_k):
        if account == "db1":
            raise doubao_gen.DoubaoQuotaError("db1 豆包今日免费额度已用完")
        return {"account": account, "local_path": "x.mp4", "conversation_id": "c"}

    bp._generate_effective = fake
    res = _run(bp.generate_video("p", "16:9", 5, "doubao-seedance-2.0-fast", max_attempts=3))
    assert res["account"] == "db2"
    rows = {a["name"]: a for a in bp.list_accounts()}
    assert rows["db1"]["rate_limited"]
