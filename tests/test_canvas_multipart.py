"""画布（infinite-canvas / Sora 风格）兼容：CORS + multipart 表单折成内部请求体。

画布是纯前端，浏览器直连号池，所以：
1. 必须回 CORS 头，否则浏览器直接拦掉；
2. 视频创建走 `POST /v1/videos` 的 multipart（字段 seconds/size/image[]），
   号池原来的 JSON 解析会报 invalid json body。
"""
from __future__ import annotations

import asyncio
import io
import sys
import types

import pytest
from starlette.datastructures import FormData, UploadFile


@pytest.fixture(autouse=True)
def _stubs():
    pa = types.ModuleType("patchright.async_api")

    def _no_playwright(*_a, **_k):
        raise RuntimeError("stub")

    pa.async_playwright = _no_playwright
    pr = types.ModuleType("patchright")
    pr.async_api = pa
    sys.modules.setdefault("patchright", pr)
    sys.modules.setdefault("patchright.async_api", pa)

    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError", "LoginExpiredError", "RiskControlError"):
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


class _FakeRequest:
    """只实现 _raw_from_multipart 用到的那点接口。"""

    def __init__(self, form: FormData):
        self._form = form
        self.headers = {"content-type": "multipart/form-data; boundary=stub"}

    async def form(self) -> FormData:
        return self._form


def _upload(filename: str, data: bytes) -> UploadFile:
    return UploadFile(io.BytesIO(data), filename=filename)


def _to_raw(form: FormData, tmp_path) -> dict:
    import config

    config.DB_PATH = str(tmp_path / "tasks.db")
    config.POOL_DB_PATH = str(tmp_path / "pool_usage.db")
    import server

    # 注意：不要用 asyncio.run() —— 它会关掉主线程的默认事件循环，
    # 而仓库里其它测试用的是 asyncio.get_event_loop()，会被带崩。
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(server._raw_from_multipart(_FakeRequest(form)))
    finally:
        loop.close()


def test_cors_middleware_is_installed(tmp_path):
    from fastapi.middleware.cors import CORSMiddleware

    import config

    config.DB_PATH = str(tmp_path / "tasks.db")
    config.POOL_DB_PATH = str(tmp_path / "pool_usage.db")
    import server

    assert any(m.cls is CORSMiddleware for m in server.app.user_middleware)


def test_multipart_maps_seconds_size_and_images(tmp_path):
    form = FormData([
        ("model", "seedance-2.5"),
        ("prompt", "一只猫追蝴蝶"),
        ("seconds", "6"),
        ("size", "720x1280"),
        ("resolution_name", "1080p"),
        ("generate_audio", "true"),
        ("image[]", _upload("ref.png", b"\x89PNG\r\n\x1a\nstub")),
    ])
    raw = _to_raw(form, tmp_path)
    assert raw["model"] == "seedance-2.5"
    assert raw["prompt"] == "一只猫追蝴蝶"
    assert raw["duration"] == 6          # seconds -> duration
    assert raw["size"] == "720x1280"
    assert raw["reference_images"][0].startswith("data:image/png;base64,")


def test_multipart_duration_wins_over_seconds(tmp_path):
    form = FormData([("prompt", "x"), ("duration", "15"), ("seconds", "6")])
    assert _to_raw(form, tmp_path)["duration"] == 15


def test_multipart_ignores_reference_video_and_audio(tmp_path):
    form = FormData([
        ("prompt", "x"),
        ("video[]", _upload("ref.mp4", b"stub")),
        ("audio[]", _upload("ref.mp3", b"stub")),
    ])
    raw = _to_raw(form, tmp_path)
    assert "reference_images" not in raw


def test_first_frame_maps_to_reference_image(tmp_path):
    form = FormData([
        ("prompt", "x"),
        ("first_frame", _upload("a.jpg", b"\xff\xd8\xff")),
    ])
    raw = _to_raw(form, tmp_path)
    assert raw["reference_images"][0].startswith("data:image/jpeg;base64,")


def test_data_url_from_upload_defaults_to_png(tmp_path):
    import config

    config.DB_PATH = str(tmp_path / "tasks.db")
    config.POOL_DB_PATH = str(tmp_path / "pool_usage.db")
    import server

    assert server._data_url_from_upload("noext", b"x").startswith("data:image/png;base64,")
    assert server._data_url_from_upload("a.WEBP", b"x").startswith("data:image/webp;base64,")
