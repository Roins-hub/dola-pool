"""Forward browser-originated assistant replies and progress to task callbacks."""
import asyncio
import inspect
import time
from urllib.parse import urlsplit


def emit(on_poll, *, note=None, progress=None):
    if not on_poll:
        return
    fields = {k: v for k, v in {"note": note, "progress": progress}.items() if v}
    params = inspect.signature(on_poll).parameters
    if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
        fields = {k: v for k, v in fields.items() if k in params}
    on_poll(time.time(), **fields)


class BrowserReplies:
    def __init__(self, page, on_poll):
        self.page, self.on_poll = page, on_poll
        self.tasks = set()
        self.last_note = ""
        self.callback = self.on_response

    def start(self):
        self.page.on("response", self.callback)

    def on_response(self, response):
        url = urlsplit(response.url)
        if url.hostname not in ("dola.com", "www.dola.com") or url.path != "/chat/completion":
            return
        task = asyncio.create_task(self.read(response))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def read(self, response):
        try:
            from browser_hello_probe import reply_from_sse
            async with asyncio.timeout(120):
                body = await response.body()
            if len(body) > 2 * 1024 * 1024:
                return
            note = reply_from_sse(body.decode("utf-8", errors="replace"))[:4000]
            if note and note != self.last_note:
                self.last_note = note
                emit(self.on_poll, note=note)
        except Exception:
            # Polling still supplies notes if a stream could not be read.
            pass

    async def close(self):
        self.page.remove_listener("response", self.callback)
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
