"""Cookie import must not turn native JS state into HttpOnly state."""
import json
import unittest

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao.accounts import parse_cookies


def auth(**changes):
    return {'name': 'sessionid', 'value': 'fixture-session', 'domain': '.doubao.com', **changes}


class CookieAttributeTests(unittest.TestCase):
    def test_playwright_attributes_and_identity_hash_are_preserved(self):
        source = [auth(path='/chat/', secure=True, httpOnly=True, sameSite='Strict', expires=1900000000.25),
                  {'name': 'passport_csrf_token', 'value': 'fixture-csrf', 'domain': 'www.doubao.com',
                   'path': '/', 'secure': False, 'httpOnly': False, 'sameSite': 'Lax', 'expires': -1}]
        result, identities = parse_cookies(json.dumps(source))
        self.assertEqual(result, source)
        _, defaults = parse_cookies(json.dumps([auth()]))
        self.assertEqual(identities, defaults)

    def test_header_defaults_keep_only_session_credentials_httponly(self):
        result, _ = parse_cookies('Cookie: sessionid=fixture; passport_csrf_token=csrf; s_v_web_id=fp; ttwid=tt; uid_tt=user')
        by_name = {cookie['name']: cookie for cookie in result}
        self.assertTrue(by_name['sessionid']['httpOnly'])
        for name in ('passport_csrf_token', 's_v_web_id', 'ttwid', 'uid_tt'):
            with self.subTest(name=name):
                self.assertFalse(by_name[name]['httpOnly'])
                self.assertEqual(by_name[name]['path'], '/')
                self.assertTrue(by_name[name]['secure'])

    def test_netscape_attributes_and_httponly_marker_are_preserved(self):
        raw = ('# Netscape HTTP Cookie File\n'
               '#HttpOnly_.doubao.com\tTRUE\t/chat/\tTRUE\t1900000000\tsessionid\tfixture-session\n'
               'www.doubao.com\tFALSE\t/chat/\tFALSE\t0\tpassport_csrf_token\tfixture-csrf')
        result, _ = parse_cookies(raw)
        self.assertEqual(result[0], {'name': 'sessionid', 'value': 'fixture-session', 'domain': '.doubao.com',
                                     'path': '/chat/', 'secure': True, 'httpOnly': True,
                                     'sameSite': 'Lax', 'expires': 1900000000})
        self.assertEqual(result[1], {'name': 'passport_csrf_token', 'value': 'fixture-csrf', 'domain': 'www.doubao.com',
                                     'path': '/chat/', 'secure': False, 'httpOnly': False, 'sameSite': 'Lax'})

    def test_chrome_export_enums_and_expiration_date_keep_semantics(self):
        result, _ = parse_cookies(json.dumps([auth(sameSite='no_restriction', expirationDate=1900000000,
                                                    path='/', secure=True, httpOnly=False)]))
        self.assertEqual(result[0]['sameSite'], 'None')
        self.assertEqual(result[0]['expires'], 1900000000)
        self.assertFalse(result[0]['httpOnly'])
        result, _ = parse_cookies(json.dumps([auth(sameSite='unspecified')]))
        self.assertNotIn('sameSite', result[0])

    def test_rejects_malformed_metadata_without_boolean_or_numeric_coercion(self):
        patches = [dict(path='chat'), dict(path='/bad\npath'), dict(path='/bad;path'),
                   dict(path=None), dict(secure=1), dict(secure='false'), dict(httpOnly='false'),
                   dict(httpOnly=0), dict(sameSite='unknown'), dict(sameSite=None),
                   dict(expires=True), dict(expires='1900000000'), dict(expires=float('nan')),
                   dict(expires=float('inf')), dict(expires=-2), dict(expires=1e99), dict(expires=10**400),
                   dict(expires=1900000000, expirationDate=1800000000)]
        for patch in patches:
            with self.subTest(patch=patch):
                with self.assertRaises(ProviderError) as caught:
                    parse_cookies(json.dumps([auth(**patch)]))
                self.assertEqual(caught.exception.code, 'invalid_cookie')

    def test_other_platform_and_cookie_injection_remain_rejected(self):
        for patch in (dict(domain='.doubao.com.evil.example'), dict(domain='.douyin.com'),
                      dict(domain='..doubao.com'), dict(domain='bad\n.doubao.com'),
                      dict(value='secret\nInjected: value'), dict(value='x;y'), dict(name='bad;name')):
            with self.subTest(patch=patch), self.assertRaises(ProviderError):
                parse_cookies(json.dumps([auth(**patch)]))
        with self.assertRaises(ProviderError):
            parse_cookies('passport_csrf_token=csrf')

    def test_netscape_rejects_invalid_flags_and_honors_host_scope(self):
        for field, value in ((1, 'yes'), (3, '0'), (4, 'NaN'), (4, '-1')):
            fields = ['.doubao.com', 'TRUE', '/', 'TRUE', '0', 'sessionid', 'fixture']
            fields[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ProviderError):
                parse_cookies('\t'.join(fields))
        result, _ = parse_cookies('doubao.com\tTRUE\t/\tTRUE\t0\tsessionid\tfixture')
        self.assertEqual(result[0]['domain'], '.doubao.com')
        result, _ = parse_cookies('.doubao.com\tFALSE\t/\tTRUE\t0\tsessionid\tfixture')
        self.assertEqual(result[0]['domain'], 'doubao.com')


class NativeCookieTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_javascript_reads_csrf_but_cannot_read_auth_session(self):
        cookies, _ = parse_cookies(json.dumps([
            auth(path='/', secure=True, httpOnly=True, sameSite='Lax', expires=-1),
            {'name': 'passport_csrf_token', 'value': 'fixture-csrf', 'domain': '.doubao.com',
             'path': '/', 'secure': True, 'httpOnly': False, 'sameSite': 'Lax', 'expires': -1}]))
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<html>Offline cookie fixture</html>'))
                await context.add_cookies(cookies)
                page = await context.new_page()
                await page.goto('https://www.doubao.com/chat/')
                visible = await page.evaluate('document.cookie')
                self.assertIn('passport_csrf_token=fixture-csrf', visible)
                self.assertNotIn('sessionid=', visible)
                self.assertEqual(len(await context.cookies()), 2)
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
