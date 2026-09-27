#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dola.com API video generation.

Anonymous pure-requests mode is kept for compatibility.  When anonymous
generation is blocked, use --login-pure with cookies exported from the logged-in
Chrome profile; --login-cdp remains as a browser-transport fallback.
"""
from __future__ import annotations

import argparse
import base64
import itertools
import hashlib
import hmac
import json
import logging
import os
import random
import re
import shutil
import socket
import string
import subprocess
import sys
import time
import tempfile
import threading
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, unquote, urlsplit
import urllib.request

import requests

BASE = "https://www.dola.com"
AID = "495671"
BOT_ID = "7339470689562525703"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
IMAGEX_REGION = "us-east-1"
IMAGEX_SERVICE = "imagex"
NOWATERMARK_API_ENDPOINT = "http://47.104.150.143:8765/tools/dola/"
NOWATERMARK_API_TIMEOUT = 60
DEFAULT_POLL_INTERVAL = 30
DEFAULT_DOWNLOAD_DIR = SCRIPT_DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"
DEFAULT_AUDIO_REFERENCE_PROMPT = "记住这个音频，他是人物张三的参考音色，后续生成视频需要用到；"
SCRIPT_DIR = Path(__file__).resolve().parent
SESSION_FILE = SCRIPT_DIR / ".dola_pure_api_session.json"
LOGIN_SESSION_FILE = SCRIPT_DIR / ".dola_pure_api_session_login.json"
LOGIN_COOKIE_FILE = SCRIPT_DIR / ".dola_login_cookies.json"
DEFAULT_CDP_URL = "http://127.0.0.1:9555"
DEFAULT_9555_LOGIN_SESSION_FILE = SCRIPT_DIR / ".dola_pure_api_session_login_9555.json"
_SIGNER_CANDIDATES = (
    SCRIPT_DIR / "js" / "bdms_sign_url.js",
    SCRIPT_DIR / "bdms_sign_url.js",
)
SIGNER = next((path for path in _SIGNER_CANDIDATES if path.exists()), _SIGNER_CANDIDATES[0])
_SIGN_URL_IMPL = None


def set_sign_url_impl(func) -> None:
    """Allow a persistent Node signer to replace per-request subprocess calls."""
    global _SIGN_URL_IMPL
    _SIGN_URL_IMPL = func


VALID_RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
VALID_DURATIONS = {5, 10, 15, 30}


def convert_audio_to_mp3(path: Path) -> Path:
    """用 ffmpeg 把 wav/m4a 等转成 mp3，返回临时 mp3 路径。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise DolaError("ffmpeg is required for non-mp3 audio. Install ffmpeg or pass an .mp3 file.")
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
        output = Path(handle.name)
    try:
        completed = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(path),
                "-vn",
                "-b:a",
                "128k",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception:
        try:
            output.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    if completed.returncode != 0:
        try:
            output.unlink(missing_ok=True)
        except Exception:
            pass
        details = (completed.stderr or completed.stdout or "").strip()
        raise DolaError(f"ffmpeg audio conversion failed: {details[:500]}")
    return output


class DolaError(RuntimeError):
    pass


class DolaNoDataError(DolaError):
    pass


@dataclass
class PollResult:
    urls: list[str]
    vids: list[str]
    status: str
    failure_reasons: list[str]
    texts: list[str]
    creation_statuses: list[str]
    wait_minutes: str = ""
    source_data: Any = None


@dataclass
class UploadedAudio:
    name: str
    uri: str
    audio_format: str
    size: int
    md5: str = ""
    identifier: str = ""
    file_type: int = 3
    resource_type: int | None = None
    inline: bool = False


@dataclass
class SubmitResult:
    conversation_id: str = ""
    section_id: str = ""
    local_conversation_id: str = ""
    local_message_id: str = ""
    question_id: str = ""
    message_index: int | None = None
    query_message_indexes: list[int] = field(default_factory=list)
    query_question_ids: list[str] = field(default_factory=list)
    error: str = ""
    error_code: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    reply_texts: list[str] = field(default_factory=list)
    final_reply_text: str = ""
    pre_handle_warnings: list[str] = field(default_factory=list)
    pre_generate_id: str = ""


def log(msg: str) -> None:
    text = str(msg).replace("\r", " ").strip()
    print(msg, flush=True)
    if text and any(
        marker in text
        for marker in (
            "Chain error",
            "Daily quota exhausted",
            "Generation voided",
            "Split-duration prompt",
        )
    ):
        logging.getLogger("dola2api.protocol").info("%s", text)


def random_decimal_id() -> str:
    return str(random.randint(7600000000000000000, 7699999999999999999))


def random_s(length: int = 11) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def make_fp() -> str:
    lower = string.ascii_lowercase + string.digits
    mixed = string.ascii_letters + string.digits
    seg = lambda n, chars: "".join(random.choice(chars) for _ in range(n))
    return f"verify_{seg(8, lower)}_{seg(5, mixed)}_{seg(6, mixed)}_{seg(7, mixed)}_{seg(8, mixed)}_{seg(12, mixed)}"


def load_state() -> dict[str, Any]:
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8")) if SESSION_FILE.exists() else {}
    except Exception:
        return {}


def load_state_file(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        p = Path(path)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def apply_state(session: requests.Session) -> dict[str, Any]:
    state = load_state()
    cookies = state.get("cookies", {})
    if isinstance(cookies, dict):
        for name, info in cookies.items():
            if isinstance(info, dict):
                session.cookies.set(name, info.get("value", ""), domain=info.get("domain") or ".dola.com", path=info.get("path") or "/")
    return state


def apply_state_cookies(session: requests.Session, state: dict[str, Any]) -> None:
    """Load Chrome-exported Dola cookies into a Python requests cookie jar."""
    cookie_list = state.get("cookies_list")
    if isinstance(cookie_list, list) and cookie_list:
        for c in cookie_list:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            if "dola.com" not in str(c.get("domain") or ""):
                continue
            session.cookies.set(
                str(c["name"]),
                str(c.get("value", "")),
                domain=str(c.get("domain") or ".dola.com"),
                path=str(c.get("path") or "/"),
            )
        return
    cookies = state.get("cookies", {})
    if isinstance(cookies, dict):
        for name, info in cookies.items():
            if isinstance(info, dict):
                session.cookies.set(
                    str(name),
                    str(info.get("value", "")),
                    domain=str(info.get("domain") or ".dola.com"),
                    path=str(info.get("path") or "/"),
                )


def state_cookie_header(state: dict[str, Any]) -> str:
    """Best-effort Cookie header in browser order, preserving Chrome path cookies."""
    header = state.get("cookie_header")
    if isinstance(header, str) and header.strip():
        return header.strip()
    parts: list[str] = []
    seen: set[str] = set()
    cookie_list = state.get("cookies_list")
    if isinstance(cookie_list, list):
        for c in cookie_list:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "")
            if not name or name in seen:
                continue
            if "dola.com" not in str(c.get("domain") or ""):
                continue
            seen.add(name)
            parts.append(f"{name}={c.get('value', '')}")
    cookies = state.get("cookies", {})
    if isinstance(cookies, dict):
        for name, info in cookies.items():
            if not name or str(name) in seen:
                continue
            if isinstance(info, dict):
                seen.add(str(name))
                parts.append(f"{name}={info.get('value', '')}")
    return "; ".join(parts)


def parse_cookie_header_pairs(header: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in (header or "").split(";"):
        item = part.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if name:
            pairs.append((name, value.strip()))
    return pairs


def merge_cookie_header(header: str, extra: dict[str, str]) -> str:
    """Keep original cookie order; append jar cookies such as ttwid / s_v_web_id."""
    pairs = parse_cookie_header_pairs(header)
    seen = {name for name, _ in pairs}
    identity = {"ttwid", "s_v_web_id", "msToken"}
    merged: list[tuple[str, str]] = []
    for name, value in pairs:
        if name in extra and name in identity and extra[name]:
            merged.append((name, extra[name]))
        else:
            merged.append((name, value))
    for name, value in extra.items():
        if name and name not in seen and value:
            merged.append((name, value))
            seen.add(name)
    return "; ".join(f"{name}={value}" for name, value in merged)


def refresh_login_cookie_header(session: requests.Session) -> str:
    state = get_login_state(session)
    header = merge_cookie_header(state_cookie_header(state), cookie_dict(session))
    if state:
        state["cookie_header"] = header
        attach_login_state(session, state)
    return header


def cookie_names(header: str) -> list[str]:
    return [name for name, _ in parse_cookie_header_pairs(header)]


def send_with_cookie_header(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: Any = None,
    timeout: int | tuple[int, int] = 30,
    stream: bool = False,
) -> requests.Response:
    """Send a request, keeping login_pure Cookie header after requests rebuilds it from the jar."""
    headers = dict(headers)
    if is_login_pure(session):
        cookie_header = refresh_login_cookie_header(session)
        if cookie_header:
            headers["Cookie"] = cookie_header
    prepared = session.prepare_request(requests.Request(method, url, headers=headers, data=data))
    if is_login_pure(session) and headers.get("Cookie"):
        prepared.headers["Cookie"] = headers["Cookie"]
    return session.send(prepared, timeout=timeout, stream=stream)


def warmup_login_session(session: requests.Session, headers: dict[str, str] | None = None, timeout: int = 30) -> requests.Response:
    """Hit create-image so ttwid lands, then fold it into the login Cookie header."""
    resp = send_with_cookie_header(
        session,
        "GET",
        f"{BASE}/chat/create-image",
        headers=dict(headers or headers_base()),
        timeout=timeout,
    )
    if is_login_pure(session):
        refresh_login_cookie_header(session)
    return resp


def _ensure_spa_cookies(session: requests.Session, ctx: dict[str, str], region: str) -> None:
    defaults = {
        "flow_user_country": region,
        "i18next": "zh",
        "user_language_code": "browser_language",
        "s_v_web_id": ctx.get("fp") or "",
        "has_biz_token": "false",
    }
    for name, value in defaults.items():
        if not value:
            continue
        if session.cookies.get(name):
            continue
        session.cookies.set(name, value, domain=".dola.com", path="/")


def save_state(session: requests.Session, ctx: dict[str, str] | None = None, path: Path = SESSION_FILE) -> None:
    cookies: dict[str, dict[str, str]] = {}
    for c in session.cookies:
        cookies[c.name] = {"value": c.value, "domain": c.domain or ".dola.com", "path": c.path or "/"}
    data: dict[str, Any] = {"cookies": cookies, "updated_at": int(time.time())}
    if ctx:
        data["ctx"] = ctx
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cookie_dict(session: requests.Session) -> dict[str, str]:
    return {c.name: c.value for c in session.cookies}


def fresh_ctx(region: str, pc_version: str, fp: str | None = None) -> dict[str, str]:
    # Query string matches the live SPA /alice/user/get_web_anon_id capture:
    # no web_id/tea_uuid, empty device_id, plus doubao_* aliases.
    return {
        "version_code": "20800",
        "language": "zh",
        "device_platform": "web",
        "doubao_device_platform": "web",
        "aid": AID,
        "real_aid": AID,
        "pkg_type": "release_version",
        "device_id": "",
        "pc_version": pc_version,
        "doubao_pc_version": pc_version,
        "web_id": "",
        "tea_uuid": "",
        "region": region or "",
        "sys_region": region or "",
        "samantha_web": "1",
        "web_platform": "browser",
        "use-olympus-account": "1",
        "web_tab_id": str(uuid.uuid4()),
        "fp": fp or make_fp(),
    }


def base_params(ctx: dict[str, str], include_fp: bool = False, extra: dict[str, str] | None = None) -> dict[str, str]:
    keys = [
        "version_code", "language", "device_platform", "doubao_device_platform",
        "aid", "real_aid", "pkg_type",
        "device_id", "pc_version", "doubao_pc_version", "region", "sys_region",
        "samantha_web", "web_platform", "use-olympus-account", "web_tab_id",
    ]
    params = {k: str(ctx.get(k, "")) for k in keys}
    if include_fp:
        params["fp"] = ctx["fp"]
    if extra:
        params.update({k: str(v) for k, v in extra.items()})
    return params


def build_url(endpoint: str, ctx: dict[str, str], include_fp: bool = False, extra: dict[str, str] | None = None) -> str:
    return f"{BASE}{endpoint}?{urlencode(base_params(ctx, include_fp=include_fp, extra=extra))}"


def sign_url(url: str, *, method: str = "POST", headers: dict[str, str] | None = None, body: Any = "{}", cookies: dict[str, str] | None = None) -> str:
    if isinstance(body, (dict, list)):
        body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(body, bytes):
        body_text = body.decode("utf-8", errors="replace")
    else:
        body_text = str(body)
    req = {"url": url, "method": method, "headers": headers or {}, "body": body_text, "cookies": cookies or {}}
    if _SIGN_URL_IMPL is not None:
        signed = _SIGN_URL_IMPL(url, method=method, headers=headers or {}, body=body_text, cookies=cookies or {})
        if "a_bogus=" not in str(signed or ""):
            raise DolaError(f"BDMS signer did not add a_bogus: {signed}")
        return str(signed)
    if not SIGNER.exists():
        raise DolaError(f"BDMS signer not found: {SIGNER}")
    proc = subprocess.run(
        ["node", str(SIGNER)],
        input=json.dumps(req, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=20,
        cwd=str(SCRIPT_DIR),
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise DolaError(f"BDMS signer failed: {proc.stderr[:500]} {proc.stdout[:500]}")
    try:
        out = json.loads(proc.stdout)
    except ValueError as exc:
        raise DolaError(f"BDMS signer returned non-JSON: {proc.stdout[:500]}") from exc
    if out.get("error"):
        raise DolaError(f"BDMS signer error: {out['error'][:800]}")
    signed = out.get("signed_url") or url
    if "a_bogus=" not in signed:
        raise DolaError(f"BDMS signer did not add a_bogus: {out}")
    return signed


def headers_base(referer: str | None = None) -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": BASE,
        "Referer": referer or f"{BASE}/chat/create-image",
    }


def request_json(resp: requests.Response, label: str) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise DolaError(f"{label} HTTP {resp.status_code}: {resp.text[:800]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise DolaError(f"{label} did not return JSON: {resp.text[:800]}") from exc
    if not isinstance(data, dict):
        # Upstream occasionally returns JSON null/array during a transient
        # gateway failure.  Do not let callers assume ``.get`` exists and
        # turn that response into an opaque NoneType internal error.
        try:
            preview = json.dumps(data, ensure_ascii=False)[:800]
        except (TypeError, ValueError):
            preview = repr(data)[:800]
        raise DolaError(f"{label} returned non-object JSON: {preview}")
    return data



class BrowserFetchResponse:
    """Tiny response shim for JSON posts executed inside Chrome."""

    def __init__(self, status_code: int, text: str, headers: dict[str, str] | None = None, url: str = ""):
        self.status_code = int(status_code or 0)
        self.text = text or ""
        self.headers = headers or {}
        self.url = url

    def json(self) -> Any:
        return json.loads(self.text or "{}")

    def close(self) -> None:
        return None


class BrowserSseResponse(BrowserFetchResponse):
    def iter_lines(self, decode_unicode: bool = False):
        raw = self.text or ""
        for line in raw.splitlines():
            yield line if decode_unicode else line.encode("utf-8", errors="replace")


class DolaCdpClient:
    """Use the logged-in Chrome page as the transport for Dola API calls."""

    def __init__(self, cdp_url: str = DEFAULT_CDP_URL, *, page_url: str = f"{BASE}/chat/create-image"):
        self.cdp_url = cdp_url.rstrip("/")
        self.page_url = page_url
        self.ws = None
        self._id_iter = itertools.count(1)
        self._send_lock = threading.Lock()

    def _http_json(self, path: str) -> Any:
        with urllib.request.urlopen(self.cdp_url + path, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _browser_ws_url(self) -> str:
        return self._http_json("/json/version")["webSocketDebuggerUrl"]

    def _tabs(self) -> list[dict[str, Any]]:
        return [x for x in self._http_json("/json/list") if x.get("webSocketDebuggerUrl")]

    def _sync_call_ws(self, ws_url: str, method: str, params: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
        try:
            import websocket  # type: ignore
        except Exception as exc:
            raise DolaError("websocket-client package is required for --login-cdp") from exc
        ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True, skip_utf8_validation=True)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}, separators=(",", ":")))
            old = ws.gettimeout()
            ws.settimeout(timeout)
            try:
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == 1:
                        return msg
            finally:
                ws.settimeout(old)
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def connect(self) -> "DolaCdpClient":
        try:
            import websocket  # type: ignore
        except Exception as exc:
            raise DolaError("websocket-client package is required for --login-cdp") from exc
        tabs = self._tabs()
        target = next((t for t in tabs if t.get("type") == "page" and "dola.com" in str(t.get("url") or "")), None)
        if not target:
            browser_ws = self._browser_ws_url()
            self._sync_call_ws(browser_ws, "Target.createTarget", {"url": self.page_url}, timeout=10)
            time.sleep(8)
            tabs = self._tabs()
            target = next((t for t in tabs if t.get("type") == "page" and "dola.com" in str(t.get("url") or "")), None)
        if not target:
            raise DolaError(f"No Dola page found/created through CDP {self.cdp_url}")
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30, suppress_origin=True, skip_utf8_validation=True)
        self.call("Runtime.enable", timeout=10)
        return self

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
        if self.ws is None:
            raise DolaError("CDP client is not connected")
        msg_id = next(self._id_iter)
        with self._send_lock:
            self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}, separators=(",", ":")))
        old = self.ws.gettimeout()
        self.ws.settimeout(timeout)
        try:
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("id") == msg_id:
                    if msg.get("error"):
                        raise DolaError(f"CDP {method} error: {msg.get('error')}")
                    return msg
        finally:
            self.ws.settimeout(old)

    def eval(self, expression: str, timeout: int = 60) -> Any:
        res = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True}, timeout=timeout)
        result = (res.get("result") or {}).get("result") or {}
        if result.get("subtype") == "error":
            raise DolaError(f"CDP eval error: {result}")
        return result.get("value")

    def storage_snapshot(self) -> dict[str, Any]:
        expr = r"""
(() => {
  const out = {url: location.href, title: document.title, cookie: document.cookie, localStorage: {}, sessionStorage: {}};
  for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); out.localStorage[k] = localStorage.getItem(k); }
  for (let i = 0; i < sessionStorage.length; i++) { const k = sessionStorage.key(i); out.sessionStorage[k] = sessionStorage.getItem(k); }
  return out;
})()
"""
        return self.eval(expr, timeout=20) or {}

    def get_cookies(self) -> list[dict[str, Any]]:
        browser_ws = self._browser_ws_url()
        res = self._sync_call_ws(browser_ws, "Storage.getCookies", {"urls": [BASE + "/", BASE + "/chat/", BASE + "/chat/create-image"]}, timeout=10)
        cookies = (res.get("result") or {}).get("cookies") or []
        return [c for c in cookies if "dola.com" in str(c.get("domain") or "")]

    def save_login_state(self, session_file: Path = LOGIN_SESSION_FILE, cookie_file: Path = LOGIN_COOKIE_FILE) -> dict[str, Any]:
        cookies = self.get_cookies()
        storage = self.storage_snapshot()
        tea: dict[str, Any] = {}
        try:
            tea = json.loads((storage.get("localStorage") or {}).get("__tea_cache_tokens_495671") or "{}")
        except Exception:
            tea = {}
        fp = ""
        for c in cookies:
            if c.get("name") == "s_v_web_id":
                fp = str(c.get("value") or "")
        cookie_dict_data = {
            str(c.get("name")): {
                "value": c.get("value", ""),
                "domain": c.get("domain") or ".dola.com",
                "path": c.get("path") or "/",
                "expires": c.get("expires"),
                "httpOnly": c.get("httpOnly"),
                "secure": c.get("secure"),
                "sameSite": c.get("sameSite"),
            }
            for c in cookies
            if c.get("name")
        }
        data: dict[str, Any] = {
            "source": "chrome_cdp_login",
            "cdp_url": self.cdp_url,
            "updated_at": int(time.time()),
            "cookies": cookie_dict_data,
            "cookies_list": cookies,
            "ctx": {
                "fp": fp,
                "web_id": str(tea.get("web_id") or tea.get("user_unique_id") or ""),
                "tea_uuid": str(tea.get("web_id") or tea.get("user_unique_id") or ""),
            },
            "storage": storage,
        }
        session_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        cookie_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    def post_json(self, url: str, body_text: str, headers: dict[str, str] | None = None, *, timeout: int = 60) -> BrowserFetchResponse:
        js_headers = dict(headers or {})
        expr = f"""
(async () => {{
  const resp = await fetch({json.dumps(url)}, {{
    method: 'POST',
    credentials: 'include',
    headers: {json.dumps(js_headers, ensure_ascii=False)},
    body: {json.dumps(body_text)},
    cache: 'no-store'
  }});
  const text = await resp.text();
  const headers = {{}};
  for (const [k, v] of resp.headers.entries()) headers[k] = v;
  return {{status: resp.status, url: resp.url, text, headers}};
}})()
"""
        val = self.eval(expr, timeout=timeout) or {}
        return BrowserFetchResponse(int(val.get("status") or 0), val.get("text") or "", val.get("headers") or {}, val.get("url") or url)


