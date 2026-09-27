"""诊断：直接打开会话 38416270392802065，看出片与否 + dump 轮询原始响应。"""
import asyncio
import json

from patchright.async_api import async_playwright

from browser import cookie_value, launch_account_context
from video_worker import POLL_JS

CONV = "38416270392802065"


async def main():
    async with async_playwright() as p:
        context = await launch_account_context(p, "acc1")
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(f"https://www.dola.com/chat/{CONV}", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)
        await page.screenshot(path="diag_conv.png")

        info = await page.evaluate("""() => {
            const videos = [...document.querySelectorAll('video')].map(v => (v.currentSrc||v.src||'').slice(0,150));
            const texts = [...document.querySelectorAll('div,p,span')].map(e => e.childElementCount===0 ? (e.textContent||'').trim() : '').filter(t => t && t.length>2 && t.length<200);
            return {videos, texts: texts.slice(-12)};
        }""")
        print("videos:", json.dumps(info["videos"], ensure_ascii=False))
        print("尾部文本:")
        for t in info["texts"]:
            print("  |", t)

        cookies = await context.cookies("https://www.dola.com")
        poll = await page.evaluate(POLL_JS, {
            "conversationId": CONV,
            "msToken": cookie_value(cookies, "msToken"),
            "fp": cookie_value(cookies, "s_v_web_id"),
        })
        print("\n=== POLL_JS 解析结果 ===")
        print(json.dumps(poll, ensure_ascii=False)[:800])
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())