"""patchright persistent context 统一启动：显式代理 + 反检测参数。

代理必须显式传（不依赖系统代理——系统代理规则变化曾把 dola 分流到直连被墙，
见 DEVELOPMENT.md 第 5 节）。所有需要打开 dola 的脚本都走这里。
"""
from pathlib import Path
import re
import time
from urllib.parse import unquote, urlsplit

import config
import proxy_store


LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    # ---- 内存优化（2026-08-31，服务器内存紧张无法升级）----
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-component-update",
    "--disable-background-networking",
    "--renderer-process-limit=2",
    "--js-flags=--max-old-space-size=256",
    "--disable-features=Translate,MediaRouter,BackForwardCache",
]

# Cookie 导入账号的标记文件：存在时使用真实 Edge 引擎
EDGE_MARKER = ".dola-browser"

# 方悦多开（DolaMultiBrowser）同款 Windows 指纹模板（win11-edge-126-intel）。
# Cookie 导入账号在 Windows WebView2 里创建/保持登录；Linux Edge 直接复用 cookie
# 会被 Dola 当作“新设备/新环境”。这里照方悦的 fingerprint_config 注入同一套
# UA + navigator + client hints + WebGL/屏幕/时区伪装，尽量复现 WebView2 指纹。
WIN_EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.2592.81"
)


