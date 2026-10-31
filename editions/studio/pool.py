"""号池管理：加载 cookie、包装 DolaPool、信号量控制并发。"""
import asyncio
import os

from dola_client import DolaPool


class PoolManager:
    def __init__(self, cookies_file: str, max_concurrency: int, video_timeout: int):
        self.cookies_file = cookies_file
        self.video_timeout = video_timeout
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.cookie_count = 0
        self.reload()

    def reload(self):
        cookies = []
        if os.path.exists(self.cookies_file):
            with open(self.cookies_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        cookies.append(line)
        self.pool = DolaPool(cookies, video_timeout=self.video_timeout)
        self.cookie_count = len(cookies)

    @property
    def available(self) -> bool:
        return bool(self.pool and self.pool.available)

    async def generate_video(self, prompt: str, ratio: str, duration: int) -> str:
        async with self.semaphore:
            return await self.pool.generate_video(prompt, ratio, duration)
