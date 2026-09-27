import asyncio, json
from patchright.async_api import async_playwright
from browser import cookie_value, launch_account_context

CONV = "38416270392802065"
RAW_JS = r"""
async ({conversationId, msToken, fp}) => {
  const params = new URLSearchParams({
    aid: "495671", channel: "g", device_platform: "web", language: "zh-Hant",
    region: "JP", sys_region: "JP", samantha_web: "1", "use-olympus-account": "1",
    version_code: "20800", web_platform: "browser", web_tab_id: crypto.randomUUID(),
  });
  if (msToken) params.set("msToken", msToken);
  if (fp) params.set("fp", fp);
  const resp = await fetch("/im/chain/single?" + params.toString(), {
    method: "POST",
    headers: {"Content-Type": "application/json; encoding=utf-8", "agw-js-conv": "str", "Accept": "*/*"},
    body: JSON.stringify({cmd: 3100, conversation_id: conversationId, anchor_index: 0, direction: 1, limit: 20}),
    credentials: "include",
  });
  const data = await resp.json();
  return JSON.stringify(data).slice(0, 6000);
}
"""

async def main():
    async with async_playwright() as p:
        context = await launch_account_context(p, "acc1")
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(f"https://www.dola.com/chat/{CONV}", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        cookies = await context.cookies("https://www.dola.com")
        raw = await page.evaluate(RAW_JS, {"conversationId": CONV, "msToken": cookie_value(cookies, "msToken"), "fp": cookie_value(cookies, "s_v_web_id")})
        print(raw)
        await context.close()

asyncio.run(main())