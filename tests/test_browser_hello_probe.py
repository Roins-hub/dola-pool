import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

import browser_hello_probe as probe


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


REPLY = 'event: STREAM_CHUNK\ndata: ' + json.dumps({"patch_op": [{"patch_value": {
    "content_block": [{"block_id": "assistant1", "content": {"text_block": {"text": "Hello there"}}}]
}}]}) + '\n\n'


def test_reply_parser_rejects_ack_html_and_error():
    assert probe.reply_from_sse(REPLY) == "Hello there"
    assert not probe.reply_from_sse('event: SSE_ACK\ndata: {"message":"你好"}\n\n')
    assert not probe.reply_from_sse('<div>Welcome! Old reply</div>')
    assert not probe.reply_from_sse(REPLY + 'event: STREAM_ERROR\ndata: {"error_code":710022002}\n\n')


@pytest.fixture
def fake_browser(monkeypatch):
    class Locator:
        first = property(lambda self: self)
        async def count(self): return 1
        async def is_visible(self): return True
        async def click(self, **kwargs): pass
        async def fill(self, value): page.input = value
        async def press(self, key):
            assert key == "Enter" and page.input == "你好"
            if page.emit:
                page.callback(response)
    class Page:
        url = "https://www.dola.com/chat"
        input = ""
        emit = True
        callback = None
        async def goto(self, url, **kwargs): self.url = url
        async def wait_for_timeout(self, ms): await asyncio.sleep(0.01)
        def get_by_text(self, *args, **kwargs): return Locator()
        def locator(self, *args): return Locator()
        def on(self, event, callback): self.callback = callback
    class Context:
        closed = False
        async def new_page(self): return page
        async def cookies(self, *args): return [{"name":"sessionid", "value":"synthetic"}]
        async def close(self): self.closed = True
    class Response:
        url = "https://www.dola.com/chat/completion"
        status = 200
        login = "1"
        request = SimpleNamespace(post_data_json={"prompt":"你好"})
        async def all_headers(self): return {"x-tt-agw-login": self.login}
        async def body(self): return REPLY.encode()
    class Playwright:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    page, context, response = Page(), Context(), Response()
    launches = []
    async def launch(p, account):
        launches.append(account)
        return context
    async def logged_out(page): return False
    monkeypatch.setitem(sys.modules, "browser", SimpleNamespace(
        is_cloak_cookie_account=lambda _: True, launch_account_context=launch,
        cookie_value=lambda cookies, name: "synthetic", _page_logged_out=logged_out))
    monkeypatch.setitem(sys.modules, "patchright.async_api", SimpleNamespace(async_playwright=Playwright))
    return page, context, response, launches


def test_browser_uses_account_launcher_and_closes(fake_browser):
    page, context, response, launches = fake_browser
    result = run(probe.probe_browser_hello("acc132", timeout=1))
    assert result["status"] == "ok" and result["reply"] == "Hello there"
    assert launches == ["acc132"] and context.closed


@pytest.mark.parametrize("login,status", [("0", "logged_out"), ("", "unconfirmed")])
def test_anonymous_or_unknown_login_never_passes(fake_browser, login, status):
    _, context, response, _ = fake_browser
    response.login = login
    result = run(probe.probe_browser_hello("acc132", timeout=1))
    assert result["status"] == status and not result["ok"] and context.closed


def test_no_response_is_unconfirmed(fake_browser):
    page, context, _, _ = fake_browser
    page.emit = False
    result = run(probe.probe_browser_hello("acc132", timeout=1))
    assert result["status"] == "unconfirmed" and not result["ok"] and context.closed


def test_cancel_releases_browser(fake_browser):
    page, context, _, _ = fake_browser
    page.emit = False
    async def scenario():
        task = asyncio.create_task(probe.probe_browser_hello("acc132", timeout=60))
        while not page.callback:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    run(scenario())
    assert context.closed


def test_cloak_manual_probes_serialize_and_never_call_http(tmp_path, monkeypatch):
    import browser_pool
    import config
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    for name in ("acc1", "acc2"):
        (tmp_path / "accounts" / name).mkdir(parents=True)
    pool = browser_pool.BrowserPool(accounts_dir=str(tmp_path / "accounts"), db_path=str(tmp_path / "pool.db"))
    monkeypatch.setattr(browser_pool, "is_cloak_cookie_account", lambda _: True)
    monkeypatch.setattr(pool, "egress_throttle_remaining", lambda _: 0)
    monkeypatch.setattr(pool, "_probe_network", lambda *a: pytest.fail("must not use HTTP"))
    persisted, active, peak = [], 0, 0
    monkeypatch.setattr(pool, "_persist_probe", lambda n, r, **kw: persisted.append(n) or r)
    async def fake_probe(name, **kwargs):
        nonlocal active, peak
        assert pool._locks[name].locked()
        active += 1; peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"ok": False, "status": "unconfirmed"}
    monkeypatch.setattr(probe, "probe_browser_hello", fake_probe)
    async def scenario():
        await asyncio.gather(pool.probe_hello_async("acc1", force=True), pool.probe_hello_async("acc2", force=True))
        async with pool._locks["acc1"]:
            result = await pool.probe_hello_async("acc1", force=True)
            assert result["status"] == "unconfirmed" and not result["probed"]
            with pytest.raises(RuntimeError): await pool.verify_account_detail("acc1")
    run(scenario())
    assert peak == 1 and not persisted
    assert not any(lock.locked() for lock in pool._locks.values())
