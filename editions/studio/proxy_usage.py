"""ISP / 静态代理请求额度管理。

次数套餐可启用本地上限；流量/时长套餐可关闭次数限制，仅保留统计。
本模块提供：

- 以 (server, username) 的 SHA-256 作为 proxy_key，密码绝不入库。
- 独立 SQLite（proxy_usage.db），避免和 tasks.db / pool_usage.db 抢锁。
- reserve() 在真正发请求之前记录次数；启用次数限制时，额度不足直接拒绝。
- 启用次数限制时达到上限自动 paused（paused_reason=exhausted）；
  统计模式不按次数暂停，仍支持手动暂停 / 恢复 / 重置。
- 浏览器 context 用 attach_request_counter() 在发请求前拦截：
  图片 / 音视频 / 字体 / 常见统计域名直接 abort（不经过 ISP，也不计数），
  其余真正发往 ISP 的 http/https 请求逐个 reserve(1) 预扣，
  额度耗尽立即 abort 请求并关闭 context 硬停。

注意：ISP 供应商的“请求”口径可能与浏览器请求数不完全一致（例如按连接计），
因此这里对真正放行的 http/https 请求按“宁可多算、不可少算”的保守口径统计。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import config


class ProxyQuotaExceeded(RuntimeError):
    """该代理请求额度已用完或已被暂停。"""


def _server_identity(server: str | None) -> str:
    """归一化代理地址：丢掉 scheme，只保留 host:port。

    同一条静态 ISP 线路可能被写成 http:// 或 socks5:// 两种 scheme，
    但它们连的是同一条线路、共享同一份计数，所以身份不能带 scheme。
    """
    s = str(server or "").strip().lower()
    if "://" in s:
        s = s.split("://", 1)[1]
    return s.strip("/")


def proxy_key(proxy: dict | None) -> str:
    """代理身份哈希：host/port + 用户名（不含 scheme、不含密码）。

    故意不带 scheme：同一条线路无论用 http 还是 socks5 都合并统计，
    启用次数限制时也共享同一份额度。
    """
    if not proxy:
        return ""
    server = _server_identity(proxy.get("server"))
    username = str(proxy.get("username") or "").strip().lower()
    raw = f"{server}|{username}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def proxy_label(proxy: dict | None) -> str:
    """面板展示用的安全名称（不含密码）。"""
    if not proxy:
        return ""
    server = str(proxy.get("server") or "")
    user = str(proxy.get("username") or "")
    return f"{server}（{user}）" if user else server


class ProxyUsageStore:
    def __init__(self, db_path: str | None = None,
                 default_limit: int | None = None,
                 warn_at: int | None = None):
        self.db_path = str(db_path or getattr(config, "PROXY_USAGE_DB", "proxy_usage.db"))
        self.default_limit = int(default_limit or getattr(config, "PROXY_REQUEST_LIMIT", 1000))
        self.warn_at = int(warn_at or getattr(config, "PROXY_WARN_AT", 900))
        self.enforce_limits = getattr(config, "PROXY_REQUEST_LIMIT_ENABLED", True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proxy_usage (
                proxy_key TEXT PRIMARY KEY,
                server TEXT DEFAULT '',
                username TEXT DEFAULT '',
                used INTEGER DEFAULT 0,
                limit_value INTEGER DEFAULT 1000,
                warn_at INTEGER DEFAULT 900,
                paused INTEGER DEFAULT 0,
                paused_reason TEXT DEFAULT '',
                updated_at REAL DEFAULT 0
            )
            """
        )
        self._migrate_locked()
        self._conn.commit()
        self._migrate_scheme_keys_locked()
        self._cache: dict[str, dict] = {}
        self._dirty: set[str] = set()
        # 最近一次出片的额度消耗（仅内存，重启后清空；用于面板展示「这次花了多少」）。
        self._last_run: dict[str, dict] = {}
        self._last_flush = time.time()
        self._flush_interval = 1.5

    def _migrate_locked(self) -> None:
        """补齐老库缺失的列（例如 paused_reason）。"""
        try:
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(proxy_usage)")}
        except sqlite3.Error:
            return
        if "paused_reason" not in cols:
            try:
                self._conn.execute(
                    "ALTER TABLE proxy_usage ADD COLUMN paused_reason TEXT DEFAULT ''")
            except sqlite3.Error:
                pass

    def _migrate_scheme_keys_locked(self) -> None:
        """把历史遗留的「同一条 ISP 被拆成 http / socks5 两份额度」合并成一份。

        早期 proxy_key 把 scheme 也算进身份，导致 http:// 与 socks5:// 的同一条
        线路（host/port/用户名相同）各占 1000 次额度，可能让一条只有 1000 次的
        ISP 被花掉 2000 次。这里按新口径（忽略 scheme）合并：used 相加
        （宁可多算、不可少算），上限取最大，暂停状态取并集。
        """
        try:
            rows = self._conn.execute("SELECT * FROM proxy_usage").fetchall()
        except sqlite3.Error:
            return
        if not rows:
            return
        groups: dict[str, list] = {}
        for row in rows:
            key = proxy_key({"server": row["server"], "username": row["username"]})
            if not key:
                continue
            groups.setdefault(key, []).append(row)
        changed = False
        for key, members in groups.items():
            if len(members) == 1 and members[0]["proxy_key"] == key:
                continue
            used = sum(max(0, int(m["used"] or 0)) for m in members)
            limit = max(int(m["limit_value"] or self.default_limit) for m in members)
            warn_at = min(max(int(m["warn_at"] or self.warn_at) for m in members), limit)
            paused = 1 if any(int(m["paused"] or 0) for m in members) else 0
            reason = ""
            for m in members:
                if int(m["paused"] or 0) and (m["paused_reason"] or ""):
                    reason = m["paused_reason"]
                    break
            if self.enforce_limits and used >= limit:
                paused, reason = 1, "exhausted"
            # 展示用 server 优先保留 http，避免面板上同一条线路显示不一致。
            server = next((m["server"] for m in members
                           if m["server"] and "socks" not in str(m["server"]).lower()),
                          members[0]["server"] or "")
            username = members[0]["username"] or ""
            updated_at = max(float(m["updated_at"] or 0) for m in members)
            self._conn.execute(
                "DELETE FROM proxy_usage WHERE proxy_key IN (%s)"
                % ",".join("?" * len(members)),
                [m["proxy_key"] for m in members],
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO proxy_usage"
                "(proxy_key, server, username, used, limit_value, warn_at, paused, paused_reason, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (key, server, username, used, limit, warn_at, paused, reason, updated_at),
            )
            changed = True
        if changed:
            self._conn.commit()

    # ===== 内部 =====

    def _decorate(self, state: dict) -> dict:
        key = state.get("proxy_key") or state.get("key") or ""
        limit = max(1, int(state.get("limit_value") or self.default_limit))
        warn_at = max(0, int(state.get("warn_at") or self.warn_at))
        used = max(0, int(state.get("used") or 0))
        paused = bool(state.get("paused"))
        reason = str(state.get("paused_reason") or "")
        if not self.enforce_limits and reason == "exhausted":
            paused, reason = False, ""
        exhausted = self.enforce_limits and used >= limit
        remaining = max(0, limit - used) if self.enforce_limits else None
        return {
            "key": key,
            "server": state.get("server") or "",
            "username": state.get("username") or "",
            "used": used,
            "limit": limit,
            "warn_at": warn_at,
            "remaining": remaining,
            "unlimited": not self.enforce_limits,
            "paused": paused,
            "paused_reason": reason,
            "exhausted": exhausted,
            "blocked": paused or exhausted,
            "warn": self.enforce_limits and (not exhausted) and used >= warn_at,
            "updated_at": float(state.get("updated_at") or 0),
            "last_run_cost": int((self._last_run.get(key) or {}).get("cost") or 0),
            "last_run_at": float((self._last_run.get(key) or {}).get("at") or 0),
            "last_run_account": str((self._last_run.get(key) or {}).get("account") or ""),
        }

    def _load_locked(self, key: str, proxy: dict | None = None) -> dict:
        if key in self._cache:
            return self._cache[key]
        row = self._conn.execute(
            "SELECT * FROM proxy_usage WHERE proxy_key=?", (key,)
        ).fetchone()
        if row:
            state = {
                "proxy_key": row["proxy_key"],
                "server": row["server"] or "",
                "username": row["username"] or "",
                "used": int(row["used"] or 0),
                "limit_value": int(row["limit_value"] or self.default_limit),
                "warn_at": int(row["warn_at"] or self.warn_at),
                "paused": int(row["paused"] or 0),
                "paused_reason": (row["paused_reason"] if "paused_reason" in row.keys() else "") or "",
                "updated_at": float(row["updated_at"] or 0),
            }
        else:
            state = {
                "proxy_key": key,
                "server": (proxy or {}).get("server", "") or "",
                "username": (proxy or {}).get("username", "") or "",
                "used": 0,
                "limit_value": self.default_limit,
                "warn_at": self.warn_at,
                "paused": 0,
                "paused_reason": "",
                "updated_at": time.time(),
            }
        self._cache[key] = state
        return state

    def _flush_locked(self, force: bool = False) -> None:
        now = time.time()
        if not self._dirty:
            return
        if not force and now - self._last_flush < self._flush_interval:
            return
        rows = []
        for key in list(self._dirty):
            state = self._cache.get(key)
            if not state:
                continue
            rows.append((
                key, state.get("server", ""), state.get("username", ""),
                int(state.get("used", 0)), int(state.get("limit_value", self.default_limit)),
                int(state.get("warn_at", self.warn_at)), 1 if state.get("paused") else 0,
                str(state.get("paused_reason") or ""),
                float(state.get("updated_at") or now),
            ))
        if rows:
            self._conn.executemany(
                "INSERT INTO proxy_usage(proxy_key, server, username, used, limit_value, warn_at, paused, paused_reason, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(proxy_key) DO UPDATE SET server=excluded.server, username=excluded.username, "
                "used=excluded.used, limit_value=excluded.limit_value, warn_at=excluded.warn_at, "
                "paused=excluded.paused, paused_reason=excluded.paused_reason, updated_at=excluded.updated_at",
                rows,
            )
            self._conn.commit()
            self._dirty.clear()
        self._last_flush = now

    def flush(self) -> None:
        with self._lock:
            self._flush_locked(force=True)

    # ===== 对外 =====

    def get(self, proxy: dict | None) -> dict:
        """查询某个代理的额度状态；不存在则返回默认 0/limit。"""
        key = proxy_key(proxy)
        if not key:
            return self._decorate({"proxy_key": "", "used": 0,
                                   "limit_value": self.default_limit, "warn_at": self.warn_at})
        with self._lock:
            state = self._load_locked(key, proxy)
            if proxy:
                changed = False
                # 同一条线路可能被写成 http:// 或 socks5://，额度已按线路合并；
                # 这里只在字段为空时补全，避免每次查询都把展示用 server 改来改去。
                if proxy.get("server") and not state.get("server"):
                    state["server"] = proxy.get("server")
                    changed = True
                if proxy.get("username") and not state.get("username"):
                    state["username"] = proxy.get("username")
                    changed = True
                if changed:
                    self._dirty.add(key)
            return self._decorate(state)

    def get_by_key(self, key: str) -> dict:
        key = (key or "").strip()
        if not key:
            return self._decorate({"proxy_key": "", "used": 0,
                                   "limit_value": self.default_limit, "warn_at": self.warn_at})
        with self._lock:
            state = self._load_locked(key)
            return self._decorate(state)

    def reserve(self, proxy: dict | None, n: int = 1) -> dict:
        """发请求前预扣 n 次额度。

        - 手动暂停：直接抛 ProxyQuotaExceeded（不修改计数）。
        - 统计模式：记录 used += n，不因次数拦截。
        - 次数限制模式：used + n > limit 拒绝请求；达到上限后标记 exhausted。
        """
        n = max(1, int(n))
        key = proxy_key(proxy)
        if not key:
            return self.get(None)
        with self._lock:
            state = self._load_locked(key, proxy)
            if proxy:
                state["server"] = state.get("server") or proxy.get("server") or ""
                state["username"] = state.get("username") or proxy.get("username") or ""
            limit = int(state.get("limit_value") or self.default_limit)
            used = max(0, int(state.get("used") or 0))
            if not self.enforce_limits and state.get("paused_reason") == "exhausted":
                state["paused"] = 0
                state["paused_reason"] = ""
            if int(state.get("paused") or 0):
                reason = str(state.get("paused_reason") or "")
                self._flush_locked(force=False)
                if reason == "manual":
                    raise ProxyQuotaExceeded(
                        f"ISP {state.get('server') or key[:8]} 已被手动暂停（{used}/{limit}），已阻止请求")
                raise ProxyQuotaExceeded(
                    f"ISP {state.get('server') or key[:8]} 额度已用完（{used}/{limit}），已阻止请求")
            if self.enforce_limits and used + n > limit:
                if used >= limit:
                    state["paused"] = 1
                    state["paused_reason"] = "exhausted"
                    state["updated_at"] = time.time()
                    self._cache[key] = state
                    self._dirty.add(key)
                    self._flush_locked(force=True)
                raise ProxyQuotaExceeded(
                    f"ISP {state.get('server') or key[:8]} 剩余额度不足（{used}/{limit}，本次需要 {n}），已阻止请求")
            state["used"] = used + n
            state["updated_at"] = time.time()
            if self.enforce_limits and state["used"] >= limit:
                state["paused"] = 1
                state["paused_reason"] = "exhausted"
            self._cache[key] = state
            self._dirty.add(key)
            # 预扣必须立即落盘：崩溃时宁可多算也不能少算，否则会超发。
            self._flush_locked(force=True)
            return self._decorate(state)

    def add(self, proxy: dict | None, n: int = 1) -> dict:
        """事后计数 +n（仅保留给非网络场景；网络请求请用 reserve）。"""
        n = max(0, int(n))
        key = proxy_key(proxy)
        if not key:
            return self.get(None)
        with self._lock:
            state = self._load_locked(key, proxy)
            if proxy:
                state["server"] = state.get("server") or proxy.get("server") or ""
                state["username"] = state.get("username") or proxy.get("username") or ""
            state["used"] = max(0, int(state.get("used") or 0)) + n
            state["updated_at"] = time.time()
            if self.enforce_limits and state["used"] >= int(state.get("limit_value") or self.default_limit):
                state["paused"] = 1
                state["paused_reason"] = "exhausted"
            self._cache[key] = state
            self._dirty.add(key)
            self._flush_locked(force=bool(state.get("paused")) or n == 0)
            return self._decorate(state)

    def set_limit(self, proxy: dict | None, limit: int,
                  warn_at: int | None = None) -> dict:
        key = proxy_key(proxy)
        limit = max(1, int(limit))
        with self._lock:
            state = self._load_locked(key, proxy)
            old_limit = max(1, int(state.get("limit_value") or self.default_limit))
            old_warn = int(state.get("warn_at") or self.warn_at)
            state["limit_value"] = limit
            if warn_at is not None:
                state["warn_at"] = min(max(0, int(warn_at)), limit)
            elif old_warn > 0:
                # 上限放大时按原比例同步预警线，避免 1000→100000 后仍停在 900。
                state["warn_at"] = min(limit, max(1, round(old_warn * limit / old_limit)))
            else:
                state["warn_at"] = min(limit, max(0, int(self.warn_at)))
            used = int(state.get("used") or 0)
            # 只因额度耗尽被自动暂停时，放宽上限后自动恢复；手动暂停不自动恢复。
            if used < limit and int(state.get("paused") or 0) \
                    and str(state.get("paused_reason") or "") == "exhausted":
                state["paused"] = 0
                state["paused_reason"] = ""
            state["updated_at"] = time.time()
            self._dirty.add(key)
            self._flush_locked(force=True)
            return self._decorate(state)

    def set_warn_at(self, proxy: dict | None, warn_at: int) -> dict:
        key = proxy_key(proxy)
        with self._lock:
            state = self._load_locked(key, proxy)
            limit = max(1, int(state.get("limit_value") or self.default_limit))
            state["warn_at"] = min(max(0, int(warn_at)), limit)
            state["updated_at"] = time.time()
            self._dirty.add(key)
            self._flush_locked(force=True)
            return self._decorate(state)

    def set_paused(self, proxy: dict | None, paused: bool, reason: str = "") -> dict:
        key = proxy_key(proxy)
        with self._lock:
            state = self._load_locked(key, proxy)
            if paused:
                state["paused"] = 1
                state["paused_reason"] = reason or "manual"
            else:
                state["paused"] = 0
                state["paused_reason"] = ""
            state["updated_at"] = time.time()
            self._dirty.add(key)
            self._flush_locked(force=True)
            return self._decorate(state)

    def reset(self, proxy: dict | None) -> dict:
        key = proxy_key(proxy)
        with self._lock:
            state = self._load_locked(key, proxy)
            state["used"] = 0
            state["paused"] = 0
            state["paused_reason"] = ""
            state["updated_at"] = time.time()
            self._dirty.add(key)
            self._flush_locked(force=True)
            return self._decorate(state)

    def is_available(self, proxy: dict | None) -> bool:
        state = self.get(proxy)
        return bool(state["key"]) and not state["blocked"]

    def ensure_available(self, proxy: dict | None) -> dict:
        state = self.get(proxy)
        if state["key"] and state["blocked"]:
            reason = "已用完" if state["exhausted"] else "已被暂停"
            raise ProxyQuotaExceeded(
                f"ISP {state['server'] or state['key'][:8]} {reason}（{state['used']}/{state['limit']}），已阻止请求"
            )
        return state

    def list_all(self, configured: list[dict] | None = None) -> list[dict]:
        """列出所有额度记录；configured 传入已配置但可能未使用的代理，一并显示。"""
        with self._lock:
            if configured:
                for proxy in configured:
                    key = proxy_key(proxy)
                    if not key:
                        continue
                    state = self._load_locked(key, proxy)
                    if proxy.get("server") and state.get("server") != proxy.get("server"):
                        state["server"] = proxy.get("server")
                        self._dirty.add(key)
                    if proxy.get("username") and state.get("username") != proxy.get("username"):
                        state["username"] = proxy.get("username")
                        self._dirty.add(key)
            self._flush_locked(force=True)
            rows = self._conn.execute("SELECT * FROM proxy_usage ORDER BY used DESC").fetchall()
            out = []
            seen = set()
            for row in rows:
                out.append(self._decorate({
                    "proxy_key": row["proxy_key"], "server": row["server"],
                    "username": row["username"], "used": row["used"],
                    "limit_value": row["limit_value"], "warn_at": row["warn_at"],
                    "paused": row["paused"],
                    "paused_reason": (row["paused_reason"] if "paused_reason" in row.keys() else ""),
                    "updated_at": row["updated_at"],
                }))
                seen.add(row["proxy_key"])
            for key, state in self._cache.items():
                if key in seen:
                    continue
                out.append(self._decorate(state))
            return out


