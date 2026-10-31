"""Replay a transient CDN failure through the browser's real resource route."""
import asyncio
from types import SimpleNamespace
import unittest

import aiohttp

from public_assets import PublicAssets


URL = 'https://sf-flow-web-cdn.ciciai.com/obj/ocean-flow-web-sg/dola_web/static/js/async/panel.js'


class Response:
    def __init__(self, status=200, headers=None):
        self.status = status
        self.headers = headers or {'Cache-Control': 'public, max-age=3600'}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def iter_chunked(self, _):
        yield b'window.videoPanelReady=true;'


class Client:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class Route:
    def __init__(self, url=URL, method='GET'):
        self.request = SimpleNamespace(url=url, method=method,
                                       headers={'Cookie': 'PRIVATE', 'Authorization': 'PRIVATE'})
        self.fulfilled = []
        self.fallbacks = 0

    async def fulfill(self, **kwargs):
        self.fulfilled.append(kwargs)

    async def fallback(self):
        self.fallbacks += 1


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def accelerator(self, *outcomes):
        assets = PublicAssets(None, 'http://127.0.0.1:1234')
        assets.client = Client(*outcomes)
        return assets

    async def test_transient_connection_failure_retries_before_isp_fallback(self):
        for failure in (aiohttp.ClientOSError(10054, 'Connection reset'),
                        asyncio.TimeoutError(), aiohttp.ClientPayloadError('Incomplete chunk')):
            with self.subTest(failure=type(failure).__name__):
                assets = self.accelerator(failure, Response())
                route = Route()
                await assets.route(route)
                self.assertEqual(len(route.fulfilled), 1, 'Transient CDN loss must not drop the panel script')
                self.assertEqual(route.fallbacks, 0)
                self.assertEqual(len(assets.client.calls), 2)
                self.assertNotIn('PRIVATE', repr(assets.client.calls))
                self.assertTrue(all(call[1]['proxy'] == assets.proxy_url for call in assets.client.calls))

    async def test_transient_server_errors_retry_once(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                assets = self.accelerator(Response(status), Response())
                route = Route()
                await assets.route(route)
                self.assertEqual(len(route.fulfilled), 1)
                self.assertEqual(route.fallbacks, 0)
                self.assertEqual(len(assets.client.calls), 2)

    async def test_permanent_or_private_responses_do_not_retry(self):
        for response in (Response(403), Response(404), Response(501),
                         Response(200, {'Set-Cookie': 'PRIVATE'}),
                         Response(503, {'Cache-Control': 'private'})):
            with self.subTest(status=response.status, headers=list(response.headers)):
                assets = self.accelerator(response)
                route = Route()
                await assets.route(route)
                self.assertEqual(route.fallbacks, 1)
                self.assertFalse(route.fulfilled)
                self.assertEqual(len(assets.client.calls), 1)

    async def test_retry_exhaustion_falls_back_once_and_evicts_failed_cache(self):
        for outcomes in ((aiohttp.ClientOSError(10054, 'Reset'), aiohttp.ClientOSError(10054, 'Reset')),
                         (Response(503), Response(503))):
            assets = self.accelerator(*outcomes)
            route = Route()
            await assets.route(route)
            self.assertEqual(len(assets.client.calls), 2)
            self.assertEqual(route.fallbacks, 1)
            self.assertFalse(assets.cached)

    async def test_concurrent_requests_share_one_retry_and_success_cache(self):
        assets = self.accelerator(aiohttp.ClientOSError(10054, 'Reset'), Response())
        routes = [Route() for _ in range(3)]
        await asyncio.gather(*(assets.route(route) for route in routes[:2]))
        await assets.route(routes[2])
        self.assertEqual(len(assets.client.calls), 2)
        self.assertTrue(all(len(route.fulfilled) == 1 and route.fallbacks == 0 for route in routes))

    async def test_business_requests_never_enter_retry_or_local_proxy(self):
        for url, method in (('https://www.dola.com/chat/completion', 'POST'),
                            ('https://www.dola.com/im/chain/single', 'POST')):
            assets = self.accelerator()
            route = Route(url, method)
            await assets.route(route)
            self.assertEqual(route.fallbacks, 1)
            self.assertEqual(assets.client.calls, [])


if __name__ == '__main__':
    unittest.main()
