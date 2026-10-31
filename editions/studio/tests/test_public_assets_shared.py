"""Public script bytes may survive account browsers; sessions and tasks may not."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import public_assets
from public_assets import PublicAssets, LANGUAGE_PATH

BASE = 'https://sf-flow-web-cdn.ciciai.com/obj/ocean-flow-web-sg/dola_web/static/'
SCRIPT = BASE + 'js/panel.1a2b3c4d.js'


class Response:
    status = 200
    def __init__(self, body=b'panel bytes', headers=None):
        self.body = body
        self.headers = headers or {'Cache-Control': 'public, max-age=60'}
        self.content = self
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    async def iter_chunked(self, _): yield self.body


class Client:
    def __init__(self, response): self.response, self.calls, self.closed = response, 0, False
    def get(self, *_, **__):
        self.calls += 1
        return self.response
    async def close(self): self.closed = True


class Context:
    async def unroute(self, *_): pass


class Route:
    def __init__(self, url=SCRIPT):
        self.request = SimpleNamespace(url=url, method='GET')
        self.fulfilled, self.fallbacks = [], 0
    async def fulfill(self, **kwargs): self.fulfilled.append(kwargs)
    async def fallback(self): self.fallbacks += 1


class SharedRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cache_patch = patch.object(public_assets, '_COMPLETED_ASSETS',
                                        public_assets.CompletedAssetCache())
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def accelerator(self, response=None):
        assets = PublicAssets(Context(), 'http://127.0.0.1:1234')
        assets.client = Client(response or Response())
        return assets

    async def test_two_contexts_reuse_completed_hashed_script_after_first_closes(self):
        first, second = self.accelerator(), self.accelerator()
        routes = [Route(BASE + 'js/two-context.0011aabb.js') for _ in range(2)]
        await first.route(routes[0])
        await first.__aexit__()
        await second.route(routes[1])
        self.assertEqual(second.client.calls, 0, 'Closing one browser must preserve verified public script bytes')
        self.assertEqual(routes[1].fulfilled[0]['body'], b'panel bytes')
        self.assertFalse(second.client.closed)

    async def test_immutable_css_shared_but_language_private_no_store_and_unversioned_are_not(self):
        for index, (url, headers, shared) in enumerate([
            (BASE + 'css/immutable.css', {'Cache-Control':'public, immutable, max-age=60'}, True),
            (BASE + 'js/plain.js', {'Cache-Control':'public, max-age=60'}, False),
            (SCRIPT, {'Cache-Control':'public, no-store, max-age=60'}, False),
            (SCRIPT, {'Cache-Control':'public, no-cache, max-age=60'}, False),
            (SCRIPT, {'Cache-Control':'private, immutable, max-age=60'}, False),
            (SCRIPT, {'Cache-Control':'public, immutable, max-age=60', 'Set-Cookie':'SECRET'}, False),
            (SCRIPT, {'Cache-Control':'public, immutable, max-age=60', 'Vary':'Cookie'}, False),
            ('https://starling-oversea.byteoversea.com'+LANGUAGE_PATH+'?lang=de-DE',
             {'Cache-Control':'public, immutable, max-age=60'}, False),
            ('https://starling-oversea.byteoversea.com'+LANGUAGE_PATH+'?lang=de%2Cen&version=1',
             {'Cache-Control':'public, immutable, max-age=60'}, False),
        ]):
            with self.subTest(index=index):
                # Unique URLs avoid a prior valid cached response hiding a private fixture.
                if url == SCRIPT: url = BASE + f'js/policy{index}.0011ccdd.js'
                a, b = self.accelerator(Response(headers=headers)), self.accelerator(Response(headers=headers))
                await a.route(Route(url)); await b.route(Route(url))
                self.assertEqual(b.client.calls, 0 if shared else 1)

    async def test_shared_response_expires_including_age_header(self):
        now = [100.0]
        with patch.object(public_assets, '_COMPLETED_ASSETS',
                          public_assets.CompletedAssetCache(clock=lambda:now[0])):
            a = self.accelerator(Response(headers={'Cache-Control':'public, max-age=60','Age':'55'}))
            url = BASE + 'js/expire.abcdef01.js'
            await a.route(Route(url))
            now[0] += 6
            b = self.accelerator()
            await b.route(Route(url))
            self.assertEqual(b.client.calls, 1)

    async def test_same_context_respects_expiry_and_shared_ttl_is_capped(self):
        now = [100.0]
        cache = public_assets.CompletedAssetCache(clock=lambda:now[0])
        with patch.object(public_assets, '_COMPLETED_ASSETS', cache):
            a = self.accelerator(Response(headers={'Cache-Control':'public, max-age=999999'}))
            url = BASE + 'js/ttl.abcdef23.js'
            await a.route(Route(url)); await a.route(Route(url))
            self.assertEqual(a.client.calls, 1)
            now[0] += 3601
            await a.route(Route(url))
            self.assertEqual(a.client.calls, 2)

    async def test_unversioned_bytes_are_scoped_and_removed_when_owner_closes(self):
        a, b = self.accelerator(), self.accelerator()
        url = BASE + 'js/unversioned.js'
        await a.route(Route(url)); await a.route(Route(url))
        self.assertEqual(a.client.calls, 1)
        self.assertGreater(a.completed.size, 0)
        await a.__aexit__()
        self.assertEqual(a.completed.size, 0)
        await b.route(Route(url))
        self.assertEqual(b.client.calls, 1)

    async def test_signed_and_account_urls_never_touch_download_or_cache(self):
        for url in [SCRIPT+'?signature=PRIVATE',
                    'https://www.dola.com/chat/completion',
                    'https://sf-flow-web-cdn.ciciai.com/login.js']:
            with self.subTest(url=url):
                a, b = self.accelerator(), self.accelerator()
                routes = [Route(url), Route(url)]
                await a.route(routes[0]); await b.route(routes[1])
                self.assertEqual(a.client.calls + b.client.calls, 0)
                self.assertTrue(all(r.fallbacks == 1 for r in routes))
                self.assertEqual(a.completed.size, 0)

    async def test_cache_has_a_total_capacity_limit(self):
        cache = public_assets.CompletedAssetCache(max_bytes=15)
        with patch.object(public_assets, '_COMPLETED_ASSETS', cache):
            a = self.accelerator(Response(b'1234567890'))
            first = BASE+'js/first.aaaabbbb.js'; second = BASE+'js/second.ccccdddd.js'
            await a.route(Route(first)); await a.route(Route(second))
            self.assertLessEqual(cache.size, 15)
            b = self.accelerator()
            await b.route(Route(second)); self.assertEqual(b.client.calls, 0)
            await b.route(Route(first)); self.assertEqual(b.client.calls, 1)

    async def test_one_context_close_does_not_cancel_another_context_download(self):
        started, release = asyncio.Event(), asyncio.Event()
        class SlowResponse(Response):
            async def iter_chunked(self, _):
                started.set(); await release.wait(); yield self.body
        a, b = self.accelerator(), self.accelerator(SlowResponse())
        route = Route(BASE + 'js/pending.0011eeff.js')
        pending = asyncio.create_task(b.route(route))
        await started.wait()
        await a.__aexit__(); release.set(); await pending
        self.assertEqual(len(route.fulfilled), 1)


if __name__ == '__main__': unittest.main()
