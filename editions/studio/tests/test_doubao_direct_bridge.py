import asyncio
import os
import unittest
from unittest.mock import patch

from providers.doubao.direct_bridge import DirectBridge


class DirectBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def exchange(self, bridge, destination):
        port = bridge.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(f'CONNECT {destination} HTTP/1.1\r\nHost: {destination}\r\n\r\n'.encode())
        await writer.drain()
        response = await reader.readuntil(b'\r\n\r\n')
        return reader, writer, response

    async def test_connect_preserves_tls_bytes_and_ignores_proxy_environment(self):
        received = []
        async def origin(reader, writer):
            received.append(await reader.read(100))
            writer.write(b'unchanged-server-tls')
            await writer.drain()
            writer.close()
        origin_server = await asyncio.start_server(origin, '127.0.0.1', 0)
        real_open = asyncio.open_connection
        connections = []
        async def connect(host, port, **kwargs):
            connections.append((host, port))
            self.assertEqual((host, port), ('93.184.216.34', 443))
            return await real_open('127.0.0.1', origin_server.sockets[0].getsockname()[1])
        try:
            async with DirectBridge() as bridge:
                with patch.object(bridge, '_resolve', return_value=['93.184.216.34']), \
                     patch.dict(os.environ, HTTPS_PROXY='http://127.0.0.1:1', ALL_PROXY='http://127.0.0.1:1'):
                    port = bridge.server.sockets[0].getsockname()[1]
                    reader, writer = await real_open('127.0.0.1', port)
                    with patch('providers.doubao.direct_bridge.asyncio.open_connection', side_effect=connect):
                        writer.write(b'CONNECT www.doubao.com:443 HTTP/1.1\r\nHost: www.doubao.com:443\r\n\r\n')
                        await writer.drain()
                        self.assertIn(b'200', await reader.readuntil(b'\r\n\r\n'))
                        writer.write(b'unchanged-client-tls')
                        await writer.drain()
                        self.assertEqual(await reader.read(100), b'unchanged-server-tls')
                    writer.close()
                    await writer.wait_closed()
                    self.assertEqual(received, [b'unchanged-client-tls'])
                    self.assertEqual(connections, [('93.184.216.34', 443)])
        finally:
            origin_server.close()
            await origin_server.wait_closed()

    async def test_unauthorized_host_and_arbitrary_port_are_rejected(self):
        async with DirectBridge() as bridge:
            for destination in ('127.0.0.1:443', 'www.doubao.com:22', 'unapproved.example:443'):
                with patch.object(bridge, '_resolve', side_effect=AssertionError('Must reject before DNS')):
                    reader, writer, response = await self.exchange(bridge, destination)
                    self.assertIn(b'502', response)
                    writer.close()
                    await writer.wait_closed()

    async def test_dns_private_or_mixed_answers_are_rejected(self):
        async with DirectBridge() as bridge:
            for addresses in (['127.0.0.1'], ['93.184.216.34', '::1']):
                answers = [(2, 1, 6, '', (ip, 443)) for ip in addresses]
                with patch.object(asyncio.get_running_loop(), 'getaddrinfo', return_value=answers):
                    with self.assertRaises(ValueError):
                        await bridge._resolve('www.doubao.com')
