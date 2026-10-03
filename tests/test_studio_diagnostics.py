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


def test_reference_upload_proxy_error_never_retries_direct_even_legacy_flag(monkeypatch):
    import pure_api_gen
    from test_reference_images import FakeDola, UploadError
    from pathlib import Path
    monkeypatch.setattr(config, 'REFERENCE_UPLOAD_DIRECT_FALLBACK', True)
    dola=FakeDola(fail_first=UploadError('403 proxy sessionid=private-value https://user:password@proxy.test'))
    with pytest.raises(pure_api_gen.PureApiError) as e:
        pure_api_gen._upload_reference_images(dola,'configured-session','ctx',{},['img'],state_file=Path('state.json'),region='jp',pc_version='1')
    assert len(dola.upload_calls)==1 and dola.setup_calls==[]
    assert 'private-value' not in str(e.value) and 'password' not in str(e.value)


def test_app_logging_redacts_message_and_exception(caplog):
    import logging
    import failure_text
    failure_text.install_log_redaction()
    logger=logging.getLogger('dola-diagnostic-test')
    with caplog.at_level(logging.WARNING):
        logger.warning('sessionid=%s https://user:password@proxy.test', 'private-value')
        try: raise ValueError('Cookie: c_user=private-cookie')
        except ValueError: logger.exception('Request failed')
    assert 'private-value' not in caplog.text and 'private-cookie' not in caplog.text and 'password' not in caplog.text

@pytest.mark.asyncio
async def test_chinese_validation_error_has_vietnamese_display(monkeypatch):
    import httpx
    async def fail_download(*a): raise ValueError('参考图片下载失败 sessionid=private-value')
    monkeypatch.setattr(server,'download_reference_images',fail_download)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as client:
        r=await client.post('/api/admin/prompts/preflight',json={'prompt':'cat','reference_images':['https://fixture.invalid/reference.png']})
    assert r.status_code==422
    assert 'private-value' not in r.text
    assert r.json()['message_vi'] and not any('\u3400' <= c <= '\u9fff' for c in r.json()['message_vi'])
    assert '参考图片' in r.json()['failure_display']['details']


def test_account_group_reason_uses_vietnamese_main_text():
    import failure_text
    result=failure_text.present_account_group('冷却','上游报额度不足（今日剩余 0）')
    assert not any('\u3400' <= c <= '\u9fff' for c in result['message_vi'])
    assert '上游' in result['details']
