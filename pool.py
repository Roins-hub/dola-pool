"""账号池选号引擎（移植自 api-pool 的 server.pool.AccountPool）。

纯状态机 + 配额 + 路由 + 代理隔离，不依赖浏览器/HTTP 库，可独立单测。
浏览器 worker（browser_pool.BrowserPool）用它做选号，保留自己的出片逻辑。

状态：standby / healthy / cooldown / expired / unsigned / checking。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


STATUS_STANDBY = "standby"
STATUS_HEALTHY = "healthy"
STATUS_COOLDOWN = "cooldown"
STATUS_EXPIRED = "expired"
STATUS_UNSIGNED = "unsigned"
STATUS_CHECKING = "checking"

DEFAULT_COOLDOWN_SECONDS = 14400
_AUTH_FAILURE_MARKERS = ("not logged in", "未登录", "login state not found", "log in", "需要登录")


def local_day() -> str:
    return date.today().isoformat()


def session_proxy(proxy: str, enabled: bool = True) -> str:
    """空代理或总闸关闭 => 'direct'。"""
    if not enabled:
        return "direct"
    proxy = (proxy or "").strip()
    return proxy or "direct"


def _egress_of(proxy: str) -> str:
    """归一化代理出口为 scheme://host:port，去掉凭据；空/直连 => 'direct'。"""
    proxy = (proxy or "").strip()
    if not proxy or proxy == "direct":
        return "direct"
    if "://" not in proxy:
        proxy = "http://" + proxy
    parts = urlsplit(proxy)
    if not parts.hostname:
        return "direct"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{parts.hostname}:{port}"


@dataclass
class AccountConfig:
    id: str
    state_file: Path
    profile_dir: str = ""
    proxy: str = ""
    max_inflight: int = 1
    weight: int = 1
    enabled: bool = True
    proxy_account_id: str = ""
    # 显式出口身份。动态代理（隧道/旋转网关）上多个号共用 host:port，
    # 靠 username 里的 sticky session 区分出口，必须由调用方显式给出；
    # 留空时回落到按 host:port 归一化（静态代理的历史行为）。
    egress_key: str = ""


@dataclass
class ProxyAccountConfig:
    id: str
    proxy: str


class ProxyBindError(RuntimeError):
    def __init__(self, code: str, extra: dict | None = None):
        super().__init__(code)
        self.code = code
        self.extra = extra or {}


@dataclass
class DailyUsage:
    """每号每日成功次数计数（SQLite）。"""

    db_path: Path

    def __post_init__(self):
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS usage (account TEXT, day TEXT, used INTEGER, "
            "PRIMARY KEY(account, day))"
        )
        self._conn.commit()

    def jobs_today(self, account: str, day: str | None = None) -> int:
        day = day or local_day()
        row = self._conn.execute(
            "SELECT used FROM usage WHERE account=? AND day=?", (account, day)
        ).fetchone()
        return int(row[0]) if row else 0

    def add(self, account: str, cost: int = 1, day: str | None = None) -> None:
        day = day or local_day()
        self._conn.execute(
            "INSERT INTO usage(account, day, used) VALUES (?,?,?) "
            "ON CONFLICT(account, day) DO UPDATE SET used=used+?",
            (account, day, cost, cost),
        )
        self._conn.commit()


@dataclass
class AccountState:
    config: AccountConfig
    status: str = STATUS_STANDBY
    inflight: int = 0
    failures_today: int = 0
    successes_today: int = 0
    consecutive_failures: int = 0
    outcomes: list[int] = field(default_factory=list)
    cooldown_until: float = 0.0
    last_submit_at: float = 0.0
    last_probe_at: float = 0.0
    last_used_at: float = 0.0
    inflight_since: float = 0.0
    proxy_account_id: str = ""
    probe_error: str = ""
    uid: str = ""
    enabled: bool = True
    last_probe_result: bool | None = None
    fail_score_step: int = 1

    @property
    def cooling(self) -> bool:
        return self.cooldown_until > time.time()

    @property
    def fail_score(self) -> int:
        return self.consecutive_failures * self.fail_score_step


