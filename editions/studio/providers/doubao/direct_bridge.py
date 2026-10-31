"""Context-owned loopback CONNECT transport with direct public-IP sockets.

Chromium receives an explicit proxy endpoint so Windows system proxy settings
cannot select another route. TLS remains end to end between Chromium and the
official server; this bridge neither decrypts TLS nor reads proxy environment.
"""
import asyncio
from contextlib import suppress
import ipaddress
import socket
from urllib.parse import urlsplit


class DirectBridge:
    def __init__(self, timeout=15):
        self.timeout = timeout
        self.server = None
        self.tasks = set()
        self.browser_proxy = {}
        self.allowed_hosts = {'www.doubao.com', 'doubao.com', 'www.cloudflare.com'}
        self.stats = {'attempted': 0, 'opened': 0, 'resets': 0}

    @property
    def running(self):
        return bool(self.server and self.server.is_serving())

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._client, '127.0.0.1', 0)
        self.browser_proxy = {'server': f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'}
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def authorize_request(self, route):
        parsed = urlsplit(route.request.url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.port not in (None, 443):
            await route.abort('blockedbyclient')
            return
        self.allowed_hosts.add(parsed.hostname)
        await route.fallback()

    async def _resolve(self, host):
        answers = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM), self.timeout)
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError('Direct destination must resolve only to public addresses.')
        return addresses

    async def _pump(self, reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def _client(self, reader, writer):
        current = asyncio.current_task()
        self.tasks.add(current)
        upstream = None
        relays = []
        started = False
        try:
            head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), self.timeout)
            method, target, _ = head.split(b'\r\n', 1)[0].decode('ascii').split()
            endpoint = urlsplit('https://' + target)
            if (method != 'CONNECT' or endpoint.hostname not in self.allowed_hosts or endpoint.port != 443
                    or endpoint.username is not None or endpoint.password is not None
                    or endpoint.path or endpoint.query or endpoint.fragment):
                raise ValueError('Unauthorized direct CONNECT destination.')
            addresses = await self._resolve(endpoint.hostname)
            for address in addresses:
                self.stats['attempted'] += 1
                try:
                    remote, upstream = await asyncio.wait_for(asyncio.open_connection(address, 443), self.timeout)
                    self.stats['opened'] += 1
                    break
                except (OSError, asyncio.TimeoutError):
                    self.stats['resets'] += 1
            if upstream is None:
                raise ConnectionError('Direct target unavailable.')
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await writer.drain()
            started = True
            relays = [asyncio.create_task(self._pump(reader, upstream)),
                      asyncio.create_task(self._pump(remote, writer))]
            await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError):
            if not started:
                with suppress(OSError):
                    writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    await writer.drain()
        finally:
            for task in relays:
                task.cancel()
            await asyncio.gather(*relays, return_exceptions=True)
            for stream in (upstream, writer):
                if stream:
                    stream.close()
                    with suppress(Exception):
                        await asyncio.wait_for(stream.wait_closed(), 2)
            self.tasks.discard(current)
