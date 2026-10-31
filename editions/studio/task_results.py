"""Publish validated local results, keeping mismatched-duration videos inspectable."""
import time
from pathlib import Path

from video_evidence import safe_text
from video_metadata import read_video_duration
from navigation_errors import BrowserCleanupError
from video_delivery import delivery_metadata, qualified_delivery, UNAVAILABLE_REASON


def complete_task(store, task_id, result, public_base):
    row = store.get(task_id)
    if not row:
        return
    if row.get('duration_profile') == 'plugin-expert30':
        from multi_clip_results import publish
        return publish(store, task_id, result, public_base)
    actual = read_video_duration(result['local_path'])
    metadata = delivery_metadata(store, task_id, result)
    if not metadata.get('_cached_provenance'):
        store.record_evidence(task_id, 'download_metadata', metadata)
    if not qualified_delivery(metadata):
        store.update(task_id, status='failed', video_url=None,
            account=result.get('account') or row.get('account'),
            conversation_id=result.get('conversation_id') or row.get('conversation_id'),
            actual_duration=actual, last_poll_at=time.time(), finished_at=time.time(),
            error=UNAVAILABLE_REASON, failure_code='no_watermark_unavailable')
        store.record_evidence(task_id, 'no_watermark_unavailable',
            {'actual_duration': actual, 'reason': UNAVAILABLE_REASON})
        return
    requested = row.get('duration')
    mismatch = bool(requested and abs(actual - requested) > 1)
    phase = 'duration_mismatch' if mismatch else 'completed'
    reason = f'已有成片，但实际 {actual:g} 秒，与请求的 {requested} 秒不符；未重新生成' if mismatch else None
    store.update(task_id, status='failed' if mismatch else 'completed',
                 video_url=f"{public_base}/videos/{Path(result['local_path']).name}",
                 account=result.get('account') or row.get('account'), actual_duration=actual,
                 last_poll_at=time.time(), finished_at=time.time(), error=reason,
                 failure_code='duration_mismatch' if mismatch else None)
    store.record_evidence(task_id, phase, {'actual_duration': actual, 'reason': reason or '视频已下载并核对实际时长'})


def fail_task(store, task_id, error, code=None):
    row = store.get(task_id)
    if (row and row.get('phase') == 'platform_rejected_no_charge'
            and isinstance(error, BrowserCleanupError)):
        store.record_evidence(task_id, 'platform_rejected_no_charge',
            {'reason': str(row.get('error') or '') + '；浏览器清理未确认完成，该 ISP 暂停提交。'})
        return
    phase = getattr(error, 'task_phase', 'download_failed' if row and row.get('phase') == 'downloading' else 'failed')
    if code == 'no_watermark_unavailable':
        phase = code
    reason = safe_text(error)[:500]
    attempted = (getattr(error, 'account', None)
                 if phase == 'watermark_setup_failed' and row and not row.get('account') else None)
    store.update(task_id, status='failed', error=reason,
                 failure_code=code or phase, finished_at=time.time(),
                 **({'account': attempted} if attempted else {}),
                 **({'video_url': None} if phase == 'no_watermark_unavailable' else {}))
    details = {'reason': reason}
    if attempted:
        details['account'] = attempted
    if getattr(error, 'reply', None):
        details['reply'] = safe_text(error.reply)
    store.record_evidence(task_id, phase, details)
