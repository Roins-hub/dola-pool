"""站点识别：账号属于 dola.com 还是 doubao.com，以及对外模型名该派给哪个站点的号。

- 账号站点看 cookie 的域名（导入时就定了，不用另存字段）；
- 豆包模型只认下面这几个精确名字。new-api 的 Doubao 任务插件用的官方名
  （doubao-seedance-2-0-260128 等）**不在其中**，照旧按 dola 处理，老接法不受影响。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

DOLA = "dola"
DOUBAO = "doubao"
SITE_LABELS = {DOLA: "dola", DOUBAO: "豆包"}

# 对外模型名 → (上游模型, 最长秒数, 是否只有会员能用, 面板显示名)
DOUBAO_MODELS: dict[str, tuple[str, int, bool, str]] = {
    "doubao-seedance-2.0-mini": ("seedance_v2.0_mini", 15, False, "Seedance 2.0 Mini"),
    "doubao-seedance-2.0-fast": ("seedance_v2.0", 15, False, "Seedance 2.0 Fast"),
    "doubao-seedance-2.0": ("seedance_v2.0_std", 15, True, "Seedance 2.0"),
    "doubao-seedance-2.5": ("seedance_v2.5", 30, True, "Seedance 2.5"),
}
DOUBAO_MIN_DURATION = 4


def _norm_model(model) -> str:
    return str(model or "").strip().lower().replace("_", "-")


def is_doubao_model(model) -> bool:
    return _norm_model(model) in DOUBAO_MODELS


def doubao_spec(model) -> tuple[str, int, bool, str] | None:
    return DOUBAO_MODELS.get(_norm_model(model))


def canonical_doubao_model(model) -> str:
    return _norm_model(model)


def site_for_model(model) -> str:
    return DOUBAO if is_doubao_model(model) else DOLA


def doubao_member_only(model) -> bool:
    spec = doubao_spec(model)
    return bool(spec and spec[2])


def site_of_cookies(cookies) -> str:
    """cookie 列表（dict 带 domain）里有 doubao.com 的会话 cookie → 豆包号，否则 dola。"""
    for c in cookies or []:
        if isinstance(c, dict) and "doubao.com" in str(c.get("domain") or ""):
            return DOUBAO
    return DOLA


_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_LOCK = threading.Lock()


def site_of_state_file(path) -> str:
    """accounts/<号>/cookie_state.json → 站点（按文件修改时间缓存）。读不到一律当 dola（老行为）。"""
    if not path:
        return DOLA
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return DOLA
    key = str(p)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DOLA
    cookies = state.get("cookies_list")
    if not isinstance(cookies, list):
        raw = state.get("cookies")
        cookies = ([{"domain": (v or {}).get("domain")} for v in raw.values()]
                   if isinstance(raw, dict) else [])
    site = site_of_cookies(cookies)
    with _CACHE_LOCK:
        _CACHE[key] = (mtime, site)
    return site