def get_login_cdp(session: requests.Session | None = None) -> DolaCdpClient | None:
    return getattr(session, "_dola_cdp", None) if session is not None else None


def attach_login_cdp(session: requests.Session, cdp: DolaCdpClient) -> None:
    setattr(session, "_dola_cdp", cdp)


def get_login_state(session: requests.Session | None = None) -> dict[str, Any]:
    state = getattr(session, "_dola_login_state", None) if session is not None else None
    return state if isinstance(state, dict) else {}


def attach_login_state(session: requests.Session, state: dict[str, Any]) -> None:
    setattr(session, "_dola_login_state", state)


def attach_dola_proxy(session: requests.Session, proxy: str) -> None:
    proxy = normalize_proxy_url(proxy)
    if proxy:
        setattr(session, "_dola_api_proxy", proxy)
        if not session.proxies:
            session.proxies.update({"http": proxy, "https": proxy})


def get_dola_proxy(session: requests.Session | None = None) -> str:
    return str(getattr(session, "_dola_api_proxy", "") or "") if session is not None else ""


def is_login_pure(session: requests.Session | None = None) -> bool:
    return bool(getattr(session, "_dola_login_pure", False)) if session is not None else False


def attach_login_pure(session: requests.Session, state: dict[str, Any], proxy: str = "") -> None:
    attach_login_state(session, state)
    setattr(session, "_dola_login_pure", True)
    attach_dola_proxy(session, proxy)


