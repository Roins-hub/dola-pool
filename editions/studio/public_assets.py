"""Narrow public-resource accelerator shared by login and video browsers."""
import asyncio
import atexit
from collections import OrderedDict
from contextlib import suppress
import re
import hashlib
import json
import sqlite3
from pathlib import Path
import time
from urllib.parse import urlsplit, parse_qsl
import aiohttp
import config
import proxy_usage

PATTERN = re.compile(r'^https://(?:sf-flow-web-cdn\.ciciai\.com|starling-oversea\.byteoversea\.com)/')
LANGUAGE_PATH = '/check_and_get_text/00d51bf0012811ee97fd5fdb0b0365a9/normal/web'


def eligible(method, url):
    parts = urlsplit(url)
    if method != 'GET' or parts.scheme != 'https' or parts.username or parts.password or parts.port not in (None,443):
        return False
    if parts.hostname == 'sf-flow-web-cdn.ciciai.com':
        return (not parts.query and parts.path.startswith('/obj/ocean-flow-web-sg/dola_web/static/')
                and bool(re.search(r'\.(js|css|woff2?|ttf)$', parts.path)))
    return (parts.hostname == 'starling-oversea.byteoversea.com' and parts.path == LANGUAGE_PATH
            # The current client requests lang=de,en together with a public
            # version marker. Rejecting version silently sends language packs
            # back to the slow ISP and leaves a partially translated composer.
            and all(key in {'lang', 'version'} for key,_ in parse_qsl(parts.query,keep_blank_values=True)))


class CompletedAssetCache:
    """Bounded process cache of public bytes, never clients or in-flight tasks."""
    def __init__(self, max_bytes=96*1024*1024, clock=None):
        self.max_bytes=max_bytes
        self.clock=clock or time.monotonic
        self.entries=OrderedDict()
        self.size=0

    def _remove(self, key):
        item=self.entries.pop(key,None)
        if item:
            self.size-=len(item[1])

    def _expire(self):
        now=self.clock()
        for key,item in list(self.entries.items()):
            if item[0]<=now:
                self._remove(key)

    def get(self, url, owner):
        self._expire()
        for key in ((None,url),(owner,url)):
            item=self.entries.get(key)
            if item:
                self.entries.move_to_end(key)
                return item[1],dict(item[2])
        return None

    def put(self, url, body, headers, owner):
        parts=urlsplit(url)
        # The language service is dynamic even when it advertises cacheability.
        if not eligible('GET',url) or parts.hostname!='sf-flow-web-cdn.ciciai.com':
            return
        values={k.lower():v for k,v in headers.items()}
        policy={part.strip().split('=',1)[0]:part.strip().partition('=')[2].strip('"')
                for part in values.get('cache-control','').lower().split(',')}
        if {'private','no-store','no-cache'} & policy.keys() or 'set-cookie' in values:
            return
        vary={v.strip().lower() for v in values.get('vary','').split(',') if v.strip()}
        if vary-{'accept-encoding','origin'}:
            return
        try:
            ttl=min(3600,max(0,int(policy['max-age'])-max(0,int(values.get('age','0')))))
        except (KeyError,ValueError):
            return
        if not ttl or len(body)>self.max_bytes:
            return
        versioned=bool(re.search(r'(?:[._-][a-f0-9]{8,})(?:[._-]|\.(?:js|css)$)',parts.path,re.I))
        shared=parts.path.endswith(('.js','.css')) and ('immutable' in policy or versioned)
        key=(None if shared else owner,url)
        self._expire()
        self._remove(key)
        while self.entries and self.size+len(body)>self.max_bytes:
            self._remove(next(iter(self.entries)))
        self.entries[key]=(self.clock()+ttl,bytes(body),dict(headers))
        self.size+=len(body)

    def discard_owner(self, owner):
        for key in list(self.entries):
            if key[0] is owner:
                self._remove(key)


class PersistentAssetCache(CompletedAssetCache):
    """Only shared public scripts/styles survive restart, with absolute expiry."""
    def __init__(self, path, max_bytes=256*1024*1024, clock=None):
        super().__init__(max_bytes=max_bytes, clock=clock or time.time)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS assets (url TEXT PRIMARY KEY, expires REAL, '
                        'body BLOB, headers TEXT, digest TEXT, touched REAL)')
        self._prune_disk()

    def _prune_disk(self):
        with self.db:
            self.db.execute('DELETE FROM assets WHERE expires<=?', (self.clock(),))
            total = self.db.execute('SELECT COALESCE(SUM(length(body)),0) FROM assets').fetchone()[0]
            for url, size in self.db.execute('SELECT url,length(body) FROM assets ORDER BY touched').fetchall():
                if total <= self.max_bytes:
                    break
                self.db.execute('DELETE FROM assets WHERE url=?', (url,))
                total -= size

    def get(self, url, owner):
        hit = super().get(url, owner)
        if hit:
            return hit
        try:
            row = self.db.execute('SELECT expires,body,headers,digest FROM assets WHERE url=?', (url,)).fetchone()
            if not row or row[0] <= self.clock():
                return None
            expires, body, encoded, digest = row
            if hashlib.sha256(body).hexdigest() != digest:
                return None
            headers = json.loads(encoded)
            # Re-apply today's allowlist/policy after restart (including future tightening).
            checker = CompletedAssetCache(max_bytes=self.max_bytes, clock=self.clock)
            checker.put(url, body, headers, owner)
            if (None, url) not in checker.entries:
                return None
            expires = min(expires, checker.entries[(None, url)][0])
            while self.entries and self.size + len(body) > self.max_bytes:
                self._remove(next(iter(self.entries)))
            self.entries[(None, url)] = (expires, body, headers)
            self.size += len(body)
            return body, dict(headers)
        except (sqlite3.Error, ValueError, TypeError):
            return None

    def put(self, url, body, headers, owner):
        super().put(url, body, headers, owner)
        item = self.entries.get((None, url))
        if not item:
            return
        try:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?)',
                    (url, item[0], item[1], json.dumps(item[2]), hashlib.sha256(item[1]).hexdigest(), self.clock()))
            self._prune_disk()
        except (sqlite3.Error, OSError):
            # Disk trouble may lose acceleration, never select another network route.
            pass

    def close(self):
        self.db.close()

    def preload(self):
        """Prepare still-fresh public bytes on startup, with no network requests."""
        rows = self.db.execute('SELECT url FROM assets WHERE expires>? ORDER BY touched DESC',
                               (self.clock(),)).fetchall()
        return sum(self.get(row[0], None) is not None for row in rows)


