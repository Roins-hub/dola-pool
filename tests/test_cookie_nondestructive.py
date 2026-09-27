"""非破坏 cookie 导入：验证失败保留 profile + 落盘 cookie_state.json（对齐 dola-pool-cookie）。"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _stubs():
    def _mod(name):
        return types.ModuleType(name)

    v = _mod("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError", "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))
    async def _g(*_a, **_k):
        raise RuntimeError("stub")
    async def _r(*_a, **_k):
        raise RuntimeError("stub")
    v.generate_video = _g
    v.resume_video = _r
    sys.modules["video_worker_ui"] = v

    d = _mod("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d

    b = _mod("browser")
    b.EDGE_MARKER = ".dola-browser"
    b.LAUNCH_ARGS = []
    b.proxy_kwargs_for = lambda *a, **k: None
    async def _login_fail(_name):
        return False
    b.check_login_state = _login_fail
    sys.modules["browser"] = b

    pr = _mod("patchright")
    pr_api = _mod("patchright.async_api")

    class _FakePage:
        async def goto(self, *a, **k):
            pass
        async def evaluate(self, *a, **k):
            return {}
        async def wait_for_timeout(self, *a, **k):
            pass

    class _FakeCtx:
        def __init__(self):
            self._cookies: list[dict] = []
        @property
        def pages(self):
            return []
        async def new_page(self):
            return _FakePage()
        async def add_cookies(self, cookies):
            self._cookies = [dict(c) for c in cookies]
        async def cookies(self, url):
            return [{"name": c["name"], "value": c["value"]} for c in self._cookies]
        async def close(self):
            pass

    class _FakeChromium:
        async def launch_persistent_context(self, profile_dir, **kwargs):
            return _FakeCtx()

    class _FakePW:
        chromium = _FakeChromium()

    class _FakeAsyncPlaywright:
        async def __aenter__(self):
            return _FakePW()
        async def __aexit__(self, *a):
            return False

    def _async_playwright(*_a, **_k):
        return _FakeAsyncPlaywright()

    pr_api.async_playwright = _async_playwright
    sys.modules["patchright"] = pr
    sys.modules["patchright.async_api"] = pr_api
    yield


def test_verify_failure_keeps_profile_and_writes_state(tmp_path, monkeypatch):
    import config
    import cookie_login

    config.HEADLESS = True
    # 强制验证失败
    async def _check(name):
        return False
    monkeypatch.setattr(cookie_login, "check_login_state", _check)

    cookie_data = {
        "cookies": [
            {"name": "sessionid", "value": "sid-abc", "domain": ".dola.com", "path": "/"},
            {"name": "passport_csrf_token", "value": "tok-1", "domain": ".dola.com", "path": "/"},
        ]
    }
    accounts_dir = Path("accounts")
    created_now = not accounts_dir.exists()
    try:
        result = __import__("asyncio").get_event_loop().run_until_complete(
            cookie_login.import_cookie_account("accX", cookie_data, require_login=True)
        )

        # 非破坏：profile 仍在
        profile = accounts_dir / "accX"
        assert profile.is_dir(), "验证失败不应删除 profile"
        # 落盘 cookie_state.json
        state_file = profile / "cookie_state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["source"] == "cookie"
        assert state["cookies"]["sessionid"]["value"] == "sid-abc"
        # 返回未验证 + 原因
        assert result["verified"] is False
        assert result["verify_error"]
    finally:
        # 清理，避免污染工作区：只删本次创建的 accounts 树
        import shutil
        if created_now and accounts_dir.exists():
            shutil.rmtree(str(accounts_dir), ignore_errors=True)
        else:
            shutil.rmtree(str(accounts_dir / "accX"), ignore_errors=True)
