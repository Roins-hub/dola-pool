"""Catch raw-language leakage, missing metadata and secrets at real API boundaries."""
import json
import re
import time
from pathlib import Path
import pytest
import failure_text as ft
import server
import config
from store import TaskStore

CJK = re.compile(r'[\u3400-\u9fff]')


def test_operational_paths_are_isolated_before_import():
    repo = Path(__file__).resolve().parents[1]
    assert Path.cwd() != repo
    for path in (config.DB_PATH, config.POOL_DB_PATH, config.DOWNLOAD_DIR,
                 server.pool.accounts_dir):
        assert not Path(path).resolve().is_relative_to(repo)
    assert not server.pool.accounts_dir.is_symlink()


def test_copyright_refusal_has_vietnamese_message():
    result = ft.present('涉嫌侵权，未授权使用他人形象', code='1234')
    assert result['category'] == 'copyright'
    assert 'Bản quyền' in result['message_vi']
    assert not CJK.search(result['message_vi'] + result['next_action_vi'])
    assert result['upstream_code'] == '1234'
    assert '涉嫌侵权' in result['details']


def test_unknown_error_does_not_invent_cause():
    result = ft.present('陌生错误 987654')
    assert result['category'] == 'unknown'
    assert 'chưa xác định' in result['message_vi'].lower()
    assert not CJK.search(result['message_vi'] + result['next_action_vi'])


def test_legacy_note_preserves_user_note():
    result = ft.present_note('Tài khoản dùng cho cảnh thiên nhiên\n[2026-09-23 01:05] 肖像保护：未认证人脸')
    assert 'Tài khoản dùng cho cảnh thiên nhiên' in result['message_vi']
    assert not CJK.search(result['message_vi'])
    assert '未认证人脸' in result['details']


def test_progress_details_redact_secrets():
    result = ft.present_update({'status':'pending', 'texts':[
        '今日剩余 100 个视频生成额度', 'sessionid=PRIVATE authorization=Bearer TOPSECRET',
        'https://user:PASS@provider.invalid/?token=SECRET'], 'wait_minutes':'3分钟'})
    encoded = json.dumps(result, ensure_ascii=False)
    for secret in ('PRIVATE','TOPSECRET','PASS','SECRET'):
        assert secret not in encoded
    assert not CJK.search(result['message_vi'])
    assert result['wait_vi'] == '3 phút'


@pytest.mark.asyncio
async def test_task_api_localizes_history_without_rewriting_original(tmp_path, monkeypatch):
    store = TaskStore(str(tmp_path/'tasks.db'))
    monkeypatch.setattr(server, 'store', store)
    monkeypatch.setattr(server, '_admin_auth', lambda _: None)
    store.create('legacy','seedance-2.5','cat','16:9',10)
    store.update('legacy',status='failed',error='涉嫌侵权',upstream_updates=json.dumps([
        {'status':'pending','texts':['正在生成视频'],'at':time.time()}]))
    response = await server.admin_get_task('legacy')
    assert response['failure_display']['category'] == 'copyright'
    assert not CJK.search(response['upstream_display'][0]['message_vi'])
    assert store.get('legacy')['error'] == '涉嫌侵权'
    listed = await server.admin_tasks()
    assert listed['tasks'][0]['failure_display']['category'] == 'copyright'


@pytest.mark.asyncio
async def test_confirmed_resume_failure_persists_category(tmp_path, monkeypatch):
    store = TaskStore(str(tmp_path/'tasks.db'))
    monkeypatch.setattr(server,'store',store)
    store.create('resume','seedance-2.5','cat','16:9',10)
    store.update('resume',account='a',conversation_id='c',deadline_at=time.time()+50)
    async def refused(*args, **kwargs):
        raise server.ConfirmedVideoFailure('涉嫌侵权')
    monkeypatch.setattr(server.pool,'resume_video',refused)
    await server._resume_task(store.get('resume'))
    row=store.get('resume')
    assert row['status']=='failed'
    assert row['failure_group']=='copyright'
    assert row['conversation_id']=='c'

def test_new_account_note_is_vietnamese():
    result = ft.account_note_line('肖像保护','未认证人脸','video_1',time.time())
    assert not CJK.search(result)
    assert 'video_1' in result

@pytest.mark.asyncio
async def test_legacy_api_fields_never_expose_secrets(tmp_path, monkeypatch):
    store=TaskStore(str(tmp_path/'tasks.db'))
    monkeypatch.setattr(server,'store',store)
    monkeypatch.setattr(server,'_admin_auth',lambda _:None)
    store.create('secret','seedance-2.5','cat','16:9',10)
    store.update('secret',error='sessionid=VERYPRIVATE',upstream_updates=json.dumps([
        {'texts':['authorization=Bearer PRIVATEKEY'], 'status':'pending'}]))
    for result in (await server.admin_get_task('secret'),await server.admin_tasks()):
        assert 'VERYPRIVATE' not in json.dumps(result)
        assert 'PRIVATEKEY' not in json.dumps(result)
    assert 'VERYPRIVATE' in store.get('secret')['error']
