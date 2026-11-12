"""豆包账号入口识别；只保存工作号 / 个人号分类，不保存身份字段。"""
from __future__ import annotations

import json
from pathlib import Path

TYPES = ("work", "personal")


def cookie_type(cookies: list[dict]) -> str:
    for cookie in cookies:
        host = str(cookie.get("domain") or "").lower().lstrip(".")
        if (host != "doubao.com" and not host.endswith(".doubao.com")) or cookie.get("name") != "has_biz_token":
            continue
        value = str(cookie.get("value", "")).lower()
        if value in ("true", "1"):
            return "work"
        if value in ("false", "0"):
            return "personal"
    return ""


def state_type(state: dict) -> str:
    saved = state.get("doubao_account_type")
    if saved in TYPES:
        return saved
    cookies = state.get("cookies_list")
    if not isinstance(cookies, list):
        cookies = [dict(value, name=name) for name, value in (state.get("cookies") or {}).items()
                   if isinstance(value, dict)]
    return cookie_type(cookies)


def from_file(path) -> str:
    try:
        return state_type(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return ""


def launch_type(data: dict, fallback: str = "") -> str:
    if data.get("code") not in (0, "0"):
        return fallback
    mode = (data.get("data") or {}).get("conversation_mode_config") or {}
    modes = {mode.get("default_conversation_mode_key_new_chat"), mode.get("default_conversation_mode_key_new_office")}
    for values in (mode.get("conversation_mode_order_map") or {}).values():
        if isinstance(values, list):
            modes.update(value for value in values if isinstance(value, str))
    if "chat" in modes:
        return "personal"
    if "office" in modes:
        return "work"
    return fallback


def remember(path, kind: str) -> None:
    if kind not in TYPES:
        return
    path = Path(path)
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("doubao_account_type") != kind:
        state["doubao_account_type"] = kind
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
