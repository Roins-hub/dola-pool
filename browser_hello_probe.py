"""Observe a greeting sent by the account's browser; no independent HTTP client."""
import asyncio
import json
import time
from urllib.parse import urlsplit


def reply_from_sse(text):
    events = []
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        kind, data = "message", []
        for line in chunk.splitlines():
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        try:
            payload = json.loads("\n".join(data))
        except ValueError:
            continue
        if kind == "STREAM_ERROR":
            return ""
        # ACK and message echoes do not establish that the assistant replied.
        if kind in ("STREAM_CHUNK", "SSE_REPLY_END"):
            events.append({"event": kind, "data": payload})
    from protocol.dola_pure_api import extract_reply_texts_from_events
    return "\n".join(extract_reply_texts_from_events(events)).strip()


async def probe_browser_hello(account, *, timeout=60):
    import browser
    from patchright.async_api import async_playwright

    started = time.monotonic()
    prompt = "你好"  # Fixed ordinary greeting, never a generation prompt.
    context = None
    readers = set()
    sent = False
    observed = {"reply": "", "logged_out": False, "login_confirmed": False}

    def result(status, reason):
        return {"ok": status == "ok", "status": status,
                "login_ok": False if status == "logged_out" else (True if status == "ok" else None),
                "replied": status == "ok", "probed": sent, "cached": False,
                "reason": reason, "reply": observed["reply"][:500],
                "elapsed": round(time.monotonic() - started, 2), "runtime": "cloak"}

    async def read_response(response):
        try:
            headers = await response.all_headers()
            if str(headers.get("x-tt-agw-login", "")) == "0":
                observed["logged_out"] = True
                return
            observed["login_confirmed"] = str(headers.get("x-tt-agw-login", "")) == "1"
            if response.status != 200:
                return
            # Only parse a response emitted by the browser, never send a second request.
            body = await response.body()
            if len(body) <= 2 * 1024 * 1024:
                reply = reply_from_sse(body.decode("utf-8", errors="replace"))
                if reply and reply != prompt:
                    observed["reply"] = reply
        except Exception:
            pass  # An interrupted stream is unconfirmed, never a successful probe.

    def on_response(response):
        parsed = urlsplit(response.url)
        if not sent or parsed.hostname not in ("www.dola.com", "dola.com") or parsed.path != "/chat/completion":
            return
        try:
            payload = response.request.post_data_json
            if prompt not in json.dumps(payload, ensure_ascii=False):
                return
        except Exception:
            return
        task = asyncio.create_task(read_response(response))
        readers.add(task)
        task.add_done_callback(readers.discard)

    try:
        if not browser.is_cloak_cookie_account(account):
            return result("unconfirmed", "账号当前不是 Cloak 模式，未启动浏览器探测")
        async with async_playwright() as p:
            try:
                async with asyncio.timeout(max(1, timeout) + 90):
                    context = await browser.launch_account_context(p, account)
                    page = await context.new_page()
                    await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(3000)
                    ok = page.get_by_text("OK", exact=True).first
                    if await ok.count() and await ok.is_visible():
                        await ok.click(timeout=1500)
                    cookies = await context.cookies("https://www.dola.com")
                    if not browser.cookie_value(cookies, "sessionid") or await browser._page_logged_out(page):
                        return result("logged_out", "浏览器显示账号已退出登录")
                    if "region-restricted" in page.url:
                        return result("unconfirmed", "浏览器页面受地区限制，未发送问候")
                    box = page.locator('textarea:visible, [contenteditable="true"]:visible').first
                    if not await box.count():
                        return result("unconfirmed", "浏览器未找到聊天输入框，未发送问候")
                    await box.fill(prompt)
                    page.on("response", on_response)
                    sent = True
                    await box.press("Enter")
                    deadline = time.monotonic() + max(1, timeout)
                    while time.monotonic() < deadline:
                        await page.wait_for_timeout(500)
                        if observed["logged_out"] or await browser._page_logged_out(page):
                            return result("logged_out", "发送问候后浏览器被登出")
                        if observed["reply"] and observed["login_confirmed"]:
                            return result("ok", "独立浏览器已收到本次问候的助手回复")
                    return result("unconfirmed", "浏览器探测超时，未确认收到回复（不判风控）")
            finally:
                for task in list(readers):
                    task.cancel()
                if readers:
                    await asyncio.gather(*list(readers), return_exceptions=True)
                if context is not None:
                    await context.close()
    except Exception:
        # Browser exceptions can embed proxy passwords or signed URLs.
        return result("unconfirmed", "浏览器启动或页面探测失败，请检查账号代理及浏览器状态（未回退纯 API）")