def _kwargs_from_url(raw: str) -> dict | None:
    """把代理 URL 解析成 Playwright proxy 参数。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    if not parts.hostname:
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    kwargs = {"server": f"{parts.scheme}://{parts.hostname}:{port}"}
    if parts.username:
        kwargs["username"] = unquote(parts.username)
    if parts.password:
        kwargs["password"] = unquote(parts.password)
    return kwargs


def proxy_kwargs_for(account: str | None = None,
                     proxy_info: dict | None = None) -> dict | None:
    """解析某个账号（或 override）应使用的 Playwright proxy 参数。

    账号已绑定代理 → 用绑定代理；否则回落 config.DOLA_PROXY。
    绑定代理走 proxy_store.proxy_url_for()，动态代理会自动带上该号自己的
    sticky session（= 独立出口 IP）。
    """
    if proxy_info:
        # 显式 override：调用方直接给记录，按静态形态处理
        kwargs = {"server": f"{proxy_info['protocol']}://{proxy_info['host']}:{proxy_info['port']}"}
        if proxy_info.get("username"):
            kwargs["username"] = proxy_info["username"]
        if proxy_info.get("password"):
            kwargs["password"] = proxy_info["password"]
        return kwargs
    if account:
        url = proxy_store.proxy_url_for(account)
        if url:
            return _kwargs_from_url(url)
    return _kwargs_from_url(config.PROXY or "")


async def launch_account_context(p, account: str, headless: bool = None, use_extension: bool = False):
    """启动 accounts/<account> profile，返回 BrowserContext。调用方负责 close。

    p: async_playwright() 实例
    headless: None = 用 config.HEADLESS
    """
    profile_dir = Path("accounts") / account
    if not profile_dir.exists():
        raise FileNotFoundError(
            f"账号 profile 不存在: {profile_dir}（先跑 python login.py {account}）"
        )
    launch_headless = config.HEADLESS if headless is None else headless
    use_edge = (profile_dir / EDGE_MARKER).exists()
    if use_edge:
        # Edge 型 cookie profile 统一有头运行，避免 headless 指纹再触发风控
        launch_headless = False
    args = list(LAUNCH_ARGS)
    apply_win_fingerprint = False
    if use_edge and config.EDGE_FINGERPRINT_ENABLED:
        # 伪装成方悦导出的 Windows WebView2（Windows 11 Edge 126）环境
        apply_win_fingerprint = True
        args.extend([
            f"--user-agent={WIN_EDGE_UA}",
            "--force-device-scale-factor=1",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ])
    if use_extension:
        if not config.EXTENSION_ENABLED:
            raise RuntimeError("Dola 扩展已禁用（DOLA_EXTENSION_ENABLED=0）")
        extension_dir = Path(config.EXTENSION_DIR).resolve()
        if not extension_dir.exists():
            raise FileNotFoundError(f"Dola 扩展目录不存在: {extension_dir}")
        # Chrome debugger API 扩展需要有头模式；扩展会拦截 skill/action-bar 响应。
        launch_headless = False
        args.extend([
            f"--disable-extensions-except={extension_dir}",
            f"--load-extension={extension_dir}",
        ])
    kwargs = {
        "headless": launch_headless,
        "args": args,
        "locale": "ja-JP",
        "timezone_id": "Asia/Tokyo",
    }
    # 真实 Edge 与 DolaMultiBrowser 使用的 WebView2 同源，
    # 能避免“换浏览器环境”触发登出。
    if use_edge:
        kwargs["channel"] = "msedge"
    proxy_kwargs = proxy_kwargs_for(account)
    if proxy_kwargs:
        kwargs["proxy"] = proxy_kwargs
    context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    return context


def cookie_value(cookies: list, name: str) -> str:
    """从 context.cookies() 结果里取指定 cookie 的值"""
    return next((c["value"] for c in cookies if c["name"] == name and c["value"]), "")


async def _page_logged_out(page) -> bool:
    """页面是否处于“未登录/被踢”状态（URL 登录路径 / 可见登录按钮 / 可见ログイン）。"""
    try:
        cur = (page.url or "").lower()
        if "/login" in cur or "/signin" in cur:
            return True
    except Exception:
        pass
    for selector in ('[class*="login-btn-header"]', "text=ログイン"):
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:
            continue
    return False


_CHAT_CHROME = [
    "新しいチャット", "今日は何をお手伝いしましょうか？", "AI生成", "最近",
    "Dolaについて", "動画を作成", "画像を作成", "文章作成", "翻訳", "宿題",
    "ログイン", "デスクトップアプリをダウンロード", "高速",
]


async def _assistant_reply_seen(page, prompt: str) -> bool:
    """发送后是否出现“非 UI 骨架、非自己回声”的回复内容。"""
    try:
        return await page.evaluate(
            """(args) => {
                const [prompt, chrome] = args;
                const els = [...document.querySelectorAll('div,p,span')];
                return els.some(e => {
                    const t = (e.innerText || '').trim();
                    if (!t || t.length < 2 || t.length > 800) return false;
                    if (t === prompt) return false;
                    if (chrome.includes(t)) return false;
                    return true;
                });
            }""",
            [prompt, _CHAT_CHROME],
        )
    except Exception:
        return False


async def chat_liveness_probe(account: str, prompt: str | None = None,
                              window: int | None = None) -> tuple[bool, str]:
    """向 dola 发送一句普通问候，做账号声誉/风控预检。

    - 发送后被踢成未登录 → (False, 'kicked')：账号“看着登录了、一碰就掉”，应下调度；
    - 出现正常回复 → (True, 'replied')：可正常收发；
    - 窗口内既没被踢、也没捕获到明确回复 → (True, 'no_reply_unverified')：保守视为可用。
    """
    window = window or config.VERIFY_CHAT_WINDOW
    prompt = (prompt or config.VERIFY_CHAT_PROMPT).strip() or "你好"
    from patchright.async_api import async_playwright
    async with async_playwright() as p:
        context = await launch_account_context(p, account)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000,
                            wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            for _ in range(4):
                try:
                    ok = page.get_by_text("OK", exact=True).first
                    if await ok.count() and await ok.is_visible():
                        await ok.click(timeout=1500)
                        break
                except Exception:
                    await page.wait_for_timeout(500)
            if await _page_logged_out(page):
                return False, "kicked_before"
            box = (await page.query_selector("textarea")
                   or await page.query_selector('[contenteditable="true"]'))
            if not box:
                return False, "no_input_box"
            try:
                await box.click()
            except Exception:
                await box.evaluate("(el) => el.focus()")
            await page.keyboard.type(prompt, delay=60)
            await page.wait_for_timeout(500)
            await page.keyboard.press("Enter")
            start = time.time()
            while time.time() - start < window:
                await page.wait_for_timeout(2000)
                if await _page_logged_out(page):
                    return False, "kicked"
                if await _assistant_reply_seen(page, prompt):
                    return True, "replied"
            return True, "no_reply_unverified"
        finally:
            await context.close()

async def check_login_state(account: str) -> bool:
    """无头打开 dola，返回登录态是否有效。

    Dola 对失效会话不是立即显示“ログイン”：实测匿名/失效会话在页面加载
    约 12-25 秒后才把头部登录按钮渲染出来。因此这里必须做“稳定窗口”轮询：
    持续 ~30 秒内都没有出现任何登录入口、且输入框已就绪，才判定登录有效。
    供 verify_login.py CLI 与面板「验证」按钮共用。
    """
    from patchright.async_api import async_playwright
    async with async_playwright() as p:
        context = await launch_account_context(p, account)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            for _ in range(4):
                try:
                    ok = page.get_by_text("OK", exact=True).first
                    if await ok.count() and await ok.is_visible():
                        await ok.click(timeout=1500)
                        break
                except Exception:
                    await page.wait_for_timeout(500)

            LOGIN_TERMS = ["ログイン", "ログ イン", "log in", "login", "sign in", "登录", "登入"]

            async def snapshot():
                cookies = await context.cookies("https://www.dola.com")
                has_sid = bool(cookie_value(cookies, "sessionid"))
                state = await page.evaluate(
                    """(terms) => {
                        const visible = n => n.offsetParent !== null;
                        const hits = [...document.querySelectorAll('button,a,[role="button"]')]
                            .filter(n => {
                                const t = (n.innerText || '').trim().toLowerCase();
                                return t && terms.includes(t) && visible(n);
                            });
                        const body = (document.body && document.body.innerText) || '';
                        return {
                            has_chat: !!(document.querySelector('textarea')
                                || document.querySelector('[contenteditable="true"]')
                                || document.querySelector('input[type="text"]')),
                            hits: hits.map(n => (n.innerText || '').trim().slice(0, 30)),
                            modal: /Googleで続ける|他の機能を利用するにはログインしてください/
                                .test(body),
                        };
                    }""", LOGIN_TERMS)
                state["has_sid"] = has_sid
                state["region"] = "/security/region-restricted" in page.url
                return state

            # 已出现登录入口/无 sessionid → 立即判失效
            first = await snapshot()
            if not first["has_sid"] or first["region"]:
                return False
            if first["hits"] or first["modal"]:
                return False

            # 未失效则继续观察，直到 ~30s 稳定窗口内确认一直没有登录入口
            clean_since = None
            start = time.time()
            while time.time() - start < 34:
                await page.wait_for_timeout(2500)
                s = await snapshot()
                if not s["has_sid"] or s["region"] or s["hits"] or s["modal"]:
                    return False
                if s["has_chat"]:
                    now = time.time()
                    if clean_since is None:
                        clean_since = now
                    elif now - clean_since >= 6 and now - start >= 24:
                        return True
                else:
                    clean_since = None
            return False
        finally:
            await context.close()
