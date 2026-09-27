import asyncio, json
from patchright.async_api import async_playwright
from browser import launch_account_context

CONV = "38416270392802065"

async def main():
    async with async_playwright() as p:
        context = await launch_account_context(p, "acc1")
        page = context.pages[0] if context.pages else await context.new_page()
        captured = []
        def on_req(req):
            if "im/chain" in req.url:
                captured.append({"url": req.url, "post": req.post_data})
        page.on("request", on_req)
        await page.goto(f"https://www.dola.com/chat/{CONV}", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)
        for c in captured:
            print("=== URL ===")
            print(c["url"])
            print("=== BODY ===")
            print((c["post"] or "")[:1500])
            print()
        await context.close()

asyncio.run(main())