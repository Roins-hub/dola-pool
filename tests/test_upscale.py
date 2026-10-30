"""视频超分（upscale.py + server 接口）：配置打码、fal / 自建模型的提交与轮询、后台执行、续查与交付。"""
import json
import time
from pathlib import Path

import pytest

import upscale
from test_task_deadline import _run


class MemStore:
    def __init__(self): self.settings = {}
    def get_setting(self, key): return self.settings.get(key)
    def set_setting(self, key, value): self.settings[key] = value


# ---------------------------------------------------------------- 配置

def test_settings_mask_secrets_and_keep_or_clear_them():
    store = MemStore()
    upscale.save_settings(store, {"provider": "fal", "fal_key": "fal-secret-key-1234"})
    pub = upscale.public_settings(upscale.load_settings(store))
    assert "fal_key" not in pub and pub["fal_key_set"] and pub["fal_key_hint"] == "••••1234"
    assert pub["ready"] == {"fal": True, "open": False, "custom": False}
    upscale.save_settings(store, {"fal_key": None, "delivery": "link"})       # None = 保持
    assert upscale.load_settings(store)["fal_key"] == "fal-secret-key-1234"
    upscale.save_settings(store, {"fal_key": ""})                             # 空字符串 = 清除
    assert upscale.load_settings(store)["fal_key"] == ""
    assert "secret" not in json.dumps(upscale.public_settings(upscale.load_settings(store)))


@pytest.mark.parametrize("patch", [{"provider": "topaz"}, {"target": "8k"}, {"delivery": "ftp"},
                                   {"open_endpoint": "gpu-box:8000"}, {"fal_preset": "anime"}])
def test_invalid_settings_are_rejected(patch):
    with pytest.raises(upscale.UpscaleError):
        upscale.save_settings(MemStore(), patch)


def test_cost_estimate_only_for_fal():
    assert upscale.estimate_cost("fal", 10) == 0.072
    assert upscale.estimate_cost("open", 10) is None


# ---------------------------------------------------------------- 服务商（假 HTTP）

class FakeResp:
    def __init__(self, status, body): self.status, self.body = status, body
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def text(self): return json.dumps(self.body)


def fake_http(monkeypatch, replies):
    """replies: [(method, url_substring, status, body)]，按顺序消费；记录请求。"""
    calls = []
    class Session:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def _reply(self, method, url, kw):
            calls.append((method, url, kw))
            want_method, part, status, body = replies.pop(0)
            assert method == want_method and part in url, (method, url)
            return FakeResp(status, body)
        def post(self, url, **kw): return self._reply("POST", url, kw)
        def get(self, url, **kw): return self._reply("GET", url, kw)
    monkeypatch.setattr(upscale.aiohttp, "ClientSession", Session)
    return calls


FAL = {"provider": "fal", "target": "1080p", "fal_key": "k-123", "fal_preset": "aigc",
       "fal_tier": "standard", "fal_fidelity": "high", "open_endpoint": "", "open_token": "", "delivery": "link"}


def test_fal_submit_sends_aigc_1080p_at_source_fps(monkeypatch):
    calls = fake_http(monkeypatch, [("POST", "queue.fal.run/fal-ai/bytedance-upscaler/upscale/video", 200,
                                     {"request_id": "r1", "status_url": "https://queue.fal.run/x/requests/r1/status",
                                      "response_url": "https://queue.fal.run/x/requests/r1"})])
    job = _run(upscale.submit(FAL, "fal", "https://cdn/v.mp4"))
    assert job == {"provider": "fal", "request_id": "r1", "status_url": "https://queue.fal.run/x/requests/r1/status",
                   "response_url": "https://queue.fal.run/x/requests/r1"}
    body, headers = calls[0][2]["json"], calls[0][2]["headers"]
    assert body["video_url"] == "https://cdn/v.mp4" and body["target_resolution"] == "1080p"
    assert body["target_fps"] == 24.0 and body["enhancement_preset"] == "aigc"
    assert headers == {"Authorization": "Key k-123"} and "k-123" not in json.dumps(job)


