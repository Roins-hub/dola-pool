import asyncio
import traceback

from patchright.async_api import async_playwright


def log(m):
    print(m, flush=True)


async def main():
    log("1 start")
    async with async_playwright() as p:
        log("2 playwright started")
        context = await p.chromium.launch_persistent_context(
            "accounts/acc1", headless=True,
            args=["--disable-blink-features=AutomationControlled"])
        log("3 context launched")
        page = context.pages[0] if context.pages else await context.new_page()
        log("4 page ready")
        await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
        log(f"5 goto done url={page.url}")
        await page.wait_for_timeout(3000)
        r = await page.evaluate("() => 1+1")
        log(f"6 evaluate ok r={r}")
        cookies = await context.cookies("https://www.dola.com")
        log(f"7 cookies={len(cookies)}")
        await context.close()
    log("8 done")


try:
    asyncio.run(main())
except Exception:
    traceback.print_exc()
