"""浏览器版号池：扫描 accounts/ profile，每号每天 2 片硬限额，同号互斥。

v2：accounts_meta 元数据表（调度开关/备注/登录态缓存/风控冷却），面板读写同一份数据。
配额计数落 SQLite（pool_usage.db），保守计数：出片成功或额度报错才记 1 次。
"""
import asyncio
import logging
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from dola_client import CreditError
from video_worker_ui import (
    AccountLimitedError, CreditInsufficientError, LoginExpiredError, RiskControlError,
    generate_video, resume_video,
)
from pure_api_gen import ShortClipError, ensure_web_playable, generate_pure_video, probe_login
import config
import proxy_store
from pool import AccountConfig as _PoolAccountConfig, AccountPool as _PoolEngine

logger = logging.getLogger("dola-pool.browser_pool")

DAILY_LIMIT = config.DAILY_LIMIT
COOLDOWN_SEC = 1800  # 风控冷却 30 分钟
FAIL_COOLDOWN_SEC = 120   # 任一任务试号失败后的共享冷却：并发任务在此窗口内不再选同一账号
WAIT_FREE_ACCOUNT_SEC = 15  # 候选账号全被其他任务占用时，最长等待重扫时间
WAIT_FREE_ACCOUNT_STEP = 2  # 等待重扫间隔（秒）

# 上游「访问频繁」类瞬时限流：只冷却一小段时间，不能按「失败」锁到次日重置。
TRANSIENT_COOLDOWN_SEC = config.TRANSIENT_COOLDOWN_SEC
TRANSIENT_THROTTLE_MARKERS = (
    "访问频繁", "710022002", "稍后重试", "服务繁忙", "系统繁忙", "请求过于频繁",
    "too many requests", "rate limit exceeded", "temporarily unavailable",
    "please try again later", "try again later",
)
# 上游明确的「当日次数用完」：等额度刷新（本地也按每日上限处理）。
DAILY_LIMIT_MARKERS = (
    "今天的生成次数已经达到上限", "今日次数已达上限", "达到每日上限",
    "每日上限", "每日次数", "daily limit",
)
# 账号还有额度、只是不够这一单（例如 10 秒要 4 点而当日只剩 2 点）。
# 上游原话：「本次视频生成需要消耗 N 个视频生成额度，今日剩余 M 个视频生成额度 ，无法生成该视频。」
# 注意**不能**用「剩余 0 个」/「剩余 2 个」当标记：链路播报里正常也会出现这句，
# 会把版权/内容拒稿误判成额度不足，把好号白锁到次日。
QUOTA_SHORT_MARKERS = (
    "额度不足", "积分不足", "无法生成该视频", "需要消耗",
)
# 内容/版权拒稿：与账号无关，换号也过不了，不能把号标记成失败或额度不足。
CONTENT_REFUSAL_MARKERS = (
    "版权限制", "涉及版权", "请更换输入内容", "内容违规", "违反社区",
    "copyright", "policy violation",
)
# 上游只给了短视频（例如 30 秒的请求只回 15 秒）：按「不拼接」策略换号重试，
# 属于上游行为，不该把号标记成失败/额度不足。
SHORT_CLIP_MARKERS = (
    "只给了", "不拼接",
)


def classify_upstream_failure(message: str) -> str:
    """把上游失败文案归类：transient / daily / content / quota / other。"""
    blob = (message or "").lower()
    if any(marker.lower() in blob for marker in TRANSIENT_THROTTLE_MARKERS):
        return "transient"
    if any(marker.lower() in blob for marker in DAILY_LIMIT_MARKERS):
        return "daily"
    if any(marker.lower() in blob for marker in CONTENT_REFUSAL_MARKERS):
        return "content"
    if any(marker.lower() in blob for marker in SHORT_CLIP_MARKERS):
        return "short"
    if any(marker.lower() in blob for marker in QUOTA_SHORT_MARKERS):
        return "quota"
    return "other"


def _reset_tz():
    """额度重置时区：config.LIMIT_RESET_TZ，缺 tzdata 时用固定偏移兜底。"""
    try:
        return ZoneInfo(config.LIMIT_RESET_TZ)
    except Exception:
        offsets = {"Asia/Tokyo": 9, "Asia/Hong_Kong": 8, "UTC": 0}
        return timezone(timedelta(hours=offsets.get(config.LIMIT_RESET_TZ, 9)))


def quota_day_anchor(now: datetime | None = None) -> datetime:
    """now 所属「额度日」的起点：当地 LIMIT_RESET_HOUR:00（默认日本时间 00:00）。"""
    now = now or datetime.now(_reset_tz())
    anchor = now.replace(hour=config.LIMIT_RESET_HOUR, minute=0, second=0, microsecond=0)
    if now < anchor:
        anchor -= timedelta(days=1)
    return anchor


def usage_day() -> str:
    """当前额度「日」：以 config.LIMIT_RESET_HOUR 为界的当地日期。

    Dola 的每日免费额度按当地零点滚动（2026-09-15 实测修正，原先按 11:00 记界偏晚），
    号池的计数日必须与它一致，否则会出现「额度已刷新但本地计数仍挡着」或
    「本地还有额度、上游已经用完」。上游回执里的「今日剩余 N 个」会实时回写校正。
    """
    return quota_day_anchor().date().isoformat()


def next_quota_reset_at() -> float:
    """下一次额度重置的时间戳（当地 LIMIT_RESET_HOUR:00）。"""
    return (quota_day_anchor() + timedelta(days=1)).timestamp()


class AllAccountsLimitedError(RuntimeError):
    """所有已开启调度的账号都达到 Dola 每日视频上限。"""


class AllAccountsQuotaBlockedError(RuntimeError):
    """所有已开启调度的账号都已知积分不足。"""


