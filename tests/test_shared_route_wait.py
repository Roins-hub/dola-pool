import asyncio
import time

import pytest

from test_task_deadline import _pool, _run


def setup_pool(tmp_path, monkeypatch):
    module, pool = _pool(tmp_path, monkeypatch, ('acc1', 'acc2', 'acc3'))
    pool.set_max_concurrency(3)
    monkeypatch.setattr(module.config, 'ISOLATE_SHARED_EGRESS', True)
    monkeypatch.setattr(module, 'WAIT_FREE_ACCOUNT_STEP', .01)
    monkeypatch.setattr(pool, '_egress_for', lambda name: 'shared')
    monkeypatch.setattr(pool, '_safe_egress', lambda name: 'shared')
    return module, pool


def test_three_tasks_share_route_serially_instead_of_failing(tmp_path, monkeypatch):
    _, pool = setup_pool(tmp_path, monkeypatch)
    active = set()
    peak = 0
    calls, waiting = [], []
    async def generate(account, *args, **kwargs):
        nonlocal peak
        active.add(account)
        peak = max(peak, len(active))
        calls.append(account)
        await asyncio.sleep(.04)
        active.remove(account)
        return {'account': account, 'local_path': 'synthetic.mp4'}
    pool._generate_effective = generate
    async def scenario():
        results = await asyncio.gather(*[
            pool.generate_video('p', None, 10, 'seedance-2.0-fast',
                                attempt_timeout=2, on_wait=waiting.append)
            for _ in range(3)])
        assert len(results) == 3
        assert len(calls) == len(set(calls)) == 3
        assert peak == 1
        assert any('共享代理出口' in note for note in waiting)
        assert pool.semaphore.active == 0
    _run(scenario())


def test_upload_pause_expires_before_first_dispatch(tmp_path, monkeypatch):
    _, pool = setup_pool(tmp_path, monkeypatch)
    pool._upload_routes['shared'] = {'until': time.time()+.08, 'reason': 'other task upload failure'}
    calls, notes = [], []
    async def generate(account, *args, **kwargs):
        assert time.time() >= pool._upload_routes['shared']['until']
        calls.append(account)
        return {'account': account, 'local_path': 'synthetic.mp4'}
    pool._generate_effective = generate
    result = _run(pool.generate_video('p', None, 5, 'seedance-2.5',
        reference_image_paths=['synthetic.png'], deadline=time.time()+1, on_wait=notes.append))
    assert result['account'] and len(calls) == 1
    assert any('上传线路暂时暂停' in n for n in notes)


def test_queued_task_timeout_does_not_invent_an_account_failure(tmp_path, monkeypatch):
    _, pool = setup_pool(tmp_path, monkeypatch)
    pool._upload_routes['shared'] = {'until': time.time()+100, 'reason': 'ApplyImageUpload HTTP 400 diagnostic'}
    tries = []
    with pytest.raises(RuntimeError) as error:
        _run(pool.generate_video('p', None, 5, 'seedance-2.5',
            reference_image_paths=['synthetic.png'], deadline=time.time()+.04,
            on_account_try=lambda *a: tries.append(a)))
    message = str(error.value)
    assert '未分配账号' in message and '尚未提交生成' in message
    assert '连续 0' not in message and '依次尝试账号' not in message
    assert tries == [] and all(pool.used_today(a) == 0 for a in pool.accounts)


def test_separate_routes_still_allow_three_concurrent_tasks(tmp_path, monkeypatch):
    _, pool = setup_pool(tmp_path, monkeypatch)
    monkeypatch.setattr(pool, '_egress_for', lambda name: name)
    monkeypatch.setattr(pool, '_safe_egress', lambda name: name)
    active = set()
    peak = 0
    async def generate(account, *args, **kwargs):
        nonlocal peak
        active.add(account)
        peak = max(peak, len(active))
        await asyncio.sleep(.05)
        active.remove(account)
        return {'account':account, 'local_path':'synthetic.mp4'}
    pool._generate_effective=generate
    async def scenario():
        await asyncio.gather(*[pool.generate_video('p',None,10,'seedance-2.0-fast',attempt_timeout=2) for _ in range(3)])
    _run(scenario())
    assert peak == 3


def test_recent_failure_cooldown_waits_then_recovers(tmp_path, monkeypatch):
    module, pool = setup_pool(tmp_path, monkeypatch)
    monkeypatch.setattr(pool, '_recently_failed',
                        module.BrowserPool._recently_failed.__get__(pool))
    until = time.time() + .08
    pool._fail_until.update({name: until for name in pool.accounts})
    calls, notes = [], []

    async def generate(account, *args, **kwargs):
        assert time.time() >= until
        calls.append(account)
        return {'account': account, 'local_path': 'synthetic.mp4'}

    pool._generate_effective = generate
    _run(pool.generate_video('p', None, 5, 'seedance-2.5',
                             deadline=time.time() + 1, on_wait=notes.append))
    assert len(calls) == 1
    assert any('短时冷却' in note for note in notes)


def test_wait_without_explicit_deadline_is_bounded(tmp_path, monkeypatch):
    module, pool = setup_pool(tmp_path, monkeypatch)
    monkeypatch.setattr(module.config, 'MAX_QUEUE_WAIT_SECONDS', 1)
    pool._upload_routes['shared'] = {'until': time.time() + 100, 'reason': 'temporary'}
    tries = []

    async def scenario():
        with pytest.raises(RuntimeError, match='尚未提交生成'):
            await asyncio.wait_for(pool.generate_video(
                'p', None, 5, 'seedance-2.5', reference_image_paths=['synthetic.png'],
                on_account_try=lambda *a: tries.append(a)), timeout=3)

    _run(scenario())
    assert tries == []
    assert pool.semaphore.active == 0