usage = ProxyUsageStore()


def _close_context_later(context) -> None:
    """在事件循环里安全地关闭浏览器 context（用于额度耗尽时硬停）。"""
    async def _close():
        try:
            await context.close()
        except Exception:
            pass
    try:
        asyncio.create_task(_close())
    except Exception:
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(_close())
        except Exception:
            pass


# ===== 省额度：发请求前先丢掉无价值的流量 =====

# 这些资源类型对页面功能没有价值，直接 abort，既不经过 ISP 也不计数。
# 注意：实测发现把 image/font 全拦会导致 dola 底部技能条（「创建视频」入口）
# 迟迟渲染不出来，慢线上尤其明显（等入口 240s 仍不出现）。入口依赖页面资源
# 完整加载，所以这里只拦 media（音视频），image/font 放行，避免帮倒忙。
_SKIP_RESOURCE_TYPES = frozenset({"media"})

# 常见第三方统计 / 广告 / 埋点域名，直接 abort（命中即丢弃，不发往 ISP）。
_BLOCK_HOST_SUFFIXES = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "googleadservices.com", "googlesyndication.com", "adservice.google.com",
    "connect.facebook.net", "hotjar.com", "hotjar.io", "clarity.ms",
    "sentry.io", "sentry-cdn.com", "bugsnag.com", "amplitude.com",
    "mixpanel.com", "segment.io", "segment.com", "branch.io",
    "appsflyer.com", "adjust.com", "umeng.com", "umengcloud.com",
    "cnzz.com", "talkingdata.com", "talkingdata.net", "growingio.com",
    "matomo.cloud", "plausible.io", "newrelic.com", "nr-data.net",
    "datadoghq.com", "optimizely.com", "criteo.com", "criteo.net",
    "taboola.com", "outbrain.com", "adsrvr.org", "rubiconproject.com",
    "pubmatic.com", "casalemedia.com", "moatads.com", "scorecardresearch.com",
)


