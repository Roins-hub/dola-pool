"""A submission permit can be released before its video finishes."""


class SubmissionSlot:
    def __init__(self, semaphore, on_release):
        self.semaphore = semaphore
        self.on_release = on_release
        self.held = False

    async def __aenter__(self):
        await self.semaphore.acquire()
        self.held = True
        return self

    def release(self):
        if self.held:
            self.held = False
            self.semaphore.release()
            self.on_release()

    async def __aexit__(self, *exc):
        self.release()
