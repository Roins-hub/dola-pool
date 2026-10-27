import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from concurrency import AdjustableConcurrency
from store import TaskStore


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_increase_wakes_existing_waiters_and_preserves_fifo():
    async def scenario():
        gate = AdjustableConcurrency(1)
        await gate.acquire()
        first = asyncio.create_task(gate.acquire())
        second = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0)
        assert gate.waiting == 2
        gate.set_limit(2)
        await asyncio.sleep(0)
        assert first.done() and not second.done()
        assert gate.active == 2
        gate.release()
        await second
        gate.release()
        gate.release()
        assert gate.active == gate.waiting == 0
    run(scenario())


@pytest.mark.parametrize('initial', [0, 3])
def test_decrease_preserves_running_work_and_blocks_until_below_limit(initial):
    async def scenario():
        gate = AdjustableConcurrency(initial)
        for _ in range(3):
            await gate.acquire()
        gate.set_limit(1)
        pending = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0)
        for remaining in (2, 1):
            gate.release()
            await asyncio.sleep(0)
            assert gate.active == remaining and not pending.done()
        gate.release()
        await pending
        assert gate.active == 1
        gate.release()
    run(scenario())


@pytest.mark.parametrize('reserved', [False, True])
def test_cancelled_waiter_cannot_leak_or_overallocate_slots(reserved):
    async def scenario():
        gate = AdjustableConcurrency(1)
        await gate.acquire()
        cancelled = asyncio.create_task(gate.acquire())
        next_task = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0)
        if reserved:
            gate.set_limit(2)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        if not reserved:
            gate.release()
        await asyncio.wait_for(next_task, 1)
        assert gate.active == (2 if reserved else 1)
        gate.release()
        if reserved:
            gate.release()
        assert gate.active == gate.waiting == 0
    run(scenario())


def test_unlimited_wakes_all_and_context_exception_releases_slot():
    async def scenario():
        gate = AdjustableConcurrency(1)
        await gate.acquire()
        pending = [asyncio.create_task(gate.acquire()) for _ in range(3)]
        await asyncio.sleep(0)
        gate.set_limit(0)
        await asyncio.gather(*pending)
        assert gate.active == 4
        for _ in range(4):
            gate.release()
        with pytest.raises(RuntimeError):
            async with gate:
                raise RuntimeError('worker failed')
        assert gate.active == 0
    run(scenario())


def test_admin_concurrency_validation_auth_persistence_and_live_update(tmp_path, monkeypatch):
    import server
    storage = TaskStore(str(tmp_path / 'settings.db'))
    gate = AdjustableConcurrency(storage.get_global_concurrency(1))
    class Pool:
        semaphore = gate
        max_concurrency = property(lambda self: gate.limit)
        def set_max_concurrency(self, value): gate.set_limit(value)
    monkeypatch.setattr(server, 'pool', Pool())
    monkeypatch.setattr(server, 'store', storage)
    monkeypatch.setattr(server, 'ADMIN_SESSIONS', {'admin-test': {
        'username': 'admin', 'expires_at': time.time() + 3600}})
    monkeypatch.setattr(server, 'user_get', lambda name: {'enabled': True})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
            url = '/api/admin/settings/concurrency'
            assert (await client.get(url)).status_code == 401
            assert (await client.patch(url, json={'max_concurrency': 2})).status_code == 401
            assert gate.limit == storage.get_global_concurrency(1) == 1
            headers = {'X-Admin-Key': 'admin-test'}
            for invalid in (-1, 1001, 1.5, '2', True, None):
                assert (await client.patch(url, headers=headers, json={'max_concurrency': invalid})).status_code == 422
            await gate.acquire()
            waiter = asyncio.create_task(gate.acquire())
            await asyncio.sleep(0)
            before = (await client.get(url, headers=headers)).json()
            assert (before['active'], before['waiting']) == (1, 1)
            result = await client.patch(url, headers=headers, json={'max_concurrency': 2})
            assert result.status_code == 200
            await asyncio.wait_for(waiter, 1)
            assert result.json()['max_concurrency'] == gate.limit == 2
            assert server._progress_payload(status='queued', created_at=time.time(), duration=5)['workers'] == 2
            reopened = TaskStore(storage.db_path)
            assert reopened.get_global_concurrency(99) == 2
            reopened._conn.close()
            result = await client.patch(url, headers=headers, json={'max_concurrency': 0})
            assert result.status_code == 200 and gate.limit == 0
            assert storage.get_global_concurrency(99) == 0
            gate.release()
            gate.release()
    run(scenario())
    storage._conn.close()


def test_failed_save_does_not_change_live_limit(tmp_path, monkeypatch):
    import server
    gate = AdjustableConcurrency(1)
    monkeypatch.setattr(server, '_admin_auth', lambda token: None)
    monkeypatch.setattr(server, 'pool', SimpleNamespace(set_max_concurrency=gate.set_limit))
    def fail(value): raise OSError('disk unavailable')
    monkeypatch.setattr(server, 'store', SimpleNamespace(set_global_concurrency=fail))
    with pytest.raises(OSError, match='disk unavailable'):
        run(server.admin_set_concurrency(server.ConcurrencySettings(max_concurrency=2), 'token'))
    assert gate.limit == 1
