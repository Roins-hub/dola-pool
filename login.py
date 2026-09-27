"""dola 账号登录态采集工具。

用法：python login.py <账号名>
打开浏览器访问 dola.com，你手动完成 Google 登录。
检测到登录态（sessionid）后自动保存 profile 到 accounts/<账号名>/ 并关闭浏览器。
之后 video_worker 复用这个 profile，无需重新登录。
"""
import asyncio
import sys
from pathlib import Path

from patchright.async_api import async_playwright

from browser import LAUNCH_ARGS, proxy_kwargs_for


async def main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    profile_dir = Path("accounts") / account
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        kwargs = {
            "headless": False,
            "args": LAUNCH_ARGS,
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
        }
        proxy_kwargs = proxy_kwargs_for(account)
        if proxy_kwargs:
            kwargs["proxy"] = proxy_kwargs
        context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.dola.com/chat", timeout=60000)
        print(f"[{account}] 浏览器已打开，请在窗口里完成 Google 登录（登进 dola 即可）...")

        # 轮询检测登录态：dola.com 域下出现 sessionid
        for _ in range(180):  # 最长 15 分钟
            cookies = await context.cookies("https://www.dola.com")
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                print(f"[{account}] 检测到登录态 sessionid，等待 session 完全写入...")
                await page.wait_for_timeout(4000)
                await context.close()
                print(f"[{account}] OK，profile 已保存到 {profile_dir}")
                return
            await page.wait_for_timeout(5000)

        print(f"[{account}] 15 分钟内未检测到登录，已退出，请重跑本脚本")
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
