import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from prompt_context import AI_REFERENCE_NOTE, submission_prompt
from store import TaskStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(tmp_path, monkeypatch):
    import server

    db = TaskStore(str(tmp_path / 'tasks.db'))
    monkeypatch.setattr(server, 'store', db)
    monkeypatch.setattr(server, '_auth', lambda _: server._anonymous_client())
    monkeypatch.setattr(server, '_reject_unaffordable', lambda *a: None)
    monkeypatch.setattr(server, 'pool', SimpleNamespace(available=True, accounts=['acc1']))
    monkeypatch.setattr(server, '_log_image_fields', lambda *a: None)
    async def validate(refs):
        return refs
    monkeypatch.setattr(server, 'validate_reference_urls', validate)
    monkeypatch.setattr(server, 'materialize_data_urls', lambda refs: refs)
    dispatched = []
    async def capture(*args):
        dispatched.append(args)
    monkeypatch.setattr(server, '_run_task', capture)
    yield server, db, dispatched
    db._conn.close()


@pytest.mark.parametrize('protocol', ['json', 'videos', 'multipart', 'ark'])
@pytest.mark.parametrize('declared', [None, 'ai_generated', 'unspecified'])
def test_request_persists_source_without_rewriting_prompt(api, protocol, declared):
    """2026-10-29: with reference images the note is the default; an explicit unspecified opts out."""
    server, db, dispatched = api
    original = '原始提示词：人物缓缓抬眸'
    raw = dict(model='seedance-2.0-fast', prompt=original, duration=10,
               ratio='16:9', reference_images=['https://example.com/image.png'])
    if declared:
        raw['reference_source'] = declared
    marked = declared != 'unspecified'

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
            if protocol == 'multipart':
                form = {k: str(v) for k, v in raw.items() if k != 'reference_images'}
                response = await client.post('/v1/videos', data=form,
                    files={'image': ('test.png', b'synthetic-image', 'image/png')})
            elif protocol == 'ark':
                body = {k: v for k, v in raw.items() if k not in ('prompt', 'reference_images')}
                body['content'] = [{'type': 'text', 'text': original},
                    {'type': 'image_url', 'image_url': {'url': raw['reference_images'][0]}}]
                response = await client.post('/api/v3/contents/generations/tasks', json=body)
            else:
                response = await client.post('/v1/videos' if protocol == 'videos' else '/v1/videos/generations', json=raw)
            assert response.status_code in (200, 202), response.text
            tid = response.json()['id']
            await asyncio.sleep(0)
            row = db.get(tid)
            assert row['prompt'] == original
            assert row['reference_source'] == ('ai_generated' if marked else 'unspecified')
            assert row['prompt_supplement'] == (AI_REFERENCE_NOTE if marked else '')
            assert len(dispatched) == 1 and dispatched[0][2] == original
            fetched = await client.get('/v1/videos/' + tid)
            assert fetched.json()['prompt'] == original
            assert AI_REFERENCE_NOTE not in fetched.json()['prompt']
    run(scenario())


@pytest.mark.parametrize('source,refs', [('ai_generated', []), ('true', ['https://example.com/a.png']),
                                      (True, ['https://example.com/a.png'])])
@pytest.mark.parametrize('protocol', ['json', 'ark'])
def test_invalid_source_is_rejected_before_dispatch(api, source, refs, protocol):
    server, db, dispatched = api
    raw = dict(model='seedance-2.0-fast', prompt='original', duration=10,
               reference_images=refs, reference_source=source)
    if protocol == 'ark':
        raw['content'] = [{'type': 'text', 'text': raw.pop('prompt')}]
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as c:
            r = await c.post('/api/v3/contents/generations/tasks' if protocol == 'ark' else '/v1/videos', json=raw)
            assert r.status_code == 422
            assert not dispatched
            assert db._conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    run(scenario())


def test_supplement_counts_toward_prompt_limit(api, monkeypatch):
    server, _, dispatched = api
    monkeypatch.setattr(server.config, 'MAX_PROMPT_CHARS', 20)
    with pytest.raises(server.HTTPException) as error:
        run(server._submit_task(server._anonymous_client(), 'seedance-2.0-fast', 'short',
            '16:9', 10, ['https://example.com/a.png'], reference_source='ai_generated'))
    assert error.value.status_code == 422
    assert not dispatched


