"""Export saved account cookies without opening browsers or touching upstream services."""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path


class CookieExportError(ValueError):
    pass


def _read_snapshot(path: Path, root: Path):
    if not path.resolve().is_relative_to(root):
        raise CookieExportError("Cookie 文件路径不合法")
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise CookieExportError("Cookie 快照过大")
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise CookieExportError("没有已保存的 Cookie 快照；仅有浏览器 profile 的账号暂不支持导出") from None
    except (OSError, ValueError) as exc:
        if isinstance(exc, CookieExportError):
            raise
        # Never return parser excerpts or filesystem errors that may contain secret values.
        raise CookieExportError("Cookie 快照无法读取或格式损坏") from None


def account_bundle(accounts_dir: Path, name: str, profile_subdir: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise CookieExportError("账号名不合法")
    root = Path(accounts_dir).resolve()
    account = root / name
    if not account.resolve().is_relative_to(root):
        raise CookieExportError("账号路径不合法")
    # A saved empty Cloak snapshot means logout. Never resurrect the original import.
    snapshot = account / profile_subdir / ".cloak-session-cookies.json"
    if snapshot.exists():
        raw = _read_snapshot(snapshot, root)
        if not isinstance(raw, list):
            raise CookieExportError("Cookie 快照格式损坏")
        source = "cloak_snapshot"
    else:
        state = _read_snapshot(account / "cookie_state.json", root)
        if not isinstance(state, dict):
            raise CookieExportError("Cookie 快照格式损坏")
        raw = state.get("cookies_list")
        if raw is None:
            raw = state.get("cookies")
            if isinstance(raw, dict):
                raw = [dict(value, name=key) if isinstance(value, dict)
                       else {"name": key, "value": value, "domain": ".dola.com"}
                       for key, value in raw.items()]
        source = "cookie_state"
    if not isinstance(raw, list):
        raise CookieExportError("Cookie 快照格式损坏")
    cookies = []
    fields = ("name", "value", "domain", "path", "expires", "expirationDate",
              "httpOnly", "secure", "sameSite", "hostOnly", "session",
              "isHttpOnly", "isSecure", "isSession", "expiresUtc")
    for item in raw:
        if not isinstance(item, dict):
            raise CookieExportError("Cookie 快照格式损坏")
        host = str(item.get("domain") or "").lower().lstrip(".")
        if not any(host == site or host.endswith("." + site)
                   for site in ("dola.com", "doubao.com")):
            continue
        if not isinstance(item.get("name"), str) or not item["name"] or not isinstance(item.get("value"), str):
            raise CookieExportError("Cookie 快照缺少有效的 name/value")
        cookies.append({k: item[k] for k in fields if k in item})
        cookies[-1].setdefault("path", "/")
    if not cookies:
        raise CookieExportError("快照中没有可导出的 Dola/豆包 Cookie（可能已退出登录）")
    return {"format": "dola-pool-cookies-v1", "account": name,
            "source": source, "cookies": cookies}


def build_export(accounts_dir: Path, names: list[str], profile_subdir: str):
    """Single-account JSON, or ZIP with one import-compatible JSON per account and a manifest."""
    bundles, failed = [], []
    total_bytes = 0
    for name in names:
        try:
            bundle = account_bundle(accounts_dir, name, profile_subdir)
            total_bytes += len(json.dumps(bundle, ensure_ascii=False).encode("utf-8"))
            if total_bytes > 32 * 1024 * 1024:
                raise CookieExportError("导出内容超过 32 MB，请减少账号数量后重试")
            bundles.append(bundle)
        except CookieExportError as exc:
            if total_bytes > 32 * 1024 * 1024:
                raise
            failed.append({"account": name, "reason": str(exc)})
    if not bundles:
        raise CookieExportError("没有账号可导出：" + "；".join(
            f"{x['account']}：{x['reason']}" for x in failed))
    encode = lambda data: json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    if len(names) == 1:
        return encode(bundles[0]), "application/json", names[0] + ".json", 1, 0
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for bundle in bundles:
            archive.writestr(bundle["account"] + ".json", encode(bundle))
        archive.writestr("_export/manifest.json", encode({
            "exported": [b["account"] for b in bundles], "failed": failed,
            "note": "Saved cookies only, not a live login check. Import the account JSON files, not this manifest.",
        }))
    return output.getvalue(), "application/zip", "dola-account-cookies.zip", len(bundles), len(failed)
