"""豆包创作接管、素材校验和账号身份的离线回归，不访问上游或提交视频。"""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import config
import doubao_gen as g
from generation_errors import UpstreamReplyError, VideoSkillUnavailableError, WorkVideoMismatchError
from test_doubao import FREE_ACCEPTED, _FakeDP, _gen, _pool, _run, _stubs


PLAIN = {"agent_name": "豆包", "agent_intention": "seed_main"}
CREATOR = {"agent_name": "Agent-Creation", "use_creation": "1"}
IMAGE_ERROR = "参考图资源当前无法读取，我先按文字设定直接生成，不再引用失效素材。"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("离线回归禁止真实网络请求")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def test_cookie_fingerprint_and_saved_device_survive_new_sessions_without_cross_site_leak(tmp_path):
    from protocol import dola_pure_api as dola, doubao_site
    db = doubao_site.load()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies_list": [
        {"name": "sessionid", "value": "test", "domain": ".doubao.com"},
        {"name": "s_v_web_id", "value": "verify_doubao", "domain": ".doubao.com"},
        {"name": "s_v_web_id", "value": "verify_other", "domain": ".dola.com"}],
        "ctx": {"fp": "outdated", "web_id": "web", "tea_uuid": "web", "device_id": "device"}}), encoding="utf-8")
    for _ in range(2):
        session, ctx, headers = db.setup_session(login_pure=True, login_state_file=state)
        try:
            assert ctx["fp"] == "verify_doubao"
            assert ctx["web_id"] == ctx["tea_uuid"] == "web" and ctx["device_id"] == "device"
            assert session.cookies.get("s_v_web_id", domain=".doubao.com") == ctx["fp"]
            assert "157" in headers["User-Agent"] and "Microsoft Edge" in db.ajax_headers()["sec-ch-ua"]
            assert db.SIGNER_ENV["BDMS_USER_AGENT"] == headers["User-Agent"]
            assert db.base_params(ctx)["tz_name"] == "Asia/Shanghai"
        finally:
            session.close()
    ctx = dola.fresh_ctx("JP", "test")
    dola.apply_state_to_ctx(ctx, {"cookies_list": [
        {"name": "s_v_web_id", "value": "other", "domain": ".doubao.com"}]})
    assert ctx["fp"] != "other" and "tz_name" not in dola.base_params(ctx)
    assert "Edg/" not in dola.UA


def test_bootstrap_uses_official_identity_and_preserves_imported_state(tmp_path, monkeypatch):
    from protocol import doubao_site
    dp = doubao_site.load()
    state = tmp_path / "state.json"
    original = {"cookies_list": [{"name": "sessionid", "value": "test", "domain": ".doubao.com"},
                                {"name": "s_v_web_id", "value": "verify_test", "domain": ".doubao.com"}],
                "doubao_account_type": "personal", "storage": {"keep": "value"}, "ctx": {"keep": "value"}}
    state.write_text(json.dumps(original), encoding="utf-8")
    calls = []
    launch = {"code": 0, "data": {"account_type": "personal"}}
    def post(session, ctx, endpoint, body, **kwargs):
        calls.append((endpoint, dict(ctx)))
        return {"code": 0, "data": {"web_id": "official_web", "device_id": "official_device"}} if "anon_id" in endpoint else launch
    monkeypatch.setattr(dp, "dola_api_post_json", post)
    for _ in range(2):
        session, ctx, _ = g._session(dp, state, "http://proxy.invalid:8080")
        try:
            assert ctx["fp"] == "verify_test" and ctx["web_id"] == ctx["tea_uuid"] == "official_web"
            assert ctx["device_id"] == "official_device" and session._doubao_launch is launch
            assert session.proxies["https"] == "http://proxy.invalid:8080" and session.trust_env is False
        finally:
            session.close()
    assert [x[0] for x in calls] == ["/alice/user/get_web_anon_id", "/alice/user/launch"] * 2
    assert calls[1][1]["web_id"] == "official_web"
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert all(saved[k] == original[k] for k in ("cookies_list", "storage", "doubao_account_type"))
    assert saved["ctx"]["keep"] == "value" and saved["ctx"]["fp"] == "verify_test"
    assert not list(tmp_path.glob(".cookie_state_*.tmp"))


