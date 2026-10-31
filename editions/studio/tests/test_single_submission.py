import asyncio
import ast
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from test_video_pipeline import pool_module
import video_worker_ui as worker
from store import TaskStore
from test_prompt_input import Locator


class SingleSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_response_and_persistent_confirmation_do_not_repeat_prompt_or_confirmation(self):
        tree = ast.parse(Path('video_worker_ui.py').read_text(encoding='utf-8'))
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'generate_video')
        blocks = [n.body for n in ast.walk(fn) if isinstance(n, ast.Try)]
        body = next(nodes for nodes in blocks if any(isinstance(n, ast.Expr) and isinstance(n.value, ast.Await)
                    and isinstance(n.value.value, ast.Call) and isinstance(n.value.value.func, ast.Name)
                    and n.value.value.func.id == '_submit_prompt_once' for n in nodes))
        start = next(i for i,n in enumerate(body) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Await)
                     and isinstance(n.value.value, ast.Call) and isinstance(n.value.value.func, ast.Name)
                     and n.value.value.func.id == '_submit_prompt_once')
        end = next(i for i,n in enumerate(body) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'deadline' for t in n.targets))
        wrapper = ast.parse('async def run(page, account, duration, on_prompt_sent): pass').body[0]
        wrapper.body = body[start:end]
        sent, recorded = [], []
        class Field:
            value = 'original prompt'
            async def is_visible(self): return True
            async def is_editable(self): return True
            async def input_value(self): return self.value
            async def click(self, **kwargs): pass
            async def fill(self, text): self.value = text
            async def press(self, key, **kwargs):
                assert key == 'Enter'
                sent.append(self.value)
                self.value = ''
        field = Field()
        class Keyboard:
            async def press(self, key):
                if key == 'Enter': raise AssertionError('send must use native button')
        class SendButton:
            async def is_visible(self): return True
            async def is_enabled(self): return True
            async def get_attribute(self, name): return None
            async def click(self, **kwargs):
                sent.append(field.value)
                field.value = ''
        class Page:
            elapsed = 0
            keyboard = Keyboard()
            @property
            def url(self):
                return 'https://dola.test/chat/12345' if self.elapsed >= 90000 else 'https://dola.test/chat/local_1'
            async def wait_for_timeout(self, ms): self.elapsed += ms
            async def query_selector(self, selector): return field
            def locator(self, selector):
                return Locator([field] if selector == 'textarea' else [])
            async def screenshot(self, **kwargs): pass
        async def absent(*args): return False
        async def confirmation(*args): return 'Dauer: 10 Sekunden. Soll ich es so generieren?'
        ns = dict(_submit_prompt_once=worker._submit_prompt_once, _find_confirmation=confirmation,
                  _page_has_overload=absent, _dismiss_login_modal=absent, find_captcha_frame=lambda page: None,
                  config=worker.config, CONFIRM_DURATION_RE=worker.CONFIRM_DURATION_RE, RiskControlError=worker.RiskControlError)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), 'actual-send-flow', 'exec'), ns)
        page = Page()
        with patch.object(worker, '_handle_birthday_modal', absent), patch.object(worker, '_dismiss_login_modal', absent):
            await ns['run'](page, 'fixture', 10, recorded.append)
        self.assertGreaterEqual(page.elapsed, 90000)
        self.assertEqual(recorded, ['fixture'])
        self.assertEqual(sent, ['original prompt', 'Ja'])

    async def test_send_is_recorded_before_press_and_never_retried_on_input_error(self):
        calls = []
        class Field:
            async def is_visible(self): return True
            async def is_editable(self): return True
            async def input_value(self): return 'confirmed draft'
            async def press(self, key, **kwargs):
                calls.append(key)
                raise RuntimeError('unknown send outcome')
        class Page:
            def locator(self, selector):
                return Locator([Field()] if selector == 'textarea' else [])
        async def clear(*args): return False
        submit = getattr(worker, '_submit_prompt_once', None)
        self.assertIsNotNone(submit, 'need a single-send boundary before waiting for the conversation')
        with patch.object(worker, '_handle_birthday_modal', clear), patch.object(worker, '_dismiss_login_modal', clear):
            with self.assertRaisesRegex(RuntimeError, 'unknown send outcome'):
                await submit(Page(), 'fixture', lambda account: calls.append(account))
        self.assertEqual(calls, ['fixture', 'Enter'])

    async def test_server_records_account_before_prompt_send(self):
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_run_task')
        updates = []
        class Store(TaskStore):
            def update(self, task, **fields):
                updates.append(fields)
                super().update(task, **fields)
        store = Store(':memory:')
        store.create('task', 'seedance-2.0', 'fixture', 'default', 10)
        class Limiter:
            async def acquire(self, *args): pass
            async def release(self, *args): pass
        class Pool:
            async def generate_video(self, *args, **kwargs):
                callback = kwargs.get('on_prompt_sent')
                if callback:
                    callback('fixture')
                self_test.assertTrue(any(r.get('account') == 'fixture' and r.get('status') == 'processing' for r in updates),
                                     'send boundary must persist account before an ambiguous send')
                raise RuntimeError('fixture stops after send')
        async def refs(*args): return None, []
        self_test = self
        from task_results import fail_task, complete_task
        ns = dict(store=store, fail_task=fail_task, complete_task=complete_task,
                  config=__import__('types').SimpleNamespace(ISP_ONLY=False),
                  submission_duration=__import__('video_capabilities').submission_duration,
                  pool=Pool(), key_limiter=Limiter(), time=time, asyncio=asyncio, download_reference_images=refs,
                  ExplicitNotAcceptedError=__import__('video_outcome').ExplicitNotAcceptedError,
                  AllAccountsLimitedError=pool_module.AllAccountsLimitedError,
                  AllAccountsQuotaBlockedError=pool_module.AllAccountsQuotaBlockedError,
                  AllAccountsProxyBlockedError=pool_module.AllAccountsProxyBlockedError)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'server-single-send', 'exec'), ns)
        try:
            await ns['_run_task']('task', 'seedance-2.0', 'fixture', None, 10, [], {})
            self.assertTrue(any(r.get('account') == 'fixture' for r in updates))
            self.assertEqual(store.get('task')['phase'], 'failed')
        finally:
            store._conn.close()

    async def test_restart_requeues_interrupted_send_on_another_account_without_touching_accepted(self):
        store = TaskStore(':memory:')
        try:
            for name in ('queued', 'sent', 'accepted', 'failed'):
                store.create(name, 'seedance-2.0', 'fixture', 'default', 10)
            store.update('sent', status='processing', account='fixture')
            store.update('accepted', status='processing', account='fixture2', conversation_id='12345')
            store.update('failed', status='failed', account='fixture3')
            fail_interrupted = getattr(store, 'fail_unconfirmed_submissions', None)
            self.assertIsNotNone(fail_interrupted)
            fail_interrupted()
            self.assertEqual([r['id'] for r in store.recoverable_queued_tasks()], ['queued', 'sent'])
            self.assertEqual([r['id'] for r in store.recoverable_tasks()], ['accepted'])
            self.assertEqual(store.get('sent')['status'], 'queued')
            self.assertEqual(store.get('failed')['status'], 'failed')
            self.assertEqual(store.task_events('sent')[0]['details']['account'], 'fixture')
            self.assertIn('未取得会话编号', store.task_events('sent')[0]['details']['reason'])
        finally:
            store._conn.close()
