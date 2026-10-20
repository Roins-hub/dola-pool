"""2.5.1：dola 网页上的 2.0 叫 Seedance 2.0 Fast，对外模型名改成 seedance-2.0-fast；旧名 seedance-2.0 仍可用。"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
    v.generate_video = _no
    v.resume_video = _no
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


def test_models_list_uses_fast_name():
    import server
    ids = [m["id"] for m in _run(server.list_models())["data"]]
    assert "seedance-2.0-fast" in ids and "seedance-2.5" in ids
    assert "seedance-2.0" not in ids                    # 旧名不再列出
    assert "doubao-seedance-2.0" in ids                 # 豆包的 Seedance 2.0（标准版）是另一个模型，不受影响


def test_old_name_is_alias_and_public_name_is_fast():
    import server
    for name in ("seedance-2.0", "seedance-2.0-fast", "seedance_v2.0", "Seedance-2.0-Fast"):
        assert server._resolve_model_for_duration(name, 10) == "seedance-2.0"
        assert server._public_model(server._resolve_model_for_duration(name, 10)) == "seedance-2.0-fast"
    assert server._public_model("seedance-2.5") == "seedance-2.5"
    # 计价照旧（内部标识不变）
    assert server._duration_cost("seedance-2.0-fast", 10) == server._duration_cost("seedance-2.0", 10)


def test_pool_cost_and_routing_accept_fast_name():
    import browser_pool
    import pure_api_gen
    assert browser_pool.duration_cost("seedance-2.0-fast", 10) == browser_pool.duration_cost("seedance-2.0", 10)
    assert pure_api_gen._map_model("seedance-2.0-fast", 10) == "seedance_v2.0"
