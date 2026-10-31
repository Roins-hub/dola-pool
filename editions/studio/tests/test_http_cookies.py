"""Replay browser-scoped cookies through the HTTP login preparation path."""
import json
import unittest
from unittest.mock import patch

import requests

from test_video_pipeline import pool_module  # Isolate the imported usage database.
import video_worker_http as worker
from navigation_errors import BrowserSetupError


def browser_cookies():
    return [
        {'name': 'sessionid', 'value': 'fixture-login', 'domain': '.dola.com', 'path': '/'},
        {'name': 'flow_user_country', 'value': 'DE', 'domain': '.dola.com', 'path': '/'},
        {'name': 'flow_user_country', 'value': 'DE', 'domain': '.www.dola.com', 'path': '/'},
        {'name': 'i18next', 'value': 'de', 'domain': '.dola.com', 'path': '/'},
        {'name': 'i18next', 'value': 'en', 'domain': 'www.dola.com', 'path': '/'},
        {'name': 'i18next', 'value': 'ko', 'domain': 'www.dola.com', 'path': '/chat'},
    ]


class HttpCookieTests(unittest.TestCase):
    def test_initialization_preserves_duplicate_names_with_distinct_scopes(self):
        with requests.Session() as session:
            worker.protocol.apply_state_cookies(session, {'cookies_list': browser_cookies()})
            before = {(c.name, c.domain, c.path): c.value for c in session.cookies}
            try:
                worker.protocol._ensure_spa_cookies(session, {'fp': 'fixture-fp'}, 'DE')
            except requests.cookies.CookieConflictError:
                self.fail('Valid browser cookie scopes must not prevent HTTP initialization')
            after = {(c.name, c.domain, c.path): c.value for c in session.cookies}
            self.assertEqual({scope: after[scope] for scope in before}, before)
            self.assertEqual(after[('s_v_web_id', '.dola.com', '/')], 'fixture-fp')
            self.assertEqual(after[('has_biz_token', '.dola.com', '/')], 'false')

    def test_empty_duplicate_entries_do_not_prevent_missing_defaults(self):
        with requests.Session() as session:
            session.cookies.set('s_v_web_id', '', domain='.dola.com', path='/')
            session.cookies.set('s_v_web_id', '', domain='www.dola.com', path='/chat')
            try:
                worker.protocol._ensure_spa_cookies(session, {'fp': 'fixture-fp'}, 'DE')
            except requests.cookies.CookieConflictError:
                self.fail('Empty browser cookies must still allow defaults to be initialized')
            self.assertEqual(session.cookies.get('s_v_web_id', domain='.dola.com', path='/'), 'fixture-fp')
            self.assertEqual(session.cookies.get('s_v_web_id', domain='www.dola.com', path='/chat'), '')

    def test_http_prepare_reaches_network_with_browser_cookies_intact(self):
        class NetworkBoundaryReached(Exception):
            pass

        observed = []
        cookies = browser_cookies()
        original = json.dumps(cookies)

        def stop_before_network(session, request, **kwargs):
            observed.append((request.method, request.url,
                             {(c.name, c.domain, c.path): c.value for c in session.cookies},
                             request.headers.get('Cookie', '')))
            raise NetworkBoundaryReached()

        with patch.object(worker, 'load_account_cookies', return_value=cookies), \
             patch.object(worker.config, 'proxy_url_for', return_value=''), \
             patch.object(worker.config, 'account_proxy_raw', return_value=('', 'none')), \
             patch.object(requests.Session, 'send', stop_before_network):
            with self.assertRaisesRegex(BrowserSetupError, '初始化 HTTP 会话失败：NetworkBoundaryReached'):
                worker.prepare('fixture', '完整原提示词', 'seedance-2.5', 30, None, None)

        self.assertEqual(len(observed), 1)
        method, url, sent_cookies, header = observed[0]
        self.assertEqual((method, url), ('GET', 'https://www.dola.com/chat/create-image'))
        self.assertEqual(sent_cookies[('flow_user_country', '.www.dola.com', '/')], 'DE')
        self.assertEqual(sent_cookies[('i18next', 'www.dola.com', '/chat')], 'ko')
        self.assertIn('sessionid=fixture-login', header)
        self.assertEqual(json.dumps(cookies), original, 'Preparing a request must not rewrite saved cookies')


if __name__ == '__main__':
    unittest.main()
