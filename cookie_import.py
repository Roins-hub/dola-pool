"""多行 Cookie 头文本解析（对齐 api-pool 的 server.cookie_import）。

把「一行一个 Cookie 头」的文本解析成可导入的 state 列表：
  passport_csrf_token=aaa; sessionid=sid-one; sid_guard=g1; uid_tt=u1; store-country-code=hk
自动跳过注释/空行/邮箱凭据行，并可按 sessionid 做去重。

纯解析层，不依赖浏览器/HTTP，可直接单测。
"""
from __future__ import annotations

import re


_EMAIL_CRED_SEP = re.compile(r"@[^@\s]+\.[^@\s]+\s*(?:----|\||,|:|\s)")


def parse_cookie_header_line(line: str) -> list[dict]:
    """解析一行 Cookie 头为 name/value 列表，domain 统一 .dola.com。"""
    line = (line or "").strip().strip(";")
    if not line or line.startswith("#"):
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


def cookie_header_to_state(header: str) -> dict | None:
    """把一行 Cookie 头转成 state；非 cookie 头（邮箱凭据/无 =）返回 None。"""
    if _is_email_cred_line(header):
        return None
    cookies = parse_cookie_header_line(header)
    if not cookies:
        return None
    state: dict = {
        "cookie_header": header.strip().strip(";"),
        "cookies_list": cookies,
        "cookies": {},
    }
    for c in cookies:
        state["cookies"][c["name"]] = {
            "value": c["value"],
            "domain": c["domain"],
            "path": c["path"],
        }
    return state


def sessionid_from_state(state: dict) -> str:
    return (state.get("cookies") or {}).get("sessionid", {}).get("value", "")


def parse_cookie_header_text(raw: str) -> list[dict]:
    """多行 Cookie 头文本 → state 列表，跳过注释/空行/邮箱凭据行。"""
    states = []
    for line in (raw or "").splitlines():
        state = cookie_header_to_state(line)
        if state:
            states.append(state)
    return states
