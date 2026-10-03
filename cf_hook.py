import os
from urllib.parse import urlparse
import certifi
import ssl

RELAY_HOST = os.environ.get("CF_RELAY_URL", "https://cloudflare-relay.duymv02.workers.dev")
ssl_ctx = ssl.create_default_context(cafile=certifi.where())

# 1. Hook requests
try:
    import requests
    from requests.adapters import HTTPAdapter

    class CloudflareRelayAdapter(HTTPAdapter):
        def send(self, request, *args, **kwargs):
            parsed = urlparse(request.url)
            if parsed.hostname == "dola.com" or (parsed.hostname or "").endswith(".dola.com"):
                path_and_query = parsed.path
                if parsed.query:
                    path_and_query += "?" + parsed.query
                request.headers["x-relay-target"] = f"{parsed.scheme}://{parsed.netloc}"
                request.headers["x-relay-path"] = path_and_query
                request.url = RELAY_HOST
                if "Host" in request.headers:
                    del request.headers["Host"]
                kwargs["proxies"] = {}
            return super().send(request, *args, **kwargs)

    _orig_session_init = requests.Session.__init__

    def _hooked_session_init(self, *args, **kwargs):
        _orig_session_init(self, *args, **kwargs)
        self.mount("https://", CloudflareRelayAdapter())
        self.mount("http://", CloudflareRelayAdapter())

    requests.Session.__init__ = _hooked_session_init
except ImportError:
    pass

# 2. Hook aiohttp
try:
    import aiohttp

    _orig_aiohttp_request = aiohttp.ClientSession._request

    async def _hooked_aiohttp_request(self, method, str_or_url, **kwargs):
        url_str = str(str_or_url)
        parsed = urlparse(url_str)
        if parsed.hostname == "dola.com" or (parsed.hostname or "").endswith(".dola.com"):
            path_and_query = parsed.path
            if parsed.query:
                path_and_query += "?" + parsed.query
            headers = dict(kwargs.get("headers") or {})
            headers["x-relay-target"] = f"{parsed.scheme}://{parsed.netloc}"
            headers["x-relay-path"] = path_and_query
            kwargs["headers"] = headers
            kwargs["ssl"] = ssl_ctx
            str_or_url = RELAY_HOST
        return await _orig_aiohttp_request(self, method, str_or_url, **kwargs)

    aiohttp.ClientSession._request = _hooked_aiohttp_request
except ImportError:
    pass

print("[cf_hook] Pure API dùng tuyến relay đã cấu hình; proxy tài khoản chỉ áp dụng cho browser.")
