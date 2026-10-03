import asyncio
import pytest
from browser_pool import BrowserPool, GROUP_FULL
import browser_pool as bp


def make_pool(tmp_path):
    p = BrowserPool(accounts_dir=str(tmp_path / 'accounts'), db_path=str(tmp_path / 'pool.db'))
    (tmp_path / 'accounts' / 'acc1').mkdir(parents=True)
    p.ensure_account('acc1')
    p.set_login_status('acc1', True)
    return p


def test_legacy_accounts_use_enabled_default_pool(tmp_path):
    p = make_pool(tmp_path)
    assert p.list_inventory_pools()[0]['id'] == 'default'
    row = p.list_accounts()[0]
    assert row['pool_id'] == 'default' and row['pool_enabled']
    p.set_note('acc1', 'Giữ ghi chú')
    p.set_inventory_pool('personal', name='Cá nhân', enabled=True)
    p.assign_inventory_pool(['acc1'], 'personal')
    assert p.list_accounts()[0]['note'] == 'Giữ ghi chú'
    with pytest.raises(ValueError):
        p.assign_inventory_pool(['acc1'], 'missing')
    assert p.list_accounts()[0]['pool_id'] == 'personal'


def test_disabled_pool_never_receives_new_job(tmp_path):
    p = make_pool(tmp_path)
    p.set_inventory_pool('default', enabled=False)
    assert p._ordered_candidates((GROUP_FULL,)) == []
    assert not p.available
    assert not p.all_accounts_limited and not p.all_accounts_quota_blocked


def test_disabling_pool_does_not_stop_existing_conversation(tmp_path, monkeypatch):
    p = make_pool(tmp_path)
    p.set_inventory_pool('default', enabled=False)
    monkeypatch.setattr(p, 'is_pure_account', lambda a: False)
    calls = []
    async def resume(account, conversation_id, timeout, **kwargs):
        calls.append((account, conversation_id))
        return {'video_url': 'fixture'}
    async def playable(result):
        return result
    monkeypatch.setattr(bp, 'resume_video', resume)
    monkeypatch.setattr(p, '_web_playable', playable)
    asyncio.run(p.resume_video('acc1', 'existing-conversation', 10, cost=0))
    assert calls == [('acc1', 'existing-conversation')]


def test_import_preview_dedupes_identity_and_reports_rows(tmp_path):
    import server
    p = make_pool(tmp_path)
    p.set_email('acc1', 'old@example.com')
    result = server._preview_account_import(p, 'new@example.com|pw\nNEW@example.com|pw2\nold@example.com|pw\ninvalid')
    assert result['ready'] == 1 and result['duplicate'] == 2 and result['invalid'] == 1
    assert [r['line'] for r in result['rows']] == [1, 2, 3, 4]
    assert 'pw' not in str(result)


def test_fb_bulk_uses_existing_onboarding_and_keeps_identity(tmp_path, monkeypatch):
    import server
    p = make_pool(tmp_path)
    monkeypatch.setattr(server, 'pool', p)
    monkeypatch.setattr(server, 'JOBS', {})
    calls = []
    async def login(name, line):
        calls.append((name, line))
        (tmp_path / 'accounts' / name).mkdir()
        return {'sessionid': 'fixture-only-session'}
    monkeypatch.setattr(server, 'add_fb_account_flow', login)
    line = '123|pw|c_user=123; xs=fixture;|token|fb@example.com'
    async def run():
        await server._run_add_job('fb1', '', '', '', facebook=line)
        preview = server._preview_account_import(p, line)
        assert preview['duplicate'] == 1
    asyncio.run(run())
    assert calls == [('fb1', line)]
    assert server.JOBS['fb1']['status'] == 'success'
    assert p._meta('fb1')['facebook_uid'] == '123'
