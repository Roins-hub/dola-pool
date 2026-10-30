"""JSON / 页面响应的 gzip 压缩。

管理面板的列表接口一次几十到几百 KB（任务列表约 200 KB），跨洋访问时传输占了大头。
这里只压缩一次性返回的文本类响应；视频、分段（Range）请求、已编码的响应原样放行，
所以不会影响视频播放、断点续传，也不依赖反向代理有没有开 gzip。
"""
import gzip

from starlette.datastructures import Headers, MutableHeaders

COMPRESSIBLE_TYPES = frozenset({
    "application/json", "text/html", "text/css", "text/plain",
    "text/javascript", "application/javascript", "image/svg+xml",
})


class GzipTextMiddleware:
    def __init__(self, app, minimum_size: int = 1024, level: int = 5):
        self.app = app
        self.minimum_size = minimum_size
        self.level = level

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or "gzip" not in Headers(scope=scope).get("accept-encoding", "").lower():
            await self.app(scope, receive, send)
            return

        start = None
        chunks: list[bytes] = []
        passthrough = False

        async def wrapped(message):
            nonlocal start, passthrough
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                headers = Headers(raw=message.get("headers") or [])
                ctype = headers.get("content-type", "").split(";")[0].strip().lower()
                if (ctype not in COMPRESSIBLE_TYPES or "content-encoding" in headers
                        or "content-range" in headers or message["status"] in (204, 206, 304)):
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            headers = MutableHeaders(scope=start)
            if len(body) >= self.minimum_size:
                body = gzip.compress(body, self.level)
                headers["Content-Encoding"] = "gzip"
                headers["Content-Length"] = str(len(body))
                headers.add_vary_header("Accept-Encoding")
            await send(start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped)
