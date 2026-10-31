"""One-process resource leases shared by the Dola and Doubao adapters.

Account and ISP leases live until polling/download finishes. A browser permit
may return earlier, but only after confirmed browser closure. No credentials
are used as resource keys, and no implicit process singleton is constructed.
"""
import asyncio
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit


_managed_accounts = ContextVar('scheduled_accounts', default=frozenset())


class _NotifyingSemaphore(asyncio.Semaphore):
    def __init__(self, value, changed):
        super().__init__(value)
        self._changed = changed

    def release(self):
        super().release()
        self._changed()


class SharedScheduler:
    def __init__(self, max_browsers=5, max_inflight=5, lanes=None, platform_limits=None):
        if any(type(n) is not int or n < 1 for n in (max_browsers, max_inflight)):
            raise ValueError('共享浏览器和在途上限必须为正整数')
        self.available = asyncio.Event()
        self._waiters = set()
        self.browser_slots = _NotifyingSemaphore(max_browsers, self.notify)
        self.inflight = _NotifyingSemaphore(max_inflight, self.notify)
        self.proxy_slots = {}
        self.unsafe_lanes = set()
        self._account_locks = {}
        self._recoveries = {}
        self._aliases = {}
        self._queues = {}
        self._last_platform = None
        limits = {'doubao': 1, **(platform_limits or {})}
        if any(type(n) is not int or n < 1 for n in limits.values()):
            raise ValueError('平台并发上限必须为正整数')
        self._platform_slots = {p: _NotifyingSemaphore(n, self.notify) for p, n in limits.items()}
        for endpoint, lane in (lanes or {}).items():
            self.lane_id(endpoint, lane_id=lane)

    def notify(self):
        self.available.set()
        for waiter in tuple(self._waiters):
            if not waiter.done():
                waiter.set_result(None)

    @staticmethod
    def _endpoint(raw):
        if isinstance(raw, dict):
            raw = raw.get('server')
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError('缺少明确的 ISP 线路标识')
        raw = raw.strip()
        if raw == 'direct':  # Legacy non-ISP tests/callers retain one local line.
            return raw
        if raw.startswith(('isp:', 'lane:')) and '://' not in raw:
            return raw
        try:
            parsed = urlsplit(raw if '://' in raw else 'http://' + raw)
            if not parsed.hostname or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
                raise ValueError()
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            return f'isp:{parsed.hostname.lower()}:{port}'
        except (ValueError, TypeError):
            raise ValueError('ISP 线路格式错误') from None

    def lane_id(self, raw, lane_id=None):
        if isinstance(raw, dict):
            lane_id = lane_id or raw.get('lane_id')
        endpoint = self._endpoint(raw)
        if lane_id is not None:
            if (not isinstance(lane_id, str) or not lane_id.strip()
                    or any(c in lane_id for c in '@:/\\\r\n')):
                raise ValueError('物理线路 lane_id 必须为不含凭证的独立名称')
            canonical = 'lane:' + lane_id.strip()
            previous = self._aliases.get(endpoint)
            if previous and previous != canonical:
                raise ValueError('同一 ISP 端点不能绑定两个物理线路标识')
            if endpoint in self.proxy_slots and endpoint != canonical:
                raise ValueError('线路使用后不能修改 lane_id，请在启动前注册别名')
            self._aliases[endpoint] = canonical
            return canonical
        return self._aliases.get(endpoint, endpoint)

    def lane_slot(self, raw, lane_id=None):
        key = self.lane_id(raw, lane_id)
        return self.proxy_slots.setdefault(key, _NotifyingSemaphore(1, self.notify))

    def account_locks(self, platform):
        return self._account_locks.setdefault(platform, {})

    def account_lock(self, platform, account):
        return self.account_locks(platform).setdefault(account, asyncio.Lock())

    def reserve_recovery(self, platform, account, raw):
        self._recoveries[(platform, account)] = self.lane_id(raw)
        self.notify()

    def release_recovery(self, platform, account):
        self._recoveries.pop((platform, account), None)
        self.notify()

    def recovery_reserved(self, raw, owner=None):
        key = self.lane_id(raw)
        return any(lane == key and account != owner for account, lane in self._recoveries.items())

    def _next_platform(self):
        # Startup recoveries reserve capacity before new generation tasks enter.
        # Otherwise five tasks waiting on those lanes could starve their owners.
        ready = [name for name, queue in self._queues.items() if queue
                 and (queue[0][1] or self.inflight._value > len(self._recoveries))
                 and (name not in self._platform_slots or not self._platform_slots[name].locked())]
        if self._last_platform in ready and len(ready) > 1:
            position = ready.index(self._last_platform)
            return ready[(position + 1) % len(ready)]
        return ready[0] if ready else None

    async def _admit(self, platform, recovery=False):
        token = object()
        queue = self._queues.setdefault(platform, deque())
        entry = (token, recovery)
        if recovery:
            queue.appendleft(entry)
        else:
            queue.append(entry)
        try:
            while True:
                waiter = asyncio.get_running_loop().create_future()
                self._waiters.add(waiter)
                try:
                    if (not self.inflight.locked() and queue[0][0] is token
                            and self._next_platform() == platform):
                        await self.inflight.acquire()
                        if platform in self._platform_slots:
                            # _next_platform checked both resources before this
                            # await; unlocked Semaphore acquisition never yields.
                            await self._platform_slots[platform].acquire()
                        queue.popleft()
                        self._last_platform = platform
                        self.notify()
                        return
                    await waiter
                finally:
                    self._waiters.discard(waiter)
        finally:
            if entry in queue:
                queue.remove(entry)
                self.notify()

    @asynccontextmanager
    async def task_slot(self, platform, *, recovery=False):
        platform_slot = self._platform_slots.get(platform)
        held = False
        try:
            await self._admit(platform, recovery)
            held = True
            yield
        finally:
            if held:
                self.inflight.release()
            if held and platform_slot:
                platform_slot.release()

    @contextmanager
    def managed_account(self, platform, account):
        token = _managed_accounts.set(_managed_accounts.get() | {(platform, account)})
        try:
            yield
        finally:
            _managed_accounts.reset(token)

    def account_is_managed(self, platform, account):
        return (platform, account) in _managed_accounts.get()

    def diagnostic(self, platform, account, raw, lane_id=None, *, browser=True):
        return _DiagnosticLease(self, platform, account, raw, lane_id, browser)


