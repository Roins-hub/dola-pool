"""Apple 官方授权登录：独立资料目录、账号代理、面板输入双重验证码。"""
import asyncio
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import proxy_store
from account_transport import http_proxy
from browser import LAUNCH_ARGS, _kwargs_from_url, _save_cloak_cookies, _dola_cookie

APPLE_HOSTS = {"appleid.apple.com", "account.apple.com", "idmsa.apple.com"}
CONTINUE_RE = re.compile(r"^(?:继续|繼續|続ける|続行|Continue|同意|Agree|允许|Allow)$", re.I)
LOGIN_RE = re.compile(r"^(?:登录|登入|ログイン|Log in|Sign in)$", re.I)
CODE_SELECTOR = ('input[autocomplete="one-time-code"], input[id^="char"], '
                 'input[id*="security-code"], input[name="securityCode"]')
SESSION_FILE = ".apple-session-cookies.json"
LOGIN_SECONDS = 600


class AppleLoginError(RuntimeError):
    """可以展示的固定错误说明，不带浏览器异常和授权 URL。"""


class AppleLoginControl:
    """验证码只在内存队列里传递，不进入 JOBS、日志或凭据库。"""

    def __init__(self, update):
        self.update = update
        self.challenge_id = ""
        self.kind = ""
        self.actions = asyncio.Queue(maxsize=1)

    def clear(self):
        self.challenge_id = self.kind = ""
        while not self.actions.empty():
            self.actions.get_nowait()
        self.update(challenge=None)

    def submit(self, challenge_id: str, code: str = ""):
        if not self.challenge_id or challenge_id != self.challenge_id or self.actions.full():
            raise ValueError("登录页面已变化，请刷新登录进度后重试")
        code = code.strip()
        if self.kind == "code" and not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("请输入 Apple 的六位验证码")
        self.actions.put_nowait(code if self.kind == "code" else "continue")

    async def ask(self, kind: str, stage: str, remaining: float):
        self.clear()
        self.kind = kind
        self.challenge_id = uuid.uuid4().hex
        self.update(stage=stage, challenge={"id": self.challenge_id, "kind": kind,
                                          "expires_at": time.time() + remaining})
        try:
            return await asyncio.wait_for(self.actions.get(), timeout=remaining)
        except TimeoutError:
            raise AppleLoginError("Apple 登录等待超时，请重新添加") from None
        finally:
            self.clear()


def apple_host(url: str) -> bool:
    return urlsplit(url).hostname in APPLE_HOSTS and urlsplit(url).scheme == "https"


async def visible(scope, selector):
    items = scope.locator(selector)
    for index in range(await items.count()):
        item = items.nth(index)
        if await item.is_visible():
            return item
    return None


async def apple_form(context):
    """只在 Apple 官方域的页面或框架里输入凭据。"""
    for page in reversed(context.pages):
        if page.is_closed():
            continue
        for frame in reversed(page.frames):
            if apple_host(frame.url):
                return frame
    return None


async def _continue(form):
    button = await visible(form, "#sign-in, #sign-in-button, #continue-password, #continue")
    if button is not None and await button.is_enabled():
        await button.click(timeout=5000)
        return True
    buttons = form.get_by_role("button", name=CONTINUE_RE)
    for index in range(await buttons.count()):
        button = buttons.nth(index)
        if await button.is_visible() and await button.is_enabled():
            await button.click(timeout=5000)
            return True
    return False


async def _fill_code(form, code: str):
    inputs = form.locator(CODE_SELECTOR)
    boxes = [inputs.nth(i) for i in range(await inputs.count())
             if await inputs.nth(i).is_visible()]
    if len(boxes) == 6:
        for box, digit in zip(boxes, code):
            await box.fill(digit, timeout=4000)
    elif len(boxes) == 1:
        await boxes[0].fill(code, timeout=4000)
    else:
        raise AppleLoginError("Apple 验证码页面已变化，请重新登录")
    try:
        await _continue(form)
    except Exception:
        # 六格验证码通常自动提交，授权页关闭后不再点击。
        if not form.is_detached():
            raise


