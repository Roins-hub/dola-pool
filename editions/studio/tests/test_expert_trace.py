import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from expert_trace import BrowserTrace, _stream_summary, record_original
from store import TaskStore


class _Context:
    def __init__(self):
        self.callbacks = {}

    def on(self, name, callback):
        self.callbacks[name] = callback


class _Request:
    method = 'POST'
    url = 'https://www.dola.com/creativity/resource/get_without_watermark?token=SECRET_QUERY'


class _Response:
    request = _Request()
    url = _Request.url
    status = 200
    headers = {'content-type': 'application/json'}

    async def json(self):
        return {'code': 0, 'data': {'without_watermark': True, 'download_video': {
            'v123': {'download_url': 'https://example.invalid/file?token=SECRET_URL'}}}}


class ExpertTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_ready_is_a_safe_task_phase(self):
        store = TaskStore(':memory:')
        try:
            store.create('trace-task', 'seedance-2.0', 'test', '16:9', 15)
            store.record_evidence('trace-task', 'trace_ready', {'trace_id': 'acct-20261025-123456-abcd1234'})
            event = store.task_events('trace-task')[-1]
            self.assertEqual(event['phase'], 'trace_ready')
            self.assertNotIn('trace_id', event['details'])
        finally:
            store._conn.close()

    async def test_sse_summary_keeps_only_field_names(self):
        raw = b'data: {"download_url":"https://example.invalid/?token=SECRET_SSE","status":4}\n\n'
        result = _stream_summary(raw)
        self.assertIn('download_url', result['field_names'])
        self.assertNotIn('SECRET_SSE', repr(result))

    async def test_browser_and_original_trace_exclude_secrets(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
                'DOLA_EXPERT_TRACE_DIR': temporary}):
            context = _Context()
            trace = BrowserTrace('acct')
            trace.attach(context)
            context.callbacks['request'](_Request())
            context.callbacks['response'](_Response())
            await trace.flush()
            record_original('acct', '123', 'v123', 200, await _Response().json())
            output = ''.join(path.read_text(encoding='utf-8')
                             for path in Path(temporary).glob('*.jsonl'))
            self.assertIn('without_watermark', output)
            self.assertIn('original_lookup', output)
            self.assertNotIn('SECRET_QUERY', output)
            self.assertNotIn('SECRET_URL', output)
            self.assertNotIn('example.invalid', output)
            self.assertEqual((Path(temporary) / '.gitignore').read_text(), '*\n')

    async def test_disabled_trace_creates_no_files(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            trace = BrowserTrace('acct')
            self.assertIsNone(trace.path)
            trace.attach(_Context())
            trace.record('test')
            await trace.flush()
            self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