@pytest.mark.parametrize("code,error", [(710012001, g.PureLoginExpired), (710022002, g.PureApiError)])
def test_bootstrap_failure_closes_session_and_does_not_expose_response(tmp_path, monkeypatch, code, error):
    from protocol import doubao_site
    dp = doubao_site.load()
    closed = []
    session = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(dp, "setup_session", lambda **kwargs: (session, {}, {}))
    monkeypatch.setattr(dp, "dola_api_post_json", lambda *a, **k: {"code": code, "cookie": "secret-state", "token": "secret-token"})
    state = tmp_path / "state.json"
    state.write_text('{"keep":true}', encoding="utf-8")
    with pytest.raises(error) as caught:
        g._session(dp, state, "http://user:secret-password@proxy.invalid")
    assert closed == [True] and "secret" not in str(caught.value)
    assert state.read_text(encoding="utf-8") == '{"keep":true}'


def test_explicit_plain_video_question_is_confirmed_once_with_original_skill(monkeypatch, tmp_path):
    dp = _FakeDP([(0, "是否需要我直接生成视频？", [], PLAIN),
                  (1, FREE_ACCEPTED, [], CREATOR)], vid_after=1)
    result = _gen(monkeypatch, tmp_path, dp)
    assert result["local_path"] and dp.replies == ["确认生成"]
    ability = dp.reply_payloads[0]["chat_ability"]
    param = json.loads(ability["ability_param"])
    assert ability["ability_type"] == 17 and param["duration"] == 5 and param["ratio"] == "16:9"


@pytest.mark.parametrize("text", ["我给你写视频提示词，要开始吗？", "是否需要我生成视频提示词？",
                                  "我不能直接生成视频，要不要为你整理提示词？"])
def test_plain_prompt_offer_never_submits_confirmation(monkeypatch, tmp_path, text):
    dp = _FakeDP([(0, text, [], PLAIN)])
    with pytest.raises(VideoSkillUnavailableError):
        _gen(monkeypatch, tmp_path, dp)
    assert not dp.replies and not dp.downloaded


@pytest.mark.parametrize("text", ["是否直接生成视频？模型：Seedance 2.5", "是否生成10秒视频？",
                                  "比例：9:16，是否需要我生成视频？", "是否消耗付费额度继续生成视频？"])
def test_plain_confirmation_never_changes_parameters_or_accepts_payment(monkeypatch, tmp_path, text):
    dp = _FakeDP([(0, text, [], PLAIN)])
    with pytest.raises(UpstreamReplyError) as caught:
        _gen(monkeypatch, tmp_path, dp)
    assert not caught.value.allow_account_retry and not dp.replies


def test_confirmation_followed_by_plain_chat_keeps_original_without_retry(monkeypatch, tmp_path):
    dp = _FakeDP([(0, "是否需要我生成视频？", [], PLAIN),
                  (1, "已确认，稍等就会生成。", [], PLAIN)])
    with pytest.raises(TimeoutError, match="保留原会话"):
        _gen(monkeypatch, tmp_path, dp)
    assert dp.replies == ["确认生成"]


@pytest.mark.parametrize("text", ["内容审核不通过，无法生成视频。", "请先上传参考图片再生成。"])
def test_content_refusal_and_missing_input_stop_without_pausing_or_switching(monkeypatch, tmp_path, text):
    dp = _FakeDP([(0, text, [], PLAIN)])
    with pytest.raises(UpstreamReplyError) as caught:
        _gen(monkeypatch, tmp_path, dp)
    assert not isinstance(caught.value, VideoSkillUnavailableError)
    assert not caught.value.allow_account_retry and not dp.replies


def test_route_failures_pause_accounts_and_switch_at_most_twice(tmp_path, monkeypatch):
    _, pool = _pool(tmp_path, monkeypatch, dola=(), doubao=("db1", "db2", "db3", "db4"))
    calls = []
    async def generate(account, *args, **kwargs):
        calls.append(account)
        raise VideoSkillUnavailableError("普通助手只回复提示词，未进入创作")
    pool._generate_effective = generate
    with pytest.raises(VideoSkillUnavailableError):
        _run(pool.generate_video("湖面", "16:9", 5, "doubao-seedance-2.0-fast", max_attempts=0))
    assert len(calls) == len(set(calls)) == 3
    rows = {row["name"]: row for row in pool.list_accounts()}
    assert all(rows[name]["abnormal_until"] > time.time() and rows[name]["login_ok"] == 1
               and not rows[name]["rate_limited"] and pool.used_today(name) == 0 for name in calls)


