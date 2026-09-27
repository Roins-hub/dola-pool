"""Playwright 自动化浏览器探测脚本。

验证：patchright 反检测浏览器 + 系统全局代理 能否正常访问 dola.com（不被墙/不被风控）。
用 headful（有界面）方便肉眼确认。运行后打开浏览器访问 dola，截图保存，打印页面信息。
"""
import asyncio
import sys

from patchright.async_api import async_playwright


async def main():
    headless = "--headless" in sys.argv
    async with async_playwright() as p:
        # 全局代理下，Chromium 默认跟随系统代理，不显式传 proxy
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",  # 去掉自动化控制特征
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        context = await browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = await context.new_page()

        # 打印关键指纹，确认反检测是否生效
        webdriver = await page.evaluate("navigator.webdriver")
        print(f"navigator.webdriver = {webdriver}  (True=被识别为自动化, False/undefined=正常)")

        try:
            await page.goto("https://www.dola.com", timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"打开 dola 失败: {e}")
            await browser.close()
            return

        await page.wait_for_timeout(6000)
        print("URL  :", page.url)
        print("TITLE:", await page.title())

        # 检测风控/验证弹窗迹象
        html = await page.content()
        flags = {
            "滑块容器(captcha)": "captcha" in html.lower(),
            "verify 验证": "verify" in html.lower(),
            "登录按钮": "登录" in html or "Log in" in html or "Sign in" in html,
        }
        for k, v in flags.items():
            print(f"  [{ '✓' if v else ' ' }] {k}")

        await page.screenshot(path="probe_screenshot.png", full_page=False)
        print("截图已保存: probe_screenshot.png")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
