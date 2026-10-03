"""Facebook OAuth account onboarding for dola-pool.

Supports:
- MMO raw line: UID|Pass|Cookie|Token|Email
- Raw cookie string: c_user=...; xs=...; fr=...; datr=...
"""
import asyncio
import re
import sys
import time
from pathlib import Path
from patchright.async_api import async_playwright

from browser import LAUNCH_ARGS, proxy_kwargs_for
from browser_pool import BrowserPool

_pool = BrowserPool()
pool = _pool


def parse_fb_input(line: str) -> tuple[str, str, str]:
    """Parse line, returns (cookie_str, email_or_uid, password)."""
    line = line.strip()
    # Check if pipe-separated MMO format
    if "|" in line:
        parts = [p.strip() for p in line.split("|")]
        uid = parts[0] if len(parts) > 0 else ""
        pwd = parts[1] if len(parts) > 1 else ""
        cookie = ""
        email = ""
        for p in parts[2:]:
            if "c_user=" in p:
                cookie = p
            elif "@" in p and not email:
                email = p
        if not cookie:
            # Fallback check index 2
            if len(parts) > 2 and "c_user=" in parts[2]:
                cookie = parts[2]
        return cookie, email or uid, pwd
    else:
        # Direct cookie string
        return line, "", ""


def make_cookie_dicts(raw_cookie: str) -> list[dict]:
    cookies = []
    for part in raw_cookie.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies.append({
                "name": k.strip(),
                "value": v.strip(),
                "domain": ".facebook.com",
                "path": "/",
                "httpOnly": k.strip() in ["xs", "datr"],
                "secure": True,
            })
    return cookies


async def add_fb_account_flow(account: str, fb_line: str, force_login: bool = False) -> dict:
    """Full Facebook onboarding flow into accounts/<account> persistent profile."""
    cookie_str, identifier, pwd = parse_fb_input(fb_line)
    if not cookie_str or "c_user=" not in cookie_str:
        raise ValueError("Không tìm thấy cookie Facebook (c_user=...) trong dữ liệu nạp.")

    fb_cookies = make_cookie_dicts(cookie_str)
    profile_dir = Path("accounts") / account
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        kwargs = {
            "headless": True,
            "args": LAUNCH_ARGS,
            "locale": "en-US",
            "timezone_id": "Asia/Singapore",
        }
        proxy_kwargs = proxy_kwargs_for(account)
        if proxy_kwargs:
            kwargs["proxy"] = proxy_kwargs

        context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        try:
            if force_login:
                await context.clear_cookies()

            # Check if already logged into Dola
            dola_cookies = await context.cookies("https://www.dola.com")
            sid = next((c["value"] for c in dola_cookies if c["name"] == "sessionid" and c["value"]), None)
            if sid and not force_login:
                print(f"[{account}] ✓ Tài khoản đã có sessionid hợp lệ, bỏ qua đăng nhập.", flush=True)
                await context.storage_state(path=str(profile_dir / "cookie_state.json"))
                (profile_dir / "cookie_state.json").chmod(0o600)
                pool.ensure_account(account)
                pool.set_source(account, "facebook")
                pool.set_sessionid(account, sid)
                pool.set_login_status(account, True)
                if identifier:
                    pool.set_email(account, identifier)
                return {"ok": True, "account": account, "sessionid": sid}

            # Inject Facebook cookies
            await context.add_cookies(fb_cookies)

            page = context.pages[0] if context.pages else await context.new_page()
            print(f"[{account}] Đang tải dola.com/chat...", flush=True)
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Check & dismiss Cookie policy banner if present
            try:
                ok_btn = page.locator('button:has-text("OK"), [data-testid="cookie_banner_notice"]').first
                if await ok_btn.is_visible():
                    await ok_btn.click(timeout=1500)
            except Exception:
                pass

            # Check if login modal is visible; if not, click Log In
            fb_btn = page.locator('[data-testid="login_third_facebook"]').first
            if not await fb_btn.is_visible():
                for sel in ['[data-testid="to_login_button"]', 'button:has-text("Log In")', 'button:has-text("ログイン")']:
                    btn = page.locator(sel).first
                    if await btn.is_visible():
                        try:
                            await btn.click(timeout=3000)
                            break
                        except Exception:
                            pass

            await fb_btn.wait_for(state="visible", timeout=20000)
            print(f"[{account}] Bấm nút Đăng nhập bằng Facebook...", flush=True)

            async with context.expect_page() as page_info:
                await fb_btn.click()
            fb_page = await page_info.value
            await fb_page.wait_for_load_state()
            await fb_page.wait_for_timeout(3000)

            # Click Continue on FB OAuth
            continue_btn = await fb_page.wait_for_selector(
                '[aria-label*="Continue"], div[role="button"]:has-text("Continue"), div[role="button"]:has-text("Tiếp tục")',
                timeout=20000,
            )
            await continue_btn.click()
            print(f"[{account}] Đã xác nhận cấp quyền Facebook OAuth!", flush=True)

            # Wait for Dola age confirmation or sessionid
            deadline = time.time() + 30
            final_sid = None
            while time.time() < deadline:
                await page.wait_for_timeout(1000)

                # Check "Confirm Your Age" button
                confirm_btn = page.locator('button:has-text("Confirm"), div[role="button"]:has-text("Confirm")').first
                if await confirm_btn.is_visible():
                    try:
                        await confirm_btn.click(timeout=2000)
                        print(f"[{account}] Đã xác nhận đủ tuổi (Confirm Age).", flush=True)
                        await page.wait_for_timeout(2000)
                    except Exception:
                        pass

                cookies = await context.cookies("https://www.dola.com")
                c_map = {c["name"]: c["value"] for c in cookies}
                if "sessionid" in c_map and c_map["sessionid"]:
                    final_sid = c_map["sessionid"]
                    break

            if not final_sid:
                await page.screenshot(path=f"dbg_fb_{account}.png")
                raise RuntimeError(f"Hết thời gian chờ nhận sessionid từ Dola (ảnh lỗi: dbg_fb_{account}.png)")

            print(f"[{account}] Đăng nhập thành công.", flush=True)

            await context.storage_state(path=str(profile_dir / "cookie_state.json"))
            (profile_dir / "cookie_state.json").chmod(0o600)

            # Register with pool
            pool.ensure_account(account)
            pool.set_sessionid(account, final_sid)
            pool.set_source(account, "facebook")
            pool.set_login_status(account, True)
            if identifier:
                pool.set_email(account, identifier)

            return {
                "ok": True,
                "account": account,
                "sessionid": final_sid,
                "email": identifier,
            }
        finally:
            await context.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Cách dùng: python add_fb_account.py <tên_acc> <chuỗi_clone_fb>")
        sys.exit(1)
    acc = sys.argv[1]
    raw = sys.argv[2]
    res = asyncio.run(add_fb_account_flow(acc, raw))
    print("Kết quả:", {k: v for k, v in res.items() if k != "sessionid"})
