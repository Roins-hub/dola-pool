"""Opt-in, redacted live trace for one Dola Expert generation.

Enable before starting the server with DOLA_EXPERT_TRACE_DIR=<private directory>.
Never persist cookies, prompts, response text, or signed media URLs.
"""

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def _directory():
    raw = os.environ.get('DOLA_EXPERT_TRACE_DIR', '').strip()
    if not raw:
        return None
    directory = Path(raw).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    ignore = directory / '.gitignore'
    if not ignore.exists():
        ignore.write_text('*\n', encoding='utf-8')
    return directory


def _safe(value):
    return re.sub(r'[^A-Za-z0-9_.-]', '_', str(value))[:100]


def _write(path, record):
    try:
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': time.time(), **record}, ensure_ascii=False,
                                    separators=(',', ':')) + '\n')
    except OSError:
        # Diagnostics must never turn an already accepted video into a failure.
        pass


def _key(value):
    text = str(value)
    if len(text) > 80 or '://' in text or '?' in text or '/' in text:
        return 'key_sha256:' + hashlib.sha256(text.encode()).hexdigest()[:16]
    return text


def _shape(value, depth=0):
    """Keep response field names/types, never string contents or bearer URLs."""
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {_key(key): _shape(item, depth + 1)
                for key, item in list(value.items())[:40]}
    if isinstance(value, list):
        return {'count': len(value), 'first': _shape(value[0], depth + 1) if value else None}
    if isinstance(value, bool):
        return value
    return type(value).__name__


def _stream_summary(raw):
    """Describe a completed SSE response without retaining its text or URLs."""
    keys = set()
    json_events = 0

    def visit(value, depth=0):
        if depth >= 7:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and len(key) <= 80 and '://' not in key:
                    keys.add(key)
                visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:80]:
                visit(item, depth + 1)

    sample = raw[:2_000_000].decode('utf-8', 'replace')
    for line in sample.splitlines():
        if not line.startswith('data:'):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except (TypeError, ValueError):
            continue
        json_events += 1
        visit(payload)
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'sample_truncated': len(raw) > 2_000_000, 'json_events': json_events,
            'field_names': sorted(keys)[:100]}


class BrowserTrace:
    BODY_PATHS = frozenset({
        '/creativity/user_config/get', '/creativity/user_config/set',
        '/creativity/resource/get_without_watermark',
        '/samantha/video/get_play_info', '/creativity/share/get_video_share_info',
    })

    def __init__(self, account):
        directory = _directory()
        self.path = (directory / f'{_safe(account)}-{time.strftime("%Y%m%d-%H%M%S")}-'
                     f'{uuid.uuid4().hex[:8]}.jsonl') if directory else None
        self.pending = set()

    def record(self, event, **fields):
        if self.path:
            _write(self.path, {'event': event, **fields})

    def attach(self, context):
        if not self.path:
            return
        self.record('browser_open')

        def request(req):
            parsed = urlsplit(req.url)
            if parsed.hostname == 'www.dola.com' and parsed.path.startswith(
                    ('/chat/', '/creativity/', '/samantha/')):
                self.record('request', path=parsed.path, method=req.method)
            elif re.fullmatch(r'v\d+-dola\.dola\.com', parsed.hostname or ''):
                self.record('media_request', host=parsed.hostname,
                            url_sha256=hashlib.sha256(req.url.encode()).hexdigest())

        async def response(resp):
            parsed = urlsplit(resp.url)
            if parsed.hostname != 'www.dola.com' or not parsed.path.startswith(
                    ('/chat/', '/creativity/', '/samantha/')):
                return
            info = {'path': parsed.path, 'status': resp.status,
                    'method': resp.request.method}
            if parsed.path == '/chat/completion' and resp.status == 200:
                try:
                    body = await asyncio.wait_for(resp.body(), timeout=45)
                    info['stream'] = _stream_summary(body)
                except Exception:
                    info['stream_unavailable'] = True
            if parsed.path in self.BODY_PATHS and 'json' in resp.headers.get('content-type', '').lower():
                try:
                    payload = await asyncio.wait_for(resp.json(), timeout=5)
                    if isinstance(payload, dict):
                        info['code'] = payload.get('code') if isinstance(payload.get('code'), int) else None
                        info['data_shape'] = _shape(payload.get('data'))
                except Exception:
                    info['body_unavailable'] = True
            self.record('response', **info)

        context.on('request', request)

        def schedule(resp):
            task = asyncio.create_task(response(resp))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

        context.on('response', schedule)

    async def flush(self):
        if self.pending:
            _, pending = await asyncio.wait(tuple(self.pending), timeout=6)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)


def record_poll(account, conversation, accepted, video_count):
    try:
        directory = _directory()
    except OSError:
        return
    if directory:
        _write(directory / f'{_safe(account)}-{_safe(conversation)}-poll.jsonl', {
            'event': 'expert_poll', 'accepted': bool(accepted), 'video_count': int(video_count)})


def record_original(account, conversation, vid, http_status, payload=None):
    try:
        directory = _directory()
    except OSError:
        return
    if not directory:
        return
    data = payload.get('data') if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    downloads = data.get('download_video')
    entry = downloads.get(vid) if isinstance(downloads, dict) else None
    _write(directory / f'{_safe(account)}-{_safe(conversation)}-original.jsonl', {
        'event': 'original_lookup', 'http_status': int(http_status),
        'code': payload.get('code') if isinstance(payload, dict) and isinstance(payload.get('code'), int) else None,
        'vid_sha256': hashlib.sha256(vid.encode()).hexdigest(),
        'without_watermark': data.get('without_watermark') is True,
        'download_count': len(downloads) if isinstance(downloads, dict) else 0,
        'entry_shape': _shape(entry),
    })
