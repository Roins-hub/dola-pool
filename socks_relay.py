"""socks5（带用户名密码）→ 本地 HTTP 代理中转。

Chromium 不支持带鉴权的 socks5 代理：Playwright 直接报
`Browser does not support socks5 proxy authentication`，浏览器路径的号（Google 登录号的
加号 / 验证 / 出片 / 凭据刷新）全部起不来。纯 API 路径（requests）不受影响。

做法：在本进程里起一个只监听 127.0.0.1 的 HTTP 代理（不要鉴权），浏览器连它；
它再用用户名密码连真正的 socks5 代理转发出去。每个 socks5 地址一个中转端口，复用。
中转跑在独立线程的事件循环里，和调用方（Playwright 的事件循环）互不影响。
"""
from __future__ import annotations

import asyncio
import struct
import threading
from urllib.parse import unquote, urlsplit

_LOCK = threading.Lock()
_RELAYS: dict[str, int] = {}          # socks5 URL -> 本地端口
_LOOP: asyncio.AbstractEventLoop | None = None
_HEAD_LIMIT = 64 * 1024


def needs_relay(proxy_url: str) -> bool:
    """socks5/socks5h 且带用户名 → 浏览器用不了，需要本地中转。"""
    parts = urlsplit((proxy_url or "").strip())
    return parts.scheme in ("socks5", "socks5h") and bool(parts.username)


def local_http_proxy_for(socks_url: str) -> str:
    """返回指向本地中转的 `http://127.0.0.1:端口`（首次调用时启动中转）。"""
    with _LOCK:
        port = _RELAYS.get(socks_url)
        if port:
            return f"http://127.0.0.1:{port}"
        loop = _ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(_start_server(socks_url), loop)
        port = fut.result(timeout=10)
        _RELAYS[socks_url] = port
        return f"http://127.0.0.1:{port}"


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    if _LOOP is not None and _LOOP.is_running():
        return _LOOP
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    threading.Thread(target=run, name="socks-relay", daemon=True).start()
    ready.wait(5)
    _LOOP = loop
    return loop


async def _start_server(socks_url: str) -> int:
    proxy = urlsplit(socks_url)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await _serve_one(proxy, reader, writer)
        except Exception:  # noqa: BLE001 - 单个连接失败不影响中转本身
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server.sockets[0].getsockname()[1]


async def _serve_one(proxy, reader, writer):
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > _HEAD_LIMIT:
        return
    request_line, _, rest = head.partition(b"\r\n")
    parts = request_line.decode("latin-1").split(" ")
    if len(parts) < 3:
        return
    method, target, version = parts[0], parts[1], parts[2]
    if method.upper() == "CONNECT":
        host, _, port = target.rpartition(":")
        up_r, up_w = await _connect_or_502(proxy, host.strip("[]"), int(port or 443), writer)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
    else:
        # 明文 HTTP：绝对 URL → 连目标主机，请求行改回相对路径
        url = urlsplit(target)
        if not url.hostname:
            return
        up_r, up_w = await _connect_or_502(proxy, url.hostname, url.port or 80, writer)
        path = url.path or "/"
        if url.query:
            path += "?" + url.query
        up_w.write(f"{method} {path} {version}\r\n".encode("latin-1") + rest)
        await up_w.drain()
    await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer), return_exceptions=True)


STATS: dict[str, int] = {}             # 失败原因 -> 次数（不含地址/凭据），供诊断


async def _connect_or_502(proxy, host: str, port: int, writer):
    """上游连不上时明确回 502：静默断开会让 Chromium 只报含糊的 ERR_FAILED/EMPTY_RESPONSE。"""
    try:
        return await socks5_connect(proxy, host, port)
    except Exception as exc:
        reason = str(exc)[:60] if isinstance(exc, ConnectionError) and str(exc) else type(exc).__name__
        STATS[reason] = STATS.get(reason, 0) + 1
        try:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        raise


async def socks5_connect(proxy, host: str, port: int):
    """按 RFC 1928 / 1929 连 socks5 代理（用户名密码鉴权），返回到目标的 (reader, writer)。"""
    r, w = await asyncio.wait_for(
        asyncio.open_connection(proxy.hostname, proxy.port or 1080), timeout=20)
    try:
        # 握手各步原先没有超时：上游半开时这条连接会一直挂着
        return await asyncio.wait_for(_socks5_handshake(r, w, proxy, host, port), timeout=20)
    except BaseException:
        w.close()
        raise


async def _socks5_handshake(r, w, proxy, host: str, port: int):
    w.write(b"\x05\x02\x00\x02")                       # 支持：无鉴权 / 用户名密码
    await w.drain()
    _ver, method = await r.readexactly(2)
    if method == 0x02:
        user = unquote(proxy.username or "").encode()
        pwd = unquote(proxy.password or "").encode()
        w.write(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
        await w.drain()
        _v, status = await r.readexactly(2)
        if status != 0:
            raise ConnectionError("socks5 用户名密码鉴权失败")
    elif method != 0x00:
        raise ConnectionError("socks5 代理不接受可用的鉴权方式")
    host_bytes = host.encode("idna")
    w.write(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack(">H", port))
    await w.drain()
    reply = await r.readexactly(4)
    if reply[1] != 0:
        raise ConnectionError(f"socks5 CONNECT 失败（代码 {reply[1]}）")
    atyp = reply[3]
    if atyp == 1:
        await r.readexactly(4)
    elif atyp == 3:
        await r.readexactly((await r.readexactly(1))[0])
    elif atyp == 4:
        await r.readexactly(16)
    await r.readexactly(2)
    return r, w


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    finally:
        try:
            dst.close()
        except Exception:  # noqa: BLE001
            pass