# 业务必需的首方图片域名（dola / 字节图床）：滑块底图、拼图、参考图缩略图都在这些域上，
# 一旦拦掉会出现「captcha images load timeout」，所以这些域名必须放行（放行即正常计数）。
_ALLOW_IMAGE_HOST_SUFFIXES = tuple(
    s.strip().lower() for s in os.getenv(
        "DOLA_PROXY_ALLOW_HOSTS",
        "ibyteimg.com,byteimg.com,ibytedtos.com,volces.com,volcengine.com,"
        "dola.com,doubao.com,bytedance.com",
    ).split(",") if s.strip()
)

# 滑块 iframe 关键字：来自该框架的图片必须放行，否则滑块无法识别。
_CAPTCHA_FRAME_KEY = "bdcaptcha"


def _host_matches(host: str, suffixes) -> bool:
    for suffix in suffixes:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def _request_frame_url(request) -> str:
    try:
        frame = getattr(request, "frame", None)
        return (getattr(frame, "url", "") or "") if frame else ""
    except Exception:
        return ""


def _should_skip_request(request, url: str) -> bool:
    """判断某个请求是否可以为了省 ISP 额度直接丢弃（不发出、不计数）。"""
    try:
        rtype = (request.resource_type or "").strip().lower()
    except Exception:
        rtype = ""
    try:
        host = (urlsplit(url).hostname or "").strip().lower()
    except Exception:
        host = ""
    # 统计 / 广告域名：无论什么类型都丢掉。
    if host and _host_matches(host, _BLOCK_HOST_SUFFIXES):
        return True
    if rtype in _SKIP_RESOURCE_TYPES:
        # 滑块 iframe 里的图片是业务必需的，放行（放行即计数）。
        if _CAPTCHA_FRAME_KEY in _request_frame_url(request):
            return False
        # 首方图床（滑块素材 / 参考图缩略图）放行。
        if host and _host_matches(host, _ALLOW_IMAGE_HOST_SUFFIXES):
            return False
        # 其余第三方图片 / 音视频 / 字体：直接丢掉，不经过 ISP、不计数。
        return True
    return False


