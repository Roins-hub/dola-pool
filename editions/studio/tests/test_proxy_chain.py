import asyncio
import base64
import unittest

from proxy_chain import ProxyChainBridge


class ProxyChainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = []
        self.peers = set()
        self.server = await asyncio.start_server(self.front, '127.0.0.1', 0)
        self.first = {'server': f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'}

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.peers):
            writer.close()
        await asyncio.sleep(0)

    async def front(self, reader, writer):
        self.peers.add(writer)
        try:
            hop = await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await writer.drain()
            request = await reader.readuntil(b'\r\n\r\n')
            self.connections.append((hop, request))
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await writer.drain()
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            self.peers.discard(writer)

    async def open_tunnel(self, bridge, host='www.dola.com'):
        from urllib.parse import urlsplit
        endpoint = urlsplit(bridge.browser_proxy['server'])
        reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
        writer.write(f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\nProxy-Authorization: Basic browser-secret\r\n\r\n'.encode())
        await writer.drain()
        reply = await reader.readuntil(b'\r\n\r\n')
        self.assertTrue(reply.startswith(b'HTTP/1.1 200'))
        return reader, writer

    async def test_two_hops_stay_pinned_and_tls_bytes_are_not_rewritten(self):
        proxy = {'server': 'http://192.0.2.11:11000', 'username': 'fixture', 'password': 'private'}
        async with ProxyChainBridge(proxy, self.first) as bridge:
            reader, writer = await self.open_tunnel(bridge)
            packet = b'\x16\x03\x01opaque-tls-record'
            writer.write(packet)
            await writer.drain()
            self.assertEqual(await reader.readexactly(len(packet)), packet)
            hop, request = self.connections[0]
            self.assertTrue(hop.startswith(b'CONNECT 192.0.2.11:11000 '))
            self.assertNotIn(b'private', hop)
            self.assertTrue(request.startswith(b'CONNECT www.dola.com:443 HTTP/1.1\r\nHost: www.dola.com:443\r\n'))
            self.assertIn(b'Proxy-Authorization: Basic ' + base64.b64encode(b'fixture:private'), request)
            self.assertNotIn(b'browser-secret', request)
            writer.close()

    async def test_bridges_do_not_share_streams_and_close_releases_listeners(self):
        first = ProxyChainBridge({'server':'http://192.0.2.11:11000'}, self.first)
        second = ProxyChainBridge({'server':'http://192.0.2.12:11000'}, self.first)
        async with first, second:
            r1, w1 = await self.open_tunnel(first)
            r2, w2 = await self.open_tunnel(second)
            self.assertNotEqual(first.browser_proxy['server'], second.browser_proxy['server'])
            w1.write(b'one'); w2.write(b'two')
            await asyncio.gather(w1.drain(), w2.drain())
            self.assertEqual(await r1.readexactly(3), b'one')
            self.assertEqual(await r2.readexactly(3), b'two')
            await first.close()
            w2.write(b'still-open'); await w2.drain()
            self.assertEqual(await r2.readexactly(10), b'still-open')
            self.assertEqual(await r1.read(), b'')
            w1.close(); w2.close()
        self.assertFalse(first.running)
        self.assertFalse(second.running)

    async def test_refused_first_hop_does_not_connect_directly_to_isp(self):
        async def deny(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(deny, '127.0.0.1', 0)
        try:
            async with ProxyChainBridge({'server':'http://127.0.0.1:1'},
                    {'server':f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'}) as bridge:
                from urllib.parse import urlsplit
                ep = urlsplit(bridge.browser_proxy['server'])
                reader, writer = await asyncio.open_connection(ep.hostname, ep.port)
                writer.write(b'CONNECT www.dola.com:443 HTTP/1.1\r\n\r\n'); await writer.drain()
                self.assertTrue((await reader.readuntil(b'\r\n\r\n')).startswith(b'HTTP/1.1 502'))
                writer.close()
        finally:
            server.close(); await server.wait_closed()

    async def test_direct_isp_authentication_retries_only_before_tunnel_exists(self):
        requests = []
        async def isp(reader, writer):
            try:
                requests.append(await reader.readuntil(b'\r\n\r\n'))
                if len(requests) == 1:
                    return
                writer.write(b'HTTP/1.1 200 OK\r\n\r\n'); await writer.drain()
                payload = await reader.readexactly(3)
                writer.write(payload); await writer.drain()
            finally:
                writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(isp, '127.0.0.1', 0)
        try:
            async with ProxyChainBridge({'server':f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}',
                                         'username':'isp','password':'fixture'}) as bridge:
                reader, writer = await self.open_tunnel(bridge)
                writer.write(b'TLS'); await writer.drain()
                self.assertEqual(await reader.readexactly(3), b'TLS')
                self.assertEqual(await reader.read(), b'')
                self.assertEqual(len(requests), 2)
                self.assertNotIn(b'browser-secret', b''.join(requests))
                writer.close()
        finally:
            server.close(); await server.wait_closed()

    async def test_background_connection_never_reaches_isp(self):
        async with ProxyChainBridge({'server':'http://127.0.0.1:1'}) as bridge:
            from urllib.parse import urlsplit
            ep = urlsplit(bridge.browser_proxy['server'])
            reader, writer = await asyncio.open_connection(ep.hostname, ep.port)
            writer.write(b'CONNECT update.googleapis.com:443 HTTP/1.1\r\n\r\n'); await writer.drain()
            self.assertTrue((await reader.readuntil(b'\r\n\r\n')).startswith(b'HTTP/1.1 502'))
            self.assertEqual(bridge.stats['attempted'], 0)
            writer.close()
