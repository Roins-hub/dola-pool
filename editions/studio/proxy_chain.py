"""A context-owned, authenticated transport to a pinned ISP.

CONNECT streams relay TLS bytes unchanged. Credentials are applied before each
upstream handshake, and failures never fall back to another ISP or direct access.
"""
import asyncio
import base64
from contextlib import suppress
from urllib.parse import urlsplit


class ProxyRefusedError(Exception):
    pass


class ProxyChainBridge:
    def __init__(self, isp, first_hop=None, timeout=8, attempts=4):
        self.isp, self.first_hop = dict(isp), dict(first_hop) if first_hop else None
        self.timeout = timeout
        self.attempts = attempts
        self.server = None
        self.tasks = set()
        self.browser_proxy = {}
        self.allowed_hosts = {'www.dola.com', 'dola.com'}
        self.stats = {'attempted':0, 'opened':0, 'resets':0}
        for proxy in filter(None, (self.isp, self.first_hop)):
            ep = urlsplit(proxy.get('server', ''))
            if ep.scheme != 'http' or not ep.hostname or not ep.port:
                raise ValueError('Proxy chaining requires explicit HTTP proxy endpoints.')

    @property
    def running(self):
        return bool(self.server and self.server.is_serving())

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._client, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.browser_proxy = {'server': f'http://127.0.0.1:{port}'}
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
        # Page traffic is authorized before CONNECT; browser updates/sync are not.
        host = urlsplit(route.request.url).hostname
        if host:
            self.allowed_hosts.add(host)
        await route.fallback()

    def _auth(self, proxy):
        if not proxy.get('username'):
            return ''
        value = (proxy['username'] + ':' + proxy.get('password', '')).encode('utf-8')
        return 'Proxy-Authorization: Basic ' + base64.b64encode(value).decode('ascii') + '\r\n'

    async def _headers(self, reader):
        return await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), self.timeout)

    async def _connect(self, reader, writer, target, proxy):
        wire = f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n' + self._auth(proxy) + '\r\n'
        writer.write(wire.encode('latin1'))
        await writer.drain()
        response = await self._headers(reader)
        if response.split(b'\r\n', 1)[0].split()[1:2] != [b'200']:
            raise ProxyRefusedError('Configured proxy refused the tunnel.')

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
            head = (await self._headers(reader)).decode('latin1')
            lines = head.split('\r\n')
            method, target, version = lines[0].split()
            if method == 'CONNECT':
                ep = urlsplit('http://' + target)
                if not ep.hostname or not ep.port or ep.username or ep.path:
                    raise ValueError('Invalid CONNECT target.')
            elif urlsplit(target).scheme != 'http':
                raise ValueError('Expected an absolute HTTP request.')
            host = ep.hostname if method == 'CONNECT' else urlsplit(target).hostname
            if host not in self.allowed_hosts:
                raise ValueError('Browser background destination is not authorized.')
            for attempt in range(self.attempts):
                try:
                    self.stats['attempted'] += 1
                    endpoint = urlsplit((self.first_hop or self.isp)['server'])
                    remote, upstream = await asyncio.wait_for(
                        asyncio.open_connection(endpoint.hostname, endpoint.port), self.timeout)
                    if self.first_hop:
                        isp = urlsplit(self.isp['server'])
                        await self._connect(remote, upstream, f'{isp.hostname}:{isp.port}', self.first_hop)
                    if method == 'CONNECT':
                        await self._connect(remote, upstream, target, self.isp)
                    self.stats['opened'] += 1
                    break
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    self.stats['resets'] += 1
                    if upstream:
                        upstream.close()
                        with suppress(Exception):
                            await asyncio.wait_for(upstream.wait_closed(), 1)
                        upstream = None
                    if attempt + 1 == self.attempts:
                        raise
                    await asyncio.sleep(0.4 * (attempt + 1))
            if method == 'CONNECT':
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                await writer.drain()
            else:
                # One plain-HTTP request per connection avoids sharing auth state.
                headers = [line for line in lines[1:] if line and line.partition(':')[0].lower()
                           not in ('proxy-authorization', 'proxy-connection', 'connection')]
                wire = '\r\n'.join([lines[0], *headers]) + '\r\n'
                wire += self._auth(self.isp) + 'Connection: close\r\n\r\n'
                upstream.write(wire.encode('latin1'))
                await upstream.drain()
            started = True
            relays = [asyncio.create_task(self._pump(reader, upstream)),
                      asyncio.create_task(self._pump(remote, writer))]
            await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
        except (ConnectionError, OSError, ValueError, ProxyRefusedError, asyncio.TimeoutError,
                asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            if not started:
                with suppress(ConnectionError, OSError):
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