def reserve_for_account(account: str | None, n: int = 1,
                        proxy: dict | None = None) -> dict | None:
    """非浏览器路径（aiohttp 下载 / 抓图 / 探测）在发请求前预扣额度。

    口径与 browser.launch_account_context 保持一致：
    只有账号专属 ISP（proxies.json 显式映射或 accounts/<name>/proxy.txt）才计入额度，
    全局共享出口（map_default / global）不计数。不计额度的场景返回 None。
    """
    if proxy is None:
        try:
            proxy = config.proxy_for(account)
        except Exception:
            proxy = None
    if not proxy:
        return None
    try:
        _raw, source = config.account_proxy_raw(account or "")
    except Exception:
        source = ""
    if source not in ("file", "map"):
        return None
    return usage.reserve(proxy, n)


def begin_run(account: str | None, proxy: dict | None = None):
    """出片开始：记录起始 used，用于统计「这次出片花了多少次」。

    只有账号专属 ISP 才统计；共享出口返回 None（调用方原样传回 finish_run 即可）。
    """
    if proxy is None:
        try:
            proxy = config.proxy_for(account)
        except Exception:
            proxy = None
    if not proxy:
        return None
    try:
        _raw, source = config.account_proxy_raw(account or "")
    except Exception:
        source = ""
    if source not in ("file", "map"):
        return None
    key = proxy_key(proxy)
    if not key:
        return None
    try:
        state = usage.get(proxy)
    except Exception:
        return None
    return (key, int(state.get("used") or 0), account or "")