def test_fal_poll_pending_completed_and_failed(monkeypatch):
    job = {"provider": "fal", "status_url": "https://q/s", "response_url": "https://q/r"}
    fake_http(monkeypatch, [("GET", "q/s", 200, {"status": "IN_QUEUE", "queue_position": 2})])
    assert _run(upscale.poll(FAL, job))["state"] == "pending"
    fake_http(monkeypatch, [("GET", "q/s", 200, {"status": "COMPLETED"}),
                            ("GET", "q/r", 200, {"video": {"url": "https://fal.media/out.mp4", "file_size": 9},
                                                 "duration": 10.08})])
    assert _run(upscale.poll(FAL, job)) == {"state": "completed", "url": "https://fal.media/out.mp4",
                                            "size": 9, "billed_seconds": 10.08}
    fake_http(monkeypatch, [("GET", "q/s", 200, {"status": "COMPLETED"}),
                            ("GET", "q/r", 422, {"detail": [{"msg": "video too long"}]})])
    failed = _run(upscale.poll(FAL, job))
    assert failed["state"] == "failed" and "video too long" in failed["error"]


def test_fal_rejected_submit_raises_with_detail(monkeypatch):
    fake_http(monkeypatch, [("POST", "queue.fal.run", 401, {"detail": "Invalid key"})])
    with pytest.raises(upscale.UpscaleError, match="401.*Invalid key"):
        _run(upscale.submit(FAL, "fal", "https://cdn/v.mp4"))


def test_open_provider_contract(monkeypatch):
    cfg = {**FAL, "provider": "open", "open_endpoint": "https://gpu.example", "open_token": "tok"}
    calls = fake_http(monkeypatch, [("POST", "gpu.example/v1/upscale", 200, {"id": "j9", "status": "queued"}),
                                    ("GET", "gpu.example/v1/upscale/j9", 200, {"status": "processing"}),
                                    ("GET", "gpu.example/v1/upscale/j9", 200,
                                     {"status": "completed", "video_url": "https://gpu.example/o.mp4"})])
    job = _run(upscale.submit(cfg, "open", "https://cdn/v.mp4"))
    assert job == {"provider": "open", "id": "j9"}
    assert calls[0][2]["headers"] == {"Authorization": "Bearer tok"}
    assert _run(upscale.poll(cfg, job))["state"] == "pending"
    assert _run(upscale.poll(cfg, job))["url"] == "https://gpu.example/o.mp4"


# ---------------------------------------------------------------- 自定义 API

def custom_settings(store, preset, **extra):
    patch = dict(upscale.CUSTOM_PRESETS[preset])
    patch.update(provider="custom", **extra)
    upscale.save_settings(store, patch)
    return upscale.load_settings(store)


def test_custom_preset_apiverse_async_flow(monkeypatch):
    cfg = custom_settings(MemStore(), "apiverse", custom_headers='{"Authorization": "Bearer av-token-9999"}')
    calls = fake_http(monkeypatch, [
        ("POST", "api.apiverse.ai/api/v2/open/aigc/super-resolution", 200,
         {"code": 0, "data": {"taskId": "task_1", "status": "processing"}}),
        ("GET", "api.apiverse.ai/api/v2/open/aigc/task_1", 200, {"code": 0, "data": {"status": "processing"}}),
        ("GET", "api.apiverse.ai/api/v2/open/aigc/task_1", 200,
         {"code": 0, "data": {"status": "success", "result": ["https://oss.example/out_1080p.mp4"]}})])
    job = _run(upscale.submit(cfg, "custom", "https://cdn/v.mp4", duration=10.08, task_id="video_x"))
    assert job == {"provider": "custom", "id": "task_1"}
    body, headers = calls[0][2]["json"], calls[0][2]["headers"]
    assert body == {"videoUrl": "https://cdn/v.mp4", "quality": "1080p", "duration": 11, "taskNickname": "video_x"}
    assert headers["Authorization"] == "Bearer av-token-9999"
    assert _run(upscale.poll(cfg, job))["state"] == "pending"
    assert _run(upscale.poll(cfg, job))["url"] == "https://oss.example/out_1080p.mp4"


def test_custom_business_code_and_failed_status(monkeypatch):
    cfg = custom_settings(MemStore(), "apiverse", custom_headers='{"Authorization": "Bearer x"}')
    fake_http(monkeypatch, [("POST", "apiverse", 200, {"code": 10006, "msg": "余额不足"})])
    with pytest.raises(upscale.UpscaleError, match="10006.*余额不足"):
        _run(upscale.submit(cfg, "custom", "https://cdn/v.mp4", duration=5))
    fake_http(monkeypatch, [("GET", "apiverse", 200, {"code": 0, "data": {"status": "failed", "errorMsg": "超分任务执行失败"}})])
    failed = _run(upscale.poll(cfg, {"provider": "custom", "id": "t"}))
    assert failed["state"] == "failed" and "超分任务执行失败" in failed["error"]


