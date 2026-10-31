"""Exercise challenge handling on a real local browser page without external login."""
import unittest
import asyncio
import tempfile
from pathlib import Path
import json
from patchright.async_api import async_playwright
import add_account


class GoogleRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(hasattr(add_account, 'handle_google_verification'),
                        'Recovery email verification is not supported')
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.addAsyncCleanup(self.close)

    async def close(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_recovery_email_is_submitted_only_to_recovery_identity_field(self):
        await self.page.set_content('''<input id="knowledge-preregistered-email-response">
            <button id="idvPreregisteredEmailNext" onclick="document.body.dataset.sent=document.querySelector('input').value">Next</button>''')
        handled = await add_account.handle_google_verification(self.page, '', 'backup@example.net')
        self.assertTrue(handled)
        self.assertEqual(await self.page.get_attribute('body', 'data-sent'), 'backup@example.net')

    async def test_sms_challenge_is_never_filled_with_a_totp_or_recovery_address(self):
        await self.page.set_content('<input type="tel" name="idvPin"><button>Next</button>')
        with self.assertRaises(add_account.GoogleLoginAttention):
            await add_account.handle_google_verification(self.page, 'JBSWY3DPEHPK3PXP', 'backup@example.net')
        self.assertEqual(await self.page.locator('input').input_value(), '')

    async def test_totp_without_a_seed_requests_attention(self):
        await self.page.set_content('<input name="totpPin" type="tel"><button id="totpNext">Next</button>')
        with self.assertRaises(add_account.GoogleLoginAttention):
            await add_account.handle_google_verification(self.page, '', 'backup@example.net')
        self.assertEqual(await self.page.locator('input').input_value(), '')

    async def test_current_dola_google_button_does_not_require_legacy_modal_css(self):
        self.assertTrue(hasattr(add_account, 'open_google_login'), 'Current Dola login dialog is unsupported')
        await self.page.set_content('<div role="dialog"><button onclick="document.body.dataset.clicked=1">Mit Google fortfahren</button></div>')
        await add_account.open_google_login(self.page)
        self.assertEqual(await self.page.get_attribute('body', 'data-clicked'), '1')

    async def test_dola_region_restriction_is_reported_before_clicking_login(self):
        await self.page.set_content('<p>Dola ist in diesem Land oder dieser Region nicht verfügbar</p><button onclick="document.body.dataset.clicked=1">Mit Google fortfahren</button>')
        with self.assertRaisesRegex(add_account.GoogleLoginAttention, '地区'):
            await add_account.open_google_login(self.page)
        self.assertIsNone(await self.page.get_attribute('body', 'data-clicked'))

    async def test_indonesian_google_consent_continues_instead_of_cancel(self):
        self.assertTrue(hasattr(add_account, 'handle_google_consent'),
                        'Indonesian Google consent is not supported')
        await self.page.set_content('''<button onclick="document.body.dataset.result='cancel'">Batal</button>
            <button onclick="document.body.dataset.result='continue'">Lanjutkan</button>''')
        self.assertTrue(await add_account.handle_google_consent(self.page))
        self.assertEqual(await self.page.get_attribute('body', 'data-result'), 'continue')

    async def test_blank_navigation_is_not_treated_as_completed_oauth(self):
        async def route(request):
            if request.request.url.startswith('https://accounts.google.com/'):
                await request.fulfill(body='''<button onclick="location.href='https://www.dola.com/chat'">Continue</button>''', content_type='text/html')
            else:
                await request.fulfill(body='<p>Signed in</p>', content_type='text/html')
        await self.page.route('**/*', route)
        async def finish_navigation():
            await asyncio.sleep(3)
            await self.page.goto('https://accounts.google.com/consent')
        navigation = asyncio.create_task(finish_navigation())
        try:
            await add_account.google_login(self.page, 'example@example.com', 'unused', '')
            self.assertEqual(self.page.url, 'https://www.dola.com/chat')
        finally:
            await navigation

    async def dola_fixture(self, html, login_body):
        async def route(request):
            if '/passport/account/info/' in request.request.url:
                await request.fulfill(json=login_body)
            else:
                await request.fulfill(body=html, content_type='text/html; charset=utf-8')
        await self.page.route('**/*', route)
        await self.page.goto('https://www.dola.com/chat')

    async def test_expired_session_cookie_is_not_saved_as_a_completed_login(self):
        self.assertTrue(hasattr(add_account, 'save_authenticated_session'))
        await self.dola_fixture('<p>Sign in</p>', {'message':'error','data':{
            'error_code':13,'description':'Session expired, please sign in again'}})
        await self.page.context.add_cookies([{'name':'sessionid','value':'expired-fixture','domain':'.dola.com','path':'/'}])
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(await add_account.save_authenticated_session(self.page.context, self.page, Path(directory)))
            self.assertFalse((Path(directory)/'cookies.json').exists())

    async def test_verified_session_is_saved(self):
        self.assertTrue(hasattr(add_account, 'save_authenticated_session'))
        await self.dola_fixture('<p>Welcome</p>', {'message':'success','data':{'user_id':'fixture-id'}})
        await self.page.context.add_cookies([{'name':'sessionid','value':'valid-fixture','domain':'.dola.com','path':'/'}])
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(await add_account.save_authenticated_session(self.page.context, self.page, Path(directory)))
            self.assertEqual(json.loads((Path(directory)/'cookies.json').read_text())[0]['value'], 'valid-fixture')

    async def test_dola_adult_confirmation_uses_supplied_birth_date(self):
        self.assertTrue(hasattr(add_account, 'handle_dola_age_confirmation'))
        await self.dola_fixture('''<div role="dialog"><h2>Dein Alter bestätigen</h2>
            <p>Bitte bestätige zum Fortfahren, dass du mindestens 18 Jahre alt bist.</p>
            <button>Nein</button><button onclick="document.body.dataset.confirmed='yes'">Bestätigen</button></div>''', {})
        self.assertTrue(await add_account.handle_dola_age_confirmation(self.page, '2002-11-05'))
        self.assertEqual(await self.page.get_attribute('body','data-confirmed'), 'yes')

    async def test_dola_adult_confirmation_requires_an_adult_birth_date(self):
        self.assertTrue(hasattr(add_account, 'handle_dola_age_confirmation'))
        await self.dola_fixture('''<h2>Dein Alter bestätigen</h2>
            <button onclick="document.body.dataset.confirmed='yes'">Bestätigen</button>''', {})
        for birth_date in ('', 'invalid', '2020-01-01', '2099-01-01'):
            with self.subTest(birth_date=birth_date), self.assertRaises(add_account.GoogleLoginAttention):
                await add_account.handle_dola_age_confirmation(self.page, birth_date)
        self.assertIsNone(await self.page.get_attribute('body','data-confirmed'))


if __name__ == '__main__':
    unittest.main()
