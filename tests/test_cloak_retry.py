"""5/10 秒浏览器提交被 710022002 拒绝时，改用 CloakBrowser 重试一次（2026-11-08）。"""
from contextlib import asynccontextmanager

import pytest

import browser_pool
import cloak_retry
import config
import video_worker_ui as ui
from test_task_deadline import _run


# ---------- 什么时候重试 ----------

@pytest.fixture
def retry_ready(monkeypatch):
    monkeypatch.setattr(config, "CLOAK_RETRY", True)
    monkeypatch.setattr(cloak_retry, "find_spec", lambda name: object())
    import account_transport
    monkeypatch.setattr(account_transport, "http_proxy", lambda account: "http://127.0.0.1:1")


def test_retry_ready_when_cloak_installed_and_proxy_bound(retry_ready):
    assert cloak_retry.unavailable_reason("acc1", 10) == ""
    assert cloak_retry.unavailable_reason("acc1", None) == ""


def test_no_retry_when_switched_off(retry_ready, monkeypatch):
    monkeypatch.setattr(config, "CLOAK_RETRY", False)
    assert "DOLA_CLOAK_RETRY" in cloak_retry.unavailable_reason("acc1", 10)


def test_no_retry_for_30s(retry_ready):
    assert "30 秒" in cloak_retry.unavailable_reason("acc1", 30)


def test_no_retry_without_cloakbrowser(retry_ready, monkeypatch):
    monkeypatch.setattr(cloak_retry, "find_spec", lambda name: None)
    assert "未安装" in cloak_retry.unavailable_reason("acc1", 10)


def test_no_retry_without_proxy(retry_ready, monkeypatch):
    import account_transport
    monkeypatch.setattr(account_transport, "http_proxy", lambda account: None)
    assert "代理" in cloak_retry.unavailable_reason("acc1", 10)


def test_no_retry_when_proxy_lookup_refuses(retry_ready, monkeypatch):
    import account_transport

    def refuse(account):
        raise RuntimeError("严格隔离模式下账号没有启用的绑定代理，禁止直连")
    monkeypatch.setattr(account_transport, "http_proxy", refuse)
    assert "禁止直连" in cloak_retry.unavailable_reason("acc1", 10)


# ---------- generate_video：先 patchright，被拒再 CloakBrowser ----------

class _Playwright:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Context:
    def __init__(self, name):
        self.name = name
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture
def browsers(monkeypatch):
    """记录每次在哪个浏览器里提交；outcomes 按顺序给出每次的结果（异常或返回值）。"""
    state = {"contexts": [], "outcomes": [], "cloak_opened": 0, "progress": [], "retries": 0}

    async def launch(p, account, headless=None, use_extension=False):
        context = _Context("patchright")
        state["contexts"].append(context)
        return context

    @asynccontextmanager
    async def cloak(account, use_extension):
        state["cloak_opened"] += 1
        context = _Context("cloak")
        state["contexts"].append(context)
        try:
            yield context
        finally:
            context.closed = True

    async def submit(context, *args):
        outcome = state["outcomes"].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome, browser=context.name)

    monkeypatch.setattr(ui, "async_playwright", _Playwright)
    monkeypatch.setattr(ui, "launch_account_context", launch)
    monkeypatch.setattr(ui, "_submit_and_poll", submit)
    monkeypatch.setattr(cloak_retry, "cloak_context", cloak)
    monkeypatch.setattr(cloak_retry, "unavailable_reason", lambda account, duration: "")
    return state


def _generate(state, duration=10):
    def on_poll(now, **fields):
        if fields.get("progress"):
            state["progress"].append(fields["progress"])

    def on_retry():
        state["retries"] += 1
    return _run(ui.generate_video("acc1", "猫", "9:16", duration, on_poll=on_poll, on_retry=on_retry))


def test_rejected_submit_is_retried_in_cloakbrowser(browsers):
    browsers["outcomes"] = [ui.SubmitRejectedError(ui.REJECTED_NOTE), {"video_url": "v"}]
    result = _generate(browsers)
    assert result["browser"] == "cloak"
    assert [c.name for c in browsers["contexts"]] == ["patchright", "cloak"]
    assert all(c.closed for c in browsers["contexts"])
    assert browsers["retries"] == 1
    assert any("CloakBrowser" in p for p in browsers["progress"])


def test_success_in_patchright_never_opens_cloakbrowser(browsers):
    browsers["outcomes"] = [{"video_url": "v"}]
    assert _generate(browsers)["browser"] == "patchright"
    assert browsers["cloak_opened"] == 0 and browsers["retries"] == 0


