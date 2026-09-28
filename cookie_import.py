"""Cookie 文本解析（对齐 api-pool 的 server.cookie_import）。

把两种粘贴格式都解析成可导入的 state 列表：
1. 一行一个 Cookie 头：
     passport_csrf_token=aaa; sessionid=sid-one; sid_guard=g1; uid_tt=u1; store-country-code=hk
2. 浏览器扩展导出的 Cookie JSON（一个数组 = 一个账号，可连续粘贴多段）：
     [{"creation_time":"1789997155","domain":"www.dola.com","name":"sessionid","value":"...","path":"/"}]

自动跳过注释/空行/邮箱凭据行，并可按 sessionid 做去重。

纯解析层，不依赖浏览器/HTTP，可直接单测。
"""
from __future__ import annotations

import json
import re


_EMAIL_CRED_SEP = re.compile(r"@[^@\s]+\.[^@\s]+\s*(?:----|\||,|:|\s)")
_JSON_BUNDLE_KEYS = ("cookies_list", "cookies")


def parse_cookie_header_line(line: str) -> list[dict]:
    """解析一行 Cookie 头为 name/value 列表，domain 统一 .dola.com。"""
    line = (line or "").strip().strip(";")
    if not line or line.startswith("#"):
        return []
    # 抓包工具/插件复制出来常带 "Cookie: " 前缀，去掉再解析
    if line[:7].lower() == "cookie:":
        line = line[7:].strip()
        if not line:
            return []
    cookies = []
    for pair in line.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".dola.com",
            "path": "/",
        })
    return cookies


def _is_email_cred_line(line: str) -> bool:
    """形如 email----password / email|password 的凭据行，不属于 Cookie 头。"""
    return bool(_EMAIL_CRED_SEP.search(line or ""))


def sessionid_from_line(line: str) -> str:
    for c in parse_cookie_header_line(line):
        if c["name"] == "sessionid" and c["value"]:
            return c["value"]
    return ""


def _state_from_cookies(cookies: list[dict], header: str = "") -> dict:
    """组装 state。header 为空时不写 cookie_header，由 cookies_list 反推（反推会按域名过滤）。"""
    state: dict = {"cookies_list": cookies, "cookies": {}}
    if header:
        state["cookie_header"] = header
    for c in cookies:
        state["cookies"][c["name"]] = {
            "value": c["value"],
            "domain": c["domain"],
            "path": c["path"],
        }
    return state


def _cookies_from_json_items(items: list) -> list[dict]:
    """Cookie JSON 对象 → name/value/domain/path。

    只保留 dola 域（与纯 API 路径 apply_state_cookies 的过滤口径一致，避免把扩展导出的
    第三方站点 cookie 塞进 dola 会话）；扩展导出的其它字段下游用不到，落盘时也只留这四个。
    """
    cookies = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        domain = str(item.get("domain") or "").strip() or ".dola.com"
        if "dola.com" not in domain:
            continue
        cookies.append({
            "name": name,
            "value": str(item.get("value") or ""),
            "domain": domain,
            "path": str(item.get("path") or "").strip() or "/",
        })
    return cookies


def parse_cookie_json_text(raw: str) -> list[dict]:
    """Cookie JSON 文本 → state 列表（一个数组一个账号，支持连续多段）。

    支持顶层数组，以及 {"cookies_list": [...]} / {"cookies": [...]} 两种包装对象。
    """
    decoder = json.JSONDecoder()
    states: list[dict] = []
    pos = 0
    while pos < len(raw):
        while pos < len(raw) and raw[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(raw):
            break
        try:
            data, pos = decoder.raw_decode(raw, pos)
        except ValueError:
            return []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = next(
                (data[key] for key in _JSON_BUNDLE_KEYS
                 if isinstance(data.get(key), list)),
                None,
            )
        else:
            items = None
        cookies = _cookies_from_json_items(items) if items else []
        if cookies:
            states.append(_state_from_cookies(cookies))
    return states


def cookie_header_to_state(header: str) -> dict | None:
    """把一行 Cookie 头转成 state；非 cookie 头（邮箱凭据/无 =）返回 None。"""
    if _is_email_cred_line(header):
        return None
    cookies = parse_cookie_header_line(header)
    if not cookies:
        return None
    return _state_from_cookies(cookies, header.strip().strip(";"))


def sessionid_from_state(state: dict) -> str:
    return (state.get("cookies") or {}).get("sessionid", {}).get("value", "")


def parse_cookie_header_text(raw: str) -> list[dict]:
    """多行文本 → state 列表；Cookie JSON 数组与 Cookie 头两种格式都支持。"""
    raw = (raw or "").lstrip("\ufeff")
    if raw.lstrip().startswith(("[", "{")):
        states = parse_cookie_json_text(raw)
        if states:
            return states
    states = []
    for line in raw.splitlines():
        state = cookie_header_to_state(line)
        if state:
            states.append(state)
    return states
