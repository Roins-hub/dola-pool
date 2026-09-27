"""自动添加账号：Google OAuth 自动登录（邮箱----密码----TOTP 密钥），存 profile。

用法：python add_account.py <账号名> "email----password----totp_secret"

只登录存 profile，不发送任何视频请求。失败抛 RuntimeError（供面板 jobs 捕获）。
Google 页有反自动化检测，用 headful 提高成功率；失败会截图 dbg_add_account.png。
"""
import asyncio
import base64
import hashlib
import hmac
import struct
import sys
import time
from pathlib import Path

from patchright.async_api import async_playwright

from browser import LAUNCH_ARGS, proxy_kwargs_for


def totp(secret: str, period: int = 30, digits: int = 6) -> str:
    """标准 TOTP（RFC 6238），Google Authenticator 兼容。"""
    secret = secret.replace(" ", "").upper()
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    counter = int(time.time() // period)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[19] & 15
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % (10 ** digits)
    return str(code).zfill(digits)


async def google_login(g, email: str, password: str, secret: str):
    """状态机跑完 Google 登录：选账号/邮箱/密码/2FA/授权同意页/年龄确认，直到离开 accounts.google.com。"""
    for step in range(20):
        await g.wait_for_timeout(2500)
        if "accounts.google.com" not in g.url:
            print("[google] 已离开 Google 域（OAuth 回跳）", flush=True)
            return
        # 1) 选择账号页（严格用 URL 判定，同意页也显示邮箱会误判）
        if "accountchooser" in g.url:
            acc = g.locator(f"text={email}").first
            if await acc.count() and await acc.is_visible():
                await acc.click(timeout=5000)
                print("[google] 选择账号页 → 点账号", flush=True)
                continue
        # 2) 邮箱页：Google 在跳转过渡时会保留一个隐藏的 identifierId，必须只处理可见输入框。
        identifier = g.locator("#identifierId").first
        if await identifier.count() and await identifier.is_visible():
            await identifier.fill(email)
            # Google 新版页面偶尔有过渡层拦截鼠标，DOM click 可安全触发同一提交事件。
            await g.locator("#identifierNext").evaluate("e => e.click()")
            print("[google] 邮箱页 → 提交", flush=True)
            await g.wait_for_timeout(1500)
            continue
        # 3) 密码页
        pwd = g.locator('input[name="Passwd"]').first
        if await pwd.count() and await pwd.is_visible():
            await g.wait_for_timeout(600)
            await pwd.fill(password)
            await g.locator("#passwordNext").evaluate("e => e.click()")
            print("[google] 密码页 → 提交", flush=True)
            await g.wait_for_timeout(1500)
            continue
        # 4) 授权同意页（继续/続行/Continue）——优先于 2FA 检测，同意页无输入框
        clicked = False
        for sel in ["#submit_button",
                    "[role='button']:has-text('続行')", "button:has-text('続行')",
                    "[role='button']:has-text('继续')", "button:has-text('继续')",
                    "[role='button']:has-text('Continue')", "button:has-text('Continue')"]:
            try:
                loc = g.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    print(f"[google] 同意页 → 点击 {sel}", flush=True)
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            continue
        # 5) 2FA 页
        if await g.locator('input[type="tel"]').count():
            if not secret:
                raise RuntimeError("Google 要求两步验证，但未提供 TOTP 密钥")
            code = totp(secret)
            print(f"[google] 2FA 页 → TOTP={code}", flush=True)
            await g.fill('input[type="tel"]', code)
            await g.click("#totpNext")
            continue
        # 6) dola 年龄确认弹窗（18+）——JS 直接派发点击，绕过选择器匹配问题
        if await g.locator("text=18").count():
            ok = await g.evaluate("""() => {
                const els = [...document.querySelectorAll('button, [role="button"], div, span')];
                const t = els.find(e => (e.textContent || '').trim() === 'OK' && e.childElementCount === 0);
                if (t) { t.click(); return true; }
                return false;
            }""")
            print(f"[dola] 年龄确认 → JS click OK = {ok}", flush=True)
            await g.wait_for_timeout(1500)
            continue
        txt = await g.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)")
        print(f"[google] step{step} 未识别页面 url={g.url[:80]} text={txt[:200]}", flush=True)
    if "accounts.google.com" in g.url:
        await g.screenshot(path="dbg_google2.png")
        raise RuntimeError("Google 登录 20 步内未完成（截图 dbg_google2.png）")


async def add_account_flow(account: str, email: str, password: str, secret: str,
                           force_login: bool = False) -> bool:
    """完整加号流程；成功返回 True，任何失败抛 RuntimeError。"""
    profile_dir = Path("accounts") / account
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        kwargs = {"headless": False, "args": LAUNCH_ARGS,
                  "locale": "ja-JP", "timezone_id": "Asia/Tokyo"}
        proxy_kwargs = proxy_kwargs_for(account)
        if proxy_kwargs:
            kwargs["proxy"] = proxy_kwargs
        context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        try:
            if force_login:
                # 强制重新登录：先清掉旧的 dola 会话 cookie，避免“已有登录态”跳过
                await context.clear_cookies()
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000)
            await page.wait_for_timeout(4000)

            cookies = await context.cookies("https://www.dola.com")
            if not force_login and any(c["name"] == "sessionid" and c["value"] for c in cookies):
                print(f"[{account}] 已有登录态，无需登录", flush=True)
                return True

            # 点登录入口 -> Google 选项。Dola 现行登录弹窗不再是 .semi-modal-wrap，
            # 直接等“Googleで続ける”按钮可见即可（失效会话下头部ログイン约 16-25s 才渲染）。
            google_opt = page.get_by_text("Googleで続ける", exact=False).first
            try:
                await google_opt.wait_for(state="visible", timeout=12000)
            except Exception:
                await page.get_by_text("ログイン", exact=True).first.click(timeout=25000)
                await google_opt.wait_for(state="visible", timeout=15000)
            await google_opt.click(timeout=10000)

            # Google 页可能在 popup 或当前 tab，轮询最多 25s 等它出现
            g = None
            deadline = time.time() + 25
            while time.time() < deadline:
                if "accounts.google.com" in page.url:
                    g = page
                    break
                g = next(
                    (pg for pg in context.pages if "accounts.google.com" in pg.url),
                    None,
                )
                if g is not None:
                    break
                await page.wait_for_timeout(1000)
            if g is None:
                await page.screenshot(path="dbg_add_account.png")
                raise RuntimeError(
                    "未跳转到 Google 登录页（截图 dbg_add_account.png）"
                )

            await google_login(g, email, password, secret)

            # 等 dola 写入 sessionid（OAuth 回跳后）
            for _ in range(60):
                await page.wait_for_timeout(3000)
                cookies = await context.cookies("https://www.dola.com")
                if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                    print(f"[{account}] ✓ 登录成功，sessionid 已写入，profile 保存到 {profile_dir}", flush=True)
                    await page.wait_for_timeout(3000)
                    return True
            await page.screenshot(path="dbg_add_account.png")
            raise RuntimeError("3 分钟内未拿到 sessionid（截图 dbg_add_account.png）")
        finally:
            await context.close()


async def main():
    account = sys.argv[1]
    email, password, secret = sys.argv[2].split("----")
    await add_account_flow(account, email, password, secret)
    print(f"[{account}] 加号完成，跑 verify_login.py {account} 可复核")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"✗ {e}")
        sys.exit(1)
