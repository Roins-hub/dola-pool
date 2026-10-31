"""公网参考素材下载与安全校验（当前先支持图片）。"""
import asyncio
import ipaddress
import socket
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
from PIL import Image

import config
from reference_uploads import copy_task_image, MEDIA_ID

_ALLOWED_IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


def validate_public_url(url: str) -> str:
    """只接受公网 HTTP(S) URL，拒绝 localhost/内网/带认证信息的 URL。"""
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("参考图片 URL 无效")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("参考图片只支持 http/https 公网 URL")
    if parsed.username or parsed.password:
        raise ValueError("参考图片 URL 不允许携带用户名或密码")
    host = parsed.hostname
    try:
        # 直接 IP 与 DNS 解析结果都做 SSRF 检查；禁止回环、私网、链路本地等地址。
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = awaitable_getaddrinfo(host)
            addresses = {ipaddress.ip_address(x) for x in infos}
        except Exception as exc:
            raise ValueError(f"无法解析参考图片域名: {host}") from exc
    if not addresses or any(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
        for ip in addresses
    ):
        raise ValueError("参考图片 URL 指向内网或保留地址")
    return url


def awaitable_getaddrinfo(host: str) -> list[str]:
    """同步 DNS 解析封装；调用方在下载协程中通过 to_thread 调用。"""
    return [item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


async def _validate_url_async(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("参考图片 URL 无效")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("参考图片只支持 http/https 公网 URL")
    if parsed.username or parsed.password:
        raise ValueError("参考图片 URL 不允许携带用户名或密码")
    host = parsed.hostname
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            resolved = await asyncio.to_thread(awaitable_getaddrinfo, host)
            addresses = {ipaddress.ip_address(x) for x in resolved}
        except Exception as exc:
            raise ValueError(f"无法解析参考图片域名: {host}") from exc
    if not addresses or any(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
        for ip in addresses
    ):
        raise ValueError("参考图片 URL 指向内网或保留地址")
    return url


async def validate_reference_urls(urls: list[str]) -> list[str]:
    if len(urls) > config.REFERENCE_IMAGE_MAX_COUNT:
        raise ValueError(f"参考图片最多 {config.REFERENCE_IMAGE_MAX_COUNT} 张")
    normalized = []
    seen = set()
    for raw in urls:
        url = await _validate_url_async(raw)
        if url not in seen:
            normalized.append(url)
            seen.add(url)
    return normalized


async def _read_response_image(resp: aiohttp.ClientResponse) -> tuple[bytes, str]:
    content_length = resp.headers.get("Content-Length")
    if content_length and int(content_length) > config.REFERENCE_IMAGE_MAX_BYTES:
        raise ValueError("参考图片超过单文件大小限制")
    chunks = []
    total = 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > config.REFERENCE_IMAGE_MAX_BYTES:
            raise ValueError("参考图片超过单文件大小限制")
        chunks.append(chunk)
    data = b"".join(chunks)
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
            fmt = image.format
    except Exception as exc:
        raise ValueError("参考文件不是有效图片") from exc
    if fmt not in _ALLOWED_IMAGE_FORMATS:
        raise ValueError("参考图片仅支持 JPEG、PNG、WEBP")
    return data, _ALLOWED_IMAGE_FORMATS[fmt]


async def download_one_image(session: aiohttp.ClientSession, url: str, dest: Path, *, proxy_url=None) -> Path:
    current = await _validate_url_async(url)
    # 公网素材优先尝试代理；部分 CDN/图片站拒绝代理出口，再直连回退。
    if config.ISP_ONLY:
        config.require_isp_proxy(config._parse_proxy(proxy_url))
    proxies = [proxy_url] if proxy_url else ([config.PROXY, None] if config.PROXY else [None])
    last_error = None
    for proxy in proxies:
        current = await _validate_url_async(url)
        for _ in range(5):
            try:
                async with session.get(
                    current,
                    allow_redirects=False,
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=config.REFERENCE_DOWNLOAD_TIMEOUT),
                    headers={"User-Agent": "dola-pool-reference-fetch/1.0"},
                ) as resp:
                    if 300 <= resp.status < 400 and resp.headers.get("Location"):
                        current = await _validate_url_async(urljoin(current, resp.headers["Location"]))
                        continue
                    if resp.status != 200:
                        raise ValueError(f"参考图片下载失败 HTTP {resp.status}")
                    data, suffix = await _read_response_image(resp)
                    path = dest.with_suffix(suffix)
                    path.write_bytes(data)
                    return path
            except Exception as exc:
                last_error = exc
                break
    raise ValueError(f'参考图片下载失败（{type(last_error).__name__}），未切换账号线路' if last_error else '参考图片重定向次数过多') from None


async def download_reference_images(urls: list[str], task_id: str, *, account=None) -> tuple[Path | None, list[str]]:
    """Prepare URL images and owned image/audio/video uploads in one task batch."""
    image_count = sum(not (url.startswith('upload:') and MEDIA_ID.fullmatch(url.rsplit('/', 1)[-1])) for url in urls)
    if image_count > config.REFERENCE_IMAGE_MAX_COUNT:
        raise ValueError(f"参考图片最多 {config.REFERENCE_IMAGE_MAX_COUNT} 张")
    if len(urls) - image_count > getattr(config, 'REFERENCE_MEDIA_MAX_COUNT', 10):
        raise ValueError('音视频参考素材过多')
    # Upload references are created only after the authenticated owner is checked.
    # Public API input still passes validate_reference_urls and cannot create them.
    urls = list(dict.fromkeys(urls))
    remote = [url for url in urls if not url.startswith('upload:')]
    proxy_url = config.proxy_url_for(account) if remote and (account or config.ISP_ONLY) else None
    await validate_reference_urls(remote)
    if not urls:
        return None, []
    root = Path(tempfile.mkdtemp(prefix=f"dola_ref_{task_id}_"))
    try:
        async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as session:
            paths = []
            for index, url in enumerate(urls):
                if url.startswith('upload:'):
                    path = await asyncio.to_thread(copy_task_image, url, config.REFERENCE_UPLOAD_DIR,
                                                   task_id, root / f'image_{index}')
                else:
                    path = await download_one_image(session, url, root / f"image_{index}", proxy_url=proxy_url)
                paths.append(str(path))
        return root, paths
    except BaseException:
        for child in root.glob("*"):
            child.unlink(missing_ok=True)
        root.rmdir()
        raise