@pytest.mark.parametrize('marked', [False, True])
def test_worker_and_restart_keep_original_and_use_saved_supplement(tmp_path, monkeypatch, marked):
    import server
    path = str(tmp_path / 'tasks.db')
    db = TaskStore(path)
    original = '原始提示词'
    db.create('task1', 'seedance-2.0-fast', original, '16:9', 10,
              reference_source='ai_generated' if marked else 'unspecified',
              prompt_supplement=AI_REFERENCE_NOTE if marked else '')
    db._conn.close()
    db = TaskStore(path)  # Read persisted context as a restarted service does.
    monkeypatch.setattr(server, 'store', db)
    calls = []
    class Pool:
        async def generate_video(self, prompt, *a, **kw):
            calls.append(prompt)
            kw['on_account_try']('acc1', 1)
            return {'account': 'acc1', 'local_path': 'fake.mp4'}
    monkeypatch.setattr(server, 'pool', Pool())
    async def download(*a):
        return None, ['fake.png']
    monkeypatch.setattr(server, 'download_reference_images', download)
    monkeypatch.setattr(server, 'save_reference_thumbnails', lambda *a: [])
    monkeypatch.setattr(server, '_video_metadata', lambda *a: {})
    monkeypatch.setattr(server, '_prune_downloads', lambda: None)
    run(server._run_task('task1', 'seedance-2.0-fast', original, '16:9', 10, [], {}))
    assert db.get('task1')['status'] == 'completed'
    assert db.get('task1')['prompt'] == original
    assert calls == [submission_prompt(original, AI_REFERENCE_NOTE if marked else '')]
    assert calls[0].count(AI_REFERENCE_NOTE) == (1 if marked else 0)
    db._conn.close()


def test_legacy_database_migrates_without_opt_in(tmp_path):
    path = str(tmp_path / 'tasks.db')
    db = TaskStore(path)
    db.create('old', 'seedance-2.0-fast', 'historical prompt', '16:9', 10)
    db._conn.execute('ALTER TABLE tasks DROP COLUMN reference_source')
    db._conn.execute('ALTER TABLE tasks DROP COLUMN prompt_supplement')
    db._conn.commit()
    db._conn.close()
    db = TaskStore(path)
    assert db.get('old')['prompt'] == 'historical prompt'
    assert db.get('old')['reference_source'] == 'unspecified'
    assert db.get('old')['prompt_supplement'] == ''
    db._conn.close()


@pytest.mark.parametrize('refs,expected', [([], 'unspecified'), (['https://example.com/a.png'], 'ai_generated')])
def test_default_note_only_for_tasks_with_reference_images(api, refs, expected):
    server, db, dispatched = api
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as c:
            r = await c.post('/v1/videos', json=dict(model='seedance-2.0-fast', prompt='海边日落', duration=10,
                                                     reference_images=refs, reference_source=None))
            assert r.status_code in (200, 202), r.text
            row = db.get(r.json()['id'])
            assert row['reference_source'] == expected and row['prompt'] == '海边日落'
            assert row['prompt_supplement'] == (AI_REFERENCE_NOTE if expected == 'ai_generated' else '')
    run(scenario())


@pytest.mark.parametrize('protocol', ['json', 'ark', 'ark_flag'])
def test_requested_resolution_is_recorded_and_flag_stripped(api, protocol):
    """2026-10-30: 720p 交原片；1080p 及以上在开启自动超分时出片后自动超分（这里只看受理记录）。"""
    import upscale
    server, db, dispatched = api
    upscale.save_settings(db, {'provider': 'fal', 'fal_key': 'k-123456789', 'auto': 'param'})
    raw = dict(model='seedance-2.0-fast', prompt='白鹭飞过湖面', duration=10, ratio='16:9', resolution='1080p')
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as c:
            if protocol == 'json':
                r = await c.post('/v1/videos/generations', json=raw)
            else:
                text = raw['prompt'] + (' --resolution 1080p' if protocol == 'ark_flag' else '')
                body = {'model': raw['model'], 'duration': 10, 'ratio': '16:9',
                        'content': [{'type': 'text', 'text': text}]}
                if protocol == 'ark':
                    body['resolution'] = '1080p'
                r = await c.post('/api/v3/contents/generations/tasks', json=body)
            assert r.status_code in (200, 202), r.text
            row = db.get(r.json()['id'])
            assert row['requested_resolution'] == '1080p' and row['upscale_auto'] == 1
            assert row['upscale_target'] == '1080p' and row['prompt'] == '白鹭飞过湖面'
            fetched = await c.get('/v1/videos/' + row['id'])
            assert fetched.json()['status'] == 'queued'
    run(scenario())


def test_720p_and_disabled_auto_keep_the_original(api):
    import upscale
    server, db, _ = api
    upscale.save_settings(db, {'provider': 'fal', 'fal_key': 'k-123456789', 'auto': 'off'})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as c:
            low = await c.post('/v1/videos/generations', json={'prompt': 'p', 'duration': 10, 'resolution': '720p'})
            high = await c.post('/v1/videos/generations', json={'prompt': 'p', 'duration': 10, 'resolution': '1080p'})
            low_row, high_row = db.get(low.json()['id']), db.get(high.json()['id'])
            assert not low_row['upscale_auto'] and not low_row['upscale_status']
            assert not high_row['upscale_auto'] and high_row['upscale_status'] == 'skipped'
            assert '没有开启自动超分' in high_row['upscale_error']
    run(scenario())