class _DiagnosticLease:
    def __init__(self, scheduler, platform, account, raw, lane_id, browser):
        self.scheduler, self.platform, self.account = scheduler, platform, account
        self.key = scheduler.lane_id(raw, lane_id)
        self.resources = [scheduler.account_lock(platform, account), scheduler.lane_slot(self.key)]
        if browser:
            self.resources.append(scheduler.browser_slots)
        self._held = []
        self._quarantined = False

    async def __aenter__(self):
        if self.key in self.scheduler.unsafe_lanes:
            raise RuntimeError('线路浏览器关闭异常，已隔离，请检查后恢复')
        if self.scheduler.recovery_reserved(self.key):
            raise RuntimeError('线路正在等待原任务恢复，请稍后检查')
        try:
            for resource in self.resources:
                await resource.acquire()
                self._held.append(resource)
            if self.key in self.scheduler.unsafe_lanes:
                raise RuntimeError('线路浏览器关闭异常，已隔离')
            if self.scheduler.recovery_reserved(self.key):
                raise RuntimeError('线路正在等待原任务恢复')
            return self
        except BaseException:
            self.release()
            raise

    def quarantine(self):
        self._quarantined = True
        self.scheduler.unsafe_lanes.add(self.key)
        self.scheduler.notify()

    def release(self):
        while self._held:
            self._held.pop().release()
        self.scheduler.notify()

    async def __aexit__(self, *exc):
        if not self._quarantined:
            self.release()