def test_custom_preset_apibobo_is_synchronous(monkeypatch):
    cfg = custom_settings(MemStore(), "apibobo", custom_headers='{"Authorization": "Bearer sk-bobo"}', target="2k")
    calls = fake_http(monkeypatch, [("POST", "apibobo.com/v1/tools/run", 200, {"out": "https://bobo.example/r.mp4"})])
    job = _run(upscale.submit(cfg, "custom", "https://cdn/v.mp4"))
    assert calls[0][2]["json"]["input"] == {"video": "https://cdn/v.mp4", "resolution": "2K"}   # target 改名
    assert _run(upscale.poll(cfg, job)) == {"state": "completed", "url": "https://bobo.example/r.mp4",
                                            "size": None, "billed_seconds": None}


def test_custom_settings_validation_and_header_masking():
    store = MemStore()
    with pytest.raises(upscale.UpscaleError, match="JSON"):
        upscale.save_settings(store, {"custom_headers": "Authorization: Bearer x"})
    with pytest.raises(upscale.UpscaleError, match="http"):
        upscale.save_settings(store, {"custom_submit_url": "api.example.com/run"})
    cfg = custom_settings(store, "apiverse", custom_headers='{"Authorization": "Bearer secret-token-1234"}')
    pub = upscale.public_settings(cfg)
    assert pub["ready"]["custom"] and pub["custom_headers_hint"] == "Authorization"
    assert "secret-token" not in json.dumps(pub, ensure_ascii=False)
    assert "custom_headers_example" in pub["custom_presets"]["apiverse"]
    assert "缺查询地址" in upscale.missing_config({**cfg, "custom_poll_url": ""}, "custom")


def test_template_rendering_keeps_types():
    out = upscale.render_template('{"a": "{{duration}}", "b": "x-{{target}}", "c": ["{{video_url}}"]}',
                                  {"duration": 11, "target": "1080p", "video_url": "u"})
    assert out == {"a": 11, "b": "x-1080p", "c": ["u"]}
    assert upscale.json_path({"data": {"result": ["r0"]}}, "data.result.0") == "r0"
    assert upscale.json_path({"data": {}}, "data.result.0") is None


def test_missing_config_blocks_submit():
    with pytest.raises(upscale.UpscaleError, match="fal API Key"):
        _run(upscale.submit({**FAL, "fal_key": ""}, "fal", "https://cdn/v.mp4"))


# ---------------------------------------------------------------- 后台执行与接口

@pytest.fixture
def srv(tmp_path, monkeypatch):
    import config
    import server
    from store import TaskStore
    monkeypatch.setattr(server, "store", TaskStore(str(tmp_path / "tasks.db")))
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PUBLIC_BASE", "https://pool.example")
    monkeypatch.setattr(config, "UPSCALE_POLL_SECONDS", 0)
    monkeypatch.setattr(server, "_admin_auth", lambda key: None)
    (tmp_path / "a.mp4").write_bytes(b"720p")
    server.store.create("video_up1", "seedance-2.0-fast", "p", "16:9", 10)
    server.store.update("video_up1", status="completed", video_url="https://pool.example/videos/a.mp4",
                        video_duration=10.08)
    upscale.save_settings(server.store, {"provider": "fal", "fal_key": "k-123", "delivery": "link"})
    return server


def _start(server, **body):
    return _run(server.admin_task_upscale("video_up1", server.UpscaleRequest(**body)))


