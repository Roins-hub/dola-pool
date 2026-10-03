import asyncio
import json
import pytest
import config
import proxy_store
import server
from store import TaskStore


@pytest.fixture(autouse=True)
def auth_boundary(monkeypatch):
    monkeypatch.setattr(server, '_admin_auth', lambda _: None)


def test_proxy_failure_never_falls_back_direct(monkeypatch):
    monkeypatch.setattr(proxy_store, 'get_account_proxy', lambda _: {'mode': 'extract'})
    monkeypatch.setattr(proxy_store, '_ensure_node', lambda *a, **kw: None)
    monkeypatch.setattr(config, 'PROXY', '')
    with pytest.raises(ValueError, match='Proxy'):
        proxy_store.proxy_url_for('acc1')
    assert proxy_store.egress_key_for('acc1') != 'direct'


def test_route_reports_relay_without_claiming_account_proxy(monkeypatch):
    monkeypatch.setattr(config, 'TRANSPORT_MODE', 'relay')
    monkeypatch.setattr(server.pool, 'preview_route', lambda: {'next_account_id': 'acc1'})
    result = asyncio.run(server.admin_route(x_admin_key=None))
    assert result['transport'] == 'relay' and result['account_proxy_used'] is False
    assert 'Relay' in result['route_vi']


def fixture_store(tmp_path, monkeypatch):
    store = TaskStore(str(tmp_path / 'tasks.db'))
    for id, account in [('a', 'acc1'), ('b', 'acc2')]:
        store.create(id, 'seedance-2.5', 'Cat', '16:9', 15, account=account)
        store.update(id, status='failed', error='请求失败 sessionid=secret-value https://user:password@proxy.test:8080\nCookie: c_user=private-value', finished_at=123)
    monkeypatch.setattr(server, 'store', store)
    return store


def test_diagnostic_api_redacts_credentials_before_serialization(tmp_path, monkeypatch):
    fixture_store(tmp_path, monkeypatch)
    result = asyncio.run(server.admin_diagnostic_events(x_admin_key=None))
    text = json.dumps(result, ensure_ascii=False)
    for secret in ['secret-value', 'private-value', 'password', 'user:']:
        assert secret not in text
    assert result['events']
    assert all(not any('\u3400' <= c <= '\u9fff' for c in e['message_vi']) for e in result['events'])


def test_diagnostic_filter_does_not_return_unrelated_task(tmp_path, monkeypatch):
    fixture_store(tmp_path, monkeypatch)
    result = asyncio.run(server.admin_diagnostic_events(task_id='a', account='acc1', level='error', limit=200, x_admin_key=None))
    assert result['events'] and all(e['task_id'] == 'a' and e['account'] == 'acc1' for e in result['events'])
    assert not asyncio.run(server.admin_diagnostic_events(task_id='a', account='acc2', x_admin_key=None))['events']
