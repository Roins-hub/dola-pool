"""Real local browser fixtures: never contact Doubao or solve a challenge."""
import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from patchright.async_api import async_playwright
from providers.doubao.runtime import BrowserSession


class ManualVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.driver = await async_playwright().start()
        self.addAsyncCleanup(self.driver.stop)
        self.context = await self.driver.chromium.launch_persistent_context(
            str(Path(self.temp.name) / 'profile'), headless=True)
        self.addAsyncCleanup(self.context.close)
        self.page = await self.context.new_page()
        await self.page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
            <div data-message-id="user-1">original cat request 15 seconds</div>
            <div class="assistant"></div><textarea id="composer"></textarea>
            <button id="send" onclick="window.sends=(window.sends||0)+1">send</button>'''))
        await self.page.goto('https://www.doubao.com/chat/local_fixture')
        self.session = BrowserSession(SimpleNamespace(
            _snapshot_path=lambda _: Path(self.temp.name) / 'snapshot.json'), {'name': 'fixture'})
        self.session.context, self.session.page = self.context, self.page
        self.session.dialog_prompt = 'original cat request 15 seconds'
        self.events = []
        async def event(phase, **details): self.events.append((phase, details))
        self.session.on_progress = event
        self.cap = {'id': 'dialog_seedance_2.0_fast', 'entry_mode': 'ordinary_dialog', 'selectors': {'assistant': '.assistant',
                    'composer': '#composer', 'send': '#send'},
                    'receipt_patterns': ['视频生成中'], 'rejection_patterns': ['无法生成']}
        self.snapshot = {'id': 'fixture', 'sent': True, 'created_at': time.time(),
            'request': {'auto_confirm': True, 'model': 'dialog_seedance_2.0_fast', 'duration': 15},
            'baseline_texts': [], 'baseline_messages': [], 'baseline_media': []}
        await self.session.capture_submission_page(self.snapshot)
        self.session.completion_error = {'code': '710022004', 'message': 'rate limited', 'no_charge': False}
        for key, value in [('VERIFICATION_GRACE_SECONDS', .5), ('VERIFICATION_WAIT_SECONDS', 1.5),
                           ('VERIFICATION_POLL_SECONDS', .03)]:
            p = patch('providers.doubao.runtime.' + key, value, create=True)
            p.start(); self.addCleanup(p.stop)

    async def challenge(self, hidden=False):
        await self.page.evaluate('''hidden=>{
          const el=document.createElement('div');el.id='captcha_container';
          el.innerHTML='<iframe title="verification"></iframe>';
          if(hidden)el.style.display='none';document.body.append(el);
        }''', hidden)

    async def test_delayed_captcha_waits_past_observation_timeout_and_does_not_click_send(self):
        async def later():
            await asyncio.sleep(.12)
            await self.challenge()
            await asyncio.sleep(.15)
            self.assertFalse(self.page.is_closed())
            self.assertIn('waiting_verification', [p for p, _ in self.events])
            await self.page.locator('#captcha_container').evaluate('(n)=>n.remove()')
            await self.page.locator('.assistant').evaluate('(n)=>n.innerText="请确认生成视频"')
        task = asyncio.create_task(later())
        result = await self.session.observe(self.snapshot, self.cap, .01)
        await task
        self.assertEqual(result['phase'], 'verification_incomplete')
        self.assertFalse(result['no_charge'])
        self.assertIn('verification_continue', [p for p, _ in self.events])
        self.assertIsNone(await self.page.evaluate('()=>window.sends', isolated_context=False))

    async def test_human_verification_original_receipt_wins_over_old_error(self):
        from test_doubao_execution_evidence import message, wire
        self.snapshot['request']['ratio'] = '16:9'
        await self.challenge()
        async def later():
            await asyncio.sleep(.15)
            await self.page.locator('#captcha_container').evaluate('(n)=>n.remove()')
            await self.page.evaluate('''()=>{history.replaceState(null,'','/chat/original-123');
              document.querySelector('.assistant').innerText='Seedance 2.0 Fast 视频生成中'}''')
            self.session.payloads = [wire(message(execution=True, conversation='original-123'))]
        task = asyncio.create_task(later())
        result = await self.session.observe(self.snapshot, self.cap, .01)
        await task
        self.assertEqual(result['phase'], 'accepted')
        self.assertEqual(result['conversation_id'], 'original-123')
        self.assertFalse(self.snapshot.get('verification_pending'))
        self.assertIsNone(await self.page.evaluate('()=>window.sends', isolated_context=False))

    async def test_manual_different_prompt_is_not_assigned_to_original_task(self):
        await self.challenge()
        async def later():
            await asyncio.sleep(.15)
            await self.page.evaluate('''()=>{document.querySelector('#captcha_container').remove();
              document.querySelector('[data-message-id="user-1"]').innerText='different cat request 10 seconds';
              history.replaceState(null,'','/chat/different-123');
              document.querySelector('.assistant').innerText='视频生成中'}''')
        task = asyncio.create_task(later())
        result = await self.session.observe(self.snapshot, self.cap, .01)
        await task
        self.assertEqual(result['phase'], 'verification_incomplete')
        self.assertIsNone(result.get('conversation_id'))
        self.assertNotIn('conversation_id', self.snapshot)

    async def test_verification_after_fenced_confirmation_accepts_original_receipt(self):
        from test_doubao_execution_evidence import message, wire
        from providers.doubao.confirmation import CONFIRM_TEXT
        self.snapshot['request']['ratio'] = '16:9'
        self.snapshot['confirmation_sent'] = True
        await self.page.evaluate('''text=>{
          const n=document.createElement('div');n.dataset.messageId='user-2';
          n.textContent=text;document.body.append(n);
          history.replaceState(null,'','/chat/original-123');
        }''', CONFIRM_TEXT)
        self.snapshot['conversation_id'] = 'original-123'
        await self.challenge()
        async def later():
            await asyncio.sleep(.15)
            await self.page.locator('#captcha_container').evaluate('(n)=>n.remove()')
            self.session.payloads = [wire(message(execution=True, conversation='original-123'))]
        task = asyncio.create_task(later())
        result = await self.session.observe(self.snapshot, self.cap, .01)
        await task
        self.assertEqual(result['phase'], 'accepted')
        self.assertFalse(self.snapshot.get('verification_pending'))
        self.assertIsNone(await self.page.evaluate('()=>window.sends', isolated_context=False))

    async def test_confirmation_fence_does_not_allow_another_prompt_after_original(self):
        from providers.doubao.verification import original_request_visible
        from providers.doubao.confirmation import CONFIRM_TEXT
        self.snapshot['confirmation_sent'] = True
        for text in (CONFIRM_TEXT, 'another request for a dog video'):
            await self.page.evaluate('''text=>{const n=document.createElement('div');
              n.dataset.messageId='user-'+document.querySelectorAll('[data-message-id]').length;
              n.textContent=text;document.body.append(n)}''', text)
        self.assertFalse(await original_request_visible(self.page, self.snapshot, self.cap))

    async def test_unfenced_confirmation_is_not_treated_as_original_request(self):
        from providers.doubao.verification import original_request_visible
        from providers.doubao.confirmation import CONFIRM_TEXT
        await self.page.evaluate('''text=>{const n=document.createElement('div');
          n.dataset.messageId='user-2';n.textContent=text;document.body.append(n)}''', CONFIRM_TEXT)
        self.assertFalse(await original_request_visible(self.page, self.snapshot, self.cap))

    async def test_hidden_captcha_does_not_become_manual_verification(self):
        await self.challenge(hidden=True)
        await self.page.locator('.assistant').evaluate('(n)=>n.innerText="验证码 captcha_container"')
        result = await self.session.observe(self.snapshot, self.cap, .01)
        self.assertEqual(result['phase'], 'rejected')
        self.assertEqual(result['error_code'], 'completion_710022004')
        self.assertNotIn('waiting_verification', [p for p, _ in self.events])

    async def test_receipt_while_challenge_still_visible_is_not_accepted_early(self):
        await self.page.evaluate('''()=>{history.replaceState(null,'','/chat/original-123');
          document.querySelector('.assistant').innerText='Seedance 2.0 Fast 视频生成中'}''')
        await self.challenge()
        result = await self.session.observe(self.snapshot, self.cap, .01)
        self.assertEqual(result['phase'], 'verification_incomplete')

    async def test_captcha_without_error_response_also_preserves_window(self):
        self.session.completion_error = None
        await self.challenge()
        result = await self.session.observe(self.snapshot, self.cap, .01)
        self.assertEqual(result['phase'], 'verification_incomplete')
        self.assertIn('waiting_verification', [p for p, _ in self.events])

    async def test_closing_window_during_verification_is_explicit_incomplete(self):
        await self.challenge()
        async def later():
            await asyncio.sleep(.15)
            await self.page.close()
        task = asyncio.create_task(later())
        result = await self.session.observe(self.snapshot, self.cap, .01)
        await task
        self.assertEqual(result['phase'], 'verification_incomplete')
        self.assertIn('关闭', result['reason'])


if __name__ == '__main__': unittest.main()