def test_upscale_runs_to_completion_and_serves_the_hd_link(srv, monkeypatch):
    submitted = []
    async def submit(settings, provider, source, **kw):
        submitted.append((provider, source))
        return {"provider": "fal", "status_url": "s", "response_url": "r"}
    replies = [{"state": "pending"}, {"state": "completed", "url": "https://fal.media/hd.mp4",
                                      "size": 30, "billed_seconds": 10.08}]
    async def poll(settings, job): return replies.pop(0)
    monkeypatch.setattr(upscale, "submit", submit)
    monkeypatch.setattr(upscale, "poll", poll)
    async def scenario():
        res = await srv.admin_task_upscale("video_up1", srv.UpscaleRequest())
        assert res["status"] == "queued" and res["estimated_cost_usd"] == 0.0726
        while srv.store.get("video_up1")["upscale_status"] in ("queued", "processing"):
            await __import__("asyncio").sleep(0.01)
    _run(scenario())
    row = srv.store.get("video_up1")
    assert submitted == [("fal", "https://pool.example/videos/a.mp4")]
    assert row["upscale_status"] == "completed" and row["upscale_url"] == "https://fal.media/hd.mp4"
    assert srv._upscale_public_url(row) == "https://pool.example/v/video_up1-1080p.mp4"
    from fastapi.testclient import TestClient
    client = TestClient(srv.app)
    r = client.get("/v/video_up1-1080p.mp4", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://fal.media/hd.mp4"
    assert client.get("/v/video_up1-4k.mp4", follow_redirects=False).status_code == 404
    with pytest.raises(srv.HTTPException) as again:          # 已超分：不带 force 不重做（避免重复计费）
        _start(srv)
    assert again.value.status_code == 409


def test_download_delivery_serves_the_local_file(srv, monkeypatch, tmp_path):
    upscale.save_settings(srv.store, {"delivery": "download"})
    async def submit(*a, **kw): return {"provider": "fal", "status_url": "s", "response_url": "r"}
    async def poll(*a): return {"state": "completed", "url": "https://fal.media/hd.mp4"}
    async def download(url, dest, **kw):
        Path(dest).write_bytes(b"1080p-bytes")
        return 11
    monkeypatch.setattr(upscale, "submit", submit)
    monkeypatch.setattr(upscale, "poll", poll)
    monkeypatch.setattr(upscale, "download", download)
    srv.store.update("video_up1", upscale_status="queued", upscale_provider="fal", upscale_target="1080p",
                     upscale_started_at=time.time())
    _run(srv._run_upscale("video_up1"))
    row = srv.store.get("video_up1")
    assert row["upscale_file"] == "video_up1-1080p.mp4" and row["upscale_size"] == 11
    from fastapi.testclient import TestClient
    r = TestClient(srv.app).get("/v/video_up1-1080p.mp4")
    assert r.status_code == 200 and r.content == b"1080p-bytes"
    srv._delete_task_files_and_record(srv.store.get("video_up1"))
    assert not (tmp_path / "video_up1-1080p.mp4").exists()


def test_failure_is_recorded_and_can_be_retried(srv, monkeypatch):
    async def submit(*a, **kw): return {"provider": "fal", "status_url": "s", "response_url": "r"}
    async def poll(*a): return {"state": "failed", "error": "fal：video too long"}
    monkeypatch.setattr(upscale, "submit", submit)
    monkeypatch.setattr(upscale, "poll", poll)
    srv.store.update("video_up1", upscale_status="queued", upscale_provider="fal", upscale_started_at=time.time())
    _run(srv._run_upscale("video_up1"))
    row = srv.store.get("video_up1")
    assert row["upscale_status"] == "failed" and "video too long" in row["upscale_error"]
    async def noop(task_id): pass
    monkeypatch.setattr(srv, "_run_upscale", noop)
    assert _start(srv)["status"] == "queued"                 # 失败后可以直接重试


def test_restart_resumes_polling_without_resubmitting(srv, monkeypatch):
    async def submit(*a, **kw): raise AssertionError("must not resubmit a job that was already accepted")
    async def poll(settings, job):
        assert job == {"provider": "fal", "status_url": "s", "response_url": "r"}
        return {"state": "completed", "url": "https://fal.media/hd.mp4"}
    monkeypatch.setattr(upscale, "submit", submit)
    monkeypatch.setattr(upscale, "poll", poll)
    srv.store.update("video_up1", upscale_status="processing", upscale_provider="fal", upscale_started_at=time.time(),
                     upscale_job=json.dumps({"provider": "fal", "status_url": "s", "response_url": "r"}))
    assert [r["id"] for r in srv.store.upscale_unfinished()] == ["video_up1"]
    _run(srv._run_upscale("video_up1"))
    assert srv.store.get("video_up1")["upscale_status"] == "completed"


def test_start_is_refused_until_the_provider_is_configured(srv):
    upscale.save_settings(srv.store, {"fal_key": ""})
    with pytest.raises(srv.HTTPException) as missing:
        _start(srv)
    assert missing.value.status_code == 400 and "fal API Key" in missing.value.detail
    srv.store.update("video_up1", status="processing")
    with pytest.raises(srv.HTTPException) as not_done:
        _start(srv, provider="open")
    assert not_done.value.status_code == 409


def test_source_url_uses_the_fresh_upstream_link_or_rejects_a_local_base(srv, monkeypatch):
    import config
    import video_link
    async def fresh(row, store): return "https://cdn.dola/fresh.mp4"
    monkeypatch.setattr(video_link, "current_link", fresh)
    assert _run(srv._upscale_source({"video_link": "old"})) == "https://cdn.dola/fresh.mp4"
    monkeypatch.setattr(config, "PUBLIC_BASE", "http://127.0.0.1:8000")
    with pytest.raises(upscale.UpscaleError, match="公网地址"):
        _run(srv._upscale_source({"video_url": "http://127.0.0.1:8000/videos/a.mp4"}))


# ---------------------------------------------------------------- 按请求分辨率自动超分

@pytest.mark.parametrize("value,expected", [("1080p", "1080p"), ("FHD", "1080p"), ("2K", "2k"), ("1440p", "2k"),
                                            ("4k", "4k"), ("720p", "720p"), ("480p", "720p"), ("8k", None), (None, None)])
def test_resolution_aliases(value, expected):
    assert upscale.normalize_resolution(value) == expected


def test_resolution_from_size_uses_the_short_side():
    assert upscale.resolution_from_size("1920x1080") == "1080p"
    assert upscale.resolution_from_size("1080x1920") == "1080p"
    assert upscale.resolution_from_size("1280x720") == "720p"
    assert upscale.resolution_from_size("3840x2160") == "4k"
    assert upscale.resolution_from_size("16:9") is None


def test_auto_plan_rules():
    ready = {**FAL, "auto": "param"}
    assert upscale.auto_plan(ready, "720p")["auto"] is False                      # 720p 原片直出
    assert upscale.auto_plan(ready, None, "1920x1080")["requested"] is None       # param 模式不看 size
    plan = upscale.auto_plan(ready, "1080p")
    assert plan["auto"] and plan["target"] == "1080p" and plan["provider"] == "fal"
    assert upscale.auto_plan({**ready, "auto": "param_or_size"}, None, "1920x1080")["target"] == "1080p"
    off = upscale.auto_plan({**FAL, "auto": "off"}, "4k")
    assert not off["auto"] and "没有开启自动超分" in off["note"]
    no_key = upscale.auto_plan({**ready, "fal_key": ""}, "2k")
    assert not no_key["auto"] and "fal API Key" in no_key["note"]


def test_auto_upscale_runs_after_completion_and_client_sees_hd(srv, monkeypatch):
    upscale.save_settings(srv.store, {"auto": "param"})
    srv.store.create("video_auto1", "seedance-2.0-fast", "p", "16:9", 10)
    srv.store.update("video_auto1", requested_resolution="1080p", upscale_auto=1, upscale_target="1080p",
                     upscale_provider="fal", status="processing")
    row = srv.store.get("video_auto1")
    assert srv._client_video(row) == ("processing", None, None)
    srv.store.update("video_auto1", status="completed", video_url="https://pool.example/videos/a.mp4")
    assert srv._client_video(srv.store.get("video_auto1"))[0] == "processing"   # 超分完成前对客户仍是生成中
    targets = []
    async def submit(settings, provider, source, **kw):
        targets.append(kw.get("target"))
        return {"provider": "fal", "status_url": "s", "response_url": "r"}
    async def poll(*a): return {"state": "completed", "url": "https://fal.media/hd.mp4"}
    monkeypatch.setattr(upscale, "submit", submit)
    monkeypatch.setattr(upscale, "poll", poll)
    started = []
    monkeypatch.setattr(srv.asyncio, "create_task", lambda coro: started.append(coro))
    async def one_tick():
        rows = srv.store.auto_upscale_pending()
        assert [r["id"] for r in rows] == ["video_auto1"]
        for r in rows:
            srv.store.update(r["id"], upscale_status="queued", upscale_started_at=time.time())
            await srv._run_upscale(r["id"])
    _run(one_tick())
    assert targets == ["1080p"] and not srv.store.auto_upscale_pending()
    status, url, hd = srv._client_video(srv.store.get("video_auto1"))
    assert status == "completed" and url == hd == "https://pool.example/v/video_auto1-1080p.mp4"


def test_failed_auto_upscale_falls_back_to_the_original(srv):
    srv.store.create("video_auto2", "seedance-2.0-fast", "p", "16:9", 10)
    srv.store.update("video_auto2", status="completed", video_url="https://pool.example/videos/a.mp4",
                     upscale_auto=1, upscale_status="failed", upscale_error="fal：boom")
    assert srv._client_video(srv.store.get("video_auto2")) == (
        "completed", "https://pool.example/videos/a.mp4", None)


def test_submit_records_requested_resolution(srv, monkeypatch):
    upscale.save_settings(srv.store, {"auto": "param"})
    plan = upscale.auto_plan(upscale.load_settings(srv.store), "2k")
    assert plan["auto"] and plan["target"] == "2k"
    upscale.save_settings(srv.store, {"auto": "off"})
    assert "没有开启" in upscale.auto_plan(upscale.load_settings(srv.store), "1080p")["note"]