@pytest.mark.parametrize("text", [IMAGE_ERROR, "参考图当前无法读取，请重新上传。",
                                  "无法访问上传的图片，请重新上传。", "改为纯文字生成。"])
def test_explicit_reference_failures_are_recognized(text):
    assert g.reference_failure(text)


@pytest.mark.parametrize("text", ["参考图已读取，将用于生成。", "如果参考图无法读取，请重新上传。",
                                  "我不会忽略参考图，将保留人物和构图。", "参考图没有失效。"])
def test_reference_success_and_conditional_advice_are_not_failures(text):
    assert not g.reference_failure(text)


@pytest.mark.parametrize("video", [False, True])
def test_reference_failure_stops_before_confirmation_or_keeps_noncompliant_clip(tmp_path, monkeypatch, video):
    dp = _FakeDP([(0, IMAGE_ERROR, [], CREATOR)], vid_after=0 if video else None)
    monkeypatch.setattr(dp, "normalize_image_paths", lambda paths: [Path("synthetic.png")])
    monkeypatch.setattr(dp, "upload_image", lambda *args: {"key": "image-ref"})
    if video:
        result = _gen(monkeypatch, tmp_path, dp, reference_image_paths=["synthetic.png"])
        assert result["local_path"] and "参考图未用于生成" in result["validation_error"]
    else:
        with pytest.raises(UpstreamReplyError, match="参考图不可用") as caught:
            _gen(monkeypatch, tmp_path, dp, reference_image_paths=["synthetic.png"])
        assert not caught.value.allow_account_retry and not dp.downloaded
    assert not dp.replies


def test_short_doubao_clip_is_retained_without_asking_for_another_generation(tmp_path, monkeypatch):
    dp = _FakeDP([], vid_after=0)
    result = _gen(monkeypatch, tmp_path, dp, duration=15)
    assert Path(result["local_path"]).exists() and "5.0 秒" in result["validation_error"]
    assert not dp.replies and len(dp.downloaded) == 1


def test_recovery_rechecks_reference_and_ratio_without_sending(tmp_path, monkeypatch):
    dp = _FakeDP([(0, IMAGE_ERROR, [], CREATOR)], vid_after=0)
    fetch = dp.fetch_single_chain
    def chain(*args):
        data = fetch(*args)
        data["video_model"] = json.dumps({"vid": "v0abc", "video_duration": 5.05,
            "video_list": {"high": {"vwidth": 960, "vheight": 720}}})
        return data
    dp.fetch_single_chain = chain
    monkeypatch.setattr(g, "_site", lambda: dp)
    monkeypatch.setattr(g, "_session", lambda *a: (SimpleNamespace(close=lambda: None), {}, {}))
    monkeypatch.setattr(g, "_media_duration", lambda path: 5.05)
    state = tmp_path / "state.json"
    state.write_text('{"cookies":{"sessionid":{"value":"test"}}}', encoding="utf-8")
    result = g.fetch_conversation_video(state, "conv1", output_dir=tmp_path, reference_count=1, duration=15, ratio="16:9")
    assert Path(result["local_path"]).exists()
    assert all(text in result["validation_error"] for text in ("参考图未用于生成", "画幅", "请求 15 秒"))
    assert not dp.replies
    dp.vid_after = None
    result = g.fetch_conversation_video(state, "conv1", output_dir=tmp_path, reference_count=1)
    assert result["final"] and result["status"] == "failed" and not dp.replies


def test_mismatching_clip_is_charged_once_and_retained_without_switch(tmp_path, monkeypatch):
    import browser_pool
    _, pool = _pool(tmp_path, monkeypatch, dola=(), doubao=("db1", "db2"))
    for name in pool.accounts:
        pool.set_source(name, "cookie")
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    monkeypatch.setattr(browser_pool, "ensure_web_playable", lambda path: path)
    output = tmp_path / "original.mp4"
    output.write_bytes(b"video")
    calls = []
    def generate(**kwargs):
        calls.append(kwargs["account"])
        return {"account": kwargs["account"], "local_path": str(output), "validation_error": "参考图未用于生成"}
    monkeypatch.setattr(g, "generate_doubao_video", generate)
    with pytest.raises(WorkVideoMismatchError) as caught:
        _run(pool.generate_video("湖面", "16:9", 15, "doubao-seedance-2.0-fast", max_attempts=3))
    assert len(calls) == 1 and Path(caught.value.result["local_path"]).exists()
    assert pool.used_today(calls[0]) == pool._duration_cost("doubao-seedance-2.0-fast", 15)