def finish_run(run) -> int | None:
    """出片结束：把本次 used 增量记为 last_run（仅内存）。"""
    if not run:
        return None
    try:
        key, start_used, account = run
    except Exception:
        return None
    try:
        used = int(usage.get_by_key(key).get("used") or 0)
    except Exception:
        return None
    cost = max(0, used - int(start_used))
    usage._last_run[key] = {"cost": cost, "at": time.time(), "account": account,
                            "start_used": int(start_used), "end_used": used}
    return cost


async def attach_request_counter(context, account: str, proxy: dict | None) -> None:
    """给浏览器 context 挂请求拦截器，尽量省 ISP 额度。

    1. 图片 / 音视频 / 字体 以及常见第三方统计域名直接 abort：
       这些请求根本不发往 ISP，既不消耗真实额度，也不计入 used。
    2. 其余真正发往 ISP 的 http/https 请求，在发出前 reserve(1) 预扣；
       额度不足立即 abort 并关闭 context，保证绝不超发。
    """
    if not proxy:
        return None
    key = proxy_key(proxy)
    if not key:
        return None
    state = usage.get(proxy)
    if state["blocked"]:
        raise ProxyQuotaExceeded(
            f"ISP {state['server'] or key[:8]} 额度已用完或已暂停，拒绝启动账号 {account}"
        )
    closing = {"done": False}

    async def _handler(route):
        request = route.request
        url = ""
        try:
            url = request.url or ""
        except Exception:
            url = ""
        if not (url.startswith("http://") or url.startswith("https://")):
            await route.fallback()
            return
        # 省额度：无用资源 / 统计域名直接拦截，不经过 ISP，也不计数。
        if _should_skip_request(request, url):
            try:
                await route.abort()
            except Exception:
                pass
            return
        new_state = None
        try:
            new_state = usage.reserve(proxy, 1)
        except ProxyQuotaExceeded as e:
            try:
                await route.abort()
            except Exception:
                pass
            if not closing["done"]:
                closing["done"] = True
                print(f"[proxy-usage] {account} 的 ISP 请求被拦截：{e}", flush=True)
                _close_context_later(context)
            return
        except Exception:
            # 计数异常时保守放行（避免因为本地 bug 卡死业务）。
            new_state = None
        if new_state and new_state.get("blocked") and not closing["done"]:
            closing["done"] = True
            print(
                f"[proxy-usage] {account} 的 ISP {new_state['server']} 达到 "
                f"{new_state['used']}/{new_state['limit']}，自动暂停并关闭浏览器",
                flush=True,
            )
            _close_context_later(context)
        # 用 fallback 而不是 continue_：Playwright 的 route 是后注册先执行（LIFO），
        # 本 handler 注册得比 duration 改写器晚，若在此 continue_ 会把
        # /chat/completion 的时长改写器整条吞掉。fallback 交给下游 handler；
        # 没有下游时 Playwright 会自动继续请求。
        await route.fallback()

    await context.route("**/*", _handler)
    return _handler