def can_connect(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def normalize_proxy_url(proxy: str = "") -> str:
    """Normalize CLI/env proxy values for requests.

    Accepts full URLs (http://, socks5://, socks5h://) and the common
    short form host:port.  "none/direct/off/no" disables proxy use.
    """
    proxy = (proxy or "").strip()
    if not proxy:
        return ""
    if proxy.lower() in {"none", "direct", "off", "no"}:
        return ""
    if "://" not in proxy:
        proxy = "http://" + proxy
    return proxy


def guess_chrome_proxy(cdp_url: str = "http://127.0.0.1:9555") -> str:
    """Chrome in this challenge runs through the local HTTP proxy on 7897."""
    env_proxy = normalize_proxy_url(os.environ.get("DOLA_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "")
    if env_proxy:
        return env_proxy
    if can_connect("127.0.0.1", 7897):
        return "http://127.0.0.1:7897"
    return ""


def resolve_session_proxy(proxy: str = "", *, login_pure: bool = False, cdp_url: str = "") -> str:
    """Per-account proxy only. Empty / 'direct' is this machine's IP — never DOLA_PROXY / HTTP_PROXY / Clash 7897."""
    del login_pure, cdp_url
    raw = (proxy or "").strip()
    if raw.lower() in {"", "none", "direct", "off", "no"}:
        return ""
    return normalize_proxy_url(raw)


def apply_state_to_ctx(ctx: dict[str, str], state: dict[str, Any]) -> None:
    saved = state.get("ctx") if isinstance(state.get("ctx"), dict) else {}
    for k in ("fp", "web_id", "tea_uuid"):
        if saved.get(k):
            ctx[k] = str(saved[k])


def apply_anon_id_to_ctx(ctx: dict[str, str], data: dict[str, Any]) -> None:
    web_id = str(data.get("web_id") or "")
    uid = str(data.get("uid") or "")
    if web_id and web_id != "0":
        ctx["web_id"] = web_id
        ctx["tea_uuid"] = web_id
    if uid:
        ctx["uid"] = uid


def ctx_for_endpoint(ctx: dict[str, str], endpoint: str) -> dict[str, str]:
    """Live SPA leaves region/sys_region empty on get_web_anon_id."""
    if "get_web_anon_id" not in endpoint:
        return ctx
    copied = dict(ctx)
    copied["region"] = ""
    copied["sys_region"] = ""
    return copied


def extract_web_uid(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    uid = str(data.get("uid") or "")
    if uid:
        return uid
    nested = data.get("data")
    if isinstance(nested, dict):
        uid = str(nested.get("uid") or "")
        if uid:
            return uid
    web_id = str(data.get("web_id") or "")
    if web_id and web_id != "0":
        return web_id
    return ""


def agw_login_flag(resp: Any) -> str:
    headers = getattr(resp, "headers", None) or {}
    try:
        items = headers.items()
    except Exception:
        return ""
    for key, value in items:
        if str(key).lower() == "x-tt-agw-login":
            return str(value or "").strip()
    return ""


def dola_api_post_response(
    session: requests.Session,
    ctx: dict[str, str],
    endpoint: str,
    body: dict[str, Any] | None = None,
    *,
    label: str | None = None,
    include_fp: bool = False,
    referer: str | None = None,
    content_type: str = "application/json",
    agw_js_conv: str = "str",
    accept: str = "application/json, text/plain, */*",
    extra_headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> BrowserFetchResponse | requests.Response:
    body = body or {}
    body_text = compact_json(body)
    headers_for_sign = {"Content-Type": content_type, "agw-js-conv": agw_js_conv}
    if extra_headers:
        for k in ("Last-Event-ID",):
            if k in extra_headers:
                headers_for_sign[k] = extra_headers[k]
    url = build_url(endpoint, ctx_for_endpoint(ctx, endpoint), include_fp=include_fp)
    cdp = get_login_cdp(session)
    if cdp is None:
        sign_cookies = dict(parse_cookie_header_pairs(refresh_login_cookie_header(session))) if is_login_pure(session) else cookie_dict(session)
        url = sign_url(url, headers=headers_for_sign, body=body_text, cookies=sign_cookies)
        headers = {
            **headers_base(referer or f"{BASE}/chat/create-image"),
            **ajax_headers(),
            "Accept": accept,
            "Content-Type": content_type,
            "agw-js-conv": agw_js_conv,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            **(extra_headers or {}),
        }
        return send_with_cookie_header(
            session,
            "POST",
            url,
            headers=headers,
            data=body_text,
            timeout=timeout,
        )
    js_headers = {
        "Accept": accept,
        "Content-Type": content_type,
        "agw-js-conv": agw_js_conv,
        **(extra_headers or {}),
    }
    return cdp.post_json(url, body_text, js_headers, timeout=timeout)


def dola_api_post_json(
    session: requests.Session,
    ctx: dict[str, str],
    endpoint: str,
    body: dict[str, Any] | None = None,
    *,
    label: str | None = None,
    include_fp: bool = False,
    referer: str | None = None,
    content_type: str = "application/json",
    agw_js_conv: str = "str",
    accept: str = "application/json, text/plain, */*",
    extra_headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    resp = dola_api_post_response(
        session,
        ctx,
        endpoint,
        body,
        label=label,
        include_fp=include_fp,
        referer=referer,
        content_type=content_type,
        agw_js_conv=agw_js_conv,
        accept=accept,
        extra_headers=extra_headers,
        timeout=timeout,
    )
    data = request_json(resp, label or endpoint)
    assert_ok(data, label or endpoint)
    if "get_web_anon_id" in str(label or endpoint):
        apply_anon_id_to_ctx(ctx, data)
    return data


def dola_post_json_raw(
    session: requests.Session,
    ctx: dict[str, str],
    bh: dict[str, str],
    endpoint: str,
    body: dict[str, Any] | None = None,
    *,
    label: str | None = None,
    include_fp: bool = False,
    referer: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """签名并提交 Dola JSON POST，返回完整原始诊断信息。

    这个 helper 不对业务 code 做 assert，方便调试 /alice/user_voice/*
    这类接口时直接看到后端返回。
    """
    body = body or {}
    body_text = compact_json(body)
    signed = sign_url(
        build_url(endpoint, ctx_for_endpoint(ctx, endpoint), include_fp=include_fp),
        headers={"Content-Type": "application/json", "agw-js-conv": "str"},
        body=body_text,
        cookies=dict(parse_cookie_header_pairs(refresh_login_cookie_header(session))) if is_login_pure(session) else cookie_dict(session),
    )
    request_headers = {
        **bh,
        **ajax_headers(),
        "Accept": "application/json, text/plain, */*",
        "Referer": referer or f"{BASE}/chat/create-image",
        "Content-Type": "application/json",
        "agw-js-conv": "str",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    response = send_with_cookie_header(
        session,
        "POST",
        signed,
        headers=request_headers,
        data=body_text,
        timeout=timeout,
    )
    item: dict[str, Any] = {
        "label": label or endpoint,
        "endpoint": endpoint,
        "http_status": response.status_code,
        "request_body": body,
    }
    try:
        item["data"] = response.json()
    except ValueError:
        item["text"] = response.text[:4000]
    return item


def dola_post_json(
    session: requests.Session,
    ctx: dict[str, str],
    bh: dict[str, str],
    endpoint: str,
    body: dict[str, Any] | None = None,
    *,
    label: str | None = None,
    include_fp: bool = False,
    referer: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    item = dola_post_json_raw(
        session,
        ctx,
        bh,
        endpoint,
        body,
        label=label,
        include_fp=include_fp,
        referer=referer,
        timeout=timeout,
    )
    if item.get("http_status", 0) >= 400:
        raise DolaError(f"{label or endpoint} HTTP {item.get('http_status')}: {item.get('text') or item.get('data')}")
    data = item.get("data")
    if not isinstance(data, dict):
        raise DolaError(f"{label or endpoint} did not return JSON: {item.get('text', '')[:800]}")
    return data


def assert_ok(data: dict[str, Any], label: str) -> None:
    if str(data.get("code")) == "712017001" or str(data.get("status_code")) == "712017001":
        raise DolaNoDataError(f"{label} returned no data: {json.dumps(data, ensure_ascii=False)[:1000]}")
    if data.get("code") not in (None, 0, 2000, "0", "2000"):
        raise DolaError(f"{label} returned error: {json.dumps(data, ensure_ascii=False)[:1000]}")
    if data.get("status_code") not in (None, 0, "0"):
        raise DolaError(f"{label} returned error: {json.dumps(data, ensure_ascii=False)[:1000]}")


def proxy_dict(proxy: str = "") -> dict[str, str] | None:
    proxy = normalize_proxy_url(proxy or "")
    return {"http": proxy, "https": proxy} if proxy else None


def ajax_headers() -> dict[str, str]:
    match = re.search(r"(?:Chrome|HeadlessChrome)/(\d+)", UA)
    chrome_major = match.group(1) if match else "149"
    return {
        "sec-ch-ua": f'"Google Chrome";v="{chrome_major}", "Chromium";v="{chrome_major}", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "priority": "u=1, i",
    }


def maybe_json(value: Any) -> Any:
    if isinstance(value, str) and value and value[0] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def walk(obj: Any):
    obj = maybe_json(obj)
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def add_unique(items: list[str], value: Any, limit: int = 500) -> None:
    if value is None:
        return
    text = str(value).strip()
    if not text or text.lower() in {"ok", "success", "succeeded", "0"}:
        return
    text = text[:limit]
    if text not in items:
        items.append(text)


def video_url_key(url: str) -> str:
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"download", "filename"}
    ]
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{urlencode(sorted(query))}"


def normalize_text(raw: Any) -> str:
    return str(raw or "").replace("&amp;", "&").replace("\\u0026", "&").replace("\\/", "/")


def fix_mojibake_text(value: Any) -> str:
    """修复 Dola SSE 中偶发的 UTF-8/latin1 mojibake。

    例如：
    - å°±ä¸º... -> 就为...
    - 宸茬粡... -> 已经...
    """
    text = str(value or "")
    if not text:
        return ""
    candidates = [text]
    for enc in ("latin1", "cp1252"):
        try:
            decoded = text.encode(enc, errors="ignore").decode("utf-8", errors="ignore")
        except Exception:
            continue
        if decoded and decoded not in candidates:
            candidates.append(decoded)

    def score(candidate: str) -> int:
        cjk = sum(1 for ch in candidate if "\u4e00" <= ch <= "\u9fff")
        bad = candidate.count("�") * 5 + candidate.count("?")
        moj = sum(candidate.count(ch) for ch in "åäæçèéêëìíîïðñòóôõöøùúûüý宸茬粡鐨勬垜鍙浠")
        return cjk * 4 - bad - moj

    return max(candidates, key=score)


def compact_json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return str(value)


def normalize_ratio(ratio: str | None) -> str | None:
    """Normalize Dola aspect ratio aliases to values accepted by chat_ability."""
    if ratio is None:
        return None
    value = str(ratio).strip()
    if not value:
        return None
    compact = re.sub(r"\s+", "", value).strip("，,。.;；")
    lower = compact.lower()
    aliases = {
        "landscape": "16:9",
        "horizontal": "16:9",
        "wide": "16:9",
        "横屏": "16:9",
        "宽屏": "16:9",
        "portrait": "9:16",
        "vertical": "9:16",
        "竖屏": "9:16",
        "square": "1:1",
        "方形": "1:1",
        "正方形": "1:1",
    }
    if lower in aliases:
        return aliases[lower]
    if compact in aliases:
        return aliases[compact]
    match = re.search(r"(\d+)\s*[:：xX×]\s*(\d+)", compact)
    if match:
        normalized = f"{int(match.group(1))}:{int(match.group(2))}"
        return normalized if normalized in VALID_RATIOS else normalized
    return compact


def normalize_duration(value: int | float | str, default: int = 5) -> int:
    """Dola UI currently exposes 5/10/15/30 second video durations."""
    try:
        duration = int(float(value))
    except (TypeError, ValueError):
        duration = int(default)
    if duration in VALID_DURATIONS:
        return duration
    return min(VALID_DURATIONS, key=lambda item: (abs(item - duration), item))


def normalize_image_paths(raw: str | Path | list[str | Path] | tuple[str | Path, ...]) -> list[Path]:
    if isinstance(raw, (str, Path)):
        items: list[str | Path] = [raw]
    else:
        items = list(raw or [])
    paths: list[Path] = []
    for item in items:
        # 兼容 "--image a,b" 这种手工传法；正常推荐 "--image a b"。
        parts = [str(item)]
        if isinstance(item, str) and "," in item and not Path(item).exists():
            parts = [part for part in item.split(",") if part.strip()]
        for part in parts:
            path = Path(part).expanduser().resolve()
            if not path.exists():
                raise DolaError(f"image not found: {path}")
            paths.append(path)
    if not paths:
        raise DolaError("at least one image is required")
    return paths


def hmac_sha256(key: bytes, data: str | bytes) -> bytes:
    return hmac.new(key, data.encode() if isinstance(data, str) else data, hashlib.sha256).digest()


def sign_imagex(method: str, url: str, access_key: str, secret_key: str, session_token: str, body: bytes = b"", include_payload_header: bool = False) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    parsed = urlsplit(url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    enc = lambda v: quote(str(v), safe="-_.~")
    canonical_query = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(query_pairs))
    hdrs = {"x-amz-date": amz_date, "x-amz-security-token": session_token}
    if include_payload_header:
        hdrs["x-amz-content-sha256"] = hashlib.sha256(body).hexdigest()
    signed_headers = sorted(hdrs)
    canonical_headers = "".join(f"{k}:{hdrs[k]}\n" for k in signed_headers)
    payload_hash = hdrs.get("x-amz-content-sha256") or hashlib.sha256(body).hexdigest()
    canonical_request = "\n".join([method.upper(), parsed.path or "/", canonical_query, canonical_headers, ";".join(signed_headers), payload_hash])
    scope = f"{date_stamp}/{IMAGEX_REGION}/{IMAGEX_SERVICE}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    signing_key = hmac_sha256(hmac_sha256(hmac_sha256(hmac_sha256(("AWS4" + secret_key).encode(), date_stamp), IMAGEX_REGION), IMAGEX_SERVICE), "aws4_request")
    sig = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    hdrs["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(signed_headers)}, Signature={sig}"
    return hdrs


def read_image_size(path: Path) -> tuple[int, int, str]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise DolaError(f"empty image file: {path}")
    with path.open("rb") as f:
        header = f.read(64)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big"), "png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return int.from_bytes(header[6:8], "little"), int.from_bytes(header[8:10], "little"), "gif"
    if header.startswith(b"\xff\xd8"):
        with path.open("rb") as f:
            f.read(2)
            while True:
                marker_prefix = f.read(1)
                if not marker_prefix:
                    break
                if marker_prefix != b"\xff":
                    continue
                marker = f.read(1)
                while marker == b"\xff":
                    marker = f.read(1)
                if marker in (b"\xc0", b"\xc1", b"\xc2", b"\xc3", b"\xc5", b"\xc6", b"\xc7", b"\xc9", b"\xca", b"\xcb", b"\xcd", b"\xce", b"\xcf"):
                    length = int.from_bytes(f.read(2), "big")
                    if length < 7:
                        break
                    f.read(1)
                    h = int.from_bytes(f.read(2), "big")
                    w = int.from_bytes(f.read(2), "big")
                    return w, h, "jpg"
                if marker in (b"\xd8", b"\xd9"):
                    continue
                lb = f.read(2)
                if len(lb) < 2:
                    break
                f.seek(max(int.from_bytes(lb, "big") - 2, 0), os.SEEK_CUR)
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        with path.open("rb") as f:
            f.seek(12)
            while True:
                chunk = f.read(8)
                if len(chunk) < 8:
                    break
                ctype, csize = chunk[:4], int.from_bytes(chunk[4:8], "little")
                data = f.read(csize)
                if csize % 2:
                    f.read(1)
                if ctype == b"VP8X" and len(data) >= 10:
                    return int.from_bytes(data[4:7], "little") + 1, int.from_bytes(data[7:10], "little") + 1, "webp"
                if ctype == b"VP8L" and len(data) >= 5:
                    bits = int.from_bytes(data[1:5], "little")
                    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, "webp"
                if ctype == b"VP8 " and len(data) >= 10:
                    return int.from_bytes(data[6:8], "little") & 0x3FFF, int.from_bytes(data[8:10], "little") & 0x3FFF, "webp"
    raise DolaError(f"unsupported image format: {path}")


def extension_for(path: Path, fmt: str) -> str:
    return path.suffix.lower() or (".jpg" if fmt == "jpg" else f".{fmt}")


def compact_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def prepare_upload(session: requests.Session, ctx: dict[str, str], bh: dict[str, str], resource_type: int = 2) -> dict[str, Any]:
    body = {"tenant_id": "5", "scene_id": "4", "resource_type": resource_type}
    data = dola_api_post_json(
        session,
        ctx,
        "/alice/resource/prepare_upload",
        body,
        label="prepare_upload",
        referer=f"{BASE}/chat/create-image",
    )
    if "data" not in data:
        raise DolaError(f"prepare_upload returned no data: {json.dumps(data, ensure_ascii=False)[:800]}")
    return data["data"]


def upload_image(session: requests.Session, ctx: dict[str, str], bh: dict[str, str], img: Path) -> dict[str, Any]:
    width, height, fmt = read_image_size(img)
    img_bytes = img.read_bytes()
    ext = extension_for(img, fmt)
    log(f"5. prepare_upload")
    upload_info = prepare_upload(session, ctx, bh, resource_type=2)
    creds = upload_info["upload_auth_token"]
    upload_host = upload_info["upload_host"]
    service_id = upload_info["service_id"]
    log(f"   service_id={service_id}")

    log("6. ApplyImageUpload")
    params = {"Action": "ApplyImageUpload", "Version": "2018-08-01", "ServiceId": service_id, "FileSize": str(len(img_bytes)), "FileExtension": ext, "s": random_s()}
    apply_url = f"https://{upload_host}/?{urlencode(params)}"
    h = sign_imagex("GET", apply_url, creds["access_key"], creds["secret_key"], creds["session_token"])
    r = session.get(apply_url, headers={**h, "Accept": "*/*", "Origin": BASE, "Referer": f"{BASE}/", "Cache-Control": "no-cache", "Pragma": "no-cache"}, timeout=30)
    ad = request_json(r, "ApplyImageUpload")
    if "Result" not in ad:
        raise DolaError(f"ApplyImageUpload unexpected: {json.dumps(ad, ensure_ascii=False)[:800]}")
    upload_addr = ad["Result"]["UploadAddress"]
    store = upload_addr["StoreInfos"][0]
    store_uri = store["StoreUri"]
    tos_host = upload_addr["UploadHosts"][0]
    log(f"   store_uri={store_uri[:80]}")

    log("7. Upload TOS")
    tos_url = f"https://{tos_host}/upload/v1/{store_uri}"
    r = session.post(
        tos_url,
        headers={
            "Accept": "*/*", "Authorization": store["Auth"], "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{img.name}"',
            "Content-CRC32": f"{zlib.crc32(img_bytes) & 0xffffffff:08x}",
            "Origin": BASE, "Referer": f"{BASE}/", "Cache-Control": "no-cache", "Pragma": "no-cache",
        },
        data=img_bytes,
        timeout=120,
    )
    td = request_json(r, "TOS upload")
    assert_ok(td, "TOS upload")
    log("   OK")

    log("8. CommitImageUpload")
    commit_url = f"https://{upload_host}/?{urlencode({'Action': 'CommitImageUpload', 'Version': '2018-08-01', 'ServiceId': service_id})}"
    commit_body = compact_json({"SessionKey": upload_addr["SessionKey"]}).encode()
    ch = sign_imagex("POST", commit_url, creds["access_key"], creds["secret_key"], creds["session_token"], body=commit_body, include_payload_header=True)
    r = session.post(commit_url, headers={**ch, "Accept": "*/*", "Origin": BASE, "Referer": f"{BASE}/", "Content-Type": "application/json", "Cache-Control": "no-cache", "Pragma": "no-cache"}, data=commit_body, timeout=30)
    cd = request_json(r, "CommitImageUpload")
    if "Result" not in cd:
        raise DolaError(f"CommitImageUpload unexpected: {json.dumps(cd, ensure_ascii=False)[:800]}")
    result = cd["Result"]
    plugin = (result.get("PluginResult") or [{}])[0]
    committed_uri = plugin.get("ImageUri") or plugin.get("SourceUri") or store_uri
    log("   OK")
    return {"name": img.name, "uri": committed_uri, "width": int(plugin.get("ImageWidth") or width), "height": int(plugin.get("ImageHeight") or height), "format": str(plugin.get("ImageFormat") or fmt)}


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_audio(
    session: requests.Session,
    ctx: dict[str, str],
    bh: dict[str, str],
    audio_path: str | Path,
    resource_type: int = 1,
    *,
    convert_non_mp3: bool = True,
) -> UploadedAudio:
    """上传音频资源。

    Dola 普通 chat 里的“记住音频/参考音色”对 wav 不稳定：附件能进会话，
    但后续模型常回复“文件类型暂不兼容”。普通聊天附件默认对齐 main.py：
    非 mp3 先转 mp3。

    注意：/alice/user_voice/create 的 UGC 音色克隆链路实测更吃原始 wav，
    因此诊断/创建 UGC voice 时要传 convert_non_mp3=False，避免把可用
    wav 先转成不被 voice create 接受的 mp3。
    """
    path = Path(audio_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise DolaError(f"audio not found: {path}")
    upload_path = path
    cleanup_path: Path | None = None
    upload_name = path.name
    ext = path.suffix.lower() or ".mp3"
    if ext != ".mp3" and convert_non_mp3:
        log(f"Converting audio {path.name} to mp3 for Dola chat reference")
        try:
            upload_path = convert_audio_to_mp3(path)
            cleanup_path = upload_path
            upload_name = path.with_suffix(".mp3").name
            ext = ".mp3"
        except DolaError as exc:
            log(f"WARNING: {exc}; fallback to original {path.suffix or 'audio'} upload, Dola may only ACK it without a normal assistant reply.")

    try:
        audio_bytes = upload_path.read_bytes()
        audio_format = ext.lstrip(".") or "mp3"
        audio_md5 = hashlib.md5(audio_bytes).hexdigest()

        log(f"Audio upload: {upload_name} ({len(audio_bytes)} bytes, {audio_format})")
        log(f"A1. prepare_upload(resource_type={resource_type})")
        upload_info = prepare_upload(session, ctx, bh, resource_type=resource_type)
        creds = upload_info["upload_auth_token"]
        upload_host = upload_info["upload_host"]
        service_id = upload_info["service_id"]
        log(f"   service_id={service_id}")

        log("A2. ApplyImageUpload for audio")
        params = {
            "Action": "ApplyImageUpload",
            "Version": "2018-08-01",
            "ServiceId": service_id,
            "FileSize": str(len(audio_bytes)),
            "FileExtension": ext,
            "s": random_s(),
        }
        apply_url = f"https://{upload_host}/?{urlencode(params)}"
        h = sign_imagex("GET", apply_url, creds["access_key"], creds["secret_key"], creds["session_token"])
        r = session.get(apply_url, headers={**h, "Accept": "*/*", "Origin": BASE, "Referer": f"{BASE}/", "Cache-Control": "no-cache", "Pragma": "no-cache"}, timeout=30)
        ad = request_json(r, "ApplyImageUpload(audio)")
        if "Result" not in ad:
            raise DolaError(f"ApplyImageUpload(audio) unexpected: {json.dumps(ad, ensure_ascii=False)[:800]}")
        upload_addr = ad["Result"]["UploadAddress"]
        store = upload_addr["StoreInfos"][0]
        store_uri = store["StoreUri"]
        tos_host = upload_addr["UploadHosts"][0]
        log(f"   store_uri={store_uri[:100]}")

        log("A3. Upload audio TOS")
        tos_url = f"https://{tos_host}/upload/v1/{store_uri}"
        tos_headers = {
            "Accept": "*/*",
            "Authorization": store["Auth"],
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{upload_name}"',
            "Content-CRC32": f"{zlib.crc32(audio_bytes) & 0xffffffff:08x}",
            "Origin": BASE,
            "Referer": f"{BASE}/",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        r: requests.Response | None = None
        last_upload_error = ""
        for attempt in range(1, 4):
            try:
                r = session.post(tos_url, headers=tos_headers, data=audio_bytes, timeout=120)
                if r.status_code < 500:
                    break
                last_upload_error = f"HTTP {r.status_code}: {r.text[:200]}"
            except requests.exceptions.RequestException as exc:
                last_upload_error = str(exc).replace("\n", " ")[:500]
            if attempt < 3:
                wait_s = 2 * attempt
                log(f"   TOS audio upload failed ({last_upload_error}); retry {attempt + 1}/3 in {wait_s}s")
                time.sleep(wait_s)
        if r is None:
            raise DolaError(f"TOS upload(audio) failed: {last_upload_error}")
        td = request_json(r, "TOS upload(audio)")
        assert_ok(td, "TOS upload(audio)")
        log("   OK")

        log("A4. CommitImageUpload for audio")
        commit_url = f"https://{upload_host}/?{urlencode({'Action': 'CommitImageUpload', 'Version': '2018-08-01', 'ServiceId': service_id})}"
        commit_body = compact_json({"SessionKey": upload_addr["SessionKey"]}).encode()
        ch = sign_imagex("POST", commit_url, creds["access_key"], creds["secret_key"], creds["session_token"], body=commit_body, include_payload_header=True)
        r = session.post(commit_url, headers={**ch, "Accept": "*/*", "Origin": BASE, "Referer": f"{BASE}/", "Content-Type": "application/json", "Cache-Control": "no-cache", "Pragma": "no-cache"}, data=commit_body, timeout=30)
        cd = request_json(r, "CommitImageUpload(audio)")
        if "Result" not in cd:
            raise DolaError(f"CommitImageUpload(audio) unexpected: {json.dumps(cd, ensure_ascii=False)[:800]}")
        plugin = (cd["Result"].get("PluginResult") or [{}])[0]
        committed_uri = plugin.get("ImageUri") or plugin.get("SourceUri") or store_uri
        content_type = plugin.get("ContentType") or ""
        log(f"   OK content_type={content_type or 'unknown'}")

        return UploadedAudio(
            name=upload_name,
            uri=committed_uri,
            audio_format=audio_format,
            size=len(audio_bytes),
            md5=audio_md5,
            identifier=str(uuid.uuid1()),
            file_type=3,
            resource_type=resource_type,
        )
    finally:
        if cleanup_path:
            try:
                cleanup_path.unlink(missing_ok=True)
            except Exception:
                pass


def audio_summary(audio: UploadedAudio) -> dict[str, Any]:
    return {
        "name": audio.name,
        "uri": audio.uri,
        "audio_format": audio.audio_format,
        "size": audio.size,
        "md5": audio.md5,
        "identifier": audio.identifier,
        "file_type": audio.file_type,
        "resource_type": audio.resource_type,
        "inline": audio.inline,
    }


def audio_file_entity(audio: UploadedAudio) -> dict[str, Any]:
    return {
        "entity_type": 1,
        "entity_content": {
            "file": {
                "file_name": audio.name,
                "key": audio.uri,
                "size": audio.size,
                "file_type": audio.file_type,
                "md5": audio.md5,
            }
        },
        "identifier": audio.identifier or str(uuid.uuid1()),
    }


def pre_handle_audio_attachment(
    session: requests.Session,
    ctx: dict[str, str],
    bh: dict[str, str],
    audio: UploadedAudio,
    *,
    conversation_id: str = "",
    section_id: str = "",
    local_message_id: str = "",
    pre_generate_id: str = "",
) -> tuple[str | None, str]:
    """注册音频附件，参考 main.py 的 pre_handle_v2/pre_handle_v2_without_conv。"""
    if audio.inline or audio.uri.startswith("data:"):
        return None, pre_generate_id
    local_message_id = local_message_id or str(uuid.uuid1())
    entity = audio_file_entity(audio)
    if conversation_id and section_id:
        endpoint = "/alice/message/pre_handle_v2"
        body: dict[str, Any] = {
            "uplink_entity": entity,
            "conversation_id": conversation_id,
            "bot_id": BOT_ID,
            "section_id": section_id,
            "local_message_id": local_message_id,
        }
    else:
        endpoint = "/alice/message/pre_handle_v2_without_conv"
        body = {
            "uplink_entity": entity,
            "bot_id": BOT_ID,
            "local_message_id": local_message_id,
        }
        if pre_generate_id:
            body["pre_generate_id"] = pre_generate_id

    try:
        data = dola_api_post_json(
            session,
            ctx,
            endpoint,
            body,
            label=endpoint,
            referer=f"{BASE}/chat/{conversation_id or ''}",
        )
        returned_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        returned_pre_generate_id = str(returned_data.get("pre_generate_id") or pre_generate_id or "")
    except Exception as exc:
        return f"{audio.name}: {endpoint} failed: {str(exc).replace(chr(10), ' ')[:300]}", pre_generate_id
    return None, returned_pre_generate_id


def pre_handle_audio_attachments(
    session: requests.Session,
    ctx: dict[str, str],
    bh: dict[str, str],
    audios: list[UploadedAudio],
    *,
    conversation_id: str = "",
    section_id: str = "",
    local_message_id: str = "",
    pre_generate_id: str = "",
) -> tuple[list[str], str]:
    warnings: list[str] = []
    current_pre_generate_id = pre_generate_id
    for audio in audios:
        warning, returned_pre_generate_id = pre_handle_audio_attachment(
            session,
            ctx,
            bh,
            audio,
            conversation_id=conversation_id,
            section_id=section_id,
            local_message_id=local_message_id,
            pre_generate_id=current_pre_generate_id,
        )
        if not conversation_id and not section_id and returned_pre_generate_id and not current_pre_generate_id:
            current_pre_generate_id = returned_pre_generate_id
            warning, returned_pre_generate_id = pre_handle_audio_attachment(
                session,
                ctx,
                bh,
                audio,
                conversation_id=conversation_id,
                section_id=section_id,
                local_message_id=local_message_id,
                pre_generate_id=current_pre_generate_id,
            )
        if returned_pre_generate_id:
            current_pre_generate_id = returned_pre_generate_id
        if warning:
            warnings.append(warning)
            log(f"WARNING: {warning}")
        else:
            log(f"Registered audio attachment: {audio.name}")
    return warnings, current_pre_generate_id


def build_attachment(image: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": 1,
        "identifier": str(uuid.uuid1()),
        "image": {
            "name": image["name"],
            "uri": image["uri"],
            "image_ori": {"url": "", "width": image["width"], "height": image["height"], "format": "", "url_formats": {}},
        },
        "parse_state": 0,
        "review_state": 1,
        "upload_status": 1,
        "progress": 100,
        "src": "",
    }


def build_audio_attachment(audio: UploadedAudio) -> dict[str, Any]:
    return {
        "type": 2,
        "identifier": audio.identifier or str(uuid.uuid1()),
        "file": {
            "name": audio.name,
            "uri": audio.uri,
            "file_name": audio.name,
            "key": audio.uri,
            "file_type": audio.file_type,
            "audio_format": audio.audio_format,
            "size": audio.size,
            "md5": audio.md5,
            "url": "" if not audio.uri.startswith("data:") else audio.uri,
        },
        "parse_state": 1,
        "review_state": 1,
        "upload_status": 1,
        "progress": 100,
        "src": "",
    }


def build_attachment_block(images: list[dict[str, Any]], audios: list[UploadedAudio] | None = None) -> dict[str, Any]:
    attachments: list[dict[str, Any]] = [build_attachment(image) for image in images]
    attachments.extend(build_audio_attachment(audio) for audio in (audios or []))
    return {
        "block_type": 10052,
        "content": {
            "attachment_block": {"attachments": attachments},
            "pc_event_block": "",
        },
        "block_id": str(uuid.uuid4()),
        "parent_id": "",
        "meta_info": [],
        "append_fields": [],
    }


def build_text_block(text: str) -> dict[str, Any]:
    return {
        "block_type": 10000,
        "content": {
            "text_block": {"text": text, "icon_url": "", "icon_url_dark": "", "summary": ""},
            "pc_event_block": "",
        },
        "block_id": str(uuid.uuid4()),
        "parent_id": "",
        "meta_info": [],
        "append_fields": [],
    }


def build_prompt_text(prompt: str, ratio: str | None) -> str:
    prompt = prompt or ""
    ratio_value = normalize_ratio(ratio)
    if ratio_value and ratio_value not in prompt and "画面比例" not in prompt:
        return f"生成视频：{prompt}，{ratio_value}"
    return f"生成视频：{prompt}" if not prompt.startswith("生成视频") else prompt


def is_prompt_echo_text(text: Any) -> bool:
    """build_prompt_text 统一给提交消息加"生成视频"前缀，Dola 会把它原样回显成一条会话文本。
    回显内容就是用户提示词本身，不参与成功/失败判定，避免提示词措辞误触发额度/拆段/失败短语。"""
    return str(text or "").strip().startswith("生成视频")


def enrich_multi_image_prompt(prompt: str, image_count: int) -> str:
    """Make multi-reference-image intent explicit for Dola's video bot."""
    text = prompt or ""
    if image_count <= 1:
        return text
    if any(token in text for token in ("多图", "多张图", "参考图", "图片1", "图1", "第一张")):
        return text
    return f"参考上传的{image_count}张图片，保持主体与画面元素一致；{text}"


def build_audio_reference_prompt(
    audios: list[UploadedAudio],
    audio_prompt: str = DEFAULT_AUDIO_REFERENCE_PROMPT,
) -> str:
    """构造第一步音频记忆提示词。

    audio_prompt 可自定义；支持简单占位符：
    {audio_names} / {audio_count} / {first_audio_name}
    """
    text = (audio_prompt or DEFAULT_AUDIO_REFERENCE_PROMPT).strip()
    audio_names = "、".join(audio.name for audio in audios) if audios else ""
    replacements = {
        "{audio_names}": audio_names,
        "{audio_count}": str(len(audios)),
        "{first_audio_name}": audios[0].name if audios else "",
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def normalize_video_prompt_for_audios(prompt: str, audios: list[UploadedAudio]) -> str:
    """让第二步视频提示词显式引用音频参考。

    注意：实测第二步视频请求如果再次携带 audio 文件附件，Dola 往往只 ACK
    用户消息而不触发视频生成。因此默认只在第一步上传音频，第二步在同会话
    文本里引用“音频1/上一步音频”。
    """
    text = prompt or ""
    if not audios:
        return text
    text = text.replace("上一步的音频", "音频1").replace("上一条音频", "音频1").replace("前一步的音频", "音频1")
    if "音频1" not in text and "参考音色" in text:
        text = f"音频1是人物张三的参考音色；{text}"
    elif "音频1" not in text and "音色" in text:
        text = f"音频1是人物张三的参考音色；{text}"
    return text


def build_ability(model: str, duration: int, ratio: str | None) -> dict[str, Any]:
    duration = normalize_duration(duration, default=duration)
    if duration >= 30:
        model = "seedance_v2.5"
        duration = 30
    ability_param: dict[str, Any] = {
        "model": model,
        "duration": duration,
    }
    ratio_value = normalize_ratio(ratio)
    if ratio_value:
        ability_param["ratio"] = ratio_value
    return {
        "ability_type": 17,
        "ability_param": compact_json(ability_param),
    }


def build_payload(
    images: list[dict[str, Any]],
    prompt: str,
    ctx: dict[str, str],
    duration: int,
    model: str,
    ratio: str | None,
    audios: list[UploadedAudio] | None = None,
    *,
    conversation_id: str = "",
    section_id: str = "",
    last_message_index: int | None = None,
    need_create_conversation: bool | None = None,
    video_ability: bool = True,
    pre_generate_id: str = "",
    local_conversation_id: str | None = None,
    local_message_id: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    now_ms = int(time.time() * 1000)
    duration = normalize_duration(duration, default=5)
    if duration >= 30:
        model = "seedance_v2.5"
        duration = 30
    ratio_value = normalize_ratio(ratio) if video_ability else None
    need_create = (not conversation_id) if need_create_conversation is None else bool(need_create_conversation)
    local_conv_id = local_conversation_id or ("local_" + str(random.randint(10**15, 10**16 - 1)))
    attachment_msg_id = local_message_id or str(uuid.uuid1())
    audios = audios or []
    text_msg_id = str(uuid.uuid4()) if (images or audios) else attachment_msg_id
    collect_id = str(uuid.uuid4())
    prompt_text = build_prompt_text(prompt, ratio_value) if video_ability else (prompt or "")
    messages: list[dict[str, Any]] = []
    if images or audios:
        messages.append({
            "local_message_id": attachment_msg_id,
            "content_block": [build_attachment_block(images, audios)],
            "message_status": 0,
        })
    messages.append({
        "local_message_id": text_msg_id,
        "content_block": [build_text_block(prompt_text)],
            "message_status": 0,
        })
    option: dict[str, Any] = {
            "send_message_scene": "", "create_time_ms": now_ms, "collect_id": collect_id, "is_audio": False,
            "answer_with_suggest": False, "tts_switch": False, "need_deep_think": 0, "click_clear_context": False,
            "from_suggest": False, "is_regen": False, "is_replace": False, "is_from_click_option": False,
            "disable_sse_cache": False, "select_text_action": "", "is_select_text": False, "resend_for_regen": False,
            "scene_type": 0, "unique_key": str(uuid.uuid4()), "start_seq": 0, "need_create_conversation": need_create,
        }
    if need_create:
        option["conversation_init_option"] = {"need_ack_conversation": True}
    option.update({
            "regen_query_id": [], "edit_query_id": [],
            "regen_instruction": "", "no_replace_for_regen": False, "message_from": 0, "shared_app_name": "",
            "shared_app_id": "", "sse_recv_event_options": {"support_chunk_delta": True}, "is_ai_playground": False,
            "is_old_user": False, "recovery_option": {"is_recovery": False, "req_create_time_sec": now_ms // 1000, "append_sse_event_scene": 0},
            "message_storage_type": 0,
    })

    ext: dict[str, Any] = {
        "fp": ctx["fp"],
        "collection_id": collect_id,
        "commerce_credit_config_enable": "0",
    }
    if video_ability:
        ext.update({
            "answer_with_suggest": "0",
            "sub_conv_firstmet_type": "1",
        })
        if len(images) > 1:
            ext["multi_image_count"] = str(len(images))
        if need_create:
            ext["conversation_init_option"] = compact_json({"need_ack_conversation": True})
    else:
        # 普通 chat/记忆步骤：对齐 chat_api.txt 的请求体，不带 chat_ability。
        ext.update({
            "use_deep_think": "0",
            "sub_conv_firstmet_type": "1",
        })
        if need_create:
            ext["conversation_init_option"] = compact_json({"need_ack_conversation": True})
    if pre_generate_id:
        ext["pre_generate_id"] = pre_generate_id

    payload: dict[str, Any] = {
        "client_meta": {
            "local_conversation_id": local_conv_id if need_create else "",
            "conversation_id": conversation_id,
            "bot_id": BOT_ID,
            "last_section_id": section_id,
            "last_message_index": last_message_index,
        },
        "messages": messages,
        "option": option,
        "user_context": [],
        "ext": ext,
    }
    if video_ability:
        payload["chat_ability"] = build_ability(model, duration, ratio_value)
    return payload, local_conv_id, attachment_msg_id


def parse_sse(resp: requests.Response, timeout_sec: int) -> tuple[str, str, str, list[dict[str, Any]]]:
    """读取 chat/completion SSE。

    返回旧接口兼容的 (conversation_id, error, error_code, events)，同时 events
    保留 ACK 的 section_id/message_index 等字段供 submit_chat_completion 解析。
    """
    deadline = time.time() + timeout_sec
    cur: dict[str, Any] = {}
    data_lines: list[str] = []
    conv_id = ""
    error = ""
    error_code = ""
    events: list[dict[str, Any]] = []

    def flush_event() -> bool:
        nonlocal conv_id, error, error_code
        if not cur and not data_lines:
            return False
        raw_data = "\n".join(data_lines)
        event_name = cur.get("event", "message")
        event: dict[str, Any] = {
            "id": cur.get("id"),
            "event": event_name,
            "raw_data": raw_data,
        }
        if raw_data:
            try:
                event["data"] = json.loads(raw_data)
            except ValueError:
                event["data"] = raw_data
        events.append(event)
        data = event.get("data")
        if isinstance(data, dict):
            if data.get("error_msg"):
                error = fix_mojibake_text(data.get("error_msg") or "")
                error_code = str(data.get("error_code") or "")
                log(f"   [{event_name}] {error} ({error_code})")
            if event_name == "SSE_ACK":
                ack_meta = data.get("ack_client_meta")
                if not isinstance(ack_meta, dict):
                    ack_meta = {}
                conv_id = str(ack_meta.get("conversation_id") or conv_id or "")
                if conv_id:
                    log(f"   SUCCESS! conv_id={conv_id}")
            if event_name == "SSE_REPLY_END" and data.get("end_type") == 3:
                cur.clear()
                data_lines.clear()
                return True
        cur.clear()
        data_lines.clear()
        return False

    try:
        for raw_line in resp.iter_lines(decode_unicode=False):
            if time.time() > deadline:
                log(f"   SSE read reached {timeout_sec}s timeout; using {len(events)} events received so far.")
                break
            if raw_line is None:
                continue
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, (bytes, bytearray)) else str(raw_line)
            if line == "":
                if flush_event():
                    break
                continue
            if line.startswith("id:"):
                cur["id"] = line[3:].strip()
            elif line.startswith("event:"):
                cur["event"] = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
    except requests.exceptions.RequestException as exc:
        log(f"   SSE stream stopped early after {len(events)} events: {str(exc).replace(chr(10), ' ')[:200]}")
    flush_event()
    return conv_id, error, error_code, events


def event_data(events: list[dict[str, Any]], name: str) -> list[Any]:
    return [event.get("data") for event in events if event.get("event") == name]


def text_from_content_block(block: Any) -> str:
    if not isinstance(block, dict):
        return ""
    content = block.get("content")
    if not isinstance(content, dict):
        return ""
    text_block = content.get("text_block")
    if not isinstance(text_block, dict):
        return ""
    return fix_mojibake_text(text_block.get("text") or "")


def extract_reply_texts_from_events(events: list[dict[str, Any]]) -> list[str]:
    """从 chat/completion SSE 中提取非视频普通回复文本。

    STREAM_CHUNK 可能按 block_id 分片返回；这里按 block_id 拼回完整文本，
    同时兜底收集 ext.brief / SSE_REPLY_END.msg_finish_attr.brief。
    """
    briefs: list[str] = []
    block_order: list[str] = []
    block_texts: dict[str, str] = {}

    def add_brief(value: Any) -> None:
        if value is None:
            return
        text = fix_mojibake_text(value).strip()
        if text and text.lower() not in {"ok", "success", "succeeded", "0"} and text not in briefs:
            briefs.append(text)

    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event.get("event") == "SSE_REPLY_END":
            finish_attr = data.get("msg_finish_attr")
            if isinstance(finish_attr, dict):
                add_brief(finish_attr.get("brief"))
        if event.get("event") != "STREAM_CHUNK":
            continue
        for op in data.get("patch_op", []) or []:
            if not isinstance(op, dict):
                continue
            patch_value = op.get("patch_value")
            if not isinstance(patch_value, dict):
                continue
            ext = patch_value.get("ext")
            if isinstance(ext, dict):
                add_brief(ext.get("brief"))
            for block in patch_value.get("content_block", []) or []:
                text = text_from_content_block(block)
                if not text:
                    continue
                block_id = str(block.get("block_id") or len(block_order))
                if block_id not in block_texts:
                    block_order.append(block_id)
                    block_texts[block_id] = ""
                block_texts[block_id] += text

    texts: list[str] = []
    for block_id in block_order:
        add_unique(texts, block_texts.get(block_id), limit=4000)
    for brief in briefs:
        add_unique(texts, brief, limit=4000)
    return texts


def extract_chain_message_texts(
    obj: Any,
    *,
    min_index: int | None = None,
    exclude_texts: list[str] | None = None,
) -> list[str]:
    """从 /im/chain/single 返回中按 message index 提取文本消息。

    用于普通音频记忆步骤：ACK 中 query_list[0].message_index 是用户问题，
    因此读取 index_in_conv > message_index 的文本即可得到服务端回复。
    """
    excludes = {str(text or "").strip() for text in (exclude_texts or []) if str(text or "").strip()}
    texts: list[str] = []
    for item in walk(obj):
        if not isinstance(item, dict):
            continue
        if "index_in_conv" not in item or "content_block" not in item:
            continue
        try:
            index = int(item.get("index_in_conv"))
        except (TypeError, ValueError):
            continue
        if min_index is not None and index < min_index:
            continue
        content_blocks = item.get("content_block")
        if not isinstance(content_blocks, list):
            continue
        for block in content_blocks:
            text = text_from_content_block(block).strip()
            if not text or text in excludes:
                continue
            if set(text) == {"?"}:
                continue
            add_unique(texts, text, limit=4000)
    return texts


def extract_attachment_statuses(obj: Any) -> list[dict[str, Any]]:
    """提取链路/SSE 里的附件解析状态。

    音频记忆步骤不是视频生成，实测没有稳定的机器人文本回复；
    成功证据主要在 FULL_MSG_NOTIFY / chain 消息里的附件状态：
    upload_status=1, parse_state=1, review_state=1。
    """
    statuses: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in walk(obj):
        attachments = item.get("attachments")
        if not isinstance(attachments, list):
            continue
        for att in attachments:
            if not isinstance(att, dict):
                continue
            file_info = att.get("file") if isinstance(att.get("file"), dict) else {}
            name = str(file_info.get("name") or file_info.get("file_name") or "")
            uri = str(file_info.get("uri") or file_info.get("key") or "")
            key = (name, uri)
            if key in seen:
                continue
            seen.add(key)
            statuses.append({
                "type": att.get("type"),
                "name": name,
                "uri": uri,
                "size": file_info.get("size"),
                "md5": file_info.get("md5"),
                "file_type": file_info.get("file_type"),
                "upload_status": att.get("upload_status"),
                "parse_state": att.get("parse_state"),
                "review_state": att.get("review_state"),
                "progress": att.get("progress"),
            })
    return statuses


def audio_reference_status(result: SubmitResult, chain_data: Any = None) -> dict[str, Any]:
    statuses = extract_attachment_statuses(result.events)
    if chain_data is not None:
        for status in extract_attachment_statuses(chain_data):
            if not any(existing.get("uri") == status.get("uri") and existing.get("name") == status.get("name") for existing in statuses):
                statuses.append(status)
    ok = bool(result.conversation_id) and not result.error
    if statuses:
        ok = ok and any(
            str(status.get("upload_status")) == "1"
            and str(status.get("parse_state")) == "1"
            and str(status.get("review_state")) == "1"
            for status in statuses
        )
    return {
        "status": "accepted" if ok else "failed",
        "ok": ok,
        "attachment_statuses": statuses,
    }


def summarize_sse_events_for_log(events: list[dict[str, Any]], *, max_raw: int = 1200) -> list[dict[str, Any]]:
    """压缩打印 SSE 返回体，保留 ACK/消息/结束事件的关键内容。"""
    output: list[dict[str, Any]] = []
    for idx, event in enumerate(events):
        data = event.get("data")
        item: dict[str, Any] = {
            "idx": idx,
            "event": event.get("event"),
            "id": event.get("id"),
        }
        if isinstance(data, dict):
            if event.get("event") == "SSE_ACK":
                item["ack_client_meta"] = data.get("ack_client_meta")
                item["query_list"] = data.get("query_list")
                item["timeout_conf"] = data.get("timeout_conf")
            elif event.get("event") == "FULL_MSG_NOTIFY":
                msg = data.get("message") if isinstance(data.get("message"), dict) else {}
                item["message"] = {
                    "conversation_id": msg.get("conversation_id"),
                    "message_id": msg.get("message_id"),
                    "index_in_conv": msg.get("index_in_conv"),
                    "content_type": msg.get("content_type"),
                    "local_message_id": msg.get("local_message_id"),
                    "section_id": msg.get("section_id"),
                    "text": extract_chain_message_texts({"message": msg}, min_index=None),
                    "attachments": extract_attachment_statuses({"message": msg}),
                }
            elif event.get("event") == "STREAM_CHUNK":
                item["texts"] = extract_reply_texts_from_events([event])
                item["message_id"] = data.get("message_id")
            elif event.get("event") == "SSE_REPLY_END":
                item["data"] = data
            elif event.get("event") == "STREAM_ERROR":
                item["data"] = data
            else:
                item["data"] = data
        else:
            raw = str(event.get("raw_data") or data or "")
            item["raw_data"] = raw[:max_raw]
        output.append(item)
    return output


def submit_result_from_events(
    events: list[dict[str, Any]],
    *,
    conversation_id: str = "",
    section_id: str = "",
    local_conversation_id: str = "",
    local_message_id: str = "",
    pre_handle_warnings: list[str] | None = None,
    pre_generate_id: str = "",
) -> SubmitResult:
    ack_items = event_data(events, "SSE_ACK")
    ack = ack_items[0] if ack_items and isinstance(ack_items[0], dict) else {}
    ack_meta = ack.get("ack_client_meta", {}) if isinstance(ack, dict) else {}
    query_list = ack.get("query_list", []) if isinstance(ack, dict) else []
    first_query = query_list[0] if query_list and isinstance(query_list[0], dict) else {}
    query_message_indexes: list[int] = []
    query_question_ids: list[str] = []
    for query in query_list:
        if not isinstance(query, dict):
            continue
        if query.get("question_id"):
            query_question_ids.append(str(query.get("question_id")))
        try:
            if query.get("message_index") is not None:
                query_message_indexes.append(int(query.get("message_index")))
        except (TypeError, ValueError):
            pass
    message_index: int | None = None
    try:
        if first_query.get("message_index") is not None:
            message_index = int(first_query.get("message_index"))
    except (TypeError, ValueError):
        message_index = None

    error = ""
    error_code = ""
    for event in events:
        data = event.get("data")
        if isinstance(data, dict) and data.get("error_msg"):
            error = fix_mojibake_text(data.get("error_msg") or "")
            error_code = str(data.get("error_code") or "")

    reply_texts = extract_reply_texts_from_events(events)
    return SubmitResult(
        conversation_id=str(ack_meta.get("conversation_id") or conversation_id or ""),
        section_id=str(ack_meta.get("section_id") or section_id or ""),
        local_conversation_id=str(ack_meta.get("local_conversation_id") or local_conversation_id or ""),
        local_message_id=local_message_id,
        question_id=str(first_query.get("question_id") or ""),
        message_index=message_index,
        query_message_indexes=query_message_indexes,
        query_question_ids=query_question_ids,
        error=error,
        error_code=error_code,
        events=events,
        reply_texts=reply_texts,
        final_reply_text=reply_texts[-1] if reply_texts else "",
        pre_handle_warnings=pre_handle_warnings or [],
        pre_generate_id=pre_generate_id,
    )


def submit_chat_completion(
    session: requests.Session,
    ctx: dict[str, str],
    bh: dict[str, str],
    *,
    images: list[dict[str, Any]],
    audios: list[UploadedAudio],
    prompt: str,
    duration: int,
    model: str,
    ratio: str | None,
    timeout_sec: int,
    video_ability: bool = True,
    conversation_id: str = "",
    section_id: str = "",
    last_message_index: int | None = None,
) -> SubmitResult:
    """提交一次 chat/completion。

    - video_ability=True：视频生成请求，带 chat_ability。
    - video_ability=False：普通音频/文本消息，用于先把参考音频放进会话上下文。
    """
    need_create = not conversation_id
    local_conv_id = "local_" + str(random.randint(10**15, 10**16 - 1))
    local_message_id = str(uuid.uuid1())
    pre_handle_warnings: list[str] = []
    pre_generate_id = ""
    if audios:
        log("   pre_handle audio attachments")
        pre_handle_warnings, pre_generate_id = pre_handle_audio_attachments(
            session,
            ctx,
            bh,
            audios,
            conversation_id=conversation_id,
            section_id=section_id,
            local_message_id=local_message_id,
        )

    payload, local_ref, local_message_id = build_payload(
        images,
        prompt,
        ctx,
        duration,
        model,
        ratio,
        audios=audios,
        conversation_id=conversation_id,
        section_id=section_id,
        last_message_index=last_message_index,
        need_create_conversation=need_create,
        video_ability=video_ability,
        pre_generate_id=pre_generate_id,
        local_conversation_id=local_conv_id,
        local_message_id=local_message_id,
    )
    body_text = compact_json(payload)
    referer_id = conversation_id or local_ref
    mode_label = "video" if video_ability else "audio/text"

    log("   Posting im/chain/recent_conv before chat/completion")
    try:
        fetch_recent_conversations(session, ctx, referer_id)
    except Exception as exc:
        log(f"   recent_conv warning: {str(exc).replace(chr(10), ' ')[:300]}")

    log(f"   Posting chat/completion for {mode_label} step")
    if need_create:
        log(f"   chat target: new_chat local_conversation_id={local_ref}")
    else:
        log(f"   chat target: existing_chat conversation_id={conversation_id}, section_id={section_id}, last_message_index={last_message_index}")
    response: requests.Response | BrowserSseResponse | None = None
    last_error = ""
    max_attempts = 4
    retry_statuses = {500, 502, 503, 504}
    cdp = get_login_cdp(session)
    for attempt in range(1, max_attempts + 1):
        try:
            if cdp is not None:
                completion_url = build_url("/chat/completion", ctx, include_fp=True)
                resp = cdp.post_json(
                    completion_url,
                    body_text,
                    {
                        "Accept": "*/*",
                        "Content-Type": "application/json",
                        "agw-js-conv": "str, str",
                        "Last-Event-ID": "undefined",
                        "x-flow-trace": "04-" + uuid.uuid4().hex[:32] + "-" + uuid.uuid4().hex[:16] + "-01",
                    },
                    timeout=max(30, min(max(int(timeout_sec), 30), 360)),
                )
                response = BrowserSseResponse(resp.status_code, resp.text, resp.headers, resp.url)
            else:
                completion_url = build_url("/chat/completion", ctx, include_fp=True)
                completion_url = sign_url(
                    completion_url,
                    headers={"Content-Type": "application/json", "agw-js-conv": "str, str", "Last-Event-ID": "undefined"},
                    body=body_text,
                    cookies=cookie_dict(session),
                )
                request_headers = {
                    **bh,
                    **ajax_headers(),
                    "Referer": f"{BASE}/chat/{referer_id}",
                    "Accept": "*/*",
                    "Content-Type": "application/json",
                    "agw-js-conv": "str, str",
                    "Last-Event-ID": "undefined",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "x-flow-trace": "04-" + uuid.uuid4().hex[:32] + "-" + uuid.uuid4().hex[:16] + "-01",
                }
                response = send_with_cookie_header(
                    session,
                    "POST",
                    completion_url,
                    headers=request_headers,
                    data=body_text,
                    stream=True,
                    timeout=(30, max(10, min(max(int(timeout_sec), 10), 300))),
                )
        except requests.exceptions.RequestException as exc:
            last_error = str(exc).replace(chr(10), " ")[:800]
            if attempt < max_attempts:
                wait_s = min(2 ** attempt, 10)
                log(f"   chat/completion request failed ({last_error}); retry {attempt + 1}/{max_attempts} in {wait_s}s")
                time.sleep(wait_s)
                continue
            raise DolaError(f"chat/completion request failed after {max_attempts} attempts: {last_error}") from exc
        except Exception as exc:
            last_error = str(exc).replace(chr(10), " ")[:800]
            if attempt < max_attempts:
                wait_s = min(2 ** attempt, 10)
                log(f"   chat/completion browser request failed ({last_error}); retry {attempt + 1}/{max_attempts} in {wait_s}s")
                time.sleep(wait_s)
                continue
            raise DolaError(f"chat/completion browser request failed after {max_attempts} attempts: {last_error}") from exc

        if response.status_code in retry_statuses and attempt < max_attempts:
            body_preview = response.text[:300]
            response.close()
            wait_s = min(2 ** attempt, 10)
            log(f"   chat/completion HTTP {response.status_code}: {body_preview}; retry {attempt + 1}/{max_attempts} in {wait_s}s")
            time.sleep(wait_s)
            continue
        break

    if response is None:
        raise DolaError(f"chat/completion request failed: {last_error or 'no response'}")
    if response.status_code >= 400:
        try:
            body_preview = response.text[:800]
        finally:
            response.close()
        raise DolaError(f"chat/completion HTTP {response.status_code}: {body_preview}")
    conv_id, error, error_code, events = parse_sse(response, timeout_sec)
    response.close()
    result = submit_result_from_events(
        events,
        conversation_id=conversation_id or conv_id,
        section_id=section_id,
        local_conversation_id=local_ref,
        local_message_id=local_message_id,
        pre_handle_warnings=pre_handle_warnings,
        pre_generate_id=pre_generate_id,
    )
    if error and not result.error:
        result.error = error
    if error_code and not result.error_code:
        result.error_code = error_code
    return result


def fetch_recent_conversations(session: requests.Session, ctx: dict[str, str], referer_id: str = "", limit: int = 10, message_count_per_conv: int = 10) -> dict[str, Any]:
    body = {
        "cmd": 3200,
        "uplink_body": {
            "pull_recent_conv_chain_uplink_body": {
                "limit": limit,
                "message_count_per_conv": message_count_per_conv,
                "api_version": 1,
                "conv_version": 0,
                "direction": 3,
                "option": {
                    "not_need_message": True,
                    "need_complete_conversation": True,
                    "need_coco_conversation": True,
                    "need_coco_bot": True,
                },
            }
        },
        "sequence_id": str(uuid.uuid4()),
        "channel": 2,
        "version": "1",
    }
    return dola_api_post_json(
        session,
        ctx,
        "/im/chain/recent_conv",
        body,
        label="im/chain/recent_conv",
        referer=f"{BASE}/chat/{referer_id}" if referer_id else f"{BASE}/chat/",
        content_type="application/json; encoding=utf-8",
    )


def fetch_single_chain(session: requests.Session, ctx: dict[str, str], conversation_id: str, limit: int = 20) -> dict[str, Any]:
    body = {
        "cmd": 3100,
        "uplink_body": {
            "pull_singe_chain_uplink_body": {
                "conversation_id": conversation_id,
                "anchor_index": 9007199254740991,
                "conversation_type": 3,
                "direction": 1,
                "limit": limit,
                "ext": {},
                "filter": {"index_list": []},
                "evaluate_ab_params": "",
                "evaluate_common_params": "",
            }
        },
        "sequence_id": str(uuid.uuid4()),
        "channel": 2,
        "version": "1",
    }
    return dola_api_post_json(
        session,
        ctx,
        "/im/chain/single",
        body,
        label="im/chain/single",
        referer=f"{BASE}/chat/{conversation_id}",
        content_type="application/json; encoding=utf-8",
    )

def find_video_urls(obj: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in walk(obj):
        for key in ("download_url", "url"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")) and "video" in value:
                dedupe_key = video_url_key(value)
                if dedupe_key not in seen:
                    seen.add(dedupe_key)
                    urls.append(value)
        main_url = item.get("main_url")
        if isinstance(main_url, str) and len(main_url) > 20:
            try:
                decoded = base64.b64decode(main_url + ("=" * (-len(main_url) % 4))).decode("utf-8", errors="replace")
            except Exception:
                decoded = ""
            if decoded.startswith(("http://", "https://")):
                dedupe_key = video_url_key(decoded)
                if dedupe_key not in seen:
                    seen.add(dedupe_key)
                    urls.append(decoded)
    return urls


def find_video_vids(obj: Any) -> list[str]:
    vids: list[str] = []
    seen: set[str] = set()

    def add_vid(value: Any) -> None:
        value = str(value or "")
        if value.startswith("v") and value not in seen:
            seen.add(value)
            vids.append(value)

    if isinstance(obj, str):
        for value in re.findall(r'"(?:vid|video_id)"\s*:\s*"([^"]+)"', obj):
            add_vid(value)
    for item in walk(obj):
        for key in ("vid", "video_id"):
            if isinstance(item.get(key), str):
                add_vid(item.get(key))
    return vids


def find_latest_message_index(obj: Any) -> int | None:
    latest: int | None = None
    for item in walk(obj):
        value = item.get("index_in_conv")
        if value is None:
            continue
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if latest is None or index > latest:
            latest = index
    return latest


def find_latest_section_id(obj: Any) -> str:
    latest_index = -1
    section = ""
    for item in walk(obj):
        sid = item.get("section_id") or item.get("last_section_id")
        if not sid:
            continue
        try:
            index = int(item.get("index_in_conv")) if item.get("index_in_conv") is not None else -1
        except (TypeError, ValueError):
            index = -1
        if not section or index >= latest_index:
            latest_index = index
            section = str(sid)
    return section


def wait_for_chain_update(
    session: requests.Session,
    ctx: dict[str, str],
    conversation_id: str,
    after_message_index: int | None,
    timeout_sec: int = 60,
    interval_sec: int = 3,
) -> dict[str, Any]:
    """等待会话链路出现新消息，用于音频参考两步提交之间同步 last_message_index。"""
    deadline = time.time() + timeout_sec
    last_data: dict[str, Any] = {}
    target_index = after_message_index + 1 if after_message_index is not None else None
    next_log_time = time.time()
    consecutive_errors = 0
    while time.time() < deadline:
        try:
            data = fetch_single_chain(session, ctx, conversation_id)
        except DolaNoDataError:
            raise
        except (requests.exceptions.RequestException, DolaError) as exc:
            consecutive_errors += 1
            detail = str(exc).replace("\n", " ")[:500]
            log(f"   Chain update query failed ({consecutive_errors}/5): {detail}")
            if consecutive_errors >= 5:
                raise DolaError(f"连续 5 次查询 Dola 会话更新失败: {detail}") from exc
            time.sleep(interval_sec)
            continue
        consecutive_errors = 0
        last_data = data
        latest_index = find_latest_message_index(data)
        chain_summary = collect_reply_signals(data, include_text_blocks=True)
        has_reply_signal = bool(chain_summary["texts"] or chain_summary["errors"] or chain_summary["briefs"])
        now = time.time()
        if now >= next_log_time:
            log("   Waiting for chain update" + (f" (latest_message_index={latest_index})" if latest_index is not None else ""))
            next_log_time = now + max(10, interval_sec)
        if target_index is None:
            if has_reply_signal or latest_index is not None:
                return data
        elif latest_index is not None and latest_index >= target_index:
            return data
        time.sleep(interval_sec)
    log("   Chain update wait timed out; using latest chain data.")
    return last_data


def collect_reply_signals(obj: Any, include_text_blocks: bool = False) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "video_gen_accepted": False,
        "briefs": [],
        "errors": [],
        "texts": [],
        "creation_statuses": [],
    }
    for item in walk(obj):
        ext = item.get("ext")
        if isinstance(ext, dict):
            if ext.get("has_video_gen") == "1" or ext.get("use_creation") == "1" or ext.get("auto_create_creation") == "1":
                summary["video_gen_accepted"] = True
            for key in ("brief", "inner_err_msg", "inner_reply_err_msg"):
                target = summary["errors"] if "err" in key else summary["briefs"]
                add_unique(target, ext.get(key))
            material_info = ext.get("creation_material_info")
            if material_info and material_info not in ("{}", "[]"):
                summary["video_gen_accepted"] = True
        for key in ("error_msg", "status_desc", "inner_err_msg", "inner_reply_err_msg"):
            add_unique(summary["errors"], item.get(key))
        finish_attr = item.get("msg_finish_attr")
        if isinstance(finish_attr, dict):
            add_unique(summary["briefs"], finish_attr.get("brief"))
        if include_text_blocks:
            text_block = item.get("text_block")
            if isinstance(text_block, dict):
                add_unique(summary["texts"], text_block.get("text"), limit=2000)
        creation_block = item.get("creation_block")
        if isinstance(creation_block, dict):
            for creation in creation_block.get("creations", []) or []:
                if isinstance(creation, dict):
                    status = creation.get("status")
                    if status is not None:
                        add_unique(summary["creation_statuses"], f"creation status={status}")
                    if creation.get("video"):
                        summary["video_gen_accepted"] = True
    return summary


def extract_wait_minutes_from_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    duration_pattern = (
        r"[0-9０-９]+(?:\s*[-~～—–至到]\s*[0-9０-９]+)?\s*(?:小时|分钟)"
        r"(?:\s*[0-9０-９]+(?:\s*[-~～—–至到]\s*[0-9０-９]+)?\s*分钟)?"
    )
    patterns = (
        rf"预计等待\s*(?:约|大约|大概)?\s*({duration_pattern})",
        rf"(?:预计|预估|大约|大概|约)?\s*需要\s*({duration_pattern})",
        rf"(?:预计|预估|大约|大概|约)\s*({duration_pattern})\s*(?:左右|后)?(?:完成|生成好|生成|出结果)?",
    )
    match = None
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            break
    if not match:
        en = re.search(r"estimated wait time of\s*(\d+)\s*(minutes?|hours?)", str(text), re.I)
        if not en:
            return ""
        amount = en.group(1)
        unit = en.group(2).lower()
        return f"{amount}小时" if unit.startswith("hour") else f"{amount}分钟"
    value = match.group(1)
    value = value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[-~～—–至到]", "-", value)
    return value


def looks_like_rate_limit(text: str) -> bool:
    lower = str(text or "").lower()
    return any(
        pattern in lower
        for pattern in (
            "访问频繁",
            "访问太频繁",
            "过于频繁",
            "操作频繁",
            "请稍后重试",
            "请稍后再试",
            "请稍候重试",
            "降低配置后重试",
            "降低配置",
            "调低配置",
            "降低画质",
            "降低参数",
            "服务繁忙",
            "请求过于频繁",
            "too many requests",
            "try again later",
            "rate limit",
            "lower the settings",
            "reduce the settings",
            "lower settings and retry",
        )
    )


DURATION_SPLIT_STATUS = "duration_split"
DURATION_SPLIT_ACCEPT_REPLY = "2"
DURATION_SPLIT_CONTINUE_REPLY = "是"
DURATION_SPLIT_CONSTRAINT_REPLY = (
    "可以拆成2段生成。硬性要求：两段必须按时间顺序首尾相接，合成为一条连续的30秒视频"
    "（前15秒在前、后15秒在后），不要改画面内容。"
    "只把合成后的完整成片发给我，不要把两段成片分开发给我。"
)
DURATION_SPLIT_MERGE_PROMPT = (
    "两段已经齐了。硬性要求：请按时间顺序把两段首尾相接，合成为一条连续的30秒视频："
    "前15秒在前、后15秒在后，不要改画面内容。只把合成后的那一条完整成片发给我。"
)
MATERIAL_REFUSAL_MARKERS = (
    "请更换参考图",
    "换其它参考图",
    "换其他参考图",
    "参考图不合格",
    "参考图不符合",
    "参考图无法",
    "图片不符合",
    "图片不满足",
    "请更换图片",
    "图片格式不支持",
    "音频不支持",
    "音频不符合",
    "音频不满足",
    "请更换音频",
    "无法使用该音频",
    "音频格式不支持",
    "参考音频不符合",
    "参考音频无法",
    "以下是为你生成的图片",
    "为你生成的图片",
    "素材不合格",
    "素材不满足",
    "音视频不满足",
)
MERGE_REFUSAL_MARKERS = (
    "无法合成",
    "不能合成",
    "无法拼接",
    "不能拼接",
    "不支持拼接",
    "不支持合成",
    "请分别下载",
    "无法将两段",
    "不能合并",
    "无法合并",
)


def _compact_duration_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).replace("–", "-").replace("—", "-")


def looks_like_our_duration_split_reply(text: str) -> bool:
    compact = _compact_duration_text(text)
    return "可以拆成2段" in compact or "两段已经齐了" in compact


def looks_like_duration_confirm(text: str) -> bool:
    compact = _compact_duration_text(text)
    if not compact or looks_like_our_duration_split_reply(compact):
        return False
    if "超出了单条生成范围" in compact or "超出支持范围" in compact:
        return True
    if "是否按以下参数生成" in compact:
        return True
    if "请确认" in compact and "15秒" in compact and ("生成前" in compact or "生成后" in compact):
        return True
    if "先生成前15秒" in compact and "后15秒" in compact:
        return True
    asked_30 = "30秒" in compact or "三十秒" in compact
    if asked_30 and ("4到15秒" in compact or "4-15秒" in compact):
        return True
    if asked_30 and ("一镜到底" in compact or "按默认" in compact):
        return True
    return False


def looks_like_duration_capped(text: str) -> bool:
    """Upstream will not emit a single 30s clip (cap to 4–15s / 5s / 15s)."""
    if looks_like_duration_confirm(text):
        return True
    compact = _compact_duration_text(text)
    if not compact or looks_like_our_duration_split_reply(compact):
        return False
    if "4到15秒" in compact or "4-15秒" in compact:
        return True
    if "按默认5秒" in compact:
        return True
    if "按15秒" in compact and ("生成" in compact or "一镜到底" in compact):
        return True
    return False


def looks_like_material_rejected(text: str) -> bool:
    if looks_like_content_refusal(text):
        return True
    blob = str(text or "")
    compact = re.sub(r"\s+", "", blob)
    if not compact:
        return False
    return any(marker in blob or re.sub(r"\s+", "", marker) in compact for marker in MATERIAL_REFUSAL_MARKERS)


def looks_like_merge_refusal(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact:
        return False
    return any(re.sub(r"\s+", "", marker) in compact for marker in MERGE_REFUSAL_MARKERS)


def looks_like_generation_accepted(text: str) -> bool:
    if looks_like_duration_confirm(text):
        return False
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    if not compact:
        return False
    seedance = "seedance" in compact
    credits = any(
        token in compact
        for token in (
            "videogenerationcredit",
            "generationcredits",
            "将消耗",
            "消耗2次",
            "消耗两",
            "次视频生成",
        )
    )
    wait = bool(extract_wait_minutes_from_text(text)) or "estimatedwait" in compact or "预计等待" in compact
    send = any(
        token in compact
        for token in (
            "oncethevideoisgenerated",
            "iwillsendittoyou",
            "willsendittoyou",
            "生成完成后",
            "生成好后",
            "完成后发给",
        )
    )
    points = any(token in compact for token in ("pointsleft", "lefttoday", "今日还剩", "今天还剩", "积分"))
    if seedance and credits:
        return True
    if seedance and wait and (send or points):
        return True
    if credits and wait:
        return True
    return False


def poll_needs_full_duration_retry(poll: Any) -> bool:
    texts = [item for item in (getattr(poll, "texts", None) or []) if not is_prompt_echo_text(item)]
    if any(looks_like_generation_accepted(item) for item in texts):
        return False
    if str(getattr(poll, "status", "") or "") == DURATION_SPLIT_STATUS:
        return True
    return any(looks_like_duration_confirm(item) for item in texts)


def poll_has_duration_split(poll: Any, job_seconds: int | None = None) -> bool:
    try:
        seconds = int(job_seconds) if job_seconds is not None else None
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None and seconds < 30:
        return False
    if str(getattr(poll, "status", "") or "") == DURATION_SPLIT_STATUS:
        return True
    blobs = [
        item
        for item in list(getattr(poll, "texts", None) or []) + list(getattr(poll, "failure_reasons", None) or [])
        if not is_prompt_echo_text(item)
    ]
    if any(looks_like_duration_confirm(item) for item in blobs):
        return True
    if seconds is not None and seconds >= 30 and any(looks_like_duration_capped(item) for item in blobs):
        return True
    return False


def poll_material_rejected_text(poll: Any) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for item in list(getattr(poll, "texts", None) or []) + list(getattr(poll, "failure_reasons", None) or []):
        text = str(item or "").strip()
        if not text or text in seen or is_prompt_echo_text(text) or not looks_like_material_rejected(text):
            continue
        seen.add(text)
        parts.append(text)
    return "\n".join(parts)


def poll_merge_refused(poll: Any) -> bool:
    blobs = [
        item
        for item in list(getattr(poll, "texts", None) or []) + list(getattr(poll, "failure_reasons", None) or [])
        if not is_prompt_echo_text(item)
    ]
    return any(looks_like_merge_refusal(item) for item in blobs)


def looks_like_content_refusal(text: str) -> bool:
    blob = str(text or "").strip()
    if not blob:
        return False
    lower = blob.lower()
    return any(
        marker in blob or marker.lower() in lower
        for marker in (
            "肖像保护",
            "未认证人脸",
            "人脸暂不支持",
            "暂不支持用",
            "不支持用",
            "换其它参考图",
            "换其他参考图",
            "内容安全",
            "安全策略",
            "审核不通过",
            "拒绝生成",
            "无法为你生成",
            "不能为你生成",
            "不支持生成视频",
            "版权限制",
            "涉及版权",
            "更换输入内容",
            "copyright restriction",
            "copyrighted content",
            "involve copyright",
            "content policy",
            "face not verified",
            "unverified face",
        )
    )


def looks_like_generation_voided(text: str) -> bool:
    blob = str(text or "")
    if looks_like_daily_quota_text(blob):
        return False
    return any(
        pattern in blob
        for pattern in (
            "生成额度未扣除",
            "额度未扣除",
            "credits were not deducted",
            "credit was not deducted",
            "quota was not deducted",
        )
    )


def looks_like_generation_failure(text: str) -> bool:
    if looks_like_content_refusal(text):
        return True
    if looks_like_generation_voided(text):
        return False
    blob = str(text or "")
    if any(token in blob for token in ("无法生成", "生成失败", "调用下游服务器失败", "违规")):
        return True
    # 英文按词边界，避免 credits / generated / Seedance 等正常话术被 "error"/"fail" 子串误伤
    return bool(re.search(r"\b(?:fail(?:ed|ure)?|error|risk|safety)\b", blob, flags=re.IGNORECASE))


def looks_like_submit_retry_state(text: str) -> bool:
    lower = str(text or "").lower()
    if looks_like_daily_quota_text(text):
        return False
    return any(pattern in lower for pattern in ("以下是为你生成的图片", "为你生成的图片", "无法生成视频", "不能生成视频", "未能生成视频"))


def looks_like_daily_quota_text(text: str) -> bool:
    blob = str(text or "")
    return any(
        pattern in blob
        for pattern in (
            "今天的生成次数已经达到上限",
            "生成次数已经达到上限",
            "明天再来免费生成",
            "明天再来",
            "视频生成额度不足",
            "生成额度不足",
        )
    )


def classify_chain_outcome(failure_reasons: list[str], texts: list[str], briefs: list[str] | None = None) -> tuple[str, list[str]]:
    blobs = [
        item
        for item in list(failure_reasons) + list(texts) + list(briefs or [])
        if str(item or "").strip() and not is_prompt_echo_text(item)
    ]
    content = [item for item in blobs if looks_like_material_rejected(item)]
    if content:
        return "failed", content
    rate = [item for item in blobs if looks_like_rate_limit(item)]
    if rate:
        return "rate_limited", rate
    quota = [item for item in blobs if looks_like_daily_quota_text(item)]
    if quota:
        return "quota_exceeded", quota
    voided = [item for item in blobs if looks_like_generation_voided(item)]
    if voided:
        return "generation_voided", voided
    split = [item for item in blobs if looks_like_duration_confirm(item)]
    accepted = [item for item in blobs if looks_like_generation_accepted(item)]
    if split and not accepted:
        return DURATION_SPLIT_STATUS, split
    retry = [item for item in blobs if looks_like_submit_retry_state(item)]
    if retry:
        return "submit_retry", retry
    terminal = [item for item in blobs if looks_like_generation_failure(item)]
    if terminal:
        return "failed", terminal
    return "pending", []


def tag_chain_text(text: Any) -> str:
    """单条 chain 文本命中的规则名，顺序与 classify_chain_outcome 一致。

    用于话术回归库和运行时打点：某条规则命中突然归零/暴涨，通常是上游改了文案。
    """
    blob = str(text or "").strip()
    if not blob:
        return "empty"
    if is_prompt_echo_text(blob):
        return "prompt_echo"
    if looks_like_material_rejected(blob):
        return "content"
    if looks_like_rate_limit(blob):
        return "rate_limited"
    if looks_like_daily_quota_text(blob):
        return "quota_exceeded"
    if looks_like_generation_voided(blob):
        return "generation_voided"
    if looks_like_duration_confirm(blob):
        return "duration_split"
    if looks_like_generation_accepted(blob):
        return "accepted"
    if looks_like_submit_retry_state(blob):
        return "submit_retry"
    if looks_like_generation_failure(blob):
        return "failed"
    if looks_like_duration_capped(blob):
        return "duration_capped"
    return "none"


def inspect_video_once(session: requests.Session, ctx: dict[str, str], conversation_id: str) -> PollResult:
    """单次查询 /im/chain/single，不睡眠。供服务 Worker 把状态写回本地库。"""
    try:
        data = fetch_single_chain(session, ctx, conversation_id)
    except DolaNoDataError as exc:
        # 712017001 数据不存在：会话已失效，继续轮询无意义
        detail = str(exc).replace("\n", " ")[:800]
        log(f"   Conversation expired / no data: {detail}")
        return PollResult([], [], "conversation_expired", [detail], [], [], "")
    urls = find_video_urls(data)
    vids = find_video_vids(data)
    failure_reasons: list[str] = []
    texts: list[str] = []
    creation_statuses: list[str] = []
    wait_minutes = ""
    chain_summary = collect_reply_signals(data, include_text_blocks=True)
    for error in chain_summary["errors"]:
        add_unique(failure_reasons, error)
        log(f"   Chain error/detail: {error}")
    for index, text in enumerate(chain_summary["texts"]):
        add_unique(texts, text, limit=2000)
        if index < 5 or looks_like_duration_confirm(text) or looks_like_content_refusal(text) or looks_like_generation_accepted(text):
            log(f"   Chain text: {text}")
        if is_prompt_echo_text(text):
            continue
        parsed_wait = extract_wait_minutes_from_text(text)
        if parsed_wait and not wait_minutes:
            wait_minutes = parsed_wait
            log(f"   wait estimate: {wait_minutes}")
    for status in chain_summary["creation_statuses"]:
        add_unique(creation_statuses, status)
        log(f"   Chain {status}")
    status, reasons = classify_chain_outcome(failure_reasons, texts, chain_summary["briefs"])
    if status == "quota_exceeded":
        log("   Daily quota exhausted; worker will switch account")
        return PollResult([], [], "quota_exceeded", reasons, texts, creation_statuses, wait_minutes, data)
    if status == "generation_voided":
        log("   Generation voided without quota; worker will retry")
        return PollResult([], [], "generation_voided", reasons, texts, creation_statuses, wait_minutes, data)
    if status == DURATION_SPLIT_STATUS:
        log("   Split-duration prompt; worker will require 2-segment merge to one 30s video")
        return PollResult(urls, vids, DURATION_SPLIT_STATUS, reasons, texts, creation_statuses, wait_minutes, data)
    if urls or vids:
        return PollResult(urls, vids, "succeeded", failure_reasons, texts, creation_statuses, wait_minutes, data)
    if status != "pending":
        return PollResult([], vids, status, reasons, texts, creation_statuses, wait_minutes, data)
    return PollResult([], vids, "pending", failure_reasons, texts, creation_statuses, wait_minutes, data)


def wait_for_video(
    session: requests.Session,
    ctx: dict[str, str],
    conversation_id: str,
    timeout_sec: int = 900,
    interval_sec: int = DEFAULT_POLL_INTERVAL,
) -> PollResult:
    """轮询 /im/chain/single，返回视频 URL/VID/状态。"""
    deadline = time.time() + timeout_sec
    consecutive_errors = 0
    last = PollResult([], [], "timeout", [], [], [], "")
    while time.time() < deadline:
        try:
            last = inspect_video_once(session, ctx, conversation_id)
        except (requests.exceptions.RequestException, DolaError) as exc:
            consecutive_errors += 1
            detail = str(exc).replace("\n", " ")[:800]
            log(f"   Chain query failed ({consecutive_errors}/5): {detail}")
            if consecutive_errors >= 5:
                return PollResult([], [], "failed", [f"连续 5 次查询 Dola 任务失败: {detail}"], last.texts, last.creation_statuses, last.wait_minutes)
            time.sleep(interval_sec)
            continue
        consecutive_errors = 0
        if last.status in {
            "succeeded",
            "failed",
            "submit_retry",
            "rate_limited",
            "quota_exceeded",
            "generation_voided",
            "conversation_expired",
            DURATION_SPLIT_STATUS,
        }:
            return last
        log(f"   No video yet, polling again in {interval_sec}s")
        time.sleep(interval_sec)
    return PollResult([], [], "timeout", last.failure_reasons, last.texts, last.creation_statuses, last.wait_minutes)


def get_video_play_info(session: requests.Session, ctx: dict[str, str], vid: str) -> dict[str, Any]:
    return dola_api_post_json(
        session,
        ctx,
        "/samantha/video/get_play_info",
        {"vid": vid},
        label="samantha/video/get_play_info",
        referer=f"{BASE}/chat/",
    )


def find_play_info_urls(obj: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in walk(obj):
        for key in ("main", "backup"):
            value = item.get(key)
            if not isinstance(value, str) or not value.startswith(("http://", "https://")):
                continue
            dedupe_key = video_url_key(value)
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                urls.append(value)
    return urls


_FPLAY_URL_RE = re.compile(r"https?://[^\"'<>\\\s]+/video/fplay/[^\"'<>\\\s]+", re.I)


def is_fplay_url(raw: Any) -> bool:
    return bool(re.search(r"/video/fplay/", normalize_text(raw), re.I))


def extract_key_seed(raw: Any) -> str:
    text = normalize_text(raw)
    match = (
        re.search(r"(?:^|[?&])key_seed=([^&\"'<>\\\s]+)", text, re.I)
        or re.search(r"[\"']key_seed[\"']\s*:\s*[\"']([^\"']+)", text, re.I)
        or re.search(r"[\"']keySeed[\"']\s*:\s*[\"']([^\"']+)", text, re.I)
    )
    if not match:
        return ""
    try:
        return unquote(match.group(1))
    except Exception:
        return match.group(1)


def find_key_seed(value: Any, depth: int = 0, seen: set[int] | None = None) -> str:
    if value is None or depth > 8:
        return ""
    if isinstance(value, str):
        return extract_key_seed(value)
    if not isinstance(value, (dict, list)):
        return ""
    if seen is None:
        seen = set()
    marker = id(value)
    if marker in seen:
        return ""
    seen.add(marker)
    if isinstance(value, dict):
        for key in ("key_seed", "keySeed"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        values = list(value.values())[:120]
    else:
        values = list(value)[:80]
    for item in values:
        found = find_key_seed(item, depth + 1, seen)
        if found:
            return found
    return ""


def _scan_base64_for_fplay(value: str) -> list[str]:
    raw = str(value or "").strip()
    if len(raw) < 20 or re.search(r"[^A-Za-z0-9_+\-/=]", raw):
        return []
    variants = [raw]
    if "-" in raw or "_" in raw:
        variants.append(raw.replace("-", "+").replace("_", "/"))
    found: list[str] = []
    for candidate in variants:
        try:
            padded = candidate + ("=" * (-len(candidate) % 4))
            decoded = base64.b64decode(padded).decode("utf-8", errors="replace")
        except Exception:
            continue
        found.extend(_FPLAY_URL_RE.findall(normalize_text(decoded)))
    return found


def find_fplay_urls(obj: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        url = normalize_text(raw).strip()
        if not url or not is_fplay_url(url):
            return
        key = video_url_key(url)
        if key in seen:
            return
        seen.add(key)
        urls.append(url)

    def scan(value: Any, depth: int = 0, seen_objects: set[int] | None = None) -> None:
        if value is None or depth > 20:
            return
        if isinstance(value, str):
            text = normalize_text(value)
            for match in _FPLAY_URL_RE.findall(text):
                add(match)
            for match in _scan_base64_for_fplay(text):
                add(match)
            parsed = maybe_json(value)
            if isinstance(parsed, (dict, list)):
                scan(parsed, depth + 1, seen_objects)
            return
        value = maybe_json(value)
        if not isinstance(value, (dict, list)):
            return
        if seen_objects is None:
            seen_objects = set()
        marker = id(value)
        if marker in seen_objects:
            return
        seen_objects.add(marker)
        items = value.values() if isinstance(value, dict) else value
        for item in list(items)[:160]:
            scan(item, depth + 1, seen_objects)

    scan(obj)
    return urls


def _looks_like_video_info(value: Any) -> bool:
    value = maybe_json(value)
    if not isinstance(value, dict):
        return False
    key_text = ",".join(str(key) for key in value.keys())
    text = compact_json_text(value)
    if not re.search(r"key_seed|keySeed|fallback_api", key_text, re.I):
        return False
    if re.search(r"main_url|backup_url|video_list|video_1", key_text, re.I):
        return True
    if re.search(r"video_list|main_url|backup_url|video_1", text, re.I):
        return True
    return "qAAB" in text


def find_video_info_objects(obj: Any) -> list[dict[str, Any]]:
    infos: list[dict[str, Any]] = []
    seen_texts: set[str] = set()

    def add(value: Any) -> None:
        value = maybe_json(value)
        if not isinstance(value, dict) or not _looks_like_video_info(value):
            return
        text = compact_json_text(value)
        if text in seen_texts:
            return
        seen_texts.add(text)
        infos.append(value)

    def scan(value: Any, depth: int = 0, seen_objects: set[int] | None = None) -> None:
        if value is None or depth > 20:
            return
        if isinstance(value, str):
            parsed = maybe_json(value)
            if isinstance(parsed, (dict, list)):
                scan(parsed, depth + 1, seen_objects)
            return
        value = maybe_json(value)
        if not isinstance(value, (dict, list)):
            return
        if seen_objects is None:
            seen_objects = set()
        marker = id(value)
        if marker in seen_objects:
            return
        seen_objects.add(marker)
        if isinstance(value, dict):
            add(value.get("video_info"))
            add(value)
            items = list(value.values())[:160]
        else:
            items = list(value)[:160]
        for item in items:
            scan(item, depth + 1, seen_objects)

    scan(obj)
    return infos


def decode_maybe_base64_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    raw = normalize_text(value).strip()
    if raw.startswith(("http://", "https://")):
        return raw
    if len(raw) < 20 or re.search(r"[^A-Za-z0-9_+\-/=]", raw):
        return ""
    variants = [raw]
    if "-" in raw or "_" in raw:
        variants.append(raw.replace("-", "+").replace("_", "/"))
    for candidate in variants:
        try:
            padded = candidate + ("=" * (-len(candidate) % 4))
            decoded = base64.b64decode(padded).decode("utf-8", errors="replace")
        except Exception:
            continue
        decoded = normalize_text(decoded).strip()
        if decoded.startswith(("http://", "https://")):
            return decoded
    return ""


def is_direct_nowatermark_url(url: str) -> bool:
    if not url or is_fplay_url(url):
        return False
    lowered = url.lower()
    if "video" not in lowered:
        return False
    try:
        params = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    except Exception:
        params = {}
    return str(params.get("lr") or "").lower() == "unwatermarked" or str(params.get("logo_type") or "").lower() == "unwatermarked" or "unwatermarked" in lowered


def _item_url_not_expired(item: dict[str, Any]) -> bool:
    expires = item.get("url_expire") or item.get("expire") or item.get("expires")
    if expires in (None, ""):
        return True
    try:
        return float(expires) > time.time() + 60
    except (TypeError, ValueError):
        return True


def find_direct_nowatermark_urls(obj: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        url = decode_maybe_base64_url(value)
        if not is_direct_nowatermark_url(url):
            return
        key = video_url_key(url)
        if key in seen:
            return
        seen.add(key)
        urls.append(url)

    for item in walk(obj):
        if not _item_url_not_expired(item):
            continue
        for key, value in item.items():
            lowered_key = str(key).lower()
            if lowered_key in {"download_url", "url", "main", "backup"}:
                add(value)
            elif lowered_key == "main_url" or lowered_key.startswith("backup_url"):
                add(value)
    return urls


def _nowatermark_response_url(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("download_url", "video_url", "url"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return normalize_text(value)
    nested = data.get("data")
    if isinstance(nested, dict):
        for key in ("download_url", "video_url", "url"):
            value = nested.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return normalize_text(value)
    return ""


def call_dola_nowatermark_api(payload: dict[str, Any], proxies: dict[str, str] | None = None) -> tuple[str, dict[str, Any]]:
    """调用 remove_water 插件中使用的第三方 Dola 去水印解析接口。

    这个方法是唯一会请求 http://47.104.150.143:8765/tools/dola/ 的地方。
    上层只有在 remove_watermark=True 时才应调用它。
    """
    # The resolver is an independent service and must not inherit the Dola
    # account proxy (nor HTTP(S)_PROXY from the container environment).  The
    # source video download below still uses the account proxy as before.
    direct_session = requests.Session()
    direct_session.trust_env = False
    response = direct_session.post(
        NOWATERMARK_API_ENDPOINT,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/plain, */*", "User-Agent": UA},
        data=compact_json(payload),
        timeout=NOWATERMARK_API_TIMEOUT,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise DolaError(f"nowatermark resolver did not return JSON: {response.text[:300]}") from exc
    if response.status_code >= 400 or data.get("ok") is False:
        message = data.get("msg") or data.get("error") or response.text[:300]
        raise DolaError(f"nowatermark resolver failed: {message}")
    video_url = _nowatermark_response_url(data)
    if not video_url:
        raise DolaError("nowatermark resolver returned no video URL")
    return video_url, data


def _build_nowatermark_api_payloads(source_url: str, *, play_info: Any = None, referer: str = "") -> list[dict[str, Any]]:
    """根据 fplay_url/video_info/key_seed 构造第三方去水印 API payload。"""
    referer = referer or f"{BASE}/"
    context = {"source_url": source_url, "play_info": play_info}
    fplay_urls = find_fplay_urls(context)
    video_infos = find_video_info_objects(context)
    payloads: list[dict[str, Any]] = []
    seen_payloads: set[str] = set()

    def add_payload(payload: dict[str, Any]) -> None:
        text = compact_json_text(payload)
        if text not in seen_payloads:
            seen_payloads.add(text)
            payloads.append(payload)

    for fplay_url in fplay_urls[:3]:
        video_info = video_infos[0] if video_infos else None
        payload = {"fplay_url": fplay_url, "referer": referer, "mode": "nowatermark"}
        key_seed = extract_key_seed(fplay_url) or find_key_seed(video_info)
        if key_seed:
            payload["key_seed"] = key_seed
        if video_info:
            payload["video_info"] = compact_json_text(video_info)
        add_payload(payload)
    for video_info in video_infos[:3]:
        payload = {"video_info": compact_json_text(video_info), "referer": referer, "mode": "nowatermark"}
        key_seed = find_key_seed(video_info)
        if key_seed:
            payload["key_seed"] = key_seed
        add_payload(payload)
    return payloads


def select_direct_download_url(source_url: str, *, play_info: Any = None) -> tuple[str, bool]:
    """不调用第三方服务：优先选择 Dola 返回数据中已有的 lr=unwatermarked 直链，否则兜底 source_url。"""
    context = {"source_url": source_url, "play_info": play_info}
    direct_nowatermark_urls = find_direct_nowatermark_urls(context)
    if direct_nowatermark_urls:
        return direct_nowatermark_urls[0], True
    return source_url, False


def resolve_by_dola_nowatermark_api(source_url: str, *, play_info: Any = None, referer: str = "", proxies: dict[str, str] | None = None) -> str:
    """显式调用第三方去水印 API 解析，失败由调用方决定如何兜底。"""
    payloads = _build_nowatermark_api_payloads(source_url, play_info=play_info, referer=referer)
    if not payloads:
        raise DolaError("no fplay_url/video_info found for nowatermark API")
    last_error = ""
    for payload in payloads:
        try:
            resolved_url, _data = call_dola_nowatermark_api(payload, proxies=proxies)
            log("   No-watermark resolver succeeded")
            return resolved_url
        except Exception as exc:
            last_error = str(exc).replace("\n", " ")[:500]
            log(f"   No-watermark resolver attempt failed: {last_error}")
    raise DolaError(last_error or "nowatermark resolver failed")


def choose_video_download_url(
    source_url: str,
    *,
    play_info: Any = None,
    referer: str = "",
    remove_watermark: bool = False,
    proxies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """选择最终下载 URL。

    remove_watermark=False：不请求第三方 API，只从 Dola 数据中优先选择 lr=unwatermarked 直链；
    remove_watermark=True：才调用 http://47.104.150.143:8765/tools/dola/，失败则回退到直链/兜底 URL。
    """
    direct_url, direct_unwatermarked = select_direct_download_url(source_url, play_info=play_info)
    result = {
        "download_url": direct_url,
        "download_source": "direct_unwatermarked" if direct_unwatermarked else "fallback",
        "direct_unwatermarked": direct_unwatermarked,
        "remove_watermark": bool(remove_watermark),
        "nowatermark_api_used": False,
        "nowatermark_api_error": "",
    }
    if not remove_watermark:
        if direct_unwatermarked:
            log("   Download URL: using Dola direct lr=unwatermarked link")
        else:
            log("   Download URL: no lr=unwatermarked link; fallback source URL")
        return result

    try:
        api_url = resolve_by_dola_nowatermark_api(
            source_url,
            play_info=play_info,
            referer=referer,
            proxies=proxies,
        )
        result.update({
            "download_url": api_url,
            "download_source": "third_party_nowatermark_api",
            "nowatermark_api_used": True,
            "direct_unwatermarked": is_direct_nowatermark_url(api_url),
        })
    except Exception as exc:
        result["nowatermark_api_error"] = str(exc).replace("\n", " ")[:500]
        log(f"   No-watermark API failed; fallback to {result['download_source']}: {result['nowatermark_api_error']}")
    return result


def fetch_play_info_urls(session: requests.Session, ctx: dict[str, str], vids: list[str]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    all_urls: list[str] = []
    seen_urls: set[str] = set()
    all_fplay_urls: list[str] = []
    seen_fplay_urls: set[str] = set()
    all_video_infos: list[dict[str, Any]] = []
    seen_video_infos: set[str] = set()
    for vid in vids:
        try:
            data = get_video_play_info(session, ctx, vid)
        except Exception as exc:
            results.append({"vid": vid, "error": str(exc)})
            continue
        urls = find_play_info_urls(data)
        fplay_urls = find_fplay_urls(data)
        video_infos = find_video_info_objects(data)
        for url in urls:
            key = video_url_key(url)
            if key not in seen_urls:
                seen_urls.add(key)
                all_urls.append(url)
        for url in fplay_urls:
            key = video_url_key(url)
            if key not in seen_fplay_urls:
                seen_fplay_urls.add(key)
                all_fplay_urls.append(url)
        for video_info in video_infos:
            key = compact_json_text(video_info)
            if key not in seen_video_infos:
                seen_video_infos.add(key)
                all_video_infos.append(video_info)
        play_data = data.get("data")
        if not isinstance(play_data, dict):
            play_data = {}
        results.append({
            "vid": vid,
            "urls": urls,
            "fplay_urls": fplay_urls,
            "video_infos": video_infos,
            "play_infos": play_data.get("play_infos", []),
        })
    return {"urls": all_urls, "fplay_urls": all_fplay_urls, "video_infos": all_video_infos, "items": results}


def get_video_result(session: requests.Session, ctx: dict[str, str], conversation_id: str, *, timeout: int = 900, interval: int = DEFAULT_POLL_INTERVAL, remove_watermark: bool = False, proxy: str = "") -> dict[str, Any]:
    """封装结果获取：轮询任务，拿链路 URL/VID，补拉 play_info，并返回可下载 URL。"""
    proxy = normalize_proxy_url(proxy or get_dola_proxy(session))
    poll = wait_for_video(session, ctx, conversation_id, timeout_sec=timeout, interval_sec=interval)
    result: dict[str, Any] = {
        "conversation_id": conversation_id,
        "status": poll.status,
        "urls": poll.urls,
        "vids": poll.vids,
        "failure_reasons": poll.failure_reasons,
        "texts": poll.texts,
        "creation_statuses": poll.creation_statuses,
        "wait_minutes": poll.wait_minutes,
    }
    if poll.status != "succeeded":
        return result
    play_info = fetch_play_info_urls(session, ctx, poll.vids) if poll.vids else {}
    play_info_urls = list(play_info.get("urls") or [])
    source_url = (play_info_urls or poll.urls or [""])[0]
    result.update({
        "play_info_urls": play_info_urls,
        "fplay_urls": list(play_info.get("fplay_urls") or []),
        "video_infos_count": len(play_info.get("video_infos") or []),
        "source_url": source_url,
        "download_url": source_url,
        "download_source": "fallback",
        "direct_unwatermarked": False,
        "remove_watermark": bool(remove_watermark),
        "nowatermark_api_used": False,
        "nowatermark_api_error": "",
    })
    if source_url:
        chosen = choose_video_download_url(
            source_url,
            play_info={"chain_data": poll.source_data, "play_info": play_info},
            referer=f"{BASE}/chat/{conversation_id}",
            remove_watermark=remove_watermark,
            proxies=proxy_dict(proxy),
        )
        result.update(chosen)
    return result


def download_video(url: str, output_path: str | Path, *, referer: str = "", proxy: str = "", max_retries: int = 5) -> Path:
    """下载视频文件，带重试；不传 proxy 时强制直连。"""
    if not url:
        raise DolaError("download url is empty")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    proxies = proxy_dict(proxy) if proxy else {"http": None, "https": None, "all": None}
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=300, proxies=proxies) as resp:
                if resp.status_code != 200:
                    raise DolaError(f"download HTTP {resp.status_code}: {resp.text[:200]}")
                with output.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
            return output
        except Exception as exc:
            last_error = exc
            log(f"   Download attempt {attempt}/{max_retries} failed: {exc}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)
    raise DolaError(f"download failed after {max_retries} attempts: {last_error}")


def setup_session(
    region: str = "JP",
    pc_version: str = "3.23.10",
    proxy: str = "",
    use_cache: bool = False,
    *,
    login_cdp: bool = False,
    cdp_url: str = DEFAULT_CDP_URL,
    login_pure: bool = False,
    login_state_file: str | Path | None = None,
) -> tuple[requests.Session, dict[str, str], dict[str, str]]:
    """?? Dola session??? (session, ctx, base_headers)?

    login_cdp=True ??????? Chrome??????? fetch ?? Dola API?
    ImageX ???? Python requests ???
    """
    session = requests.Session()
    # Account proxy selection must be deterministic. In Docker hosts it is
    # common to have HTTP(S)_PROXY exported globally; allowing requests to
    # merge those values can create a second proxy hop and break HTTPS CONNECT.
    session.trust_env = False
    proxy = resolve_session_proxy(proxy, login_pure=login_pure, cdp_url=cdp_url)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})

    state: dict[str, Any] = {}
    cdp: DolaCdpClient | None = None
    if login_cdp:
        cdp = DolaCdpClient(cdp_url).connect()
        state = cdp.save_login_state(LOGIN_SESSION_FILE, LOGIN_COOKIE_FILE)
        # Preserve duplicate path cookies from Chrome.
        for c in state.get("cookies_list", []) if isinstance(state.get("cookies_list"), list) else []:
            if "dola.com" in str(c.get("domain") or "") and c.get("name"):
                session.cookies.set(c["name"], c.get("value", ""), domain=c.get("domain") or ".dola.com", path=c.get("path") or "/")
        attach_login_cdp(session, cdp)
    elif login_pure:
        state_path = Path(login_state_file) if login_state_file else (DEFAULT_9555_LOGIN_SESSION_FILE if DEFAULT_9555_LOGIN_SESSION_FILE.exists() else LOGIN_SESSION_FILE)
        state = load_state_file(state_path)
        if not state:
            raise DolaError(f"login state not found: {state_path}. Run --save-login-cookies first.")
        apply_state_cookies(session, state)
        attach_login_pure(session, state, proxy)
    elif use_cache:
        apply_state(session)
        state = load_state()

    fp = make_fp()
    ctx = fresh_ctx(region, pc_version, fp=fp)
    if state:
        apply_state_to_ctx(ctx, state)
    bh = headers_base()

    # Cookies that the SPA normally sets before API calls.
    # login_pure still needs ttwid/s_v_web_id; do not overwrite existing login cookies.
    if not login_cdp:
        _ensure_spa_cookies(session, ctx, region)
    if login_pure:
        refresh_login_cookie_header(session)
    return session, ctx, bh


def restore_session_for_result(
    region: str = "JP",
    pc_version: str = "3.23.10",
    proxy: str = "",
    *,
    login_cdp: bool = False,
    cdp_url: str = DEFAULT_CDP_URL,
    login_pure: bool = False,
    login_state_file: str | Path | None = None,
) -> tuple[requests.Session, dict[str, str]]:
    """????????? session/ctx???? conversation_id ????????"""
    state = {}
    session = requests.Session()
    session.trust_env = False
    proxy = resolve_session_proxy(proxy, login_pure=login_pure, cdp_url=cdp_url)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    if login_cdp:
        cdp = DolaCdpClient(cdp_url).connect()
        state = cdp.save_login_state(LOGIN_SESSION_FILE, LOGIN_COOKIE_FILE)
        for c in state.get("cookies_list", []) if isinstance(state.get("cookies_list"), list) else []:
            if "dola.com" in str(c.get("domain") or "") and c.get("name"):
                session.cookies.set(c["name"], c.get("value", ""), domain=c.get("domain") or ".dola.com", path=c.get("path") or "/")
        attach_login_cdp(session, cdp)
    elif login_pure:
        state_path = Path(login_state_file) if login_state_file else (DEFAULT_9555_LOGIN_SESSION_FILE if DEFAULT_9555_LOGIN_SESSION_FILE.exists() else LOGIN_SESSION_FILE)
        state = load_state_file(state_path)
        if not state:
            raise DolaError(f"login state not found: {state_path}. Run --save-login-cookies first.")
        apply_state_cookies(session, state)
        attach_login_pure(session, state, proxy)
    else:
        state = load_state()
        apply_state(session)
    ctx = state.get("ctx") if isinstance(state.get("ctx"), dict) else {}
    if not ctx:
        ctx = fresh_ctx(region, pc_version)
    fresh = fresh_ctx(region, pc_version, fp=ctx.get("fp") or None)
    fresh.update({k: str(v) for k, v in ctx.items() if v is not None})
    return session, fresh


def fetch_or_download_conversation(
    conversation_id: str,
    *,
    timeout: int = 900,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    region: str = "JP",
    pc_version: str = "3.23.10",
    proxy: str = "",
    download: bool = False,
    output: str = "",
    remove_watermark: bool = False,
    login_cdp: bool = False,
    cdp_url: str = DEFAULT_CDP_URL,
    login_pure: bool = False,
    login_state_file: str | Path | None = None,
) -> dict[str, Any]:
    """按已有 conversation_id 获取结果；download=True 时直接下载。"""
    session, ctx = restore_session_for_result(
        region=region,
        pc_version=pc_version,
        proxy=proxy,
        login_cdp=login_cdp,
        cdp_url=cdp_url,
        login_pure=login_pure,
        login_state_file=login_state_file,
    )
    proxy = proxy or get_dola_proxy(session)
    # 打开 chat 页补齐 ttwid/会话 cookie。
    try:
        session.get(f"{BASE}/chat/{conversation_id}", headers=headers_base(f"{BASE}/chat/{conversation_id}"), timeout=30)
    except Exception as exc:
        log(f"   chat page init warning: {str(exc).replace(chr(10), ' ')[:300]}")
    result = get_video_result(
        session,
        ctx,
        conversation_id,
        timeout=timeout,
        interval=poll_interval,
        remove_watermark=remove_watermark,
        proxy=proxy,
    )
    if download and result.get("status") == "succeeded":
        out_path = Path(output) if output else DEFAULT_DOWNLOAD_DIR / f"dola_{conversation_id}_{int(time.time())}.mp4"
        saved = download_video(
            str(result.get("download_url") or ""),
            out_path,
            referer=f"{BASE}/chat/{conversation_id}",
            proxy=proxy,
        )
        result["saved_path"] = str(saved)
        result["saved_size"] = saved.stat().st_size
    return result


def test_audio_upload(audio_path: str | Path, *, region: str = "JP", pc_version: str = "3.23.10", proxy: str = "", use_cache: bool = False) -> dict[str, Any]:
    """只初始化会话并上传音频，不提交视频任务。"""
    proxy = normalize_proxy_url(proxy or os.environ.get("DOLA_PROXY", ""))
    session, ctx, bh = setup_session(region=region, pc_version=pc_version, proxy=proxy, use_cache=use_cache)
    if proxy:
        log(f"Proxy: {proxy}")
    log("1. Init session")
    r = session.get(f"{BASE}/chat/create-image", headers=bh, timeout=30)
    log(f"   GET create-image HTTP {r.status_code}; ttwid={'yes' if session.cookies.get('ttwid') else 'no'}; fp={ctx['fp'][:32]}...")
    if use_cache:
        save_state(session, ctx, SESSION_FILE)

    log("2. get_web_anon_id")
    url = build_url("/alice/user/get_web_anon_id", ctx)
    url = sign_url(url, headers={"Content-Type": "application/json", "agw-js-conv": "str"}, body={}, cookies=cookie_dict(session))
    rr = session.post(url, headers={**bh, "Content-Type": "application/json", "agw-js-conv": "str"}, data="{}", timeout=30)
    data = request_json(rr, "get_web_anon_id")
    assert_ok(data, "get_web_anon_id")
    log(f"   uid={data.get('uid')}")

    log("3. launch")
    url = build_url("/alice/user/launch", ctx)
    url = sign_url(url, headers={"Content-Type": "application/json", "agw-js-conv": "str"}, body={}, cookies=cookie_dict(session))
    rr = session.post(url, headers={**bh, "Content-Type": "application/json", "agw-js-conv": "str"}, data="{}", timeout=30)
    data = request_json(rr, "launch")
    if data.get("code") not in (0, "0", None):
        log(f"   launch warning: {json.dumps(data, ensure_ascii=False)[:500]}")
    else:
        log("   code=0")

    audio = upload_audio(session, ctx, bh, audio_path, resource_type=1)
    if use_cache:
        save_state(session, ctx)
    return {"ok": True, "audio": audio_summary(audio)}


def first_value_for_key(obj: Any, key: str) -> Any:
    for item in walk(obj):
        if isinstance(item, dict) and key in item:
            return item.get(key)
    return None


def summarize_ugc_voice_list(data: Any) -> dict[str, Any]:
    ugc_voice_list = first_value_for_key(data, "ugc_voice_list")
    if not isinstance(ugc_voice_list, list):
        ugc_voice_list = []
    voices: list[dict[str, Any]] = []
    for voice in ugc_voice_list[:20]:
        if not isinstance(voice, dict):
            continue
        voices.append({
            "id": voice.get("id"),
            "style_id": voice.get("style_id"),
            "name": voice.get("name"),
            "private_status": voice.get("private_status"),
            "preview": voice.get("preview"),
        })
    return {
        "count": len(ugc_voice_list),
        "has_more": first_value_for_key(data, "has_more"),
        "voices": voices,
    }


def response_json(item: dict[str, Any] | Any) -> dict[str, Any]:
    if isinstance(item, dict) and isinstance(item.get("data"), dict) and "http_status" in item:
        return item["data"]
    return item if isinstance(item, dict) else {}


def api_code_ok(data: dict[str, Any] | Any) -> bool:
    data = response_json(data)
    return data.get("code") in (0, "0", 2000, "2000", None) and data.get("status_code") in (None, 0, "0")


def summarize_created_ugc_voice(item: dict[str, Any] | Any) -> dict[str, Any]:
    """从 /alice/user_voice/create 响应中提取真实创建出的 UGC voice。

    不用 list 的 count 判断成功，因为 list 里可能已有历史音色；真正可作为
    本次音频成功证据的是 create 响应里返回了 id/style_id/ugc_voice。
    """
    data = response_json(item)
    if not api_code_ok(data):
        return {}
    payload = data.get("data") if isinstance(data.get("data"), (dict, list)) else data
    candidates: list[dict[str, Any]] = []
    for node in walk(payload):
        if not isinstance(node, dict):
            continue
        if node.get("style_id") or node.get("voice_id") or node.get("ugc_voice") is True:
            candidates.append(node)
    if not candidates and isinstance(payload, dict):
        candidates.append(payload)
    if not candidates:
        return {}
    voice = candidates[0]
    preview = voice.get("preview") if isinstance(voice.get("preview"), dict) else {}
    return {
        "id": str(voice.get("id") or voice.get("voice_id") or ""),
        "style_id": str(voice.get("style_id") or ""),
        "name": voice.get("name"),
        "language_code": voice.get("language_code") or voice.get("bot_lang"),
        "ugc_voice": voice.get("ugc_voice"),
        "private_status": voice.get("private_status"),
        "preview": preview or None,
        "preview_audio": voice.get("preview_audio") or voice.get("preview_audio_url") or preview.get("preview_audio") or preview.get("previewMp3Url"),
        "preview_audio_uri": voice.get("preview_audio_uri") or preview.get("preview_audio_uri"),
    }


def summarize_launch_for_voice(data: Any) -> dict[str, Any]:
    new_voice_list = first_value_for_key(data, "new_voice_list")
    voice_count = len(new_voice_list) if isinstance(new_voice_list, list) else 0
    return {
        "show_copy_my_voice": first_value_for_key(data, "show_copy_my_voice"),
        "upload_audio_entity_conf": first_value_for_key(data, "upload_audio_entity_conf"),
        "new_voice_list_count": voice_count,
        "first_new_voice": (new_voice_list[0] if isinstance(new_voice_list, list) and new_voice_list else None),
    }


def test_ugc_voice(
    audio_path: str | Path,
    *,
    region: str = "SG",
    pc_version: str = "3.23.10",
    proxy: str = "",
    use_cache: bool = False,
    preview_text: str = "长老，我被人打了，怎么办，你要给我做主呀！",
    voice_name: str = "张三参考音色",
) -> dict[str, Any]:
    """诊断“音频附件”能否变成 Dola UGC 音色。

    结论用于判断视频生成是否可能真正使用 --audio 的音色。普通 chat 附件
    只会进入会话上下文；只有 /alice/user_voice/* 成功创建出 UGC voice id，
    后续才有可能被某些语音/机器人链路当作音色使用。
    """
    proxy = normalize_proxy_url(proxy or os.environ.get("DOLA_PROXY", ""))
    session, ctx, bh = setup_session(region=region, pc_version=pc_version, proxy=proxy, use_cache=use_cache)
    if proxy:
        log(f"Proxy: {proxy}")
    log("1. Init session")
    r = session.get(f"{BASE}/chat/create-image", headers=bh, timeout=30)
    log(f"   GET create-image HTTP {r.status_code}; ttwid={'yes' if session.cookies.get('ttwid') else 'no'}; fp={ctx['fp'][:32]}...")
    if use_cache:
        save_state(session, ctx, SESSION_FILE)

    log("2. get_web_anon_id")
    uid_data = dola_post_json(session, ctx, bh, "/alice/user/get_web_anon_id", {}, label="get_web_anon_id")
    assert_ok(uid_data, "get_web_anon_id")
    log(f"   uid={uid_data.get('uid')}")

    log("3. launch")
    launch_data = dola_post_json(session, ctx, bh, "/alice/user/launch", {}, label="launch")
    if launch_data.get("code") not in (0, "0", None):
        log(f"   launch warning: {json.dumps(launch_data, ensure_ascii=False)[:500]}")
    else:
        log("   code=0")
    log("   launch voice summary:")
    log(json.dumps(summarize_launch_for_voice(launch_data), ensure_ascii=False, indent=2)[:3000])

    log("4. Upload audio for UGC voice clone")
    audio = upload_audio(session, ctx, bh, audio_path, resource_type=1, convert_non_mp3=False)
    audio_info = audio_summary(audio)

    log("5. Probe /alice/user_voice/*")
    calls: list[dict[str, Any]] = []

    def call(label: str, endpoint: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        log(f"   POST {endpoint} [{label}]")
        item = dola_post_json_raw(
            session,
            ctx,
            bh,
            endpoint,
            body or {},
            label=label,
            referer=f"{BASE}/chat/create-image",
        )
        calls.append(item)
        data = item.get("data")
        if isinstance(data, dict):
            log(f"      HTTP {item.get('http_status')} code={data.get('code')} msg={fix_mojibake_text(data.get('msg') or data.get('message') or '')}")
        else:
            log(f"      HTTP {item.get('http_status')} non-json")
        return item

    call("authorize", "/alice/user_voice/authorize", {})
    call("list.before", "/alice/user_voice/list", {
        "visit_id": "",
        "page_index": 1,
        "page_size": 20,
        "language_code": "zh",
    })
    call("voice_check.zh-CN", "/alice/user_voice/voice_check", {
        "tos_key": audio.uri,
        "preview_text": preview_text,
        "voice_lang": "zh-CN",
    })

    # 前端 SDK 暴露的克隆链路：clone(tos_key, lang_code, local_voice_id) -> create/v2(local_voice_id)
    local_voice_id = "local_" + str(random.randint(10**15, 10**16 - 1))
    clone_item = call("clone", "/alice/user_voice/clone", {
        "tos_key": audio.uri,
        "lang_code": "zh",
        "local_voice_id": local_voice_id,
    })
    clone_data = clone_item.get("data") if isinstance(clone_item.get("data"), dict) else {}
    clone_payload = clone_data.get("data") if isinstance(clone_data, dict) and isinstance(clone_data.get("data"), dict) else {}
    returned_local_voice_id = str(
        clone_payload.get("local_voice_id")
        or clone_payload.get("voice_id")
        or clone_payload.get("id")
        or local_voice_id
    )
    call("create.v2", "/alice/user_voice/create/v2", {
        "name": voice_name,
        "icon": "",
        "local_voice_id": returned_local_voice_id,
        "private_status": 1,
    })
    create_v1_item = call("create.v1", "/alice/user_voice/create", {
        "name": voice_name,
        "tos_key": audio.uri,
        "bot_lang": "zh",
        "icon": "",
        "need_delete_id": "",
    })
    call("list.after", "/alice/user_voice/list", {
        "visit_id": "",
        "page_index": 1,
        "page_size": 20,
        "language_code": "zh",
    })

    list_after = calls[-1].get("data") if calls else {}
    ugc_summary = summarize_ugc_voice_list(list_after)
    created_voice = summarize_created_ugc_voice(create_v1_item)
    ok = bool(created_voice.get("style_id") or created_voice.get("id"))
    result = {
        "ok": ok,
        "diagnosis": (
            "UGC 音色创建成功；但当前 Seedance 视频 chat_ability 里还没发现可用的 voice/style_id 入参，不能证明视频会自动使用这个音色。"
            if ok
            else "该音频没有被 /alice/user_voice/create 接受为 UGC 音色；把它作为普通 chat 附件只会进入聊天上下文，不能保证视频配音使用该音色。"
        ),
        "audio": audio_info,
        "created_voice": created_voice,
        "launch_voice_summary": summarize_launch_for_voice(launch_data),
        "ugc_voice_list_after": ugc_summary,
        "raw_calls": calls,
    }
    if use_cache:
        save_state(session, ctx)
    return result


def generate(
    image_paths: str | Path | list[str | Path] | tuple[str | Path, ...],
    prompt: str,
    *,
    wait_for_video: bool = False,
    timeout: int = 300,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    region: str = "JP",
    pc_version: str = "3.23.10",
    duration: int = 10,
    model: str = "seedance_v2.0",
    ratio: str | None = None,
    proxy: str = "",
    download: bool = False,
    output: str = "",
    remove_watermark: bool = False,
    audio_paths: str | Path | list[str | Path] | tuple[str | Path, ...] | None = None,
    audio_prompt: str = DEFAULT_AUDIO_REFERENCE_PROMPT,
    attach_audio_to_video_step: bool = False,
    use_cache: bool = False,
    login_cdp: bool = False,
    cdp_url: str = DEFAULT_CDP_URL,
    login_pure: bool = False,
    login_state_file: str | Path | None = None,
) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        imgs = normalize_image_paths(image_paths)
    except DolaError as exc:
        log(f"ERROR: {exc}")
        return 1
    duration = normalize_duration(duration, default=5)
    ratio = normalize_ratio(ratio)
    log(f"Images: {len(imgs)}")
    for idx, img in enumerate(imgs, 1):
        log(f"  [{idx}] {img.name} ({img.stat().st_size} bytes)")
    log(f"Prompt: {prompt}\n")
    log(f"Duration: {duration}s")
    if ratio:
        log(f"Ratio: {ratio}")
    audio_files = normalize_image_paths(audio_paths) if audio_paths else []
    if audio_files:
        log(f"Audios: {len(audio_files)}")
        for idx, audio in enumerate(audio_files, 1):
            log(f"  [A{idx}] {audio.name} ({audio.stat().st_size} bytes)")

    proxy = normalize_proxy_url(proxy or os.environ.get("DOLA_PROXY", ""))
    session, ctx, bh = setup_session(
        region=region,
        pc_version=pc_version,
        proxy=proxy,
        use_cache=use_cache,
        login_cdp=login_cdp,
        cdp_url=cdp_url,
        login_pure=login_pure,
        login_state_file=login_state_file,
    )
    proxy = proxy or get_dola_proxy(session)
    fp = ctx["fp"]
    if login_cdp:
        log(f"Login CDP: {cdp_url}; fp={fp[:32]}...")
    if login_pure:
        log(f"Login pure: state={login_state_file or DEFAULT_9555_LOGIN_SESSION_FILE}; fp={fp[:32]}...")
    if proxy:
        log(f"Proxy: {proxy}")

    log("1. Init session")
    if login_cdp:
        log(f"   Chrome CDP page ready; ttwid={'yes' if any(c.name == 'ttwid' for c in session.cookies) else 'no'}; fp={fp[:32]}...")
    else:
        r = warmup_login_session(session, bh)
        header = state_cookie_header(get_login_state(session)) if login_pure else ""
        log(
            f"   GET create-image HTTP {r.status_code}; ttwid={'yes' if ('ttwid=' in header or session.cookies.get('ttwid')) else 'no'}; "
            f"sessionid={'yes' if ('sessionid=' in header or session.cookies.get('sessionid')) else 'no'}; fp={fp[:32]}..."
        )
    if use_cache or login_cdp:
        save_state(session, ctx, LOGIN_SESSION_FILE if login_cdp else SESSION_FILE)

    log("2. get_web_anon_id")
    data = dola_api_post_json(session, ctx, "/alice/user/get_web_anon_id", {}, label="get_web_anon_id")
    log(f"   uid={data.get('uid')}")

    log("3. launch")
    data = dola_api_post_json(session, ctx, "/alice/user/launch", {}, label="launch")
    if data.get("code") not in (0, "0", None):
        log(f"   launch warning: {json.dumps(data, ensure_ascii=False)[:500]}")
    else:
        log("   code=0")

    audio_step_summary: dict[str, Any] | None = None
    uploaded_audios: list[UploadedAudio] = []
    first_result: SubmitResult | None = None
    video_conversation_id = ""
    video_section_id = ""
    video_last_message_index: int | None = None
    if audio_files:
        for idx, audio in enumerate(audio_files, 1):
            log(f"4A. Upload audio {idx}/{len(audio_files)}: {audio.name}")
            uploaded_audios.append(upload_audio(session, ctx, bh, audio, resource_type=1))
        if use_cache:
            save_state(session, ctx, LOGIN_SESSION_FILE if login_cdp else SESSION_FILE)

        log("5A. chat/completion audio reference step (new non-video conversation)")
        audio_reference_prompt = build_audio_reference_prompt(uploaded_audios, audio_prompt)
        log(f"   audio prompt: {audio_reference_prompt}")
        first_result = submit_chat_completion(
            session,
            ctx,
            bh,
            images=[],
            audios=uploaded_audios,
            prompt=audio_reference_prompt,
            duration=duration,
            model=model,
            ratio=None,
            timeout_sec=min(max(timeout, 30), 180),
            video_ability=False,
        )
        log("\n[Step 1 Raw Response: chat/completion SSE events]")
        log(json.dumps(first_result.events, ensure_ascii=False, indent=2)[:30000])
        log("\n[Step 1 Raw Response Summary]")
        log(json.dumps(summarize_sse_events_for_log(first_result.events), ensure_ascii=False, indent=2)[:12000])
        if not first_result.conversation_id:
            result = {
                "conversation_id": "",
                "error": first_result.error or "audio reference step did not return conversation_id",
                "error_code": first_result.error_code,
                "events": len(first_result.events),
            }
            log("\n" + json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        log(f"   audio step OK: conversation_id={first_result.conversation_id}, section_id={first_result.section_id}, message_index={first_result.message_index}")
        log("5B. parse audio reference result from chain")
        chain_texts: list[str] = []
        first_chain: dict[str, Any] = {}
        try:
            first_chain = fetch_single_chain(session, ctx, first_result.conversation_id)
            min_reply_index = (max(first_result.query_message_indexes) + 1) if first_result.query_message_indexes else (first_result.message_index + 1 if first_result.message_index is not None else None)
            chain_texts = extract_chain_message_texts(
                first_chain,
                min_index=min_reply_index,
                exclude_texts=[audio_reference_prompt],
            )
            for text in chain_texts:
                add_unique(first_result.reply_texts, text, limit=4000)
            if chain_texts:
                first_result.final_reply_text = chain_texts[-1]
        except Exception as exc:
            log(f"   audio chain parse warning: {str(exc).replace(chr(10), ' ')[:300]}")
        if first_result.reply_texts:
            for text in first_result.reply_texts:
                log(f"   Audio step reply: {text}")
        else:
            log("   Audio step reply not found in SSE/chain; continuing video generation")
        status_info = audio_reference_status(first_result, first_chain)
        log(f"   Audio step status: {status_info['status']}")
        latest_index = find_latest_message_index(first_chain)

        video_conversation_id = first_result.conversation_id
        video_section_id = first_result.section_id
        video_last_message_index = latest_index if latest_index is not None else (
            max(first_result.query_message_indexes) if first_result.query_message_indexes else first_result.message_index
        )
        log(f"   Video step will reuse audio conversation_id={video_conversation_id}, last_message_index={video_last_message_index}")
        audio_step_summary = {
            "conversation_id": first_result.conversation_id,
            "section_id": first_result.section_id,
            "message_index": first_result.message_index,
            "query_message_indexes": first_result.query_message_indexes,
            "query_question_ids": first_result.query_question_ids,
            "prompt": audio_reference_prompt,
            "events": len(first_result.events),
            "status": status_info["status"],
            "ok": status_info["ok"],
            "latest_message_index": latest_index,
            "attachment_statuses": status_info["attachment_statuses"],
            "reply_texts": first_result.reply_texts,
            "final_reply_text": first_result.final_reply_text,
            "error": first_result.error,
            "error_code": first_result.error_code,
        }
        log("\n[Step 1 Output: audio reference]")
        log(json.dumps(audio_step_summary, ensure_ascii=False, indent=2)[:4000])

    uploaded_images: list[dict[str, Any]] = []
    for idx, img in enumerate(imgs, 1):
        log(f"6. Upload image {idx}/{len(imgs)}: {img.name}")
        uploaded_images.append(upload_image(session, ctx, bh, img))
    if use_cache:
        save_state(session, ctx, LOGIN_SESSION_FILE if login_cdp else SESSION_FILE)

    log("9. chat/completion video step" + (" (same audio conversation)" if video_conversation_id else " (new conversation)"))
    video_prompt = normalize_video_prompt_for_audios(prompt, uploaded_audios)
    video_prompt = enrich_multi_image_prompt(video_prompt, len(uploaded_images))
    if len(uploaded_images) > 1 and video_prompt != prompt:
        log(f"   multi-image prompt: {video_prompt}")
    if uploaded_audios:
        if attach_audio_to_video_step:
            log(f"   attaching audio references to video step: {len(uploaded_audios)}")
        else:
            log("   audio references were submitted in step 1; video step will reference them by text only")
        for idx, audio in enumerate(uploaded_audios, 1):
            log(f"   [音频{idx}] {audio.name} uri={audio.uri[:80]}")
        if video_prompt != prompt:
            log(f"   normalized video prompt: {video_prompt}")
    final_result = submit_chat_completion(
        session,
        ctx,
        bh,
        images=uploaded_images,
        audios=uploaded_audios if attach_audio_to_video_step else [],
        prompt=video_prompt,
        duration=duration,
        model=model,
        ratio=ratio,
        timeout_sec=timeout,
        video_ability=True,
        conversation_id=video_conversation_id,
        section_id=video_section_id,
        last_message_index=video_last_message_index,
    )
    video_step_summary = {
        "conversation_id": final_result.conversation_id,
        "section_id": final_result.section_id,
        "message_index": final_result.message_index,
        "query_message_indexes": final_result.query_message_indexes,
        "query_question_ids": final_result.query_question_ids,
        "events": len(final_result.events),
        "reply_texts": final_result.reply_texts,
        "final_reply_text": final_result.final_reply_text,
        "error": final_result.error,
        "error_code": final_result.error_code,
        "reused_audio_conversation": bool(video_conversation_id),
        "last_message_index_used": video_last_message_index,
        "attached_audio_count": len(uploaded_audios) if attach_audio_to_video_step else 0,
        "audio_reference_count": len(uploaded_audios),
        "attach_audio_to_video_step": bool(attach_audio_to_video_step),
        "video_prompt": video_prompt,
    }
    log("\n[Step 2 Output: video submit]")
    log(json.dumps(video_step_summary, ensure_ascii=False, indent=2)[:4000])
    if use_cache:
        save_state(session, ctx, LOGIN_SESSION_FILE if login_cdp else SESSION_FILE)

    conv_id = final_result.conversation_id
    result = {
        "conversation_id": conv_id,
        "section_id": final_result.section_id,
        "message_index": final_result.message_index,
        "error": final_result.error,
        "error_code": final_result.error_code,
        "events": len(final_result.events),
        "video_step": video_step_summary,
    }
    if audio_step_summary:
        result["audio_reference_step"] = audio_step_summary
    log("\n" + json.dumps(result, ensure_ascii=False, indent=2))
    if conv_id:
        log(f"\nSUCCESS! {BASE}/chat/{conv_id}")
        if wait_for_video or download:
            log("\n10. 获取生成结果")
            result = get_video_result(
                session,
                ctx,
                conv_id,
                timeout=timeout,
                interval=poll_interval,
                remove_watermark=remove_watermark,
                proxy=proxy,
            )
            log(json.dumps({k: v for k, v in result.items() if k != "source_data"}, ensure_ascii=False, indent=2)[:4000])
            if result.get("status") != "succeeded":
                return 2
            if download:
                download_url = str(result.get("download_url") or "")
                if not download_url:
                    raise DolaError("video succeeded but download_url is empty")
                out_path = Path(output) if output else DEFAULT_DOWNLOAD_DIR / f"dola_{conv_id}_{int(time.time())}.mp4"
                log(f"\n11. 下载视频 -> {out_path}")
                saved = download_video(
                    download_url,
                    out_path,
                    referer=f"{BASE}/chat/{conv_id}",
                    proxy=proxy,
                )
                result["saved_path"] = str(saved)
                result["saved_size"] = saved.stat().st_size
                log(f"   Saved: {saved} ({saved.stat().st_size / (1024 * 1024):.2f} MB)")
                log(json.dumps({"download_url": download_url, "saved_path": str(saved), "saved_size": saved.stat().st_size}, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dola video generation via pure API + local BDMS signer")
    parser.add_argument("--image", nargs="*", default=[], help="One or more reference images. Also accepts comma-separated paths.")
    parser.add_argument("--audio", nargs="*", default=[], help="One or more audio attachments, e.g. 11.wav")
    parser.add_argument(
        "--audio-prompt",
        default=DEFAULT_AUDIO_REFERENCE_PROMPT,
        help="Prompt used in the first non-video audio-reference conversation. Supports {audio_names}, {audio_count}, {first_audio_name}.",
    )
    parser.add_argument("--test-audio-upload", default="", help="Only test uploading an audio file, e.g. 11.wav")
    parser.add_argument("--test-ugc-voice", default="", help="Probe whether an audio file can be converted to a Dola UGC voice; no video quota is used.")
    parser.add_argument("--voice-name", default="张三参考音色", help="Name used by --test-ugc-voice when probing UGC voice creation.")
    parser.add_argument("--voice-preview-text", default="长老，我被人打了，怎么办，你要给我做主呀！", help="Preview text used by --test-ugc-voice.")
    parser.add_argument("--prompt", default="generate video")
    parser.add_argument("--conversation-id", default="", help="Only fetch/download an existing Dola conversation id")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--no-wait", action="store_true", help="Submit only; do not poll unless --download is set.")
    parser.add_argument("--download", action="store_true", help="Wait for result and download the generated video")
    parser.add_argument("--output", default="", help="Output mp4 path when --download is used")
    parser.add_argument(
        "--remove-watermark",
        action="store_true",
        help="Call the third-party remove_water API (http://47.104.150.143:8765/tools/dola/) to resolve a no-watermark URL. Default: do not call it; prefer Dola lr=unwatermarked direct link, then fallback URL.",
    )
    parser.add_argument(
        "--watermark",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--region", default="JP")
    parser.add_argument("--pc-version", default="3.33.11")
    parser.add_argument("--duration", type=int, choices=sorted(VALID_DURATIONS), default=5)
    parser.add_argument("--model", default="seedance_v2.0")
    parser.add_argument("--ratio", default="")
    parser.add_argument("--proxy", default="", help="HTTP/SOCKS proxy for Dola API, polling, upload and download. Accepts http://host:port, socks5h://host:port, or host:port; also reads DOLA_PROXY. In --login-pure mode it auto-uses 127.0.0.1:7897 when available.")
    parser.add_argument("--login-cdp", action="store_true", help="Use logged-in Chrome CDP transport for Dola API calls. Required when anonymous generation is blocked.")
    parser.add_argument("--cdp-url", default=os.environ.get("DOLA_CDP_URL", DEFAULT_CDP_URL), help="Chrome DevTools URL for --login-cdp, default: %(default)s")
    parser.add_argument("--login-pure", action="store_true", help="Use saved Chrome login cookies with pure Python requests (no browser transport).")
    parser.add_argument("--login-state-file", default=os.environ.get("DOLA_LOGIN_STATE_FILE", str(DEFAULT_9555_LOGIN_SESSION_FILE)), help="Chrome cookie/session JSON for --login-pure.")
    parser.add_argument("--save-login-cookies", action="store_true", help="Only refresh/save login cookies from Chrome CDP and exit.")
    parser.add_argument("--use-cache", action="store_true", help="Reuse/save .dola_pure_api_session.json cookies. Default: disabled; every generation starts a fresh anonymous session.")
    parser.add_argument("--attach-audio-to-video-step", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.save_login_cookies:
            cdp = DolaCdpClient(args.cdp_url).connect()
            try:
                data = cdp.save_login_state(LOGIN_SESSION_FILE, LOGIN_COOKIE_FILE)
            finally:
                cdp.close()
            log(json.dumps({"saved": str(LOGIN_SESSION_FILE), "cookie_file": str(LOGIN_COOKIE_FILE), "cookie_names": list(data.get("cookies", {}).keys()), "ctx": data.get("ctx", {})}, ensure_ascii=False, indent=2))
            sys.exit(0)
        if args.test_audio_upload:
            result = test_audio_upload(
                args.test_audio_upload,
                region=args.region,
                pc_version=args.pc_version,
                proxy=args.proxy or "",
                use_cache=bool(args.use_cache),
            )
            log(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0)
        if args.test_ugc_voice:
            result = test_ugc_voice(
                args.test_ugc_voice,
                region=args.region,
                pc_version=args.pc_version,
                proxy=args.proxy or "",
                use_cache=bool(args.use_cache),
                preview_text=args.voice_preview_text,
                voice_name=args.voice_name,
            )
            log("\n[UGC Voice Probe Result]")
            log(json.dumps(result, ensure_ascii=False, indent=2)[:30000])
            sys.exit(0 if result.get("ok") else 2)
        if args.conversation_id:
            result = fetch_or_download_conversation(
                args.conversation_id,
                timeout=args.timeout,
                poll_interval=max(1, args.poll_interval),
                region=args.region,
                pc_version=args.pc_version,
                proxy=args.proxy or "",
                download=args.download,
                output=args.output,
                remove_watermark=bool(args.remove_watermark),
                login_cdp=bool(args.login_cdp),
                cdp_url=args.cdp_url,
                login_pure=bool(args.login_pure),
                login_state_file=args.login_state_file,
            )
            log(json.dumps(result, ensure_ascii=False, indent=2)[:6000])
            sys.exit(0 if result.get("status") == "succeeded" else 2)
        if not args.image:
            parser.error("--image is required unless --conversation-id is used")
        effective_wait = (bool(args.wait) or bool(args.download)) and not bool(args.no_wait)
        sys.exit(generate(
            args.image,
            args.prompt,
            wait_for_video=effective_wait,
            timeout=args.timeout,
            poll_interval=max(1, args.poll_interval),
            region=args.region,
            pc_version=args.pc_version,
            duration=args.duration,
            model=args.model,
            ratio=args.ratio or None,
            proxy=args.proxy or "",
            download=args.download,
            output=args.output,
            remove_watermark=bool(args.remove_watermark),
            audio_paths=args.audio,
            audio_prompt=args.audio_prompt,
            attach_audio_to_video_step=bool(args.attach_audio_to_video_step),
            use_cache=bool(args.use_cache),
            login_cdp=bool(args.login_cdp),
            cdp_url=args.cdp_url,
            login_pure=bool(args.login_pure),
            login_state_file=args.login_state_file,
        ))
    except DolaError as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
