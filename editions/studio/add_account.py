"""自动添加账号：Google OAuth 自动登录（邮箱----密码----TOTP 密钥），存 profile。

用法：python add_account.py <账号名> "email----password----totp_secret"

只登录存 profile，不发送任何视频请求。失败抛 RuntimeError（供面板 jobs 捕获）。
Google 页有反自动化检测，用 headful 提高成功率；失败会截图 dbg_add_account.png。
"""
import asyncio
import base64
import re
import hashlib
import hmac
import json
import struct
import sys
import time
from pathlib import Path
from datetime import date
from urllib.parse import urlsplit

from patchright.async_api import async_playwright, TimeoutError as BrowserTimeoutError

from browser import launch_account_context
import config
from account_login import ACCOUNT_INFO_URL, classify_login_response


def totp(secret: str, period: int = 30, digits: int = 6) -> str:
    """标准 TOTP（RFC 6238），Google Authenticator 兼容。"""
    secret = secret.replace(" ", "").upper()
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    counter = int(time.time() // period)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[19] & 15
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % (10 ** digits)
    return str(code).zfill(digits)


class GoogleLoginAttention(RuntimeError):
    """A fixed, safe message for an interactive verification or rejected login."""


async def handle_google_verification(g, secret: str, recovery_email: str) -> bool:
    recovery = g.locator('#knowledge-preregistered-email-response, input[name="knowledgePreregisteredEmailResponse"]').first
    if await recovery.count() and await recovery.is_visible():
        if not recovery_email:
            raise GoogleLoginAttention('Google 要求确认备用邮箱，请补充备用邮箱资料。')
        await recovery.fill(recovery_email)
        await g.locator('#idvPreregisteredEmailNext').click(timeout=5000)
        return True
    otp = g.locator('input[name="totpPin"], #totpPin').first
    if await otp.count() and await otp.is_visible():
        if not secret:
            raise GoogleLoginAttention('Google 要求验证器动态码，备用邮箱不能替代，请人工完成验证。')
        await otp.fill(totp(secret))
        await g.locator('#totpNext').click(timeout=5000)
        return True
    # Confirming a recovery email address is distinct from reading its OTP.
    recovery_choice = g.locator('[data-challengetype="12"]').first
    if recovery_email and await recovery_choice.count() and await recovery_choice.is_visible():
        await recovery_choice.click(timeout=5000)
        return True
    sms = g.locator('input[type="tel"], input[name="knowledgePreregisteredEmailCode"], #idvPin').first
    if await sms.count() and await sms.is_visible():
        raise GoogleLoginAttention('Google 要求手机或邮箱验证码，需要人工完成验证。')
    return False


async def open_google_login(page):
    body = await page.locator('body').inner_text(timeout=5000)
    if re.search(r'in diesem Land oder dieser Region nicht verfügbar|not available in (?:your|this) (?:country|region)|所在.*地区.*不可用', body, re.I):
        raise GoogleLoginAttention('登录代理所在地区不支持 Dola，请切换可同时访问 Google 和 Dola 的线路后重试。')
    google = page.get_by_text(re.compile(
        r'Googleで続ける|Mit Google fortfahren|Continue with Google|'
        r'Sign in with Google|Google.*anmelden|使用 Google|通过 Google', re.I)).first
    try:
        await google.wait_for(state='visible', timeout=10000)
    except BrowserTimeoutError:
        await page.get_by_text(re.compile(
            r'^(ログイン|Anmelden|Einloggen|Sign in|Log in|登录)$', re.I)).first.click(timeout=10000)
        await google.wait_for(state='visible', timeout=15000)
    await google.click(timeout=10000)


async def handle_google_consent(g) -> bool:
    """Continue the ordinary Google sign-in consent in supported UI languages."""
    candidates = [g.locator('#submit_button').first,
                  g.get_by_role('button', name=re.compile(
                      r'^(Continue|Weiter|Lanjutkan|続行|继续)$', re.I)).first]
    for button in candidates:
        if await button.count() and await button.is_visible():
            await button.click(timeout=3000)
            print('[google] 登录确认页 → 继续', flush=True)
            return True
    return False


async def handle_dola_age_confirmation(page, birth_date: str = '') -> bool:
    if urlsplit(page.url).hostname not in ('www.dola.com', 'dola.com'):
        return False
    title = page.get_by_text(re.compile(r'^(Dein Alter bestätigen|Confirm your age|确认你的年龄|确认年龄)$')).first
    if not await title.count() or not await title.is_visible():
        return False
    try:
        birth = date.fromisoformat(birth_date)
        today = date.today()
        age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
        if age < 18:
            raise ValueError()
    except (ValueError, TypeError):
        raise GoogleLoginAttention('Dola 要求确认已满 18 岁，需要本人提供真实生日后处理。') from None
    await page.get_by_role('button', name=re.compile(r'^(Bestätigen|Confirm|确认)$')).click(timeout=5000)
    print('[dola] 已按用户提供的生日完成年龄确认', flush=True)
    return True


async def save_authenticated_session(context, page, profile_dir: Path) -> bool:
    """A session cookie alone may be anonymous or expired; require a real identity."""
    if urlsplit(page.url).hostname not in ('www.dola.com', 'dola.com'):
        return False
    cookies = await context.cookies('https://www.dola.com')
    if not any(c['name'] == 'sessionid' and c['value'] for c in cookies):
        return False
    body = await page.locator('body').inner_text(timeout=5000)
    if re.search(r'Wann hast du Geburtstag|When.*birthday|出生日期|你的生日', body, re.I):
        raise GoogleLoginAttention('Dola 需要生日验证，请在账号浏览器中填写真实生日后重试。')
    try:
        result = await page.evaluate('''async url => {
            const response = await fetch(url, {credentials:'include', signal:AbortSignal.timeout(10000)});
            return {status:response.status, body:await response.json(), flag:response.headers.get('x-tt-agw-login') || ''};
        }''', ACCOUNT_INFO_URL)
    except Exception:
        return False
    if classify_login_response(result['status'], result['body'], result['flag']) is not True:
        return False
    (profile_dir / 'cookies.json').write_text(json.dumps(cookies), encoding='utf-8')
    return True


async def google_login(g, email: str, password: str, secret: str, recovery_email: str = ''):
    """Complete Google steps; transient navigation is not an OAuth callback."""
    for step in range(12):
        if g.is_closed():
            return
        await g.wait_for_timeout(2500)
        if g.is_closed():
            return
        hostname = urlsplit(g.url).hostname
        if hostname in ('www.dola.com', 'dola.com'):
            print('[google] 已返回 Dola，等待登录状态确认', flush=True)
            return
        if hostname != 'accounts.google.com':
            print(f'[google] 等待跳转完成（主机：{hostname or "空白页"}）', flush=True)
            continue
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
        if await handle_google_verification(g, secret, recovery_email):
            continue
        if await handle_google_consent(g):
            continue
        print(f"[google] step{step} 等待登录或验证页面", flush=True)
    await g.screenshot(path="dbg_google2.png")
    raise GoogleLoginAttention('Google 登录或跳转尚未完成，请检查登录页面后重试。')


async def add_account_flow(account: str, email: str, password: str, secret: str, *,
                           recovery_email: str = '', login_proxy: str | None = None,
                           birth_date: str = '') -> bool:
    """完整加号流程；成功返回 True，任何失败抛 RuntimeError。"""
    profile_dir = Path("accounts") / account
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await launch_account_context(p, account, headless=False, proxy_override=login_proxy)
        stage = '打开 Dola 页面'
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(4000)

            await handle_dola_age_confirmation(page, birth_date)
            if await save_authenticated_session(context, page, profile_dir):
                print(f"[{account}] 已有登录态，无需登录", flush=True)
                return True

            # Match the visible provider action; current dialogs no longer use semi-modal CSS.
            stage = '打开 Google 登录'
            await open_google_login(page)

            # Google 页可能在 popup 或当前 tab
            g = None
            for _ in range(30):
                g = next((pg for pg in context.pages if urlsplit(pg.url).hostname == 'accounts.google.com'), None)
                if g is not None:
                    break
                if any(urlsplit(pg.url).hostname in ('www.dola.com', 'dola.com')
                       and urlsplit(pg.url).path == '/auth/callback' for pg in context.pages):
                    break
                await page.wait_for_timeout(1000)
            if g is not None:
                stage = '完成 Google 登录验证'
                await google_login(g, email, password, secret, recovery_email)

            # 等 dola 写入 sessionid（OAuth 回跳后）
            stage = '等待 Dola 登录回调'
            for _ in range(60):
                await asyncio.sleep(3)
                for current in context.pages:
                    await handle_dola_age_confirmation(current, birth_date)
                    if await save_authenticated_session(context, current, profile_dir):
                        print(f"[{account}] ✓ Dola 身份验证成功，profile 保存到 {profile_dir}", flush=True)
                        return True
            await page.screenshot(path="dbg_add_account.png")
            raise GoogleLoginAttention('等待 Dola 登录状态确认超时，请检查登录页面后重试。')
        except Exception as error:
            try:
                target = next((pg for pg in context.pages if urlsplit(pg.url).hostname == 'accounts.google.com'), page)
                await target.screenshot(path=str(profile_dir / 'login-error.png'), timeout=5000)
            except Exception:
                pass
            if isinstance(error, BrowserTimeoutError):
                raise GoogleLoginAttention(f'{stage}超时，请检查代理或稍后重试。') from None
            raise
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
