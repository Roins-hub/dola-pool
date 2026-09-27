"""`/v1/videos` 请求体时长别名：new-api 的 sora 任务插件把 duration 改写成 seconds
后转发过来，服务端必须一起认，否则 30 秒会被当成「没带时长」而落到默认 10 秒。

注入桩模块（patchright / video_worker_ui / dola_client），避免依赖浏览器运行时。
"""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _stubs():
    pr = types.ModuleType("patchright")
    pa = types.ModuleType("patchright.async_api")

    def _no_playwright(*_a, **_k):
        raise RuntimeError("stub")

    pa.async_playwright = _no_playwright
    pr.async_api = pa
    sys.modules.setdefault("patchright", pr)
    sys.modules.setdefault("patchright.async_api", pa)

    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError", "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))

    async def _gen(*_a, **_k):
        raise RuntimeError("stub")

    async def _resume(*_a, **_k):
        raise RuntimeError("stub")

    v.generate_video = _gen
    v.resume_video = _resume
    sys.modules["video_worker_ui"] = v

    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d
    yield


def _resolve(tmp_path, payload: dict) -> int:
    import config

    config.DB_PATH = str(tmp_path / "tasks.db")
    config.POOL_DB_PATH = str(tmp_path / "pool_usage.db")
    import server

    req = server.VideoGenRequest.model_validate(payload)
    return req.duration or req.seconds or req.duration_seconds or 10


def test_seconds_alias_is_honored(tmp_path):
    """画布只发 seconds：30 秒不能被当成默认 10 秒。"""
    assert _resolve(tmp_path, {"model": "seedance-2.5", "prompt": "x", "seconds": 30}) == 30


def test_duration_wins_over_seconds(tmp_path):
    assert _resolve(tmp_path, {"model": "seedance-2.5", "prompt": "x", "duration": 15}) == 15
    assert _resolve(
        tmp_path, {"model": "seedance-2.5", "prompt": "x", "duration": 15, "seconds": 30}
    ) == 15


def test_duration_seconds_alias(tmp_path):
    assert _resolve(tmp_path, {"model": "seedance-2.5", "prompt": "x", "duration_seconds": 30}) == 30


def test_missing_duration_defaults_to_ten(tmp_path):
    assert _resolve(tmp_path, {"model": "seedance-2.5", "prompt": "x"}) == 10