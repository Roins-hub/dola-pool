"""公网参考素材下载与安全校验（当前先支持图片）。"""
import asyncio
import base64
import ipaddress
import socket
import tempfile
import uuid
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


def is_local_reference(path: str) -> bool:
    """本服务自己落盘的参考图（data: URL 物化出来的）——只认 REFERENCE_FILE_DIR 下的文件。"""
    if not isinstance(path, str) or not path or "://" in path:
        return False
    try:
        resolved = Path(path).resolve()
        resolved.relative_to(Path(config.REFERENCE_FILE_DIR).resolve())
    except (OSError, ValueError):
        return False
    return resolved.is_file()


def materialize_data_urls(urls: list[str]) -> list[str]:
    """把 data: URL 参考图落盘，返回本地路径列表；其余 URL 原样返回。

    为什么必须落盘：客户端（画布）会把几 MB 的图片内联成 base64 传进来，如果直接塞进
    tasks.db 的 reference_images，库会被撑爆（实测 27 条任务 → 587MB，面板接口 MemoryError）。
    落盘后库里只存路径，出片走到 `download_reference_images` 时直接复用这些文件。
    """
    if not urls:
        return []
    root = Path(config.REFERENCE_FILE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    total = 0
    for index, raw in enumerate(urls):
        if not _is_data_image(raw):
            out.append(raw)
            continue
        data, suffix = _decode_data_image(raw)
        total += len(data)
        if total > config.REFERENCE_TOTAL_MAX_BYTES:
            raise ValueError("参考图片总量超过限制")
        target = root / ("ref_%s_%d%s" % (uuid.uuid4().hex[:12], index, suffix))
        target.write_bytes(data)
        out.append(str(target))
    return out


def cleanup_local_references(paths: list[str]) -> None:
    """任务结束后删掉落盘的参考图（只删自己目录里的）。"""
    for raw in paths or []:
        if not is_local_reference(raw):
            continue
        try:
            Path(raw).unlink()
        except OSError:
            pass


def save_reference_thumbnails(paths: list[str], task_id: str) -> list[str]:
    """把参考图另存一份小缩略图，返回相对文件名列表（面板回看用）。

    为什么需要另存：任务终态会把原参考图清理掉（data: URL 落盘的文件 unlink、
    远程图所在临时目录 rmtree），但面板"参考图"要能回看历史任务，所以趁图还在时
    存一份缩略图到 THUMB_DIR，跟随任务保留期一起清。
    """
    count = max(0, int(getattr(config, "REFERENCE_THUMB_COUNT", 1)))
    if not paths or count <= 0:
        return []
    root = Path(config.THUMB_DIR)
    root.mkdir(parents=True, exist_ok=True)
    max_px = max(16, int(getattr(config, "REFERENCE_THUMB_MAX_PX", 160)))
    quality = min(95, max(30, int(getattr(config, "REFERENCE_THUMB_QUALITY", 80))))
    out: list[str] = []
    for index, raw in enumerate(paths[:count]):
        try:
            src = Path(str(raw))
            if not src.is_file():
                continue
            with Image.open(src) as img:
                thumb = img.convert("RGB")
                thumb.thumbnail((max_px, max_px))
                name = "%s_%d.jpg" % (task_id, index)
                thumb.save(root / name, "JPEG", quality=quality, optimize=True)
            out.append(name)
        except Exception:
            continue    # 缩略图失败不影响出片主流程
    return out


def thumb_file(name: str) -> Path | None:
    """把文件名解析到 THUMB_DIR 内（防目录穿越）；非法返回 None。"""
    if not name:
        return None
    root = Path(config.THUMB_DIR).resolve()
    candidate = (root / Path(str(name)).name).resolve()
    if candidate.parent != root:
        return None
    return candidate


def delete_reference_thumbnails(names: list[str]) -> int:
    """删除指定缩略图文件，返回删除数量。"""
    removed = 0
    for name in names or []:
        path = thumb_file(name)
        if not path:
            continue
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def sweep_stale_references(max_age_seconds: float = 86400) -> int:
    """启动时清一次残留（进程崩在生成中途时会留下文件）。"""
    import time

    root = Path(config.REFERENCE_FILE_DIR)
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for item in root.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
        except OSError:
            continue
    return removed


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
    if is_local_reference(url):
        # 本服务落盘的参考图（data: URL 物化出来的），直接复用文件
        return str(Path(url).resolve())
    if isinstance(url, str) and url.startswith(("refs/", "/")) and (
            Path(config.REFERENCE_FILE_DIR).name in url):
        # 形如 refs/xxx.png 但文件不在了：多半是任务被重排/清理过，
        # 原来会报「只支持 http/https 公网 URL」，让人以为是格式问题，这里说清楚
        raise ValueError(f"参考图文件已丢失（任务重排或被清理）：{url}")
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
    if is_local_reference(url):
        # 本服务落盘的参考图（data: URL 物化出来的），直接复用文件
        return str(Path(url).resolve())
    if isinstance(url, str) and url.startswith(("refs/", "/")) and (
            Path(config.REFERENCE_FILE_DIR).name in url):
        # 形如 refs/xxx.png 但文件不在了（任务重排/被清理）：说清楚，别报成「格式不支持」
        raise ValueError(f"参考图文件已丢失（任务重排或被清理）：{url}")
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
    if all(is_local_reference(u) for u in urls):
        # 全是本地落盘的参考图：不动它们（生命周期由 server 侧任务结束时的清理负责）
        return None, list(urls)
    root = Path(tempfile.mkdtemp(prefix=f"dola_ref_{task_id}_"))
    try:
        async with aiohttp.ClientSession() as session:
            paths = []
            for index, url in enumerate(urls):
                if is_local_reference(url):
                    paths.append(url)
                    continue
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