async def complete_apple_login(context, email: str, password: str, control: AppleLoginControl):
    deadline = time.monotonic() + LOGIN_SECONDS
    submitted_user = submitted_password = False
    while time.monotonic() < deadline:
        cookies = await context.cookies("https://www.dola.com")
        if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
            return
        form = await apple_form(context)
        if form is None:
            await asyncio.sleep(1)
            continue
        # 不读取或转发 Apple 页面原文，避免把账号、验证码或授权参数写进任务记录。
        alert = await visible(form, '[role="alert"], .error-message, #errMsg')
        if alert is not None and (await alert.inner_text()).strip():
            raise AppleLoginError("Apple 拒绝了本次登录，请检查账号、密码或验证码后重试")
        code_box = await visible(form, CODE_SELECTOR)
        if code_box is not None:
            code = await control.ask("code", "等待 Apple 六位验证码，请先在受信任设备上允许登录",
                                     max(1, deadline - time.monotonic()))
            await _fill_code(form, code)
            code = ""
            await asyncio.sleep(3)
            continue
        user = await visible(form, '#account_name_text_field, #account_name')
        pwd = await visible(form, '#password_text_field, input[autocomplete="current-password"]')
        if pwd is not None and not submitted_password:
            if user is not None and not submitted_user:
                await user.fill(email, timeout=5000)
                submitted_user = True
            await pwd.fill(password, timeout=5000)
            if not await _continue(form):
                raise AppleLoginError("Apple 登录提交按钮不可用，请稍后重试")
            password = ""
            submitted_password = True
            control.update(stage="Apple 正在验证账号，可能需要在受信任设备上允许登录")
            await asyncio.sleep(3)
            continue
        if user is not None and not submitted_user:
            await user.fill(email, timeout=5000)
            if not await _continue(form):
                raise AppleLoginError("Apple 账号提交按钮不可用，请稍后重试")
            submitted_user = True
            await asyncio.sleep(2)
            continue
        # 首次授权可能询问共享/隐藏邮箱：保留 Apple 默认设置，由用户确认继续。
        consent = form.get_by_role("button", name=CONTINUE_RE)
        if submitted_password and await consent.count():
            enabled = [consent.nth(i) for i in range(await consent.count())
                       if await consent.nth(i).is_visible() and await consent.nth(i).is_enabled()]
            if enabled and pwd is None and user is None:
                await control.ask("continue", "Apple 等待授权确认，继续使用当前邮箱共享设置登录 Dola",
                                  max(1, deadline - time.monotonic()))
                await enabled[0].click(timeout=5000)
                await asyncio.sleep(3)
        await asyncio.sleep(1)
    raise AppleLoginError("10 分钟内未完成 Apple 授权，请重试；需要验证码时请在面板输入")


async def apple_login_flow(account: str, email: str, password: str, control: AppleLoginControl):
    """成功后才把隐藏的临时资料目录交给号池；失败、取消都关闭并清理本次目录。"""
    from patchright.async_api import async_playwright

    if not proxy_store.get_account_proxy(account):
        raise AppleLoginError("Apple 登录需要先选择一个已启用的账号代理")
    proxy = http_proxy(account)
    if not proxy:
        raise AppleLoginError("账号代理不可用，Apple 登录已停止")
    root = Path("accounts")
    root.mkdir(exist_ok=True)
    target = root / account
    if target.exists():
        raise AppleLoginError("账号资料目录已存在，请使用新的账号名")
    staging = Path(tempfile.mkdtemp(prefix=f".apple-{account}-", dir=root))
    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(staging), headless=False, args=LAUNCH_ARGS,
                locale="ja-JP", timezone_id="Asia/Tokyo", proxy=_kwargs_from_url(proxy))
            try:
                control.update(stage="正在打开 Dola 的 Apple 登录入口")
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
                entry = page.get_by_test_id("login_third_apple")
                try:
                    await entry.wait_for(state="visible", timeout=15000)
                except Exception:
                    await page.get_by_role("button", name=LOGIN_RE).first.click(timeout=20000)
                    await entry.wait_for(state="visible", timeout=15000)
                await entry.click(timeout=8000)
                control.update(stage="正在等待 Apple 官方授权页")
                await complete_apple_login(context, email, password, control)
                password = ""
                _save_cloak_cookies(staging / SESSION_FILE,
                                   [c for c in await context.cookies() if _dola_cookie(c)])
            finally:
                await context.close()
        staging.rename(target)
    except asyncio.CancelledError:
        raise
    except AppleLoginError:
        raise
    except Exception:
        raise AppleLoginError("Apple 登录页面或代理连接失败，请检查线路后重试") from None
    finally:
        control.clear()
        if staging.exists():
            shutil.rmtree(staging)