class _UnlimitedSemaphore:
    """max_concurrency <= 0 时顶替 asyncio.Semaphore：不限制并发。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class BrowserPool:
    def __init__(self, accounts_dir: str = "accounts", db_path: str | None = None,
                 max_concurrency: int = 1):
        if not db_path:
            db_path = config.POOL_DB_PATH
        self.accounts_dir = Path(accounts_dir)
        # 0（或负数）= 取消全局并发闸门。
        self.semaphore = (asyncio.Semaphore(max_concurrency)
                          if max_concurrency > 0 else _UnlimitedSemaphore())
        self._locks: dict[str, asyncio.Lock] = {}
        self._fail_until: dict[str, float] = {}
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS usage (account TEXT, day TEXT, used INTEGER, "
            "PRIMARY KEY(account, day))"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts_meta (
                name TEXT PRIMARY KEY,
                scheduling INTEGER DEFAULT 1,
                note TEXT DEFAULT '',
                email TEXT DEFAULT '',
                created_at REAL,
                last_used_at REAL DEFAULT 0,
                login_ok INTEGER,
                login_checked_at REAL DEFAULT 0,
                cooldown_until REAL DEFAULT 0,
                rate_limited_until REAL DEFAULT 0,
                limit_reason TEXT DEFAULT '',
                quota_blocked_until REAL DEFAULT 0,
                quota_reason TEXT DEFAULT '',
                credit_balance INTEGER,
                credit_checked_at REAL DEFAULT 0,
                failed_at REAL DEFAULT 0,
                failed_reason TEXT DEFAULT '',
                weight INTEGER DEFAULT 1,
                preferred INTEGER DEFAULT 0,
                egress TEXT DEFAULT '',
                sessionid TEXT DEFAULT '',
                source TEXT DEFAULT '',
                risk_control INTEGER DEFAULT 0,
                risk_reason TEXT DEFAULT ''
            )
            """
        )
        self._conn.commit()
        # 旧库兼容：补新元数据列
        for column, definition in (
            ("email", "TEXT DEFAULT ''"),
            ("rate_limited_until", "REAL DEFAULT 0"),
            ("limit_reason", "TEXT DEFAULT ''"),
            ("quota_blocked_until", "REAL DEFAULT 0"),
            ("quota_reason", "TEXT DEFAULT ''"),
            ("credit_balance", "INTEGER"),
            ("credit_checked_at", "REAL DEFAULT 0"),
            ("failed_at", "REAL DEFAULT 0"),
            ("failed_reason", "TEXT DEFAULT ''"),
            ("weight", "INTEGER DEFAULT 1"),
            ("preferred", "INTEGER DEFAULT 0"),
            ("egress", "TEXT DEFAULT ''"),
            ("sessionid", "TEXT DEFAULT ''"),
            ("source", "TEXT DEFAULT ''"),
            ("risk_control", "INTEGER DEFAULT 0"),
            ("risk_reason", "TEXT DEFAULT ''"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE accounts_meta ADD COLUMN {column} {definition}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass
        self._engine: _PoolEngine | None = None

    # ===== 账号发现/元数据 =====

    def _ensure_meta(self, name: str):
        self._conn.execute(
            "INSERT OR IGNORE INTO accounts_meta (name, created_at) VALUES (?, ?)",
            (name, time.time()),
        )
        self._conn.commit()

    @property
    def accounts(self) -> list:
        if not self.accounts_dir.exists():
            return []
        names = sorted(d.name for d in self.accounts_dir.iterdir()
                       if d.is_dir() and not d.name.startswith("."))
        for n in names:
            self._ensure_meta(n)
        return names

    def _meta(self, name: str):
        return self._conn.execute(
            "SELECT * FROM accounts_meta WHERE name=?", (name,)).fetchone()

    def find_accounts_by_email(self, email: str) -> list:
        """按邮箱（忽略大小写与首尾空格）反查已有账号名，用于添加账号时去重。"""
        norm = (email or "").strip().lower()
        if not norm:
            return []
        rows = self._conn.execute(
            "SELECT name FROM accounts_meta WHERE lower(trim(email))=?", (norm,)
        ).fetchall()
        return [r["name"] for r in rows]

    def set_sessionid(self, name: str, sessionid: str) -> None:
        self._conn.execute(
            "UPDATE accounts_meta SET sessionid=? WHERE name=?", (sessionid or "", name)
        )
        self._conn.commit()

    def set_source(self, name: str, source: str) -> None:
        """记录账号来源：login（Google OAuth 登录） / cookie（cookie 载入）。"""
        self._conn.execute(
            "UPDATE accounts_meta SET source=? WHERE name=?", (source or "", name)
        )
        self._conn.commit()

    def ensure_account(self, name: str) -> None:
        """确保账号在 accounts_meta 有行（新账号打标/写邮箱前调用）。"""
        self._ensure_meta(name)

    def find_by_sessionid(self, sessionid: str) -> list:
        """按 sessionid 反查已有账号名（用于导入去重）。"""
        if not sessionid:
            return []
        rows = self._conn.execute(
            "SELECT name FROM accounts_meta WHERE sessionid=? AND sessionid<>''", (sessionid,)
        ).fetchall()
        return [r["name"] for r in rows]

    def used_today(self, account: str) -> int:
        row = self._conn.execute(
            "SELECT used FROM usage WHERE account=? AND day=?",
            (account, usage_day()),
        ).fetchone()
        return row[0] if row else 0

    def _claim(self, account: str, cost: int = 1):
        """按额度点数记账：出片/占额度事件扣除 cost 点（默认 1）。"""
        if cost <= 0:
            cost = 1
        self._conn.execute(
            "INSERT INTO usage(account, day, used) VALUES (?,?,?) "
            "ON CONFLICT(account, day) DO UPDATE SET used=used+?",
            (account, usage_day(), cost, cost),
        )
        self._conn.commit()

    def _claim_for(self, account: str, table_cost: int, result: dict | None) -> int:
        """按额度表记账（2.5 一条 2 点、2.0 一条 3 点）。

        上游回执里的「将消耗 N 个视频生成额度」是**报价**，与实际扣点不一致
        （30 秒报价 6 点、实扣 2 点），所以记账以额度表为准；表里查不到（未知模型/时长）
        才退回上游报价。上游真实余额由回执里的「今日剩余 N 个」回写校正。
        """
        quoted = int((result or {}).get("credits_used") or 0)
        charge = table_cost or quoted or 1
        if quoted and quoted != charge:
            print(f"[pool] {account} 记账 {charge} 点（额度表）· 上游报价 {quoted} 点", flush=True)
        self._claim(account, charge)
        return charge

    def _duration_cost(self, model: str, duration: int) -> int:
        """按用户确认的模型-时长矩阵返回单次出片消耗的点数。"""
        m = (model or "").lower().replace("_", "-")
        if "2.5" in m or "2-5" in m:
            m = "seedance-2.5"
        else:
            m = "seedance-2.0"
        return config.MODEL_DURATION_COSTS.get(m, {}).get(duration, 1)

    def _next_limit_reset(self) -> float:
        """下一次每日额度刷新时间（默认日本时间次日 00:00）。"""
        return next_quota_reset_at()

    def next_quota_reset_at(self) -> float:
        return next_quota_reset_at()

    def reset_daily_quotas(self) -> int:
        """每日额度重置：恢复所有被「额度不足/每日上限/失败」挡住的账号。

        风控冷却（cooldown_until）和登录态（login_ok）不动：
        前者是上游处罚窗口，后者要靠验证功能恢复。
        """
        cur = self._conn.execute(
            "UPDATE accounts_meta SET rate_limited_until=0, limit_reason='', "
            "quota_blocked_until=0, quota_reason='', failed_at=0, failed_reason='', "
            "credit_balance=NULL, credit_checked_at=0"
        )
        self._conn.commit()
        self._fail_until.clear()
        return cur.rowcount

    def _clear_expired_rate_limits(self):
        now = time.time()
        self._conn.execute(
            "UPDATE accounts_meta SET rate_limited_until=0, limit_reason='', "
            "quota_blocked_until=0, quota_reason='' "
            "WHERE (rate_limited_until > 0 AND rate_limited_until <= ?) "
            "OR (quota_blocked_until > 0 AND quota_blocked_until <= ?)", (now, now))
        # 必须无条件 commit：即使一条都没命中，Python sqlite3 也已经为这条 UPDATE
        # 隐式开了写事务。不提交就把 pool_usage.db 的写锁一直攥在手里 ——
        # 之后所有写操作（建/删代理、导入 cookie、改账号元数据）都会 15s 超时
        # 报 "database is locked"。这个方法由 list_accounts() 调用，
        # 也就是「面板一打开就锁库」。
        self._conn.commit()

    def _mark_quota_blocked(self, account: str, reason: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=?, last_used_at=? WHERE name=?",
            (self._next_limit_reset(), reason[:300], time.time(), account),
        )
        self._conn.commit()

    def _mark_daily_limit(self, account: str, reason: str = ""):
        """Dola 明确返回每日上限：未成功不扣本地点数（2026-09-02 规则），
        只把账号限流标记到次日零点，期间跳过该号。"""
        self._conn.execute(
            "UPDATE accounts_meta SET last_used_at=?, rate_limited_until=?, limit_reason=? WHERE name=?",
            (time.time(), self._next_limit_reset(), reason[:300], account),
        )
        self._conn.commit()

    def _mark_failed(self, account: str, reason: str = ""):
        """叠加“失败”状态（持久记录，不清到全池重置不罢休）。
        只用于底层没有自动恢复标记的失败（普通生成失败等），
        每日上限/积分不足/风控/登录失效仍走各自底层状态，避免误锁。"""
        self._conn.execute(
            "UPDATE accounts_meta SET failed_at=?, failed_reason=? WHERE name=?",
            (time.time(), (reason or "生成失败")[:300], account),
        )
        self._conn.commit()

    def _note_upstream_failure(self, account: str, message: str, attempt: int = 0) -> str:
        """按上游失败文案落状态，返回归类：transient / daily / quota / other。

        - transient：出口 IP/上游瞬时限流（如 710022002「当前服务访问频繁」）→ 短冷却，**不写 failed**；
        - daily：当日次数用完 → 标记到额度刷新；
        - quota：本单额度不够（如 30 秒需 3 点只剩 2 点）→ 标记到额度刷新；
        - other：普通生成失败 → 叠加失败标记（下次全池/每日重置时清）。
        """
        kind = classify_upstream_failure(message)
        if kind == "transient":
            print(
                f"[pool] {account} 上游瞬时限流，冷却 {TRANSIENT_COOLDOWN_SEC // 60} 分钟后可再试: "
                f"{message[:160]}",
                flush=True,
            )
            self._conn.execute(
                "UPDATE accounts_meta SET cooldown_until=? WHERE name=?",
                (time.time() + TRANSIENT_COOLDOWN_SEC, account),
            )
            self._conn.commit()
        elif kind == "daily":
            print(f"[pool] {account} 当日次数已用完，标记到额度刷新: {message[:160]}", flush=True)
            self._mark_daily_limit(account, message)
        elif kind == "quota":
            print(f"[pool] {account} 本单额度不够，标记到额度刷新: {message[:160]}", flush=True)
            self._mark_quota_blocked(account, message)
        elif kind == "content":
            # 版权/内容拒稿：同一条提示词换号也过不了，不能把号标记成失败或额度不足。
            print(f"[pool] {account} 内容/版权拒稿（与账号无关，不标记账号）: {message[:160]}", flush=True)
        elif kind == "short":
            print(f"[pool] {account} 只出了短视频（不拼接，不标记账号）: {message[:160]}", flush=True)
        else:
            print(f"[pool] {account} 生成失败（第 {attempt} 次），换号重试: {message[:200]}", flush=True)
            self._mark_failed(account, message)
        return kind

    def _clear_all_failed(self) -> int:
        """清空所有账号的叠加失败标记（全池兜底重置用）。"""
        cur = self._conn.execute(
            "UPDATE accounts_meta SET failed_at=0, failed_reason='' WHERE failed_at>0"
        )
        self._conn.commit()
        return cur.rowcount

    def _overlay_unlockable(self, cost: int = 1) -> bool:
        """清掉全部叠加失败标记后，是否至少解锁一个底层健康且满足本次点数的账号。"""
        for a in self.list_accounts():
            if not a["failed"]:
                continue
            if (a["scheduling"] and not a["cooling"] and not a["rate_limited"]
                    and not a["quota_blocked"] and a["login_ok"] != 0
                    and a["used_today"] < DAILY_LIMIT
                    and (a["credit_balance"] is None or a["credit_balance"] >= 2)
                    and a["remaining"] >= cost):
                return True
        return False

    def list_accounts(self) -> list:
        """面板视图：meta + 配额 + 限流状态 + 是否忙合并。"""
        self._clear_expired_rate_limits()
        now = time.time()
        out = []
        for a in self.accounts:
            m = self._meta(a)
            used = self.used_today(a)
            lock = self._locks.get(a)
            out.append({
                "name": a,
                "scheduling": bool(m["scheduling"]) if m else True,
                "note": m["note"] if m else "",
                "email": m["email"] if m else "",
                "created_at": m["created_at"] if m else 0,
                "last_used_at": m["last_used_at"] if m else 0,
                "login_ok": m["login_ok"] if m else None,
                "login_checked_at": m["login_checked_at"] if m else 0,
                "cooldown_until": m["cooldown_until"] if m else 0,
                "cooling": bool(m and m["cooldown_until"] > now),
                "rate_limited_until": m["rate_limited_until"] if m and m["rate_limited_until"] else 0,
                "rate_limited": bool(m and m["rate_limited_until"] > now),
                "limit_reason": m["limit_reason"] if m else "",
                "quota_blocked_until": m["quota_blocked_until"] if m and m["quota_blocked_until"] else 0,
                "quota_blocked": bool(m and m["quota_blocked_until"] > now),
                "quota_reason": m["quota_reason"] if m else "",
                "credit_balance": m["credit_balance"] if m else None,
                "credit_checked_at": m["credit_checked_at"] if m else 0,
                "failed_at": m["failed_at"] if m else 0,
                "failed_reason": m["failed_reason"] if m else "",
                "failed": bool(m and m["failed_at"] > 0),
                "weight": m["weight"] if m and m["weight"] else 1,
                "preferred": bool(m and m["preferred"]),
                "source": m["source"] if m else "",
                "risk_control": bool(m and m["risk_control"]),
                "risk_reason": m["risk_reason"] if m else "",
                "status": self._derive_status(m, now),
                "effective_egress": self._egress_for(a),
                "used_today": used,
                "limit": DAILY_LIMIT,
                "remaining": max(0, DAILY_LIMIT - used),
                "busy": bool(lock and lock.locked()),
            })
        return out

    def _derive_status(self, m, now: float) -> str:
        if not m or m["login_ok"] is None:
            return "unsigned"
        if m["cooldown_until"] and m["cooldown_until"] > now:
            return "cooldown"
        if m["rate_limited_until"] and m["rate_limited_until"] > now:
            return "cooldown"
        if m["quota_blocked_until"] and m["quota_blocked_until"] > now:
            return "cooldown"
        if m["login_ok"] == 0:
            return "expired"
        if not m["scheduling"]:
            return "standby"
        if m["failed_at"] and m["failed_at"] > 0:
            return "cooldown"
        return "healthy"

    def set_scheduling(self, name: str, on: bool):
        self._conn.execute(
            "UPDATE accounts_meta SET scheduling=? WHERE name=?", (1 if on else 0, name))
        self._conn.commit()

    def set_email(self, name: str, email: str):
        self._conn.execute(
            "UPDATE accounts_meta SET email=? WHERE name=?", (email, name))
        self._conn.commit()

    def set_login_status(self, name: str, ok: bool):
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=? WHERE name=?",
            (1 if ok else 0, time.time(), name),
        )
        self._conn.commit()

    def _set_risk(self, name: str, reason: str = "") -> None:
        """标记该账号被风控（登录态失效/生成即被踢），供账号管理面板显示「风控」。"""
        self._conn.execute(
            "UPDATE accounts_meta SET risk_control=1, risk_reason=? WHERE name=?",
            (str(reason)[:200], name),
        )
        self._conn.commit()

    def _clear_risk(self, name: str) -> None:
        self._conn.execute(
            "UPDATE accounts_meta SET risk_control=0, risk_reason='' WHERE name=?",
            (name,),
        )
        self._conn.commit()

    def set_note(self, name: str, note: str):
        self._conn.execute(
            "UPDATE accounts_meta SET note=? WHERE name=?", (note, name))
        self._conn.commit()

    def delete_account(self, name: str):
        lock = self._locks.get(name)
        if lock and lock.locked():
            raise RuntimeError("账号正在出片，不能删除")
        d = self.accounts_dir / name
        if d.exists():
            shutil.rmtree(d)
        self._conn.execute("DELETE FROM accounts_meta WHERE name=?", (name,))
        self._conn.commit()

    async def verify_account(self, name: str) -> bool:
        """验证登录态并写回缓存。号忙抛 RuntimeError。

        cookie 账号走纯 API 探活（不开浏览器，避免 profile 被并发占用）；
        login 账号走浏览器 check_login_state。
        """
        if name not in self.accounts:
            raise FileNotFoundError(f"profile 不存在: {name}")
        lock = self._locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("账号正在出片，稍后再验证")
        pure_state = self._pure_state(name)
        if pure_state is not None and self._is_cookie_source(name):
            ok, _msg = await asyncio.to_thread(
                probe_login, pure_state, proxy=self._proxy_url_for(name),
            )
        else:
            from browser import check_login_state, chat_liveness_probe
            ok = await check_login_state(name)
            if ok and config.VERIFY_CHAT_PROBE:
                ok, reason = await chat_liveness_probe(name)
                print(f"[verify] {name} chat_probe -> ok={ok} reason={reason}", flush=True)
                if not ok:
                    self._conn.execute(
                        "UPDATE accounts_meta SET login_ok=0, failed_at=?, "
                        "failed_reason=? WHERE name=?",
                        (time.time(), f"chat_probe:{reason}", name),
                    )
                    self._conn.commit()
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=? WHERE name=?",
            (1 if ok else 0, time.time(), name),
        )
        self._conn.commit()
        return ok

    # ===== 调度 =====

    def _set_credit_balance(self, account: str, balance: int, source: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET credit_balance=?, credit_checked_at=? WHERE name=?",
            (max(0, int(balance)), time.time(), account),
        )
        if balance < 2:
            self._conn.execute(
                "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=? WHERE name=?",
                (self._next_limit_reset(), source[:300] or "积分不足", account),
            )
        else:
            # 上游已经报出可用余额 → 之前那条「积分不足」的阻塞立刻解除。
            self._conn.execute(
                "UPDATE accounts_meta SET quota_blocked_until=0, quota_reason='' "
                "WHERE name=? AND quota_blocked_until>0",
                (account,),
            )
        self._conn.commit()

    def _credit_available(self, account: str, required: int = 2) -> bool:
        row = self._meta(account)
        return not row or row["credit_balance"] is None or row["credit_balance"] >= required

    def _schedulable(self, a: dict) -> bool:
        return (a["scheduling"] and not a["cooling"] and not a["rate_limited"]
                and not a["quota_blocked"] and a["login_ok"] != 0
                and not a["failed"]
                and a["used_today"] < DAILY_LIMIT
                and (a["credit_balance"] is None or a["credit_balance"] >= 2))

    def _recently_failed(self, account: str) -> bool:
        """账号刚被任一任务尝试并失败：并发任务应跳过，避免 A 失败后 B 立刻重试同一账号。"""
        return self._fail_until.get(account, 0) > time.time()

    def _mark_attempt_failed(self, account: str) -> None:
        self._fail_until[account] = time.time() + FAIL_COOLDOWN_SEC

    @property
    def all_accounts_limited(self) -> bool:
        """所有开启调度且未处于风控冷却的账号都已达到每日上限。"""
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["rate_limited"] or a["used_today"] >= DAILY_LIMIT for a in candidates
        )

    @property
    def all_accounts_quota_blocked(self) -> bool:
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["quota_blocked"] or a["rate_limited"] or a["used_today"] >= DAILY_LIMIT
            for a in candidates
        ) and any(a["quota_blocked"] for a in candidates)

    @property
    def available(self) -> bool:
        return any(self._schedulable(a) for a in self.list_accounts())

    @property
    def cookie_count(self) -> int:  # 兼容 /health 旧字段
        return len(self.accounts)

    def account_status(self) -> list:
        return [{
            "account": a["name"], "used_today": a["used_today"], "limit": a["limit"],
            "rate_limited": a["rate_limited"], "rate_limited_until": a["rate_limited_until"],
            "quota_blocked": a["quota_blocked"], "quota_blocked_until": a["quota_blocked_until"],
        } for a in self.list_accounts()]

    @staticmethod
    def _egress_of(proxy_url: str) -> str:
        proxy_url = (proxy_url or "").strip()
        if not proxy_url or proxy_url == "direct":
            return "direct"
        if "://" not in proxy_url:
            proxy_url = "http://" + proxy_url
        from urllib.parse import urlsplit
        parts = urlsplit(proxy_url)
        if not parts.hostname:
            return "direct"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return f"{parts.scheme}://{parts.hostname}:{port}"

    def _egress_for(self, name: str) -> str:
        """账号出口身份。动态代理带上该号自己的 sticky session。"""
        return proxy_store.egress_key_for(name)

    def _egress_blocked(self, name: str) -> bool:
        """同出口隔离：该出口是否已被其它 in-flight 号占用，或已被标记为异常。"""
        egress = self._egress_for(name)
        engine = self._engine
        if engine and engine.egress_is_unhealthy(egress):
            return True
        if not config.ISOLATE_SHARED_EGRESS:
            return False
        for other in self.accounts:
            if other == name:
                continue
            if self._egress_for(other) != egress:
                continue
            lock = self._locks.get(other)
            if lock and lock.locked():
                return True
        return False

    def _sorted_accounts(self) -> list:
        """钉住账号优先，其次按 weight 降序，最后按名称；供派号扫描使用。"""
        rows = self.list_accounts()
        preferred = self._conn.execute(
            "SELECT name FROM accounts_meta WHERE preferred=1"
        ).fetchall()
        pref = {r["name"] for r in preferred}
        rows.sort(key=lambda a: (0 if a["name"] in pref else 1, -int(a.get("weight", 1)), a["name"]))
        return rows

    # ===== 选号引擎（移植自 api-pool 的 AccountPool）=====

    def _proxy_url_for(self, name: str) -> str:
        """账号实际使用的代理 URL（动态代理会自动带上独立的 sticky session）。"""
        return proxy_store.proxy_url_for(name)

    def rotate_account_ip(self, name: str, reason: str = "",
                          force: bool = False) -> dict:
        """给号换一个出口 IP（动态代理换 session；静态代理无操作）。

        force=True 是面板手动操作，不受「最小换IP间隔」限制。
        """
        new_session = proxy_store.rotate_account_session(
            name, reason=reason, force=force)
        if not new_session:
            info = proxy_store.get_account_proxy(name)
            if info and info.get("mode") == "dynamic":
                return {"ok": False, "account": name, "rotated": False,
                        "reason": "距上次轮换不足最小间隔"}
            return {"ok": False, "account": name, "rotated": False,
                    "reason": "该号未绑定动态代理"}
        if self._engine:
            self._engine.set_account_egress_key(name, proxy_store.egress_key_for(name))
        return {"ok": True, "account": name, "rotated": True,
                "session": new_session,
                "proxy_url": proxy_store.proxy_url_for(name)}

    def _on_egress_unhealthy(self, egress: str, reason: str) -> None:
        """某个出口被上游判异常时：动态代理的号直接换 IP，而不是干等冷却。

        静态代理没有可换的出口，保持原有冷却行为。
        """
        for name in self.accounts:
            try:
                if proxy_store.egress_key_for(name) != egress:
                    continue
            except Exception:  # noqa: BLE001
                continue
            # 提取型：把当前节点标记废弃，否则轮换后可能又挑回同一个坏节点
            marked_dead = False
            try:
                sess = proxy_store.get_session(name)
                if sess and sess.get("node_id"):
                    proxy_store.mark_node_dead(
                        int(sess["node_id"]), reason or "egress unhealthy")
                    marked_dead = True
            except Exception:  # noqa: BLE001
                pass
            try:
                # 刚把这个节点判废，就必须换走 —— 否则号被钉死在一个已知坏节点上，
                # 还要等「最小换IP间隔」才动。这里 force 只用于「已判定为坏」的确定场景，
                # 自动轮换的防刷间隔对其它情况照旧生效。
                result = self.rotate_account_ip(name, reason=reason,
                                                force=marked_dead)
            except Exception:  # noqa: BLE001
                continue
            if result.get("rotated"):
                logger.info("出口 %s 异常，已为 %s 换出口 IP（%s）", egress, name, reason)

    def _pure_state(self, name: str) -> str | None:
        """cookie 账号的纯 API 状态文件；不存在则返回 None（走浏览器出片）。"""
        if not config.PURE_API_ENABLED:
            return None
        p = Path(self.accounts_dir) / name / "cookie_state.json"
        return str(p) if p.is_file() else None

    def _is_cookie_source(self, name: str) -> bool:
        meta = self._meta(name)
        return bool(meta and meta["source"] == "cookie")

    async def _generate_effective(self, account, prompt, ratio, duration, model, *,
                                  on_conversation_id, on_poll, on_balance,
                                  reference_image_paths):
        """cookie 账号走纯 API；失败直接抛出真实原因（不再回退浏览器）。

        浏览器 profile 对 cookie 号不可靠（导入的浏览器会话常显示已登出），
        所以 cookie 号只用纯 API；login 号走浏览器出片。
        """
        pure_state = self._pure_state(account)
        if pure_state is not None and self._is_cookie_source(account):
            # cookie 号：纯 API 出片；失败抛真实错误，不再回退浏览器
            result = await asyncio.to_thread(
                generate_pure_video,
                account=account, prompt=prompt, ratio=ratio, duration=duration,
                model=model, cookie_state_path=pure_state,
                proxy=self._proxy_url_for(account),
                on_conversation_id=on_conversation_id, on_poll=on_poll,
                on_balance=on_balance,
                reference_image_paths=reference_image_paths,
            )
        else:
            # login 账号走浏览器出片
            result = await generate_video(
                account, prompt, ratio, duration, model=model,
                on_conversation_id=on_conversation_id, on_poll=on_poll,
                on_balance=on_balance, reference_image_paths=reference_image_paths)
        return self._web_playable(result)

    def _web_playable(self, result: dict) -> dict:
        """上游成片是 HEVC，转成 H.264 后浏览器（画布 <video>）才能直接预览。"""
        local = (result or {}).get("local_path")
        if local and Path(local).exists():
            result["local_path"] = str(ensure_web_playable(local))
        return result

    async def test_generate(self, account: str, prompt: str, ratio: str | None = None,
                            duration: int = 10, model: str = "seedance-2.0") -> dict:
        """账号级测试出片：强制用指定账号生成一条，返回成功/失败与成品 URL。

        不经过号池选号调度（跳过 _schedulable / 每日上限等），只验证该号本身能否出片。
        仍走该号的代理与 profile（cookie 号走纯 API，login 号走浏览器）。
        """
        if account not in self.accounts:
            raise FileNotFoundError(f"profile 不存在: {account}")
        lock = self._locks.setdefault(account, asyncio.Lock())
        async with lock:
            cost = self._duration_cost(model, duration)

            def on_balance(balance, source=""):
                self._set_credit_balance(account, balance, source)

            try:
                result = await self._generate_effective(
                    account, prompt, ratio, duration, model,
                    on_conversation_id=None, on_poll=None, on_balance=on_balance,
                    reference_image_paths=[],
                )
                local = result.get("local_path")
                if not local or not Path(local).exists():
                    raise RuntimeError("出片未产出本地视频文件")
                self._claim_for(account, cost, result)
                self._conn.execute(
                    "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                    (time.time(), account))
                self._conn.commit()
                self._clear_risk(account)
                return {
                    "ok": True,
                    "account": account,
                    "local_path": str(local),
                    "video_url": f"{config.PUBLIC_BASE}/videos/{Path(local).name}",
                }
            except TimeoutError as exc:
                return {"ok": False, "account": account, "error": f"生成超时: {exc}"}
            except ShortClipError as exc:
                # 只给了短视频（例如 30 秒只回 15 秒）：不拼接、也不把这个号标成失败。
                return {"ok": False, "account": account, "error": str(exc)[:300]}
            except LoginExpiredError as exc:
                self._set_risk(account, str(exc))
                return {"ok": False, "account": account, "error": f"登录态失效: {exc}"}
            except FileNotFoundError as exc:
                return {"ok": False, "account": account, "error": f"profile 缺失: {exc}"}
            except Exception as exc:
                if "登录态失效" in str(exc):
                    self._set_risk(account, str(exc))
                else:
                    # 测试生成同样要按上游文案落状态：瞬时限流只短冷却，不当成永久失败。
                    self._note_upstream_failure(account, str(exc))
                return {"ok": False, "account": account, "error": str(exc)[:300]}

    def _meta_weight(self, name: str) -> int:
        m = self._meta(name)
        return int(m["weight"]) if m and m["weight"] else 1

    def _sync_engine(self) -> _PoolEngine:
        """从 accounts + proxy_store + accounts_meta 重建/刷新共享选号引擎。"""
        now = time.time()
        cfg_list = []
        for name in self.accounts:
            cfg_list.append(_PoolAccountConfig(
                id=name,
                state_file=Path(self.accounts_dir) / name / "state.json",
                profile_dir=str(Path(self.accounts_dir) / name),
                proxy=self._proxy_url_for(name),
                egress_key=self._egress_for(name),
                enabled=True,
                weight=self._meta_weight(name),
            ))
        engine = _PoolEngine(
            cfg_list,
            daily_success_limit=DAILY_LIMIT,
            max_open_accounts=config.MAX_OPEN_ACCOUNTS,
            isolate_shared_egress=config.ISOLATE_SHARED_EGRESS,
            fail_score_cap=config.FAIL_SCORE_CAP,
            min_submit_interval_seconds=config.MIN_SUBMIT_INTERVAL_SECONDS,
            probe_interval_seconds=config.PROBE_INTERVAL_SECONDS,
        )
        # 出口被判异常时给动态代理换 IP（静态代理走原冷却逻辑）
        engine.on_egress_unhealthy = self._on_egress_unhealthy
        for name in self.accounts:
            st = engine.states.get(name)
            m = self._meta(name)
            if not st:
                continue
            st.successes_today = self.used_today(name)
            if m and m["preferred"]:
                engine.set_preferred(name)
            if m:
                if m["cooldown_until"] and m["cooldown_until"] > now:
                    st.status = "cooldown"
                    st.cooldown_until = m["cooldown_until"]
                if m["rate_limited_until"] and m["rate_limited_until"] > now:
                    st.status = "cooldown"
                    st.cooldown_until = max(st.cooldown_until, m["rate_limited_until"])
                if m["quota_blocked_until"] and m["quota_blocked_until"] > now:
                    st.status = "cooldown"
                    st.cooldown_until = max(st.cooldown_until, m["quota_blocked_until"])
                if m["login_ok"] == 0 and m["login_checked_at"]:
                    st.status = "expired"
                elif m["login_ok"] == 1:
                    st.status = "healthy"
                elif m["login_ok"] is None:
                    st.status = "standby"
                if m["failed_at"] and m["failed_at"] > 0:
                    st.consecutive_failures = max(st.consecutive_failures, 1)
        self._engine = engine
        return engine

    def preview_route(self) -> dict:
        return self._sync_engine().preview_route()

    def set_preferred(self, name: str | None) -> None:
        self._conn.execute(
            "UPDATE accounts_meta SET preferred=? WHERE name=?",
            (1 if name else 0, name or ""),
        )
        if name:
            self._conn.execute(
                "UPDATE accounts_meta SET preferred=0 WHERE name<>?", (name,)
            )
        self._conn.commit()
        engine = self._sync_engine()
        engine.set_preferred(name)

    def set_weight(self, name: str, weight: int) -> None:
        self._conn.execute(
            "UPDATE accounts_meta SET weight=? WHERE name=?", (max(1, int(weight)), name)
        )
        self._conn.commit()

    def mark_egress_unhealthy(self, egress: str, reason: str = "", seconds: int = 120):
        engine = self._sync_engine()
        engine.mark_egress_unhealthy(egress, reason, seconds)
        return engine.isolation_report()

    def rebalance_proxy_bindings(self) -> dict:
        from pool import ProxyBindError
        proxy_ids = [p["id"] for p in proxy_store.list_proxies()
                     if p.get("enabled") is not False]
        if not proxy_ids:
            raise ProxyBindError("proxy_pool_empty")
        busy = {n for n in self.accounts if self._locks.get(n) and self._locks[n].locked()}
        idle = [n for n in self.accounts if n not in busy]
        moved = 0
        for i, name in enumerate(idle):
            pid = proxy_ids[i % len(proxy_ids)]
            if (proxy_store.get_account_proxy(name) or {}).get("id") == pid:
                continue
            proxy_store.set_account_proxy(name, pid)
            moved += 1
        distribution = {pid: 0 for pid in proxy_ids}
        for name in self.accounts:
            info = proxy_store.get_account_proxy(name)
            if info and info["id"] in distribution:
                distribution[info["id"]] += 1
        return {"moved_count": moved, "busy_count": len(busy), "distribution": distribution}

    def bind_proxies(self, proxies: list[str], force: bool = False, mode: str = "sticky") -> dict:
        from pool import ProxyBindError
        if mode != "sticky":
            raise ProxyBindError("unsupported_mode", {"mode": mode})
        if not proxies:
            raise ProxyBindError("proxy_pool_empty")
        ids = [self._ensure_proxy_row(u) for u in proxies]
        needed = len(self.accounts)
        if len(ids) < needed:
            if not force:
                raise ProxyBindError(
                    "proxy_pool_short",
                    {"needed": needed, "got": len(ids), "short": needed - len(ids)},
                )
            ids = (ids * ((needed // len(ids)) + 1))[:needed]
        bound = []
        skipped = []
        for i, name in enumerate(self.accounts):
            if self._locks.get(name) and self._locks[name].locked():
                skipped.append({"id": name})
                continue
            pid = ids[i]
            proxy_store.set_account_proxy(name, pid)
            bound.append({"id": name, "proxy": proxies[i % len(proxies)]})
        return {"ok": True, "bound": bound, "skipped": skipped}

    @staticmethod
    def _ensure_proxy_row(url: str) -> str:
        from urllib.parse import unquote, urlsplit
        if "://" not in (url or ""):
            url = "http://" + url
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        user = unquote(parts.username or "")
        pwd = unquote(parts.password or "")
        for p in proxy_store.list_proxies():
            if p["host"] == host and p["port"] == port:
                return p["id"]
        name = f"auto-{host}-{port}"
        try:
            return proxy_store.create_proxy(name, parts.scheme, host, port, user, pwd)["id"]
        except ValueError:
            for p in proxy_store.list_proxies():
                if p["host"] == host and p["port"] == port:
                    return p["id"]
            raise

    def activate_standby(self, probe_fn, region: str, version: str):
        engine = self._sync_engine()
        activated = engine.activate_standby(probe_fn, region, version)
        if activated:
            self.set_login_status(activated, True)
        return activated

    def probe_all(self, probe_fn, region: str, version: str) -> list:
        engine = self._sync_engine()
        results = engine.probe_all(probe_fn, region, version)
        for account, ok, detail in results:
            self.set_login_status(account, ok)
        return results

    def engine_snapshot(self) -> dict:
        return self._sync_engine().snapshot_meta()

    async def resume_video(self, account: str, conversation_id: str, timeout: int,
                           on_poll=None, duration: int | None = None,
                           ratio: str | None = None, cost: int = 1) -> dict:
        """恢复已受理会话；不参与选号，也不因账号当前额度状态跳过。"""
        async with self.semaphore:
            lock = self._locks.setdefault(account, asyncio.Lock())
            async with lock:
                def on_balance(balance, source=""):
                    self._set_credit_balance(account, balance, source)
                try:
                    result = self._web_playable(await resume_video(
                        account, conversation_id, timeout,
                        on_poll=on_poll, on_balance=on_balance,
                        duration=duration, ratio=ratio))
                    self._claim_for(account, cost, result)
                    self._conn.execute(
                        "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                        (time.time(), account))
                    self._conn.commit()
                    return result
                except TimeoutError:
                    raise
                except LoginExpiredError as e:
                    print(f"[pool] {account} 登录态失效，标记需重新登录: {e}", flush=True)
                    self._set_risk(account, str(e))
                    self._conn.execute(
                        "UPDATE accounts_meta SET login_ok=0, login_checked_at=? WHERE name=?",
                        (time.time(), account))
                    self._conn.commit()
                    raise

    async def generate_video(self, prompt: str, ratio: str = None, duration: int = None,
                             model: str = "seedance_v2.0", on_conversation_id=None,
                             on_poll=None, on_balance=None,
                             reference_image_paths: list[str] | None = None,
                             on_account_try=None, max_attempts: int = 3,
                             deadline: float | None = None) -> dict:
        """挑一个可调度且空闲的号出片；失败自动换号重试（最多 max_attempts 次）。

        - 额度不足/每日上限/风控/积分不足/通用失败（未真正出片、Dola 报错等）
          → 换下一个号以同样参数继续（风控号进冷却、上限号封顶；
          普通失败/额度报错等无底层恢复的失败额外叠加持久“失败”标记）。
        - 叠加“失败”标记不清到全池重置：后续任务直接跳过这些号；
          当某任务扫遍号池一个候选都找不到、且清掉叠加层后能解锁底层健康账号时，
          触发一次全池重置（只清叠加层）并重跑一轮；仍失败即停止，不循环。
        - TimeoutError（已拿到 conversation_id 后轮询超时）→ 不换号直接失败：
          Dola 端可能仍在生成，换号重提会重复扣额度/重复出片。
        """
        async with self.semaphore:
            self._sync_engine()
            cost = self._duration_cost(model, duration)
            last_err = None
            attempt = 0
            tried: set[str] = set()
            tried_order: list[str] = []
            wait_deadline = 0.0
            pool_reset_used = False
            # max_attempts <= 0：不限次数，只要没到 deadline 就一轮轮换号重试。
            unlimited = max_attempts <= 0

            def attempts_left() -> bool:
                return unlimited or attempt < max_attempts

            def time_left() -> bool:
                return deadline is None or time.time() < deadline

            while attempts_left() and time_left():
                picked = False
                waiting_possible = False
                for a in self._sorted_accounts():
                    if (not self._schedulable(a) or a["name"] in tried
                            or a["remaining"] < cost
                            or self._recently_failed(a["name"])):
                        continue
                    if config.ISOLATE_SHARED_EGRESS and self._egress_blocked(a["name"]):
                        continue
                    account = a["name"]
                    lock = self._locks.setdefault(account, asyncio.Lock())
                    # 并发任务跳过已占用账号，避免多个请求排队到同一个 profile。
                    if lock.locked():
                        waiting_possible = True
                        continue
                    async with lock:
                        if not self._schedulable(next(x for x in self.list_accounts() if x['name'] == account)):
                            continue  # 等待期间状态变化
                        picked = True
                        wait_deadline = 0.0
                        attempt += 1
                        if on_account_try:
                            on_account_try(account, attempt)
                        try:
                            def on_balance(balance, source=""):
                                self._set_credit_balance(account, balance, source)

                            result = await self._generate_effective(
                                account, prompt, ratio, duration, model=model,
                                on_conversation_id=on_conversation_id, on_poll=on_poll,
                                on_balance=on_balance,
                                reference_image_paths=reference_image_paths)
                            self._claim_for(account, cost, result)
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            return result
                        except CreditInsufficientError as e:
                            print(f"[pool] {account} 生成前积分不足，跳过: {e}", flush=True)
                            self._mark_quota_blocked(account, str(e))
                            last_err = e
                        except ShortClipError as e:
                            # 30 秒只要原生成片：上游这次只给了短视频就换号重试，不做两段拼接，
                            # 也不把号标成失败（短视频是上游行为，不是这个号的错）。
                            print(f"[pool] {account} 只出了短视频，不拼接，换号重试: {e}", flush=True)
                            last_err = e
                        except AccountLimitedError as e:
                            print(f"[pool] {account} 达到每日上限，立即换号: {e}", flush=True)
                            self._mark_daily_limit(account, str(e))
                            last_err = e
                        except CreditError as e:
                            print(f"[pool] {account} 额度不足，换号: {e}", flush=True)
                            self._mark_failed(account, f"额度不足: {e}")
                            last_err = e
                        except RiskControlError as e:
                            print(f"[pool] {account} 风控，冷却 30 分钟，换号: {e}", flush=True)
                            self._conn.execute(
                                "UPDATE accounts_meta SET cooldown_until=? WHERE name=?",
                                (time.time() + COOLDOWN_SEC, account))
                            self._conn.commit()
                            last_err = e
                        except TimeoutError as e:
                            # 请求已拿到 conversation_id 后仍可能在 Dola 端继续生成。
                            # 未成功出片不扣点（2026-09-02 规则）；也绝不能换号重提
                            # （Dola 端可能仍在生成，会重复出片）。
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            raise
                        except LoginExpiredError as e:
                            print(f"[pool] {account} 登录态失效，标记需重新登录并换号: {e}", flush=True)
                            self._set_risk(account, str(e))
                            self._conn.execute(
                                "UPDATE accounts_meta SET login_ok=0, login_checked_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            last_err = e
                        except FileNotFoundError as e:
                            print(f"[pool] {account} profile 缺失，跳过: {e}", flush=True)
                            self._mark_failed(account, f"profile 缺失: {e}")
                            last_err = e
                        except Exception as e:
                            message = str(e)
                            self._note_upstream_failure(account, message, attempt)
                            last_err = e
                        self._mark_attempt_failed(account)
                        tried.add(account)
                        tried_order.append(account)
                        if not attempts_left() or not time_left():
                            break
                if not picked:
                    # 只剩被其他并发任务占用的账号：有界等待重扫，而不是立刻误判“无可用账号”。
                    if waiting_possible:
                        if wait_deadline == 0:
                            wait_deadline = time.time() + WAIT_FREE_ACCOUNT_SEC
                        if time.time() < wait_deadline:
                            await asyncio.sleep(WAIT_FREE_ACCOUNT_STEP)
                            continue
                    # 全池无候选：如果只是叠加“失败”标记把所有底层健康号挡住，
                    # 触发一次全池重置（只清叠加层，不清登录/上限/风控等底层状态）再重跑一轮。
                    if not pool_reset_used and self._overlay_unlockable(cost):
                        cleared = self._clear_all_failed()
                        # 全池重置是显式重试整轮：同时清掉并发防抖的内存冷却，
                        # 否则刚失败的号仍会被 _recently_failed 挡 120 秒。
                        self._fail_until.clear()
                        print(f"[pool] 触发全池失败重置，清除 {cleared} 个叠加标记后重试", flush=True)
                        pool_reset_used = True
                        attempt = 0
                        tried.clear()
                        wait_deadline = 0.0
                        continue
                    # 有时限且还没到点：这一轮号都试过了，等失败冷却结束后从头再来一轮。
                    # 全池都已达上限/积分不足时没有等待意义，直接走下方的 429 报错。
                    if (deadline is not None and unlimited and time_left() and tried_order
                            and not self.all_accounts_limited and not self.all_accounts_quota_blocked):
                        tried.clear()
                        wait_deadline = 0.0
                        await asyncio.sleep(min(10.0, max(1.0, deadline - time.time())))
                        continue
                    break
            if self.all_accounts_quota_blocked:
                raise AllAccountsQuotaBlockedError(
                    f"429: 所有已开启调度的账号均已知积分不足: {last_err or '无号'}"
                )
            if self.all_accounts_limited:
                raise AllAccountsLimitedError(
                    f"429: 所有已开启调度的账号均已达到 Dola 每日视频上限: {last_err or '无号'}"
                )
            if last_err is not None:
                chain = "、".join(tried_order) if tried_order else str(last_err)
                timed_out = deadline is not None and time.time() >= deadline
                prefix = "超过任务时限仍未生成成功，" if timed_out else ""
                raise RuntimeError(
                    f"{prefix}连续 {len(tried_order)} 次生成均失败（依次尝试账号: {chain}）: {str(last_err)[:300]}"
                )
            raise RuntimeError(f"号池无可用账号（调度关闭/冷却/额度用完）: {last_err or '无号'}")
