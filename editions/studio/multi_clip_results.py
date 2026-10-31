"""Publish expert30 clips separately, with one private provenance record per clip."""
import hashlib
import json
from pathlib import Path
import time

from video_delivery import delivery_metadata, qualified_delivery
from video_metadata import read_video_duration
from video_evidence import safe_text
from video_capabilities import plugin_expert_result_shape


def cached_clips(store, row):
    result = []
    base = Path(store.db_path).resolve().parent if store.db_path != ':memory:' else None
    for clip in row.get('clips') or []:
        if not base or clip.get('status') != 'completed':
            continue
        key = row['id'] + ':' + str(clip['clip_id'])
        manifest = base / '.result_provenance' / (hashlib.sha256(key.encode()).hexdigest() + '.json')
        try:
            saved = json.loads(manifest.read_text(encoding='utf-8'))
            path = Path(saved['local_path'])
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if saved.get('task_id') != key or digest.hexdigest() != saved['sha256']:
                continue
            if any(saved.get('metadata', {}).get(field) and clip.get(field) and
                   saved['metadata'][field] != clip[field] for field in ('clip_id', 'vid', 'creation_id')):
                continue
            if not qualified_delivery(saved.get('metadata') or {}):
                continue
            result.append({**clip, **saved['metadata'], 'local_path': str(path),
                           'actual_duration': read_video_duration(path)})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def publish(store, task_id, result, public_base):
    row = store.get(task_id)
    if not row:
        return
    if store.db_path != ':memory:':
        verified = {clip['clip_id'] for clip in cached_clips(store, row)}
        invalid = [{'clip_id': clip['clip_id'], 'status': 'failed', 'video_url': None,
                    'actual_duration': None, 'error': '本地文件或来源指纹已变化，等待从原会话补收'}
                   for clip in row.get('clips') or []
                   if clip.get('status') == 'completed' and clip['clip_id'] not in verified]
        if invalid:
            row = store.merge_clips(task_id, invalid)
    published = []
    for clip in result.get('clips') or []:
        clip_id = str(clip.get('clip_id') or '')
        if not clip_id:
            continue
        item = {key: clip.get(key) for key in ('clip_id', 'index', 'vid', 'creation_id')}
        if not clip.get('local_path'):
            item.update(status=clip.get('status') or 'processing', error=safe_text(clip.get('error')) or None)
            # A later failed resolver must not discard an earlier verified local original.
            if not any(c.get('clip_id') == clip_id and c.get('status') == 'completed' for c in row.get('clips', [])):
                published.append(item)
            continue
        try:
            actual = read_video_duration(clip['local_path'])
            from providers.doubao.downloads import read_video_dimensions
            dimensions = read_video_dimensions(clip['local_path']) or {}
            metadata = delivery_metadata(store, task_id + ':' + clip_id, clip)
            if not qualified_delivery(metadata):
                raise ValueError('尚未取得可验证的无水印资源')
            item.update(status='completed', actual_duration=actual,
                video_url=public_base.rstrip('/') + '/videos/' + Path(clip['local_path']).name,
                watermark='verified_download', download_source=metadata['download_source'], error=None)
            item.update({key: dimensions[key] for key in ('width', 'height') if key in dimensions})
        except (OSError, ValueError) as error:
            if any(c.get('clip_id') == clip_id and c.get('status') == 'completed' for c in row.get('clips', [])):
                continue
            item.update(status='failed', video_url=None, actual_duration=None, error=safe_text(error))
        published.append(item)
    saved = store.merge_clips(task_id, published)
    clips = saved.get('clips') or []
    completed = [c for c in clips if c.get('status') == 'completed']
    durations = [float(c['actual_duration']) for c in completed]
    try:
        options = json.loads(row.get('generation_options') or '{}')
    except (ValueError, TypeError):
        options = {}
    requested = row.get('duration') or 30
    shape = len(clips) == len(completed) and plugin_expert_result_shape(
        durations, requested, options.get('two_part', False) if isinstance(options, dict) else False)
    final = result.get('observation_finished', result.get('generation_finished', True))
    success = bool(final and result.get('generation_finished', True) and shape)
    reason = None if success else ('已取得部分结果，继续等待原会话。' if not final else
                                  f'本次{requested}秒申请结果尚不完整或规格不符；已核验条目仍可下载，可补收原会话。')
    fields = {key: result[key] for key in ('account', 'conversation_id') if result.get(key)}
    store.update(task_id, **fields, status='completed' if success else 'failed' if final else 'processing',
        video_url=None, actual_duration=None, total_actual_duration=sum(durations),
        error=reason, failure_code=None if success or not final else 'partial_results',
        finished_at=time.time() if final else None, last_poll_at=time.time())
    store.record_evidence(task_id, 'completed' if success else 'partial_results' if final else 'polling',
        {'video_count': len(completed), 'reason': reason or '已分别核验全部原片，未拼接视频'})
