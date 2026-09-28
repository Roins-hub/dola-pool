"""任意时长（4~30 秒）与扣点口径的单测（不联网、不开浏览器）。

背景：
  1) 扣点改成与 dola-pool-cookie 一致 —— **只看模型，不看时长**；
     旧实现是「白名单查不到就返回 1 点」，属于少扣：一旦放开任意时长，
     每个未登记时长都会被按 1 点记账，一个号一天能跑 4 条。
  2) 时长白名单分级放开 —— 原生档位（含 30 秒）**永远放行**，只有非原生档位
     受 NATIVE_DURATION_MAX 约束。30 秒是线上主力流量，被夹掉就是灾难。

注入桩模块（patchright），避免依赖浏览器运行时（同 test_video_request_aliases）。
"""
from __future__ import annotations

import sys
import types

import pytest


def _install_stubs() -> None:
    pr = types.ModuleType("patchright")
    pa = types.ModuleType("patchright.async_api")

    def _no_playwright(*_a, **_k):
        raise RuntimeError("stub")

    pa.async_playwright = _no_playwright
    pr.async_api = pa
    sys.modules.setdefault("patchright", pr)
    sys.modules.setdefault("patchright.async_api", pa)


_install_stubs()

import config  # noqa: E402
import server  # noqa: E402


@pytest.fixture(autouse=True)
def _knob_off(monkeypatch):
    """每个用例默认从「关闭」出发，保证旧行为可回归。"""
    _install_stubs()
    monkeypatch.setattr(config, "NATIVE_DURATION_MAX", 0)
    yield


def _set_knob(monkeypatch, value):
    monkeypatch.setattr(config, "NATIVE_DURATION_MAX", value)


# --- 时长全集 ---------------------------------------------------------------

def test_all_durations_off_is_native_only():
    assert config.all_durations() == [5, 10, 15, 30]


def test_all_durations_on_keeps_native_and_adds_range(monkeypatch):
    _set_knob(monkeypatch, 15)
    got = config.all_durations()
    assert 4 in got and 15 in got
    assert 30 in got, "30 秒是原生档位，不能被上限夹掉"
    assert 20 not in got, "上限设成 15 时不应出现 20"


def test_all_durations_on_30_covers_full_range(monkeypatch):
    _set_knob(monkeypatch, 30)
    assert config.all_durations() == list(range(4, 31))


# --- 时长可用性：原生档位永不设闸 -------------------------------------------

def test_native_durations_always_supported_including_30(monkeypatch):
    """关键回归：把上限设成 15，30 秒也必须仍然可下发。"""
    _set_knob(monkeypatch, 15)
    for d in (5, 10, 15, 30):
        assert server._duration_supported("seedance-2.5", d) is True
        assert server._duration_supported("seedance-2.0", d) is True


def test_non_native_rejected_when_off():
    assert server._duration_supported("seedance-2.5", 20) is False


def test_non_native_only_on_seedance_25(monkeypatch):
    _set_knob(monkeypatch, 30)
    assert server._duration_supported("seedance-2.5", 20) is True
    assert server._duration_supported("seedance-2.0", 20) is False


def test_non_native_outside_range_rejected(monkeypatch):
    _set_knob(monkeypatch, 15)
    assert server._duration_supported("seedance-2.5", 20) is False
    assert server._duration_supported("seedance-2.5", 3) is False


# --- 模型/时长组合解析 -------------------------------------------------------

def test_resolve_native_durations_use_20():
    for d in (5, 10, 15):
        assert server._resolve_model_for_duration("", d) == "seedance-2.0"


def test_resolve_30_uses_25():
    assert server._resolve_model_for_duration("", 30) == "seedance-2.5"


def test_resolve_non_native_uses_25_when_enabled(monkeypatch):
    _set_knob(monkeypatch, 30)
    assert server._resolve_model_for_duration("", 20) == "seedance-2.5"


def test_resolve_non_native_is_none_when_disabled():
    assert server._resolve_model_for_duration("", 20) is None


def test_resolve_explicit_20_cannot_do_non_native(monkeypatch):
    """显式点名 2.0 时不悄悄换型号（本项目既有契约）。"""
    _set_knob(monkeypatch, 30)
    assert server._resolve_model_for_duration("seedance-2.0", 20) is None
    assert server._resolve_model_for_duration("seedance-2.5", 20) == "seedance-2.5"


def test_supported_durations_for_version(monkeypatch):
    _set_knob(monkeypatch, 30)
    assert server._supported_durations_for("seedance-2.0") == [5, 10, 15]
    assert 20 in server._supported_durations_for("seedance-2.5")


# --- 扣点：只看模型，不看时长（对齐 dola-pool-cookie）------------------------

def test_duration_cost_is_flat_per_model():
    assert server._duration_cost("seedance-2.5", 5) == 2
    assert server._duration_cost("seedance-2.5", 30) == 2
    assert server._duration_cost("seedance-2.0", 5) == 3
    assert server._duration_cost("seedance-2.0", 15) == 3


def test_duration_cost_unknown_model_is_most_expensive():
    """未知模型按最贵的算：宁可少派，也不要拿号去白撞额度。"""
    assert server._duration_cost("__unknown__", 10) == config.DEFAULT_CREDIT_COST
    assert config.DEFAULT_CREDIT_COST == 3


def test_duration_cost_never_returns_the_old_undercharge():
    """回归：旧实现对未登记的 (模型, 时长) 返回 1 点（少扣）。"""
    assert server._duration_cost("seedance-2.5", 7) >= 2
    assert server._duration_cost("seedance-2.0", 30) >= 3


def test_browser_pool_duration_cost_matches_server():
    from browser_pool import BrowserPool

    assert BrowserPool._duration_cost(None, "seedance-2.5", 20) == 2
    assert BrowserPool._duration_cost(None, "seedance-2.0", 5) == 3
    assert BrowserPool._duration_cost(None, "???", 5) == config.DEFAULT_CREDIT_COST


# --- 面板必须跟着后端的档位走（前端硬编码会导致"后端认了、面板勾不到"）------

def _panel_html() -> str:
    from pathlib import Path

    return Path("web/index.html").read_text(encoding="utf-8")


def test_panel_duration_checkboxes_are_not_hardcoded():
    """回归：面板原先把允许时长写死成 [5,10,15,30]。

    这样即使后端放开了任意时长（DOLA_NATIVE_DURATION_MAX），面板也勾不到新档位，
    功能等于从面板上用不了。
    """
    html = _panel_html()
    assert "SUPPORTED_DURATIONS.map(" in html, "时长勾选项应遍历 SUPPORTED_DURATIONS"
    assert "SUPPORTED_DURATIONS.filter(" in html, "已勾选时长的收集应基于 SUPPORTED_DURATIONS"
    # 写死的使用点（map/filter）必须已清除；只允许保留一处兜底默认值声明。
    assert "[5,10,15,30].map(" not in html
    assert "[5,10,15,30].filter(" not in html


def test_panel_takes_durations_from_admin_keys_api():
    """面板要从 /api/admin/keys 的 supported_durations 更新档位。"""
    html = _panel_html()
    assert "r.supported_durations" in html


def test_admin_keys_response_exposes_supported_durations():
    """后端必须把当前档位下发给面板。"""
    import inspect

    src = inspect.getsource(server.admin_keys)
    assert "supported_durations" in src
    assert "config.all_durations()" in src
