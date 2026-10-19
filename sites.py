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


# ---------------------------------------------------------------- 生图模型（2.4.0）
# 对外模型名 → (站点, 上游模型名（网页显示名，原样下发）, 支持的比例)
# 2026-10-19 网页配置：dola 只有 5.0 Pro / 4.5，比例 6 档、一次出 4 张；
# 豆包多了 5.0 Flash / 4.0、「自动」和 3:2，一次出 1 张。
DOLA_IMAGE_RATIOS = ("1:1", "2:3", "3:4", "4:3", "9:16", "16:9")
DOUBAO_IMAGE_RATIOS = ("auto", "9:16", "2:3", "3:4", "1:1", "4:3", "3:2", "16:9")
IMAGE_MODELS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "dola-seedream-5.0-pro": (DOLA, "Seedream 5.0 Pro", DOLA_IMAGE_RATIOS),
    "dola-seedream-4.5": (DOLA, "Seedream 4.5", DOLA_IMAGE_RATIOS),
    "doubao-seedream-5.0-flash": (DOUBAO, "Seedream 5.0 Flash", DOUBAO_IMAGE_RATIOS),
    "doubao-seedream-5.0-pro": (DOUBAO, "Seedream 5.0 Pro", DOUBAO_IMAGE_RATIOS),
    "doubao-seedream-4.5": (DOUBAO, "Seedream 4.5", DOUBAO_IMAGE_RATIOS),
    "doubao-seedream-4.0": (DOUBAO, "Seedream 4.0", DOUBAO_IMAGE_RATIOS),
}


def image_spec(model) -> tuple[str, str, tuple[str, ...]] | None:
    return IMAGE_MODELS.get(_norm_model(model))


def is_image_model(model) -> bool:
    return _norm_model(model) in IMAGE_MODELS


def _ratio_value(text: str) -> float | None:
    parts = str(text).replace("x", ":").replace("X", ":").replace("×", ":").split(":")
    try:
        w, h = float(parts[0]), float(parts[1])
        return w / h if w > 0 and h > 0 else None
    except (ValueError, IndexError):
        return None


def pick_image_ratio(ratio=None, size=None, allowed: tuple[str, ...] = DOLA_IMAGE_RATIOS) -> str | None:
    """请求里的 ratio（如 16:9）或 OpenAI 的 size（如 1536x1024）→ 站点支持的最接近比例。

    都没给：豆包用「自动」，dola 不传（网页默认）。"""
    raw = str(ratio or "").strip().lower()
    if raw in ("auto", "自动") and "auto" in allowed:
        return "auto"
    want = _ratio_value(raw) if raw else None
    if want is None and size and str(size).lower() != "auto":
        want = _ratio_value(str(size).lower())
    if want is None:
        return "auto" if "auto" in allowed else None
    fixed = [r for r in allowed if r != "auto"]
    return min(fixed, key=lambda r: abs(_ratio_value(r) - want))