class AccountPool:
    def __init__(
        self,
        accounts: list[AccountConfig],
        daily_success_limit: int = 2,
        max_open_accounts: int = 0,
        open_buffer_ratio: float = 0.0,
        isolate_shared_egress: bool = False,
        account_proxy_enabled: bool = True,
        fail_score_cap: int = 0,
        min_submit_interval_seconds: float = 0.0,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        proxy_accounts: list[ProxyAccountConfig] | None = None,
        usage: DailyUsage | None = None,
        probe_interval_seconds: int = 120,
    ):
        self.daily_success_limit = daily_success_limit
        self.max_open_accounts = max_open_accounts or len(accounts)
        self.open_buffer_ratio = open_buffer_ratio
        self.isolate_shared_egress = isolate_shared_egress
        self.account_proxy_enabled = account_proxy_enabled
        self.fail_score_cap = fail_score_cap
        self.min_submit_interval_seconds = min_submit_interval_seconds
        self.cooldown_seconds = cooldown_seconds
        self.probe_interval_seconds = probe_interval_seconds
        self.usage = usage
        self.proxy_accounts = {p.id: p for p in (proxy_accounts or [])}
        self._fail_score_step = max(1, self.fail_score_cap // 2) if self.fail_score_cap else 1
        self.states: dict[str, AccountState] = {}
        for cfg in accounts:
            data = self._read_state(cfg.state_file)
            proxy = data.get("proxy") or cfg.proxy
            st = AccountState(
                config=cfg,
                enabled=cfg.enabled,
                proxy_account_id=data.get("proxy_account_id") or cfg.proxy_account_id,
            )
            st.fail_score_step = self._fail_score_step
            if not Path(str(cfg.state_file)).exists() or not data.get("cookies"):
                st.status = STATUS_UNSIGNED
            else:
                st.status = STATUS_STANDBY
            st.config.proxy = proxy
            self.states[cfg.id] = st
        self.preferred: str | None = None
        self._working: set[str] = set()  # 当前被 assign 占用（还没 release）的号
        self._egress_unhealthy: dict[str, tuple[float, str]] = {}
        # 出口被判异常时的回调（egress, reason）。browser_pool 用它给动态代理换出口 IP。
        self.on_egress_unhealthy: Callable[[str, str], None] | None = None
        self._registration: list[str] = [c.id for c in accounts]
        self._cursor = 0

    # ===== 基础 =====

    @property
    def effective_max_open(self) -> int:
        if self.open_buffer_ratio <= 0:
            return self.max_open_accounts
        return int(self.max_open_accounts * (1.0 - self.open_buffer_ratio))

    def _open_count(self) -> int:
        return sum(1 for s in self.states.values() if s.inflight > 0)

    @staticmethod
    def _read_state(state_file) -> dict:
        try:
            path = Path(str(state_file))
            if not path.exists():
                return {}
            return json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return {}

    def _quota_exhausted(self, state: AccountState) -> bool:
        return self.daily_success_limit > 0 and state.successes_today >= self.daily_success_limit

    def has_daily_capacity(self, account: str | None = None) -> bool:
        if account is not None:
            st = self.states.get(account)
            if not st:
                return False
            if self.daily_success_limit <= 0:
                return True
            return st.successes_today < self.daily_success_limit
        return any(not self._quota_exhausted(s) for s in self.states.values())

    def has_recoverable_account(self) -> bool:
        for st in self.states.values():
            if st.config.state_file.exists() and st.status != STATUS_UNSIGNED:
                return True
        return False

    # ===== 出口/隔离 =====

    def effective_proxy(self, account: str) -> str:
        st = self.states.get(account)
        if not st or not self.account_proxy_enabled:
            return "direct"
        return session_proxy(st.config.proxy, enabled=True)

    def effective_egress(self, account: str) -> str:
        """出口身份。

        动态代理的多个号共用同一个 host:port，出口 IP 由各自的 sticky session
        决定，所以优先用显式的 egress_key；没有时才按 host:port 归一化。
        """
        st = self.states.get(account)
        if not st or not self.account_proxy_enabled:
            return "direct"
        if st.config.egress_key:
            return st.config.egress_key
        return _egress_of(session_proxy(st.config.proxy, enabled=True))

    def set_account_egress_key(self, account: str, egress_key: str) -> None:
        """换 IP / 换绑代理后刷新出口身份（旧出口的异常标记不会跟过来）。"""
        st = self.states.get(account)
        if st:
            st.config.egress_key = egress_key or ""

    def set_account_proxy_enabled(self, enabled: bool) -> None:
        self.account_proxy_enabled = enabled

    def egress_is_unhealthy(self, egress: str) -> bool:
        rec = self._egress_unhealthy.get(egress)
        return bool(rec and rec[0] > time.time())

    def mark_egress_unhealthy(self, egress: str, reason: str = "", seconds: int = 0) -> None:
        self._egress_unhealthy[egress] = (time.time() + seconds, reason or "")
        callback = self.on_egress_unhealthy
        if callback is not None:
            try:
                callback(egress, reason or "")
            except Exception:  # noqa: BLE001 - 回调只是通知，不能影响选号主流程
                pass

    def has_assignable_other_egress(self, account: str) -> bool:
        egress = self.effective_egress(account)
        for st in self.states.values():
            if st.config.id == account:
                continue
            if (
                self.effective_egress(st.config.id) != egress
                and self._assignable(st) is None
                and st.inflight == 0
            ):
                return True
        return False

    def _egress_slot_free(self, account: str) -> bool:
        """同出口隔离：该出口是否已被其他 in-flight 号占用。"""
        if not self.isolate_shared_egress:
            return True
        egress = self.effective_egress(account)
        if self.egress_is_unhealthy(egress):
            return False
        for st in self.states.values():
            if st.config.id == account:
                continue
            if st.inflight > 0 and self.effective_egress(st.config.id) == egress:
                return False
        return True

    # ===== 分配 =====

    def _assignable(self, state: AccountState) -> str | None:
        """返回被挡原因（str）或 None（可分配）。"""
        self._resolve_cooldown(state)
        if not state.enabled:
            return "disabled"
        if state.status == STATUS_EXPIRED:
            return "expired"
        if state.status == STATUS_UNSIGNED:
            return "unsigned"
        if state.cooling:
            return "cooldown"
        if state.status != STATUS_HEALTHY:
            return "not_healthy"
        if self._quota_exhausted(state):
            return "quota_exhausted"
        if self.min_submit_interval_seconds and state.last_submit_at and (
            time.time() - state.last_submit_at < self.min_submit_interval_seconds
        ):
            return "min_interval"
        if self._open_count() >= self.effective_max_open and state.inflight == 0:
            return "max_open"
        if state.config.max_inflight <= 1 and state.inflight >= 1:
            return "max_inflight"
        if not self._egress_slot_free(state.config.id):
            return "shared_egress"
        return None

    def _resolve_cooldown(self, state: AccountState) -> None:
        """冷却到期后自动恢复：清失败分、转回 healthy。"""
        if state.status == STATUS_COOLDOWN and not state.cooling:
            state.status = STATUS_HEALTHY
            state.consecutive_failures = 0
            state.outcomes = []

    def _assign(self, state: AccountState) -> str:
        state.inflight += 1
        state.inflight_since = time.time()
        state.last_used_at = time.time()
        self._working.add(state.config.id)
        return state.config.id

    def try_assign(self, preferred: str | None = None) -> str | None:
        target = preferred if preferred is not None else self.preferred
        if target:
            st = self.states.get(target)
            if st and self._assignable(st) is None:
                return self._assign(st)
            return None  # 钉住的号不可用：不静默转投其它号
        candidates: list[AccountState] = []
        for st in self.states.values():
            if st.inflight > 0:
                continue
            if self._assignable(st) is None:
                candidates.append(st)
        if not candidates:
            return None
        base_order = {name: idx for idx, name in enumerate(self._registration)}

        def key(st: AccountState):
            cursor_idx = base_order.get(st.config.id, 0)
            dist = (cursor_idx - self._cursor) % max(1, len(self._registration))
            return (st.successes_today, st.fail_score, -st.config.weight, dist)

        candidates.sort(key=key)
        picked = candidates[0]
        self._cursor = (base_order.get(picked.config.id, 0) + 1) % max(1, len(self._registration))
        return self._assign(picked)

    def release(self, account: str) -> None:
        st = self.states.get(account)
        if not st:
            return
        st.inflight = max(0, st.inflight - 1)
        self._working.discard(account)

    # ===== 状态转移 =====

    def mark_healthy(self, account: str, uid: str = "") -> None:
        st = self.states.get(account)
        if not st:
            return
        st.status = STATUS_HEALTHY
        st.uid = uid or st.uid
        st.cooldown_until = 0.0
        st.consecutive_failures = 0
        st.outcomes = []

    def mark_expired(self, account: str, reason: str = "") -> None:
        st = self.states.get(account)
        if not st:
            return
        st.status = STATUS_EXPIRED
        st.probe_error = reason

    def mark_day_exhausted(self, account: str, reason: str = "") -> None:
        st = self.states.get(account)
        if not st:
            return
        st.successes_today = self.daily_success_limit
        st.status = STATUS_COOLDOWN
        st.cooldown_until = time.time() + self.cooldown_seconds
        st.consecutive_failures = 0

    def set_enabled(self, account: str, enabled: bool) -> None:
        st = self.states.get(account)
        if st:
            st.enabled = enabled
            st.config.enabled = enabled

    def set_preferred(self, account: str | None) -> None:
        self.preferred = account or None

    def record_success(self, account: str) -> None:
        st = self.states.get(account)
        if not st:
            return
        st.successes_today += 1
        st.consecutive_failures = 0
        st.outcomes = []
        if st.status != STATUS_COOLDOWN:
            st.status = STATUS_HEALTHY
        if self.usage:
            self.usage.add(account, 1)

    def record_failure(self, account: str, reason: str = "") -> None:
        st = self.states.get(account)
        if not st:
            return
        st.consecutive_failures += 1
        st.outcomes.append(0)
        st.outcomes = st.outcomes[-20:]
        st.probe_error = (reason or "")[:300]
        if self.fail_score_cap and st.fail_score >= self.fail_score_cap:
            st.status = STATUS_COOLDOWN
            st.cooldown_until = time.time() + self.cooldown_seconds

    def set_proxy(self, account: str, proxy: str) -> None:
        st = self.states.get(account)
        if not st:
            return
        st.config.proxy = proxy
        self._persist_proxy(state_file=st.config.state_file, proxy=proxy)

    @staticmethod
    def _persist_proxy(state_file, proxy: str):
        try:
            path = Path(str(state_file))
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            if proxy:
                data["proxy"] = proxy
            else:
                data.pop("proxy", None)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError):
            pass

    # ===== 探活 =====

    def probe_one(
        self, account: str, probe_fn, region: str, version: str
    ) -> tuple[str, bool, str]:
        st = self.states.get(account)
        if not st:
            return account, False, "unknown account"
        st.status = STATUS_CHECKING
        st.last_probe_at = time.time()
        try:
            ok, detail = probe_fn(str(st.config.state_file), region, version)
        except TypeError:
            ok, detail = probe_fn(str(st.config.state_file))
        st.last_probe_result = ok
        if ok:
            st.status = STATUS_HEALTHY
            st.consecutive_failures = 0
            return account, True, detail
        text = (detail or "").lower()
        if any(marker in text for marker in _AUTH_FAILURE_MARKERS):
            st.status = STATUS_EXPIRED
        else:
            st.status = STATUS_COOLDOWN
            st.cooldown_until = time.time() + self.cooldown_seconds
            if self.effective_egress(account) != "direct":
                self.mark_egress_unhealthy(self.effective_egress(account), detail, 120)
        st.probe_error = detail[:300]
        return account, False, detail

    def probe_all(self, probe_fn, region: str, version: str) -> list[tuple[str, bool, str]]:
        results: list[tuple[str, bool, str]] = []
        seen_egress: set[str] = set()
        for name in self._registration:
            st = self.states.get(name)
            if not st or st.status != STATUS_HEALTHY:
                continue
            if self.probe_interval_seconds and st.last_probe_at and (
                time.time() - st.last_probe_at < self.probe_interval_seconds
            ):
                continue
            egress = self.effective_egress(name)
            if egress in seen_egress:
                continue
            seen_egress.add(egress)
            if self.egress_is_unhealthy(egress):
                continue
            results.append(self.probe_one(name, probe_fn, region, version))
        return results

    def activate_standby(self, probe_fn, region: str, version: str) -> str | None:
        for name in self._registration:
            st = self.states.get(name)
            if not st or st.status != STATUS_STANDBY or not st.enabled:
                continue
            if not self._egress_slot_free(name):
                continue
            account, ok, detail = self.probe_one(name, probe_fn, region, version)
            if ok:
                return account
        return None

    # ===== 监控 =====

    def snapshot(self) -> list[dict]:
        out = []
        for name in self._registration:
            st = self.states.get(name)
            if not st:
                continue
            proxy = st.config.proxy
            out.append({
                "id": name,
                "status": st.status,
                "proxy": proxy,
                "proxy_display": self._mask_proxy(proxy) if proxy else "",
                "effective_egress": self.effective_egress(name),
                "proxy_enabled": self.account_proxy_enabled,
                "successes_today": st.successes_today,
                "remaining_today": max(0, self.daily_success_limit - st.successes_today) if self.daily_success_limit else 99,
                "inflight": st.inflight,
                "blocked": self._assignable(st),
                "enabled": st.enabled,
                "weight": st.config.weight,
                "preferred": name == self.preferred,
                "isolation": self.isolation_report(),
            })
        return out

    @staticmethod
    def _mask_proxy(proxy: str) -> str:
        if "://" not in proxy:
            return proxy
        parts = urlsplit(proxy)
        if parts.password:
            return proxy.replace(parts.password, "***")
        return proxy

    def isolation_report(self) -> dict:
        groups: dict[str, list[str]] = {}
        without_proxy: list[str] = []
        for name in self._registration:
            st = self.states.get(name)
            if not st:
                continue
            egress = self.effective_egress(name)
            if egress == "direct":
                without_proxy.append(name)
            groups.setdefault(egress, []).append(name)
        shared = [{"egress": e, "accounts": accts} for e, accts in groups.items() if len(accts) > 1]
        unhealthy = [
            {"egress": e, "reason": self._egress_unhealthy[e][1]}
            for e in groups
            if self.egress_is_unhealthy(e) and e in self._egress_unhealthy
        ]
        ok = self.isolate_shared_egress and not without_proxy and not shared and not unhealthy
        return {
            "ok": ok,
            "account_proxy_enabled": self.account_proxy_enabled,
            "accounts_without_proxy": len(without_proxy),
            "accounts_without_proxy_ids": without_proxy,
            "shared_egress": shared,
            "unhealthy_egress": unhealthy,
        }

    def snapshot_meta(self) -> dict:
        counts = {
            "account_count": len(self._registration),
            "healthy_count": 0,
            "standby_count": 0,
            "checking_count": 0,
            "unsigned_count": 0,
            "cooldown_count": 0,
            "expired_count": 0,
            "open_accounts": self._open_count(),
            "inflight": sum(s.inflight for s in self.states.values()),
            "jobs_today": sum(s.successes_today for s in self.states.values()),
        }
        for name in self._registration:
            st = self.states.get(name)
            if not st:
                continue
            key = f"{st.status}_count"
            if key in counts:
                counts[key] += 1
        counts["accounts"] = self.snapshot()
        counts["isolation"] = self.isolation_report()
        return counts

    def preview_route(self) -> dict:
        open_ids = [name for name in self._registration if self.states[name].inflight > 0]
        candidates = [
            {"id": s.config.id, "blocked": self._assignable(s)} for s in self.states.values()
        ]
        if self.preferred:
            st = self.states.get(self.preferred)
            next_id = self.preferred if st and self._assignable(st) is None else None
            return {
                "next_account_id": next_id,
                "strategy": "pinned",
                "preferred_id": self.preferred,
                "open_ids": open_ids,
                "candidates": candidates,
            }
        # 纯预测“下一个会轮到谁”，不推进游标、不实际分配。
        base_order = {name: idx for idx, name in enumerate(self._registration)}
        pickable = [
            st for st in self.states.values()
            if st.inflight == 0 and self._assignable(st) is None
        ]
        pickable.sort(key=lambda st: (
            st.successes_today,
            st.fail_score,
            -st.config.weight,
            (base_order.get(st.config.id, 0) - self._cursor) % max(1, len(self._registration)),
        ))
        picked = pickable[0].config.id if pickable else None
        return {
            "next_account_id": picked,
            "strategy": "weighted",
            "preferred_id": None,
            "open_ids": open_ids,
            "candidates": candidates,
        }

    def reconcile_inflight(self, busy_account_ids: set[str], grace_seconds: int = 30) -> list[str]:
        now = time.time()
        released: list[str] = []
        for name in self._registration:
            st = self.states.get(name)
            if not st or st.inflight == 0:
                continue
            if name in busy_account_ids:
                continue
            if grace_seconds and now - st.inflight_since < grace_seconds:
                continue
            st.inflight = 0
            self._working.discard(name)
            released.append(name)
        return released

    # ===== 代理池绑定 =====

    def bind_proxies(self, proxies: list[str], force: bool = False, mode: str = "sticky") -> dict:
        if mode != "sticky":
            raise ProxyBindError("unsupported_mode", {"mode": mode})
        if not proxies:
            raise ProxyBindError("proxy_pool_empty")
        needed = len(self._registration)
        if len(proxies) < needed:
            if not force:
                raise ProxyBindError(
                    "proxy_pool_short", {"needed": needed, "got": len(proxies), "short": needed - len(proxies)}
                )
            proxies = proxies + [proxies[-1]] * (needed - len(proxies))
        # 去掉重复出口，重复即视为配置错误。
        seen: set[str] = set()
        for p in proxies:
            egress = _egress_of(p)
            if egress in seen:
                raise ProxyBindError("duplicate_proxy", {"egress": egress})
            seen.add(egress)
        bound = []
        skipped = []
        for idx, name in enumerate(self._registration):
            st = self.states.get(name)
            if st and st.inflight > 0:
                skipped.append({"id": name})
                continue
            proxy = proxies[idx]
            st.config.proxy = proxy
            self._persist_proxy(state_file=st.config.state_file, proxy=proxy)
            self._persist_proxy_account_id(state_file=st.config.state_file, proxy_account_id="")
            bound.append({"id": name, "proxy": proxy})
        return {
            "ok": True,
            "bound": bound,
            "skipped": skipped,
            "missing": [],
            "isolation": self.isolation_report(),
        }

    def rebalance_proxy_bindings(self) -> dict:
        if not self.proxy_accounts:
            raise ProxyBindError("proxy_pool_empty")
        proxy_ids = list(self.proxy_accounts.keys())
        n = len(self._registration)
        target_each, remainder = divmod(n, len(proxy_ids))
        caps = {}
        for i, pid in enumerate(proxy_ids):
            caps[pid] = target_each + (1 if i < remainder else 0)
        counts = {pid: 0 for pid in proxy_ids}
        for name in self._registration:
            pid = self.states[name].proxy_account_id
            if pid not in counts:
                pid = proxy_ids[0]
                self.states[name].proxy_account_id = pid
            counts[pid] += 1
        # 流出队列：超容量的 proxy，且其中的空闲号可被移动。
        movers: list[str] = []
        for pid in proxy_ids:
            if counts[pid] <= caps[pid]:
                continue
            excess = counts[pid] - caps[pid]
            free_on_proxy = [
                name for name in self._registration
                if self.states[name].proxy_account_id == pid and not self._is_busy(name)
            ]
            movers.extend(free_on_proxy[:excess])
        # 流入队列：未满容量的 proxy。
        receivers: list[str] = []
        for pid in proxy_ids:
            if counts[pid] < caps[pid]:
                receivers.extend([pid] * (caps[pid] - counts[pid]))
        moved = 0
        for name in movers:
            if not receivers:
                break
            pid = receivers.pop(0)
            self.states[name].proxy_account_id = pid
            self.states[name].config.proxy = self.proxy_accounts[pid].proxy
            self._persist_proxy(state_file=self.states[name].config.state_file, proxy=self.proxy_accounts[pid].proxy)
            self._persist_proxy_account_id(state_file=self.states[name].config.state_file, proxy_account_id=pid)
            moved += 1
        busy = sum(1 for name in self._registration if self._is_busy(name))
        distribution = {
            pid: len([n for n in self._registration if self.states[n].proxy_account_id == pid])
            for pid in proxy_ids
        }
        return {"moved_count": moved, "busy_count": busy, "distribution": distribution}

    def _is_busy(self, account: str) -> bool:
        st = self.states.get(account)
        if not st:
            return False
        return st.inflight > 0 or account in self._working

    @staticmethod
    def _persist_proxy_account_id(state_file, proxy_account_id: str):
        try:
            path = Path(str(state_file))
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            if proxy_account_id:
                data["proxy_account_id"] = proxy_account_id
            else:
                data.pop("proxy_account_id", None)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError):
            pass
