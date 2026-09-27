"""cookie 批量文本导入的命名/去重/错误分支（键接 pool + cookie_login）。

通过注入桩模块让 cookie_login 可导入，再 monkeypatch 掉真实浏览器导入函数。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _stubs():
    def _mod(name):
        return types.ModuleType(name)

    # video_worker_ui
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

    # browser
    b = _mod("browser")
    b.EDGE_MARKER = ".dola-browser"
    b.LAUNCH_ARGS = []
    b.proxy_kwargs_for = lambda *a, **k: None
    async def _check(_name):
        return True
    b.check_login_state = _check
    sys.modules["browser"] = b

    # patchright
    pr = _mod("patchright")
    pr_api = _mod("patchright.async_api")
    class _FakeAsyncPlaywright:
        @staticmethod
        async def __aenter__(*_a, **_k):
            return _FakeAP()
        @staticmethod
        async def __aexit__(*_a, **_k):
            return False
    class _FakeAP:
        pass
    async def _async_playwright(*_a, **_k):
        return _FakeAsyncPlaywright()
    pr_api.async_playwright = _async_playwright
    sys.modules["patchright"] = pr
    sys.modules["patchright.async_api"] = pr_api
    yield


def _make_pool(tmp_path):
    import browser_pool, proxy_store
    import config
    config.POOL_DB_PATH = str(tmp_path / "pool_usage.db")
    proxy_store.DB_PATH = Path(config.POOL_DB_PATH)
    accounts_dir = tmp_path / "accounts"
    for name in ("acc9", "acc10"):
        d = accounts_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / ".dola-browser").write_text("")
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir), max_concurrency=1)
    _ = bp.accounts  # 触发扫描，创建 accounts_meta 行
    return bp


def test_batch_import_dedup_and_naming(tmp_path, monkeypatch):
    import cookie_login
    import browser_pool
    bp = _make_pool(tmp_path)
    # 已有一个 sessionid=sid-one 的账号
    bp.set_sessionid("acc9", "sid-one")
    bp.set_login_status("acc9", True)

    written: dict[str, str] = {}

    async def fake_import(name, cookie_data, require_login=True):
        # 假导入：建 meta 行 + 记录 sessionid
        (bp.accounts_dir / name).mkdir(parents=True, exist_ok=True)
        bp.set_sessionid(name, next(
            (c["value"] for c in cookie_data.get("cookies", []) if c["name"] == "sessionid"), ""
        ))
        bp.set_login_status(name, True)
        written[name] = "ok"
        return {"ok": True, "account": name, "verified": True}

    monkeypatch.setattr(cookie_login, "import_cookie_account", fake_import)
    raw = (
        "passport_csrf_token=aaa; sessionid=sid-one\n"   # 已存在 → updated
        "passport_csrf_token=bbb; sessionid=sid-two\n"   # 新 → acc11（跳过 acc9/acc10）
    )
    result = __import__("asyncio").get_event_loop().run_until_complete(
        cookie_login.import_cookie_accounts_from_text(bp, raw)
    )
    assert result["updated"] == ["acc9"]
    # 新号命名为 acc1（从 acc1 起顺延，acc9/acc10 被占也不影响前面的空闲名）
    assert result["created"] == ["acc1"]
    assert written.get("acc1") == "ok"
    # cookie 导入路径应把来源标记为 cookie
    assert next(x for x in bp.list_accounts() if x["name"] == "acc1")["source"] == "cookie"
