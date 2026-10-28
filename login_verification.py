"""Observe login evidence from the account browser's own read-only requests."""
import asyncio
from urllib.parse import urlsplit


class LoginEvidence:
    PATHS = {'/alice/user/config/pull', '/alice/profile/self',
             '/alice/profile/self_brief', '/im/chain/recent_conv'}

    def __init__(self, page):
        self.page = page
        self.confirmed = False
        self.logged_out = False
        self.tasks = set()
        self.callback = self.on_response

    def start(self):
        self.page.on('response', self.callback)

    def on_response(self, response):
        url = urlsplit(response.url)
        if url.scheme != 'https' or url.hostname not in ('www.dola.com', 'dola.com'):
            return
        if url.path.rstrip('/') not in self.PATHS:
            return
        task = asyncio.create_task(self.read(response))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def read(self, response):
        try:
            headers = await asyncio.wait_for(response.all_headers(), timeout=2)
            flag = str(headers.get('x-tt-agw-login', ''))
            if flag == '0':
                self.logged_out = True
            elif flag == '1' and response.status == 200:
                self.confirmed = True
        except Exception:
            pass  # Missing evidence requires the original stability check.

    async def close(self):
        self.page.remove_listener('response', self.callback)
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
