import unittest
from types import SimpleNamespace
from public_assets import eligible,PublicAssets


class AssetScopeTests(unittest.TestCase):
    def test_current_multilanguage_version_request_uses_public_route(self):
        url = ('https://starling-oversea.byteoversea.com/check_and_get_text/'
               '00d51bf0012811ee97fd5fdb0b0365a9/normal/web?lang=de%2Cen&version=1')
        self.assertTrue(eligible('GET', url))
        for extra in ('token=PRIVATE', 'sessionid=PRIVATE', 'signature=PRIVATE'):
            with self.subTest(extra=extra):
                self.assertFalse(eligible('GET', url + '&' + extra))

    def test_public_bundles_and_language_pack_are_allowed(self):
        self.assertTrue(eligible('GET','https://sf-flow-web-cdn.ciciai.com/obj/ocean-flow-web-sg/dola_web/static/js/chat.123.js'))
        self.assertTrue(eligible('GET','https://starling-oversea.byteoversea.com/check_and_get_text/00d51bf0012811ee97fd5fdb0b0365a9/normal/web?lang=zh-CN'))

    def test_accounts_auth_signed_urls_and_nonpublic_methods_keep_isp(self):
        for method,url in [
            ('GET','https://www.dola.com/passport/web/get_qrcode/'),
            ('GET','https://accounts.google.com/login'),
            ('POST','https://sf-flow-web-cdn.ciciai.com/obj/ocean-flow-web-sg/dola_web/static/js/a.js'),
            ('GET','https://sf-flow-web-cdn.ciciai.com.evil.test/obj/ocean-flow-web-sg/dola_web/static/js/a.js'),
            ('GET','https://sf-flow-web-cdn.ciciai.com/login.js'),
            ('GET','https://sf-flow-web-cdn.ciciai.com/obj/ocean-flow-web-sg/dola_web/static/js/a.js?token=secret'),
            ('GET','https://starling-oversea.byteoversea.com/check_and_get_text/00d51bf0012811ee97fd5fdb0b0365a9/normal/web?lang=zh-CN&sessionid=secret'),
        ]:
            with self.subTest(url=url):
                self.assertFalse(eligible(method,url))


class NoStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_language_no_store_is_accelerated_without_retaining_or_forwarding_credentials(self):
        calls=[]
        fulfilled=[]
        class Content:
            async def iter_chunked(self,_):
                yield b'{"public":"translation"}'
        class Response:
            status=200
            headers={'Cache-Control':'max-age=0, no-cache, no-store','Access-Control-Allow-Origin':'*'}
            content=Content()
            async def __aenter__(self): return self
            async def __aexit__(self,*_): pass
        class Client:
            def get(self,url,**kwargs):
                calls.append(kwargs)
                return Response()
        class Route:
            request=SimpleNamespace(method='GET',url='https://starling-oversea.byteoversea.com/check_and_get_text/00d51bf0012811ee97fd5fdb0b0365a9/normal/web?lang=de%2Cen&version=1',headers={'Cookie':'PRIVATE','Authorization':'PRIVATE'})
            async def fulfill(self,**kwargs): fulfilled.append(kwargs)
            async def fallback(self): self.failed=True
        accelerator=PublicAssets(None,'http://127.0.0.1:1234')
        accelerator.client=Client()
        await accelerator.route(Route())
        await accelerator.route(Route())
        self.assertEqual(len(fulfilled),2)
        self.assertEqual(len(calls),2)
        self.assertFalse(accelerator.cached)
        self.assertNotIn('PRIVATE',repr(calls))


if __name__=='__main__':
    unittest.main()