@pytest.mark.parametrize("restart", [False, True])
def test_service_recovery_keeps_noncompliant_video_failed_and_accounts_once(tmp_path, monkeypatch, restart):
    from test_auto_recover import FakePool, _server
    output = tmp_path / "original.mp4"
    class RecoveryPool(FakePool):
        def fetch_conversation_video(self, account, conv, **kwargs):
            self.requirements = kwargs
            return super().fetch_conversation_video(account, conv)
    pool = RecoveryPool([{"local_path": str(output), "validation_error": "参考图未用于生成"}])
    server = _server(tmp_path, monkeypatch, pool)
    server.store.update("video_t1", model="doubao-seedance-2.0-fast", duration=15,
                        reference_thumbs=json.dumps(["ref-one", "ref-two"]),
                        recover_state="watching", recover_until=time.time() + 60)
    if restart:
        _run(server._resume_pure_conversation(server.store.get("video_t1")))
    else:
        _run(server._recover_loop("video_t1"))
    row = server.store.get("video_t1")
    assert row["status"] == "failed" and "参考图未用于生成" in row["error"]
    assert row["video_url"].endswith("/videos/original.mp4") and output.exists()
    assert pool.calls == 1 and len(pool.claimed) == 1
    assert pool.requirements == {"reference_count": 2, "duration": 15, "ratio": "9:16"}


def test_work_service_recovery_keeps_reference_failure_failed(tmp_path, monkeypatch):
    from test_work_followups import work_server
    output = tmp_path / "original.mp4"
    server, pool = work_server(tmp_path, monkeypatch, [{"local_path": str(output),
                                                       "validation_error": "参考图未用于生成"}])
    server.store.update("video_t1", model="doubao-seedance-2.0-fast", execution_mode="work",
                        reference_thumbs=json.dumps(["ref-one"]))
    seen = []
    fetch = pool.fetch_work_conversation_video
    async def recovered(account, conv, **kwargs):
        seen.append(kwargs)
        return await fetch(account, conv, **kwargs)
    pool.fetch_work_conversation_video = recovered
    _run(server._recover_loop("video_t1"))
    row = server.store.get("video_t1")
    assert row["status"] == "failed" and row["recover_state"] == "gave_up"
    assert row["video_url"] and "参考图未用于生成" in row["error"]
    assert len(pool.claimed) == 1 and pool.calls == 1
    assert seen[0]["reference_count"] == 1 and seen[0]["ratio"] == "9:16"


def test_unbound_account_does_not_block_another_account_with_a_valid_bound_proxy(tmp_path, monkeypatch):
    module, pool = _pool(tmp_path, monkeypatch, dola=(), doubao=("db1", "db2"))
    monkeypatch.setattr(config, "ISOLATE_SHARED_EGRESS", True)
    monkeypatch.setattr(config, "EGRESS_PRECHECK", False)
    monkeypatch.setattr(module.proxy_store, "get_account_proxy", lambda name:
                        {"mode": "static", "protocol": "http", "host": "bound.invalid", "port": 8080,
                         "username": "", "password": ""} if name == "db2" else None)
    monkeypatch.setattr(module.proxy_store, "account_proxy_url", lambda name: "http://bound.invalid:8080")
    calls = []
    async def generate(account, *args, **kwargs):
        calls.append(account)
        return {"account": account, "local_path": ""}
    pool._generate_effective = generate
    result = _run(pool.generate_video("湖面", "16:9", 5, "doubao-seedance-2.0-fast", max_attempts=1))
    assert result["account"] == "db2" and calls == ["db2"]
    assert pool.used_today("db1") == 0


@pytest.mark.parametrize("failure", ["network", "http"])
def test_doubao_submission_does_not_repeat_an_ambiguous_request(monkeypatch, failure):
    from protocol import doubao_site
    dp = doubao_site.load()
    calls, closed = [], []
    monkeypatch.setattr(dp, "fetch_recent_conversations", lambda *args: {})
    monkeypatch.setattr(dp, "get_login_cdp", lambda session: None)
    monkeypatch.setattr(dp, "sign_url", lambda url, **kwargs: url)
    def send(*args, **kwargs):
        calls.append(True)
        if failure == "network":
            raise requests.ReadTimeout("https://cdn.invalid/?signature=secret")
        return SimpleNamespace(status_code=503, close=lambda: closed.append(True))
    monkeypatch.setattr(dp, "send_with_cookie_header", send)
    with pytest.raises(TimeoutError, match="未自动重发") as caught:
        dp.submit_chat_completion(SimpleNamespace(cookies=[]), {"fp": "test"}, {}, images=[], audios=[],
            prompt="湖面", duration=15, model="seedance_v2.0", ratio="16:9", timeout_sec=30)
    assert calls == [True] and "secret" not in str(caught.value)
    assert closed == ([True] if failure == "http" else [])


