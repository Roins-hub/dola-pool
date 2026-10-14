"""2.2.5：放开 21:9；30 秒每次出片最多等 2 小时。

- dola 网页的比例选项本来就有 21:9（协议层 VALID_RATIOS 也认），只是 Ark 风格接口把它换成了默认比例；
- 2026-10-14 线上：30 秒单 dola 报「预计等待 60 分钟」，我们正好等满 60 分钟判超时，片子之后才出来（30.08 秒）。
"""
from __future__ import annotations

import sys
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


def test_ark_ratio_accepts_21_9():
    import server
    assert server._map_ark_ratio("21:9") == "21:9"
    assert server._map_ark_ratio("16:9") == "16:9"
    assert server._map_ark_ratio("adaptive") is None
    assert server._map_ark_ratio("2:1") is None


def test_size_maps_to_21_9():
    import server
    assert server._resolve_ratio("1680x720", None) == "21:9"
    assert server._resolve_ratio("2520x1080", None) == "21:9"
    assert server._resolve_ratio(None, "21:9") == "21:9"
    assert server.RATIO_TO_DIMENSIONS["21:9"] == (1680, 720)


def test_protocol_keeps_21_9():
    from protocol import dola_pure_api as dola
    assert dola.normalize_ratio("21:9") == "21:9"
    assert "21:9" in dola.VALID_RATIOS


def test_30s_waits_up_to_two_hours_by_default(monkeypatch):
    import importlib
    monkeypatch.delenv("DOLA_30S_TIMEOUT_MAX", raising=False)
    assert importlib.reload(config).LONG_VIDEO_TIMEOUT_MAX == 7200
    assert config.LONG_VIDEO_TIMEOUT_MAX > config.PURE_API_TIMEOUT_MAX