_COMPLETED_ASSETS=CompletedAssetCache()
_DISK_CACHE=None


def account_asset_cache():
    global _DISK_CACHE
    if _DISK_CACHE is None:
        try:
            _DISK_CACHE=PersistentAssetCache(Path(config.PUBLIC_ASSET_CACHE_DIR)/'assets.db',
                                           max_bytes=config.PUBLIC_ASSET_CACHE_MB*1024*1024)
            atexit.register(_DISK_CACHE.close)
        except (OSError, sqlite3.Error):
            return _COMPLETED_ASSETS
    return _DISK_CACHE


class PublicAssets:
    def __init__(self, context, proxy_url, timeout=45, *, isp_proxy=None):
        if not proxy_url:
            raise ValueError('No fast public-resource proxy configured.')
        self.context=context
        self.proxy_url=proxy_url
        self.timeout=timeout
        self.isp_proxy=isp_proxy
        self.cached={}
        self.stats={'served':0,'fallback':0,'cache_hits':0,'network_requests':0}
        self.completed=account_asset_cache() if isp_proxy else _COMPLETED_ASSETS
        self.owner=object()
        self.closed=False

    async def __aenter__(self):
        self.client=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout),
            connector=aiohttp.TCPConnector(limit=32),cookie_jar=aiohttp.DummyCookieJar(),trust_env=False)
        await self.context.route(PATTERN,self.route)
        return self

    async def __aexit__(self,*_):
        self.closed=True
        with suppress(Exception):
            await self.context.unroute(PATTERN,self.route)
        tasks=list(self.cached.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.cached.clear()
        self.completed.discard_owner(self.owner)
        await self.client.close()

    async def download(self,url):
        # Never forward browser cookies, Authorization, or other account headers.
        # A lost lazy-loaded script can replace the whole chat with an error page.
        # Retry the same public URL once before falling back to the account route.
        for attempt in range(2):
            try:
                if self.isp_proxy:
                    proxy_usage.usage.reserve(self.isp_proxy, 1)
                self.stats['network_requests']+=1
                async with self.client.get(url,proxy=self.proxy_url,allow_redirects=False,
                                           headers={'Origin':'https://www.dola.com'}) as response:
                    cache_control=response.headers.get('Cache-Control','').lower()
                    if 'Set-Cookie' in response.headers or 'private' in cache_control:
                        raise ValueError('Not a public cacheable resource.')
                    if response.status in (500, 502, 503, 504) and attempt == 0:
                        continue
                    if response.status != 200:
                        raise ValueError('Not a public cacheable resource.')
                    chunks=[]
                    size=0
                    async for chunk in response.content.iter_chunked(65536):
                        size+=len(chunk)
                        if size>8*1024*1024:
                            raise ValueError('Resource too large.')
                        chunks.append(chunk)
                    headers={k:v for k,v in response.headers.items() if k.lower() not in
                             ('content-encoding','content-length','transfer-encoding','connection','set-cookie','keep-alive','proxy-authenticate','trailer','upgrade')}
                    return b''.join(chunks),headers,('no-store' not in cache_control and 'no-cache' not in cache_control)
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, asyncio.TimeoutError):
                if attempt:
                    raise

    async def route(self,route):
        request=route.request
        if not eligible(request.method,request.url):
            await route.fallback()
            return
        task=None
        try:
            hit=self.completed.get(request.url,self.owner)
            if hit:
                body,headers=hit
                await route.fulfill(status=200,headers=headers,body=body)
                self.stats['served']+=1
                self.stats['cache_hits']+=1
                return
            if request.url not in self.cached:
                self.cached[request.url]=asyncio.create_task(self.download(request.url))
            task=self.cached[request.url]
            body,headers,cacheable=await task
            if cacheable and not self.closed:
                self.completed.put(request.url,body,headers,self.owner)
            await route.fulfill(status=200,headers=headers,body=body)
            self.stats['served']+=1
        except Exception:
            self.stats['fallback']+=1
            with suppress(Exception):
                await route.fallback()
        finally:
            if task is not None and self.cached.get(request.url) is task:
                self.cached.pop(request.url,None)
