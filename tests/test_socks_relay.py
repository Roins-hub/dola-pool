"""2.1.6：带用户名密码的 socks5 → 本地 HTTP 中转（Chromium 不支持 socks5 鉴权）。

用一个假的 socks5 服务器（要求用户名密码）+ 回显服务器，验证浏览器式的 CONNECT
经过本地中转能正确鉴权、连到目标并双向转发。
"""
from __future__ import annotations

import asyncio
import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import socks_relay


def _start_servers(user=b"u1", pwd=b"p@ss"):
    """后台线程里起：回显服务器 + 需要鉴权的 socks5 服务器。返回 (socks端口, 回显端口, 记录)。"""
    loop = asyncio.new_event_loop()
    seen = {"auth": None, "target": None}
    ports = {}
    ready = threading.Event()

    async def echo(r, w):
        while data := await r.read(1024):
            w.write(b"echo:" + data)
            await w.drain()
        w.close()

    async def socks(r, w):
        await r.readexactly(2)                       # ver, nmethods
        await r.readexactly(2)                       # methods
        w.write(b"\x05\x02")
        await w.drain()
        await r.readexactly(1)
        u = await r.readexactly((await r.readexactly(1))[0])
        p = await r.readexactly((await r.readexactly(1))[0])
        seen["auth"] = (u, p)
        if (u, p) != (user, pwd):
            w.write(b"\x01\x01")
            await w.drain()
            w.close()
            return
        w.write(b"\x01\x00")
        await w.drain()
        head = await r.readexactly(4)
        assert head[3] == 3
        host = await r.readexactly((await r.readexactly(1))[0])
        port = struct.unpack(">H", await r.readexactly(2))[0]
        seen["target"] = (host.decode(), port)
        ur, uw = await asyncio.open_connection("127.0.0.1", ports["echo"])
        w.write(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack(">H", port))
        await w.drain()

        async def pipe(a, b):
            while data := await a.read(1024):
                b.write(data)
                await b.drain()
            b.close()

        await asyncio.gather(pipe(r, uw), pipe(ur, w), return_exceptions=True)

    async def main():
        e = await asyncio.start_server(echo, "127.0.0.1", 0)
        s = await asyncio.start_server(socks, "127.0.0.1", 0)
        ports["echo"] = e.sockets[0].getsockname()[1]
        ports["socks"] = s.sockets[0].getsockname()[1]
        ready.set()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(5)
    return ports["socks"], ports["echo"], seen


def _connect_via(proxy_url: str, host: str, port: int) -> socket.socket:
    hostport = proxy_url.rsplit("//", 1)[1]
    ph, pp = hostport.split(":")
    s = socket.create_connection((ph, int(pp)), timeout=5)
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(1024)
        if not chunk:
            break
        resp += chunk
    assert resp.startswith(b"HTTP/1.1 200"), resp
    return s


def test_needs_relay_only_for_authenticated_socks5():
    assert socks_relay.needs_relay("socks5://u:p@1.2.3.4:1080")
    assert socks_relay.needs_relay("socks5h://u:p@1.2.3.4:1080")
    assert not socks_relay.needs_relay("socks5://1.2.3.4:1080")       # 无鉴权 Chromium 支持
    assert not socks_relay.needs_relay("http://u:p@1.2.3.4:8080")      # http 鉴权 Chromium 支持
    assert not socks_relay.needs_relay("")


def test_relay_authenticates_and_tunnels():
    socks_port, _echo_port, seen = _start_servers()
    url = f"socks5://u1:p%40ss@127.0.0.1:{socks_port}"
    local = socks_relay.local_http_proxy_for(url)
    assert local.startswith("http://127.0.0.1:")
    assert socks_relay.local_http_proxy_for(url) == local               # 同一个代理复用中转
    s = _connect_via(local, "www.dola.com", 443)
    s.sendall(b"hello")
    assert s.recv(1024) == b"echo:hello"
    s.close()
    assert seen["auth"] == (b"u1", b"p@ss")                              # URL 编码的密码已还原
    assert seen["target"] == ("www.dola.com", 443)                       # 域名交给代理远端解析


def test_browser_proxy_kwargs_use_relay_for_socks5_auth(monkeypatch):
    import importlib
    # 别的用例会往 sys.modules 里塞一个假的 browser 模块：这里临时换成真模块，结束后自动还原
    monkeypatch.delitem(sys.modules, "browser", raising=False)
    browser = importlib.import_module("browser")
    socks_port, _e, _s = _start_servers()
    kw = browser._kwargs_from_url(f"socks5://u1:p%40ss@127.0.0.1:{socks_port}")
    assert kw["server"].startswith("http://127.0.0.1:")
    assert "username" not in kw and "password" not in kw
    kw2 = browser._kwargs_from_url("http://u:p@1.2.3.4:8080")           # http 代理不变
    assert kw2 == {"server": "http://1.2.3.4:8080", "username": "u", "password": "p"}


def test_relay_answers_502_when_upstream_socks_fails():
    """Silent close made Chromium report vague ERR_FAILED/EMPTY_RESPONSE on uploads."""
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()                                                        # nothing listens here
    local = socks_relay.local_http_proxy_for(f"socks5://u:p@127.0.0.1:{port}")
    ph, pp = local.rsplit("//", 1)[1].split(":")
    s = socket.create_connection((ph, int(pp)), timeout=10)
    s.sendall(b"CONNECT imagex.invalid:443 HTTP/1.1\r\nHost: imagex.invalid:443\r\n\r\n")
    resp = b""
    while chunk := s.recv(1024):
        resp += chunk
    s.close()
    assert resp.startswith(b"HTTP/1.1 502")
    assert sum(socks_relay.STATS.values()) >= 1
    assert not any("u:p" in key for key in socks_relay.STATS)
