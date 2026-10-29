import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

import proxy_store


@pytest.fixture
def api(tmp_path, monkeypatch):
    import server
    from browser_pool import BrowserPool
    monkeypatch.setattr(proxy_store, 'DB_PATH', tmp_path / 'proxy.db')
    monkeypatch.setattr(server, 'ADMIN_SESSIONS', {'test-token': {'username': 'admin', 'expires_at': time.time()+600}})
    monkeypatch.setattr(server, 'user_get', lambda _: {'enabled': True})
    pool = SimpleNamespace(accounts=['acc1'], _locks={}, _activities={}, _phases={})
    pool._use_account = BrowserPool._use_account.__get__(pool)
    monkeypatch.setattr(server, 'pool', pool)
    monkeypatch.setattr(server, 'JOBS', {})
    return server, pool


def test_password_reveal_requires_session_and_does_not_leak_in_list(api):
    server, _ = api
    record = proxy_store.create_proxy('test', 'http', 'example.test', 8080, password='synthetic-secret')
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as c:
            url = '/api/admin/proxies/' + record['id'] + '/password'
            for headers in ({}, {'X-Admin-Key':'invalid'}):
                r=await c.post(url, headers=headers)
                assert r.status_code == 401 and 'synthetic-secret' not in r.text
            headers={'X-Admin-Key':'test-token'}
            listed=await c.get('/api/admin/proxies', headers=headers)
            assert 'synthetic-secret' not in listed.text
            assert 'password' not in listed.json()['proxies'][0]
            r=await c.post(url, headers=headers)
            assert r.json() == {'password':'synthetic-secret'}
            assert r.headers['cache-control']=='no-store'
            assert (await c.post('/api/admin/proxies/missing/password',headers=headers)).status_code==404
            await c.patch('/api/admin/proxies/'+record['id'],headers=headers,json={'remark':'updated'})
            assert proxy_store.get_proxy(record['id'])['password']=='synthetic-secret'
    asyncio.run(check())


def test_account_probe_skips_busy_accounts_and_locks_idle_account(api, monkeypatch):
    server,pool=api
    calls=[]
    def probe(name):
        assert pool._locks[name].locked()
        calls.append(name)
        return {'ok':True,'ip':'203.0.113.5'}
    monkeypatch.setattr(proxy_store,'probe_account_exit_ip',probe)
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as c:
            url='/api/admin/accounts/acc1/probe-ip';headers={'X-Admin-Key':'test-token'}
            assert (await c.post(url)).status_code==401
            assert (await c.post('/api/admin/accounts/missing/probe-ip',headers=headers)).status_code==404
            lock=pool._locks.setdefault('acc1',asyncio.Lock())
            await lock.acquire()
            assert (await c.post(url,headers=headers)).json()['skipped']
            lock.release()
            server.JOBS['acc1']={'status':'running'}
            assert (await c.post(url,headers=headers)).json()['skipped']
            server.JOBS.clear()
            assert calls==[]
            assert (await c.post(url,headers=headers)).json()['ip']=='203.0.113.5'
            assert calls==['acc1'] and not lock.locked() and not pool._activities
    asyncio.run(check())


def test_probe_errors_redact_credentials(api, monkeypatch):
    server,_=api
    monkeypatch.setattr(proxy_store,'probe_proxy_record',lambda _: {'ok':False,'error':'failed socks5://user:secret@example.test:1080 password=secret'})
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as c:
            r=await c.post('/api/admin/proxies/test/probe',headers={'X-Admin-Key':'test-token'})
            assert 'secret' not in r.text and not r.json()['ok']
    asyncio.run(check())


def test_static_exit_persists_and_dynamic_session_is_preserved(api, monkeypatch):
    static=proxy_store.create_proxy('static','http','example.test',8080)
    proxy_store.set_account_proxy('acc1',static['id'])
    monkeypatch.setattr(proxy_store,'probe_exit_ip',lambda _: {'ok':True,'ip':'203.0.113.6','isp':'test'})
    assert proxy_store.probe_account_exit_ip('acc1')['ok']
    assert proxy_store.get_session('acc1')['last_exit_ip']=='203.0.113.6'
    dynamic=proxy_store.create_proxy('dynamic','http','example.test',8081,mode='dynamic',sticky_ttl=0)
    proxy_store.set_account_proxy('acc2',dynamic['id'])
    proxy_store.proxy_url_for('acc2')
    before=proxy_store.get_session('acc2')['session']
    assert proxy_store.probe_account_exit_ip('acc2')['ok']
    assert proxy_store.get_session('acc2')['session']==before


def test_missing_extract_node_does_not_probe_default(api,monkeypatch):
    monkeypatch.setattr(proxy_store,'proxy_url_for',lambda _: 'http://default.test:80')
    monkeypatch.setattr(proxy_store,'get_account_proxy',lambda _: {'id':'p','mode':'extract'})
    monkeypatch.setattr(proxy_store,'get_session',lambda _: None)
    def unexpected(*a):raise AssertionError('must not probe fallback')
    monkeypatch.setattr(proxy_store,'probe_exit_ip',unexpected)
    assert not proxy_store.probe_account_exit_ip('acc1')['ok']