@pytest.mark.parametrize("error", [
    ui.RiskControlError("滑块 3 次未通过"),
    ui.LoginExpiredError("acc1 登录态失效"),
    TimeoutError("30s 未拿到 conversation_id"),
])
def test_other_failures_are_not_retried(browsers, error):
    browsers["outcomes"] = [error]
    with pytest.raises(type(error)):
        _generate(browsers)
    assert browsers["cloak_opened"] == 0


def test_no_retry_when_unavailable(browsers, monkeypatch):
    monkeypatch.setattr(cloak_retry, "unavailable_reason", lambda account, duration: "未安装 CloakBrowser")
    browsers["outcomes"] = [ui.SubmitRejectedError(ui.REJECTED_NOTE)]
    with pytest.raises(ui.SubmitRejectedError):
        _generate(browsers)
    assert browsers["cloak_opened"] == 0 and browsers["retries"] == 0


def test_rejected_again_in_cloakbrowser_raises_once(browsers):
    browsers["outcomes"] = [ui.SubmitRejectedError(ui.REJECTED_NOTE), ui.SubmitRejectedError(ui.REJECTED_NOTE)]
    with pytest.raises(ui.SubmitRejectedError) as exc:
        _generate(browsers)
    assert "CloakBrowser 重试也被拒" in str(exc.value)
    assert browsers["cloak_opened"] == 1
    assert all(c.closed for c in browsers["contexts"])


def test_rejection_counts_as_upstream_throttle():
    # 号池按 710022002 给账号和出口记限流、换号
    assert isinstance(ui.SubmitRejectedError(ui.REJECTED_NOTE), ui.RiskControlError)
    assert browser_pool.classify_upstream_failure(ui.REJECTED_NOTE) == "transient"


# ---------- _submit_and_poll：发送后拦下退出登录并判为被拒 ----------

class _Request:
    def __init__(self, url, resource_type="fetch"):
        self.url = url
        self.resource_type = resource_type


class _Route:
    def __init__(self, request):
        self.request = request
        self.aborted = self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


class _Box:
    async def click(self):
        pass


class _Keyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        # dola 拒绝后页面自己调退出登录
        if key == "Enter" and self.page.reject_on_send:
            self.page.routes += await self.page.context.fire("https://www.dola.com/passport/web/logout/")


class _Page:
    def __init__(self, context, reject_on_send):
        self.context = context
        self.reject_on_send = reject_on_send
        self.url = "https://www.dola.com/chat/local_1"
        self.keyboard = _Keyboard(self)
        self.routes = []

    async def goto(self, *a, **k):
        pass

    async def wait_for_timeout(self, ms):
        pass

    def get_by_text(self, *a, **k):
        raise RuntimeError("没有模型按钮")

    async def query_selector(self, selector):
        return _Box()

    async def screenshot(self, **k):
        pass


class _RoutingContext:
    def __init__(self, reject_on_send):
        self.handlers = []
        self.pages = [_Page(self, reject_on_send)]

    async def cookies(self, url):
        return []

    async def route(self, pattern, handler):
        self.handlers.append((pattern, handler))

    async def fire(self, url):
        routes = []
        for pattern, handler in self.handlers:
            if pattern.search(url):
                route = _Route(_Request(url))
                routes.append(route)
                await handler(route)
        return routes


class _Replies:
    def __init__(self, *a):
        pass

    def start(self):
        pass

    async def close(self):
        pass


@pytest.fixture
def quiet_page(monkeypatch):
    async def nothing(*a, **k):
        return None
    for name in ("dismiss_popups", "ensure_login_stable", "_preflight_balance",
                 "enter_video_mode", "type_prompt"):
        monkeypatch.setattr(ui, name, nothing)
    monkeypatch.setattr(ui, "raise_if_region_restricted", lambda page, account: None)
    monkeypatch.setattr(ui, "find_captcha_frame", lambda page: None)
    monkeypatch.setattr(ui, "BrowserReplies", _Replies)

    async def logged_in(page):
        return False
    monkeypatch.setattr(ui, "is_logged_out", logged_in)
    import browser_video_params
    monkeypatch.setattr(browser_video_params, "select_video_parameters", nothing)


def _submit(context):
    return _run(ui._submit_and_poll(context, "acc1", "猫", "9:16", 10, 60, "seedance_v2.0",
                                    None, None, None, None))


def test_logout_after_send_is_blocked_and_reported_as_rejection(quiet_page):
    context = _RoutingContext(reject_on_send=True)
    with pytest.raises(ui.SubmitRejectedError) as exc:
        _submit(context)
    assert "710022002" in str(exc.value)
    page = context.pages[0]
    assert page.routes and all(r.aborted and not r.continued for r in page.routes)


def test_without_rejection_waits_for_conversation_as_before(quiet_page):
    context = _RoutingContext(reject_on_send=False)
    with pytest.raises(TimeoutError, match="conversation_id"):
        _submit(context)
