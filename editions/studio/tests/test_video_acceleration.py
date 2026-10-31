import ast
import asyncio
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
import aiohttp
from public_assets import PublicAssets, LANGUAGE_PATH


class AccelerationWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_context_accelerates_language_pack_and_closes_connections(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'browser.py').read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='install_cdn_accelerator')
        namespace=dict(aiohttp=aiohttp,asyncio=asyncio,PublicAssets=PublicAssets,
            config=SimpleNamespace(CDN_LOCAL_PROXY='http://127.0.0.1:9',CDN_LOCAL_TIMEOUT=45, ISP_ONLY=False),
            _CDN_HOST_PATTERN=re.compile(r'^https://[a-z0-9-]*cdn[a-z0-9-]*\.ciciai\.com/',re.I))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'video-acceleration','exec'),namespace)
        class Context:
            handlers=[]
            closer=None
            async def route(self,pattern,handler): self.handlers.append((pattern,handler))
            async def unroute(self,*args): pass
            def on(self,event,handler): self.closer=handler
        context=Context()
        accelerator=await namespace['install_cdn_accelerator'](context)
        try:
            language='https://starling-oversea.byteoversea.com'+LANGUAGE_PATH+'?lang=de-DE'
            self.assertTrue(any(pattern.match(language) for pattern,_ in context.handlers),
                            'The video browser must use the fast route for its public language pack')
            self.assertIsNotNone(accelerator)
            self.assertEqual(accelerator.client.timeout.total, 45,
                             'Video browsers must honor the configured resource timeout')
            self.assertIsNotNone(context.closer)
            await context.closer(context)
            self.assertTrue(accelerator.client.closed)
        finally:
            if accelerator and not accelerator.client.closed:
                await accelerator.__aexit__()


if __name__=='__main__': unittest.main()
