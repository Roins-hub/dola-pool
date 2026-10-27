"""Resizable FIFO concurrency gate for the service's single asyncio loop."""
import asyncio
from collections import deque


class AdjustableConcurrency:
    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.active = 0
        self._waiters = deque()

    @property
    def waiting(self):
        return sum(not waiter.done() for waiter in self._waiters)

    def _wake(self):
        while self._waiters and (self.limit == 0 or self.active < self.limit):
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            # Reserve the slot before scheduling the waiter; new arrivals cannot steal it.
            self.active += 1
            waiter.set_result(None)

    def set_limit(self, limit: int):
        if type(limit) is not int or limit < 0:
            raise ValueError('Concurrency must be a non-negative integer')
        self.limit = limit
        self._wake()

    async def acquire(self):
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        self._wake()
        try:
            await waiter
        except BaseException:
            # Cancellation can arrive after the slot was reserved but before entry.
            if waiter.done() and not waiter.cancelled():
                self.active -= 1
            else:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            self._wake()
            raise

    def release(self):
        if self.active <= 0:
            raise RuntimeError('Concurrency slot released without acquisition')
        self.active -= 1
        self._wake()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info):
        self.release()
        return False