def test_partial_receipt_keeps_conversation_id_for_service_recovery(tmp_path, monkeypatch):
    dp = _FakeDP([])
    submitted = []
    def submit(*args, **kwargs):
        submitted.append(True)
        return SimpleNamespace(conversation_id="original-conv", error="stream interrupted", error_code="")
    dp.submit_chat_completion = submit
    acknowledgements = []
    with pytest.raises(TimeoutError, match="会话已建立但回执异常"):
        _gen(monkeypatch, tmp_path, dp, on_conversation_id=lambda *args: acknowledgements.append(args))
    assert submitted == [True] and acknowledgements[0][:2] == ("d1", "original-conv")
    assert not dp.replies


def test_existing_creator_handoff_is_read_without_sending_confirmation(tmp_path, monkeypatch):
    dp = _FakeDP([(0, "正在整理创作参数", [], CREATOR),
                  (0, "以下是参考提示词", [], PLAIN)])
    original = dp.fetch_single_chain
    reads = []
    def chain(*args):
        reads.append(True)
        if len(reads) >= 3:
            dp.vid_after = 0
        return original(*args)
    dp.fetch_single_chain = chain
    assert _gen(monkeypatch, tmp_path, dp)["local_path"]
    assert not dp.replies and len(reads) == 3


def test_fixed_account_mismatching_link_remains_available(tmp_path, monkeypatch):
    module, pool = _pool(tmp_path, monkeypatch, dola=(), doubao=("db1",))
    pool.set_source("db1", "cookie")
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    result = {"account": "db1", "local_path": "", "delivery": "link", "video_duration": 15.1,
              "video_url": "https://cdn.invalid/original.mp4", "validation_error": "参考图未用于生成"}
    monkeypatch.setattr(g, "generate_doubao_video", lambda **kwargs: dict(result))
    output = _run(pool.test_generate("db1", "湖面", "16:9", 15, "doubao-seedance-2.0-fast"))
    assert not output["ok"] and output["video_url"] == result["video_url"]
    assert pool.used_today("db1") == pool._duration_cost("doubao-seedance-2.0-fast", 15)


def test_service_keeps_a_mismatching_link_in_the_failed_fixed_account_task(tmp_path, monkeypatch):
    from test_auto_recover import FakePool, _server
    pool = FakePool([])
    server = _server(tmp_path, monkeypatch, pool)
    monkeypatch.setattr(server, "TEST_JOBS", {"test_link": {}})
    async def fixed(*args, **kwargs):
        return {"ok": False, "account": "db1", "local_path": "", "delivery": "link",
                "video_url": "https://cdn.invalid/original.mp4", "error": "参考图未用于生成",
                "video_model": json.dumps({"video_duration": 15.1})}
    pool.test_generate = fixed
    _run(server._run_test_generate("test_link", "db1", "湖面", "16:9", 15, "doubao-seedance-2.0-fast"))
    row = server.store.get("test_link")
    assert row["status"] == "failed" and row["video_link"] == "https://cdn.invalid/original.mp4"
    assert row["video_url"].endswith("/v/test_link.mp4")
    assert server.TEST_JOBS["test_link"]["result"]["video_url"] == row["video_url"]


def test_download_failure_after_video_creation_preserves_original_instead_of_retrying(tmp_path, monkeypatch):
    dp = _FakeDP([], vid_after=0)
    def failed(*args, **kwargs):
        raise requests.ReadTimeout("https://cdn.invalid/?signature=secret")
    dp.download_video = failed
    with pytest.raises(TimeoutError, match="视频已生成但交付失败") as caught:
        _gen(monkeypatch, tmp_path, dp)
    assert "secret" not in str(caught.value) and not dp.replies


def test_confirmation_does_not_confuse_the_start_time_with_the_aspect_ratio():
    assert g.confirm_mismatch("比例：16:9，时长：15秒，预计14:30开始。", duration=15,
                              model_display="Seedance 2.0 Fast", ratio="16:9") == ""
