"""Explicit Doubao direct transport ignores proxy environment and system fallback."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp.abc import AbstractResolver
from patchright.async_api import async_playwright
from providers.doubao.runtime import BrowserSession
import providers.doubao.runtime as runtime


class Resolver(AbstractResolver):
    def __init__(self, port=None): self.port = port
    async def resolve(self, host, port=0, family=0):
        return [{'hostname': host, 'host': '127.0.0.1', 'port': self.port or port, 'family': 2,
                 'proto': 0, 'flags': 0}]
    async def close(self): pass


class DirectRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proxy_hits = []
        async def trap(reader, writer):
            self.proxy_hits.append(await reader.read(4096))
            writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
            writer.close()
        self.trap = await asyncio.start_server(trap, '127.0.0.1', 0)
        self.addAsyncCleanup(self.close_server, self.trap)
        proxy = 'http://127.0.0.1:' + str(self.trap.sockets[0].getsockname()[1])
        self.environment = {key: proxy for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                                   'http_proxy', 'https_proxy', 'all_proxy')}
        self.environment.update(NO_PROXY='', no_proxy='')

    async def close_server(self, server):
        server.close()
        await server.wait_closed()

    async def test_explicit_direct_chromium_disables_system_proxy_and_never_creates_bridge(self):
        launches = []
        async with async_playwright() as real:
            async def launch(folder, **kwargs):
                launches.append(kwargs)
                context = await real.chromium.launch_persistent_context(folder, **kwargs)
                await context.route('**/*', lambda route: route.fulfill(status=200,
                    content_type='text/html', body='<textarea></textarea><p>direct fixture</p>'))
                return context
            class Manager:
                async def start(self): return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
            provider = SimpleNamespace(root=self.root, scheduler=None)
            session = BrowserSession(provider, {'name': 'direct', 'account_id': 'direct', '_login': True,
                                                '_binding': {'server': 'direct'}}, login=True)
            with patch.dict(os.environ, self.environment), \
                 patch.object(runtime, 'async_playwright', return_value=Manager()), \
                 patch.object(runtime, 'ProxyChainBridge', side_effect=AssertionError('Direct must not create an ISP bridge')), \
                 patch.object(runtime.proxy_usage.usage, 'ensure_available', side_effect=AssertionError('Direct has no ISP quota')), \
                 patch.object(runtime.proxy_usage, 'attach_request_counter', side_effect=AssertionError('Direct has no ISP counter')):
                async with session:
                    self.assertIsNotNone(session.bridge)
                    self.assertNotIn('--no-proxy-server', launches[0]['args'])
                    self.assertTrue(launches[0]['proxy']['server'].startswith('http://127.0.0.1:'))
                    self.assertEqual(await session.page.locator('p').inner_text(), 'direct fixture')
            self.assertEqual(self.proxy_hits, [])

    async def test_direct_stream_uses_verified_https_without_proxy_environment(self):
        openssl = shutil.which('openssl') or 'C:/Program Files/Git/usr/bin/openssl.exe'
        if not Path(openssl).is_file(): self.skipTest('OpenSSL is required for a local verified TLS fixture')
        cert, key = self.root / 'cert.pem', self.root / 'key.pem'
        result = subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-keyout', str(key), '-out', str(cert), '-subj', '/CN=fixture-video.example',
            '-addext', 'subjectAltName=DNS:fixture-video.example'], capture_output=True)
        self.assertEqual(result.returncode, 0, 'Unable to create isolated TLS certificate')
        server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ssl.load_cert_chain(cert, key)
        client_ssl = ssl.create_default_context(cafile=str(cert))
        content = b'local direct download fixture'
        async def origin(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(content)).encode() + b'\r\nConnection: close\r\n\r\n' + content)
            await writer.drain()
            writer.close()
        origin_server = await asyncio.start_server(origin, '127.0.0.1', 0, ssl=server_ssl)
        self.addAsyncCleanup(self.close_server, origin_server)
        port = origin_server.sockets[0].getsockname()[1]
        original_session = aiohttp.ClientSession
        def local_transport(**kwargs):
            kwargs['connector'] = aiohttp.TCPConnector(ssl=client_ssl, resolver=Resolver())
            return original_session(**kwargs)
        session = BrowserSession(None, {'name': 'direct'})
        session.binding = {'server': 'direct'}
        session.context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
        target = self.root / 'result.bin'
        with patch.dict(os.environ, self.environment), \
             patch('media.awaitable_getaddrinfo', return_value=['93.184.216.34']), \
             patch.object(runtime.aiohttp, 'ClientSession', side_effect=local_transport), \
             patch.object(runtime.proxy_usage.usage, 'reserve', side_effect=AssertionError('Direct has no ISP quota')):
            await session.stream_to(f'https://fixture-video.example:{port}/video.mp4', target, 1024)
        self.assertEqual(target.read_bytes(), content)
        self.assertEqual(self.proxy_hits, [])

    async def test_direct_does_not_relax_private_material_url_rejection(self):
        session = BrowserSession(None, {'name': 'direct'})
        session.binding = {'server': 'direct'}
        from providers.base import ProviderError
        with self.assertRaises(ProviderError):
            await session.stream_to('https://127.0.0.1/private', self.root / 'bad', 1024)

    async def test_direct_verification_reads_same_route_cloudflare_ipv6_without_environment_proxy(self):
        openssl = shutil.which('openssl') or 'C:/Program Files/Git/usr/bin/openssl.exe'
        if not Path(openssl).is_file(): self.skipTest('OpenSSL is required for a local verified TLS fixture')
        cert, key = self.root / 'trace-cert.pem', self.root / 'trace-key.pem'
        result = subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-keyout', str(key), '-out', str(cert), '-subj', '/CN=www.cloudflare.com',
            '-addext', 'subjectAltName=DNS:www.cloudflare.com'], capture_output=True)
        self.assertEqual(result.returncode, 0)
        server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ssl.load_cert_chain(cert, key)
        client_ssl = ssl.create_default_context(cafile=str(cert))
        content = b'fl=fixture\nip=2409:8a20:6f0:1234::1\nloc=CN\n'
        paths = []
        async def origin(reader, writer):
            request = await reader.readuntil(b'\r\n\r\n')
            paths.append(request.split(b'\r\n', 1)[0])
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(content)).encode() + b'\r\nConnection: close\r\n\r\n' + content)
            await writer.drain()
            writer.close()
        origin_server = await asyncio.start_server(origin, '127.0.0.1', 0, ssl=server_ssl)
        self.addAsyncCleanup(self.close_server, origin_server)
        port = origin_server.sockets[0].getsockname()[1]
        original_session = aiohttp.ClientSession
        def local_transport(**kwargs):
            kwargs['connector'] = aiohttp.TCPConnector(ssl=client_ssl, resolver=Resolver(port))
            return original_session(**kwargs)
        class Empty:
            async def count(self): return 0
            def filter(self, **kwargs): return self
        class Page:
            def locator(self, *args): return Empty()
            def get_by_text(self, *args, **kwargs): return Empty()
            def get_by_role(self, *args, **kwargs): return Empty()
            async def evaluate(self, expression, arg=None, *, isolated_context=True): return []
        session = BrowserSession(None, {'name': 'direct'})
        session.binding = {'server': 'direct'}
        session.context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
        session.page = Page()
        with patch.dict(os.environ, self.environment), \
             patch.object(runtime.aiohttp, 'ClientSession', side_effect=local_transport):
            checks = await session.verify()
            self.assertTrue(checks['route_verified'])
            self.assertEqual(checks['route_mode'], 'direct')
            self.assertEqual(checks['egress'], '2409:8a20:6f0:1234::1')
            self.assertEqual(paths, [b'GET /cdn-cgi/trace HTTP/1.1'])
            self.assertEqual(self.proxy_hits, [])
            content = b'fl=fixture\nip=127.0.0.1\nloc=CN\n'
            self.assertFalse((await session.verify())['route_verified'])
            content = b'fl=fixture\nloc=CN\n'
            self.assertFalse((await session.verify())['route_verified'])


if __name__ == '__main__': unittest.main()
