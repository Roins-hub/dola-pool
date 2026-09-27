"""公网参考素材下载与安全校验（当前先支持图片）。"""
import asyncio
import base64
import ipaddress
import socket
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
from PIL import Image

import config

_ALLOWED_IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_DATA_URL_RE = "data:image/"


def _is_data_image(url: str) -> bool:
    return isinstance(url, str) and url.startswith(_DATA_URL_RE) and ";base64," in url


def _decode_data_image(url: str) -> tuple[bytes, str]:
    """解码 data:image/xxx;base64,... 参考图；返回 (字节, 扩展名)。"""
    head, _, b64 = url.partition(",")
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise ValueError("参考图片 data URL base64 无效") from exc
    if len(data) > config.REFERENCE_IMAGE_MAX_BYTES:
        raise ValueError("参考图片超过单文件大小限制")
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
            fmt = image.format
    except Exception as exc:
        raise ValueError("参考文件不是有效图片") from exc
    suffix = _ALLOWED_IMAGE_FORMATS.get(fmt)
    if not suffix:
        raise ValueError("参考图片仅支持 JPEG、PNG、WEBP")
    return data, suffix


def validate_public_url(url: str) -> str:
    """只接受公网 HTTP(S) URL，拒绝 localhost/内网/带认证信息的 URL。"""
    if not isinstance(url, str):
        raise ValueError("参考图片 URL 无效")
    if _is_data_image(url):
        _decode_data_image(url)
        return url
    if len(url) > 4096:
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
    if not isinstance(url, str):
        raise ValueError("参考图片 URL 无效")
    if _is_data_image(url):
        _decode_data_image(url)
        return url
    if len(url) > 4096:
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


async def download_one_image(session: aiohttp.ClientSession, url: str, dest: Path) -> Path:
    current = await _validate_url_async(url)
    # 公网素材优先尝试代理；部分 CDN/图片站拒绝代理出口，再直连回退。
    proxies = []
    if config.PROXY:
        proxies.append(config.PROXY)
    proxies.append(None)
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
    raise ValueError(str(last_error) if last_error else "参考图片下载失败")


async def download_reference_images(urls: list[str], task_id: str) -> tuple[Path | None, list[str]]:
    """下载图片到临时目录，返回 (目录, 本地路径列表)。调用方负责 cleanup。"""
    urls = await validate_reference_urls(urls)
    if not urls:
        return None, []
    root = Path(tempfile.mkdtemp(prefix=f"dola_ref_{task_id}_"))
    try:
        async with aiohttp.ClientSession() as session:
            paths = []
            for index, url in enumerate(urls):
                if _is_data_image(url):
                    data, suffix = _decode_data_image(url)
                    path = root / f"image_{index}{suffix}"
                    path.write_bytes(data)
                    paths.append(str(path))
                else:
                    paths.append(str(await download_one_image(session, url, root / f"image_{index}")))
        return root, paths
    except Exception:
        for child in root.glob("*"):
            child.unlink(missing_ok=True)
        root.rmdir()
        raise
