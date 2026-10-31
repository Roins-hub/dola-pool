"""浏览器版号池：扫描 accounts/ profile，可切换本地每日上限，同号互斥。

v2：accounts_meta 元数据表（调度开关/备注/登录态缓存/风控冷却），面板读写同一份数据。
配额计数落 SQLite（pool_usage.db），保守计数：出片成功或额度报错才记 1 次。
"""
import asyncio
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from dola_client import CreditError
from video_worker_ui import (
    AccountLimitedError, BirthdayVerificationRequired, CreditInsufficientError, LoginExpiredError, RiskControlError,
    generate_video, resume_video,
)
import config
import proxy_usage
from submission_slot import SubmissionSlot
from navigation_errors import BrowserCleanupError
from video_outcome import ExplicitNotAcceptedError, PlatformNoChargeError, PlatformVideoError, PlatformDailyLimitError, conversation_outcome
from video_evidence import safe_text, SubmissionUnconfirmedError
from video_polling import HttpVideoSession
from video_prompt import build_video_prompt
from video_capabilities import normalize_model
from dola_watermark import WatermarkPreferenceError
from quota_ledger import QuotaLedger, QUOTA_TYPES, quota_type, reset_at_from_reply

_generation_quota = ContextVar('dola_generation_quota', default=None)

DAILY_LIMIT = 2
QUOTA_POLICIES = {'seedance25': DAILY_LIMIT, 'fast': 4}
MAX_ACCOUNT_ATTEMPTS = 5
COOLDOWN_SEC = 1800  # 风控冷却 30 分钟
PROXY_RETRY_BASE_SEC = 30
PROXY_RETRY_MAX_SEC = 120
PREPARATION_RETRY_BASE_SEC = 30
PREPARATION_RETRY_MAX_SEC = 120
ACCOUNT_CATEGORIES = {"long_term": "接码长期号", "disposable": "日抛号"}


def _minimum_credit_balance(model=None):
    # This only permits a 1.0 trial with a nonzero balance, not a billing estimate.
    if model is not None:
        try:
            if normalize_model(model) == 'seedance-1.0':
                return 1
        except ValueError:
            pass
    return 2


def _limit_timezone():
    """返回每日额度刷新时区；Windows 未安装 tzdata 时使用内置回退。"""
    try:
        return ZoneInfo(config.LIMIT_RESET_TZ)
    except Exception:
        if config.LIMIT_RESET_TZ == "Europe/Berlin":
            now_utc = datetime.now(timezone.utc)
            year = now_utc.year

            def last_sunday(month: int) -> date:
                d = date(year, month, 31)
                while d.weekday() != 6:
                    d -= timedelta(days=1)
                return d

            dst_start = datetime.combine(
                last_sunday(3), dt_time(1)).replace(tzinfo=timezone.utc)
            dst_end = datetime.combine(
                last_sunday(10), dt_time(1)).replace(tzinfo=timezone.utc)
            hours = 2 if dst_start <= now_utc < dst_end else 1
            return timezone(timedelta(hours=hours))
        offsets = {"Asia/Tokyo": 9, "Asia/Hong_Kong": 8, "Asia/Shanghai": 8, "UTC": 0}
        return timezone(timedelta(hours=offsets.get(config.LIMIT_RESET_TZ, 9)))


def _today_key() -> str:
    return datetime.now(_limit_timezone()).date().isoformat()


class AllAccountsLimitedError(RuntimeError):
    """所有已开启调度的账号都达到 Dola 每日视频上限。"""


class AllAccountsQuotaBlockedError(RuntimeError):
    """所有已开启调度的账号都已知积分不足。"""


class AllAccountsProxyBlockedError(RuntimeError):
    """所有已开启调度的账号，其 ISP 请求额度都已用完或被暂停。"""


class AccountAttemptsExhaustedError(RuntimeError):
    attempts_exhausted = True
    task_phase = 'failed'

    def __init__(self, count, error=None, account=None, conversation_id=None, reply=''):
        self.account, self.conversation_id, self.reply = account, conversation_id, reply
        reason = '已达到上限' if count >= MAX_ACCOUNT_ATTEMPTS else '没有更多可用的不同账号'
        super().__init__(f'已尝试 {count}/{MAX_ACCOUNT_ATTEMPTS} 个不同账号，{reason}；'
                         f'最后原因：{safe_text(error) or "前次尝试未确认受理"}')


class BrowserPool:
    def __init__(self, accounts_dir: str = "accounts", db_path: str = "pool_usage.db",
                 max_concurrency: int = 1, *, scheduler=None, platform='dola'):
        self.accounts_dir = Path(accounts_dir)
        self.scheduler, self.platform = scheduler, platform
        self.semaphore = scheduler.browser_slots if scheduler else asyncio.Semaphore(max_concurrency)
        self.inflight = scheduler.inflight if scheduler else asyncio.Semaphore(config.MAX_INFLIGHT)
        self._proxy_slots = scheduler.proxy_slots if scheduler else {}
        self._available = scheduler.available if scheduler else asyncio.Event()
        self._recovering: set[str] = set()
        self._unsafe_proxies = scheduler.unsafe_lanes if scheduler else set()
        self._proxy_backoff: dict[str, dict] = {}
        self._preparation_backoff: dict[str, dict] = {}
        self._locks = scheduler.account_locks(platform) if scheduler else {}
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS usage (account TEXT, day TEXT, used INTEGER, "
            "PRIMARY KEY(account, day))"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS accepted_videos "
            "(conversation_id TEXT PRIMARY KEY, account TEXT, day TEXT)"
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
                credit_checked_at REAL DEFAULT 0
            )
            """
        )
        self._conn.commit()
        # 旧库兼容：补新元数据列
        for column, definition in (
            ("email", "TEXT DEFAULT ''"),
            ("login_reason", "TEXT DEFAULT ''"),
            ("rate_limited_until", "REAL DEFAULT 0"),
            ("limit_reason", "TEXT DEFAULT ''"),
            ("quota_blocked_until", "REAL DEFAULT 0"),
            ("quota_reason", "TEXT DEFAULT ''"),
            ("credit_balance", "INTEGER"),
            ("credit_checked_at", "REAL DEFAULT 0"),
            ("verification_required", "TEXT DEFAULT ''"),
            ("category", "TEXT NOT NULL DEFAULT 'long_term'"),
            ("category_override", "INTEGER NOT NULL DEFAULT 0"),
            ("onboarding_state", "TEXT NOT NULL DEFAULT ''"),
            ("onboarding_error", "TEXT NOT NULL DEFAULT ''"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE accounts_meta ADD COLUMN {column} {definition}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass
        with self._conn:
            # Rotation is independent of daily quota and survives quota resets/restarts.
            self._conn.execute('BEGIN')
            columns = {row['name'] for row in self._conn.execute('PRAGMA table_info(accounts_meta)')}
            new_rotation = 'dispatch_count' not in columns
            if new_rotation:
                self._conn.execute('ALTER TABLE accounts_meta ADD COLUMN dispatch_count INTEGER NOT NULL DEFAULT 0')
            if 'last_dispatched_at' not in columns:
                self._conn.execute('ALTER TABLE accounts_meta ADD COLUMN last_dispatched_at REAL NOT NULL DEFAULT 0')
            if new_rotation:
                self._conn.execute('''UPDATE accounts_meta SET dispatch_count=MAX(
                    COALESCE((SELECT SUM(used) FROM usage WHERE account=accounts_meta.name),0),
                    CASE WHEN last_used_at>0 THEN 1 ELSE 0 END),
                    last_dispatched_at=COALESCE(last_used_at,0)''')
            # Preparation attempts are diagnostic only. Preserve prior actual quota
            # reservations as the conservative baseline for the sending round.
            if 'send_count' not in columns:
                self._conn.execute('ALTER TABLE accounts_meta ADD COLUMN send_count INTEGER NOT NULL DEFAULT 0')
                self._conn.execute('''UPDATE accounts_meta SET send_count=MAX(0,
                    COALESCE((SELECT SUM(used) FROM usage WHERE account=accounts_meta.name),0))''')
            if 'last_sent_at' not in columns:
                self._conn.execute('ALTER TABLE accounts_meta ADD COLUMN last_sent_at REAL NOT NULL DEFAULT 0')
                self._conn.execute('UPDATE accounts_meta SET last_sent_at=CASE WHEN send_count>0 THEN COALESCE(last_used_at,0) ELSE 0 END')
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS account_categories "
                "(id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1)")
            self._conn.executemany(
                "INSERT OR IGNORE INTO account_categories(id) VALUES (?)",
                [(category,) for category in ACCOUNT_CATEGORIES])
            self._conn.execute("CREATE TABLE IF NOT EXISTS pool_settings ("
                "id INTEGER PRIMARY KEY CHECK(id=1), quota_mode TEXT NOT NULL "
                "CHECK(quota_mode IN ('seedance25','fast')))")
            self._conn.execute("INSERT OR IGNORE INTO pool_settings(id,quota_mode) VALUES (1,'seedance25')")
        self.quota_ledger = QuotaLedger(self._conn, timezone=_limit_timezone)
        self.quota_ledger.migrate_legacy()

    def get_quota_policy(self) -> dict:
        mode = self._conn.execute('SELECT quota_mode FROM pool_settings WHERE id=1').fetchone()[0]
        return {'mode': mode, 'limit': QUOTA_POLICIES[mode]}

    def set_quota_policy(self, mode: str) -> dict:
        if not isinstance(mode, str) or mode not in QUOTA_POLICIES:
            raise ValueError('本地每日上限模式仅支持 fast 或 seedance25')
        with self._conn:
            self._conn.execute('UPDATE pool_settings SET quota_mode=? WHERE id=1', (mode,))
        self._available.set()
        return self.get_quota_policy()

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

    def list_categories(self, accounts=None) -> list:
        accounts = self.list_accounts() if accounts is None else accounts
        enabled = dict(self._conn.execute("SELECT id, enabled FROM account_categories"))
        return [{"id": category, "label": label, "enabled": bool(enabled[category]),
                 "count": sum(a["category"] == category for a in accounts)}
                for category, label in ACCOUNT_CATEGORIES.items()]

    def set_category(self, name: str, category: str):
        if category not in ACCOUNT_CATEGORIES:
            raise ValueError("账号分类无效")
        if name not in self.accounts:
            raise ValueError("账号不存在")
        with self._conn:
            self._conn.execute(
                "UPDATE accounts_meta SET category=?, category_override=0 WHERE name=?", (category, name))
        self._available.set()

    def set_category_enabled(self, category: str, enabled: bool):
        if category not in ACCOUNT_CATEGORIES:
            raise ValueError("账号分类无效")
        with self._conn:
            self._conn.execute("UPDATE account_categories SET enabled=? WHERE id=?", (int(enabled), category))
            self._conn.execute("UPDATE accounts_meta SET category_override=0 WHERE category=?", (category,))
        self._available.set()

    def _proxy_state(self, name: str) -> dict:
        """账号当前出口的 ISP 额度状态。

        只有账号专属 ISP（file/map）才参与额度统计；
        全局 DOLA_PROXY / _default 默认映射属于共享出口，返回空额度状态。
        """
        empty = {"proxy_key": "", "proxy_server": "", "proxy_used": 0,
                 "proxy_limit": 0, "proxy_remaining": 0, "proxy_paused": False,
                 "proxy_exhausted": False, "proxy_blocked": False, "proxy_warn": False}
        raw, source = config.account_proxy_raw(name)
        if config.ISP_ONLY:
            try:
                if source not in ('file', 'map'):
                    raise ValueError()
                config.require_isp_proxy(config._parse_proxy(raw))
            except (ValueError, TypeError):
                return {**empty, 'proxy_blocked': True}
        if source not in ("file", "map"):
            return empty
        proxy = config._parse_proxy(raw) if raw else None
        if not proxy:
            return empty
        st = proxy_usage.usage.get(proxy)
        return {"proxy_key": st["key"], "proxy_server": st["server"],
                "proxy_used": st["used"], "proxy_limit": st["limit"],
                "proxy_remaining": st["remaining"], "proxy_paused": st["paused"],
                "proxy_exhausted": st["exhausted"], "proxy_blocked": st["blocked"],
                "proxy_warn": st["warn"]}

    def used_today(self, account: str) -> int:
        row = self._conn.execute(
            "SELECT used FROM usage WHERE account=? AND day=?",
            (account, _today_key()),
        ).fetchone()
        return row[0] if row else 0

    def _claim(self, account: str):
        self._conn.execute(
            "INSERT INTO usage(account, day, used) VALUES (?,?,1) "
            "ON CONFLICT(account, day) DO UPDATE SET used=used+1",
            (account, _today_key()),
        )
        self._conn.commit()

    def _claim_conversation(self, account: str, conversation_id: str):
        """Reserve daily quota once, including after a process restart."""
        context = _generation_quota.get()
        if context and context.get('kind'):
            self.quota_ledger.confirm(account, context['kind'], context['task_id'], conversation_id)
            return
        with self._conn:
            day = _today_key()
            inserted = self._conn.execute(
                "INSERT OR IGNORE INTO accepted_videos VALUES (?,?,?)",
                (conversation_id, account, day)).rowcount
            if inserted:
                self._conn.execute(
                    "INSERT INTO usage(account, day, used) VALUES (?,?,1) "
                    "ON CONFLICT(account, day) DO UPDATE SET used=used+1", (account, day))

    def _release_unaccepted_claim(self, account: str, conversation_id: str):
        # Remove only this proven no-start reservation, never an engine credit balance.
        context = _generation_quota.get()
        if context and context.get('kind'):
            return self.quota_ledger.release(account,context['kind'],context['task_id'],proven_unconsumed=True)
        if self.quota_ledger.release_conversation(account,conversation_id):
            return True
        with self._conn:
            row = self._conn.execute('SELECT day FROM accepted_videos WHERE account=? AND conversation_id=?',
                                     (account, conversation_id)).fetchone()
            if row:
                removed = self._conn.execute('DELETE FROM accepted_videos WHERE account=? AND conversation_id=?',
                                             (account, conversation_id)).rowcount
                if removed:
                    self._conn.execute('UPDATE usage SET used=MAX(used-1,0) WHERE account=? AND day=?',
                                       (account, row[0]))
                return bool(removed)
        return False

    def release_no_charge_conversation(self, account: str, conversation_id: str, error) -> bool:
        """Return only this proven refusal's local reservation, never platform credits."""
        if not isinstance(error, PlatformNoChargeError):
            return False
        error.account, error.conversation_id = account, conversation_id
        context = _generation_quota.get() or {}
        if context.get('kind') and getattr(error,'task_phase','') == 'expert_weekly_limit':
            self.quota_ledger.block(account,context['kind'],getattr(error,'reset_at',None) or
                reset_at_from_reply(getattr(error,'reply','')),safe_text(error))
        if not account or not conversation_id:
            return False
        released = self._release_unaccepted_claim(account, conversation_id)
        # A refund must not clear recovery/ISP quarantine after failed browser cleanup.
        self._available.set()
        return released

    def release_unaccepted_conversation(self, account: str, conversation_id: str):
        """Release a proven no-start conversation before the same task rotates."""
        self._release_unaccepted_claim(account, conversation_id)
        self._recovering.discard(account)
        if self.scheduler:
            self.scheduler.release_recovery(self.platform, account)
        self._available.set()

    def reserve_recoveries(self, rows):
        for row in rows:
            account = row['account']
            self._recovering.add(account)
            if self.scheduler:
                try:
                    self.scheduler.reserve_recovery(self.platform, account,
                        self._submission_proxy({'name': account, **self._proxy_state(account)}))
                except (ValueError, TypeError):
                    # Its resume fails closed with the original binding error;
                    # a removed profile must not prevent the whole API starting.
                    continue

    def _task_slot(self, recovery=False):
        return (self.scheduler.task_slot(self.platform, recovery=recovery)
                if self.scheduler else self.inflight)

    def reserve_unconfirmed_submission(self, account, task_id):
        # Local-only key: a restart between the two DB commits must not charge twice.
        existing = self._conn.execute('SELECT quota_type FROM generation_quota WHERE account=? AND request_id=? AND state!=?',
                                      (account,task_id,'released')).fetchone()
        if existing:
            self.quota_ledger.mark_sent(account,existing[0],task_id)
            return
        context = _generation_quota.get()
        if context and context.get('kind'):
            self.quota_ledger.mark_sent(account,context['kind'],task_id)
            return
        self._claim_conversation(account, f'unconfirmed:{task_id}:{account}')

    def get_generation_quota(self, account, model, duration=None, duration_profile=None):
        kind = quota_type(model,duration,duration_profile)
        return self.quota_ledger.snapshot(account,kind) if kind else None

    def _require_unsubmitted_intent(self, task_id):
        context = _generation_quota.get() or {}
        if not context.get('kind'):
            return
        existing = self._conn.execute('''SELECT account,conversation_id FROM generation_quota
            WHERE request_id=? AND state IN ('reserved','used','uncertain') LIMIT 1''',(task_id,)).fetchone()
        if existing:
            error = SubmissionUnconfirmedError('该申请已有额度预占或发送记录，未再次提交；请核对或补收原任务')
            error.account,error.conversation_id = existing[0],existing[1] or ''
            error.task_phase = 'submission_unknown'
            raise error

    def _rotation_usage(self, account):
        """Rank this route's current quota window, including in-flight claims."""
        context = _generation_quota.get() or {}
        if context.get('kind'):
            return self.quota_ledger.snapshot(account['name'], context['kind'])['occupied']
        return account['used_today']

    def _rotation_eligible(self, accounts, model, attempted):
        # A failed attempt stays excluded from THIS task's floor even if its
        # backoff expires while another ISP is busy. New tasks may retry it.
        return [a for a in accounts if self._schedulable(a, model)
                and not (a['name'] in attempted and a['name'] in self._preparation_backoff)]

    async def _submission_candidates(self, excluded_accounts=(), model=None, preferred_account=None):
        attempted = set(excluded_accounts or ())
        while True:
            self._available.clear()
            accounts = self.list_accounts()
            retry_at = [max(a.get('proxy_cooling_until', 0), a.get('preparation_retry_until', 0)) for a in accounts
                        if a['name'] not in attempted
                        and (preferred_account is None or a['name'] == preferred_account)
                        and max(a.get('proxy_cooling_until', 0), a.get('preparation_retry_until', 0)) > time.time()
                        and self._schedulable({**a, 'proxy_cooling_until': 0, 'preparation_retry_until': 0}, model)]
            eligible = self._rotation_eligible(accounts, model, attempted)
            candidates = sorted((a for a in eligible if a['name'] not in attempted
                                 and (preferred_account is None or a['name'] == preferred_account)),
                                key=lambda a: (self._rotation_usage(a), a['last_sent_at'],
                                               a['last_dispatched_at'], a['name']))
            if candidates:
                # Busy accounts still hold their round. Failed preparation is
                # temporarily ineligible, so one broken composer cannot hold it.
                if preferred_account is None:
                    floor = min(self._rotation_usage(a) for a in eligible)
                    candidates = [a for a in candidates if self._rotation_usage(a) == floor]
                if not candidates:
                    return
            else:
                # No event is emitted when a clock expires: wake ourselves for a
                # temporary ISP outage, rather than silently parking the queue.
                if not retry_at:
                    return
                await self._wait_for_available(retry_at)
                continue
            selected = None
            for account in candidates:
                # A busy ISP must not let an already-used account overtake this round.
                name = account['name']
                lock = self._locks.setdefault(name, asyncio.Lock())
                proxy_key = self._submission_proxy(account)
                proxy_slot = (self.scheduler.lane_slot(proxy_key) if self.scheduler else
                              self._proxy_slots.setdefault(proxy_key, asyncio.Semaphore(1)))
                if (lock.locked() or proxy_slot.locked()
                        or self._proxy_reserved_for_recovery(self._submission_proxy(account))):
                    continue
                selected = account
                break
            if selected is None:
                await self._wait_for_available(retry_at)
                continue
            attempted.add(selected['name'])
            yield selected
            # Re-read counts after every attempt; concurrent tasks changed the order.

    async def _wait_for_available(self, retry_at):
        timeout = max(.01, min(retry_at)-time.time()) if retry_at else None
        try:
            await asyncio.wait_for(self._available.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def _record_dispatch(self, account):
        with self._conn:
            self._conn.execute('''UPDATE accounts_meta
                SET dispatch_count=dispatch_count+1, last_dispatched_at=? WHERE name=?''',
                (time.time(), account))

    def _record_send(self, account, proxy_key):
        with self._conn:
            self._conn.execute('UPDATE accounts_meta SET send_count=send_count+1, last_sent_at=? WHERE name=?',
                               (time.time(), account))
        self._proxy_backoff.pop(proxy_key, None)
        self._preparation_backoff.pop(account, None)
        self._available.set()

    def _pause_network_failure(self, proxy_key, error):
        code = re.search(r'\bERR_(?:CONNECTION_(?:RESET|CLOSED|ABORTED|REFUSED|TIMED_OUT)|'
                         r'TIMED_OUT|PROXY_CONNECTION_FAILED|TUNNEL_CONNECTION_FAILED|'
                         r'SOCKS_CONNECTION_FAILED|NAME_NOT_RESOLVED)\b', str(error))
        if not code and not isinstance(error, (ConnectionError, OSError)):
            return False
        # OSError includes filesystem problems; those must not quarantine a line.
        if not code and not isinstance(error, ConnectionError):
            return False
        previous = self._proxy_backoff.get(proxy_key, {})
        failures = min(3, previous.get('failures', 0)+1)
        seconds = min(PROXY_RETRY_MAX_SEC, PROXY_RETRY_BASE_SEC * (2 ** (failures-1)))
        self._proxy_backoff[proxy_key] = {'until': time.time()+seconds, 'failures': failures,
                                        'reason': f'提交前网络连接失败：{code.group(0) if code else type(error).__name__}，线路暂缓重试'}
        self._available.set()
        return True

    def _pause_preparation_failure(self, account, error):
        # No send/charge/login mutation: only this profile waits, not its ISP.
        previous = self._preparation_backoff.get(account, {})
        failures = min(3, previous.get('failures', 0) + 1)
        seconds = min(PREPARATION_RETRY_MAX_SEC, PREPARATION_RETRY_BASE_SEC * (2 ** (failures - 1)))
        self._preparation_backoff[account] = {
            'until': time.time() + seconds, 'failures': failures,
            'reason': f'发送前准备失败，账号暂缓重试：{safe_text(error)[:240]}',
        }
        self._available.set()

    def _submission_proxy(self, account):
        # Same endpoint is one physical line even when credentials differ.
        if account.get('proxy_server'):
            raw = account['proxy_server']
        else:
            proxy = config.proxy_for(account['name'])
            raw = proxy['server'] if proxy else 'direct'
        return self.scheduler.lane_id(raw, account.get('lane_id')) if self.scheduler else raw

    def _proxy_reserved_for_recovery(self, proxy_key):
        # Startup reserves original conversations before their polling coroutine
        # runs. A different account on that ISP must not jump ahead of recovery.
        if self.scheduler and self.scheduler.recovery_reserved(proxy_key):
            return True
        for name in self._recovering:
            try:
                key = self._submission_proxy({'name': name, **self._proxy_state(name)})
            except (ValueError, TypeError):
                continue
            if key == proxy_key:
                return True
        return False

    def _next_limit_reset(self) -> float:
        """计算下一次每日额度刷新时间（默认德国时间次日 00:00）。"""
        tz = _limit_timezone()
        now = datetime.now(tz)
        next_day = now.date() + timedelta(days=1)
        return datetime.combine(next_day, dt_time.min, tzinfo=tz).timestamp()

    def _clear_expired_rate_limits(self):
        now = time.time()
        cur = self._conn.execute(
            "UPDATE accounts_meta SET rate_limited_until=0, limit_reason='', "
            "quota_blocked_until=0, quota_reason='' "
            "WHERE (rate_limited_until > 0 AND rate_limited_until <= ?) "
            "OR (quota_blocked_until > 0 AND quota_blocked_until <= ?)", (now, now))
        if cur.rowcount:
            self._conn.commit()

    def _mark_quota_blocked(self, account: str, reason: str = ""):
        context = _generation_quota.get() or {}
        if context.get('kind'):
            self.quota_ledger.block(account,context['kind'],None,reason)
            return
        self._conn.execute(
            "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=?, last_used_at=? WHERE name=?",
            (self._next_limit_reset(), reason[:300], time.time(), account),
        )
        self._conn.commit()

    def _mark_daily_limit(self, account: str, reason: str = ""):
        """Dola 明确返回每日上限：封顶计数并标记到恢复时间，期间完全跳过该号。"""
        context = _generation_quota.get() or {}
        if context.get('kind'):
            self.quota_ledger.block(account,context['kind'],None,reason)
            return
        self._conn.execute(
            "INSERT INTO usage(account, day, used) VALUES (?,?,?) "
            "ON CONFLICT(account, day) DO UPDATE SET used=MAX(used, excluded.used)",
            (account, _today_key(), self.get_quota_policy()['limit']),
        )
        self._conn.execute(
            "UPDATE accounts_meta SET last_used_at=?, rate_limited_until=?, limit_reason=? WHERE name=?",
            (time.time(), self._next_limit_reset(), reason[:300], account),
        )
        self._conn.commit()

    def list_accounts(self) -> list:
        """面板视图：meta + 配额 + 限流状态 + 是否忙合并。"""
        self._clear_expired_rate_limits()
        now = time.time()
        policy = self.get_quota_policy()
        out = []
        categories = dict(self._conn.execute("SELECT id, enabled FROM account_categories"))
        for a in self.accounts:
            m = self._meta(a)
            used = self.used_today(a)
            lock = self._locks.get(a)
            category = m["category"] if m else "long_term"
            category_enabled = bool(categories.get(category, False))
            category_override = bool(m["category_override"]) if m else False
            scheduling = bool(m["scheduling"]) if m else True
            out.append({
                "name": a,
                "scheduling": scheduling,
                "category": category,
                "category_enabled": category_enabled,
                "category_override": category_override,
                "effective_scheduling": scheduling and (category_enabled or category_override)
                    and (not m or m['onboarding_state'] in ('', 'ready')),
                "onboarding_state": m['onboarding_state'] if m else '',
                "onboarding_error": m['onboarding_error'] if m else '',
                "note": m["note"] if m else "",
                "email": m["email"] if m else "",
                "created_at": m["created_at"] if m else 0,
                "last_used_at": m["last_used_at"] if m else 0,
                "dispatch_count": m['dispatch_count'] if m else 0,
                "last_dispatched_at": m['last_dispatched_at'] if m else 0,
                "send_count": m['send_count'] if m else 0,
                "last_sent_at": m['last_sent_at'] if m else 0,
                "login_ok": m["login_ok"] if m else None,
                "login_checked_at": m["login_checked_at"] if m else 0,
                "login_reason": m["login_reason"] if m else "",
                "verification_required": m["verification_required"] if m else "",
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
                "used_today": used,
                "quota_mode": policy['mode'],
                "limit": policy['limit'],
                "remaining": max(0, policy['limit'] - used),
                "busy": bool(lock and lock.locked()),
                **self._proxy_state(a),
            })
            out[-1]['quota_pools'] = {kind:self.quota_ledger.snapshot(a,kind) for kind in QUOTA_TYPES}
            out[-1]['quotas'] = out[-1]['quota_pools']
            try:
                proxy_key = self._submission_proxy(out[-1])
            except (ValueError, TypeError):
                proxy_key = ''  # Invalid bindings remain visible as blocked accounts.
            backoff = self._proxy_backoff.get(proxy_key, {})
            out[-1]['proxy_cooling_until'] = backoff.get('until', 0)
            out[-1]['proxy_cooling'] = backoff.get('until', 0) > now
            out[-1]['proxy_cooling_reason'] = backoff.get('reason', '') if backoff.get('until', 0) > now else ''
            preparation = self._preparation_backoff.get(a, {})
            out[-1]['preparation_retry_until'] = preparation.get('until', 0)
            out[-1]['preparation_retrying'] = preparation.get('until', 0) > now
            out[-1]['preparation_retry_reason'] = preparation.get('reason', '') if out[-1]['preparation_retrying'] else ''
        return out

    def set_onboarding(self, name: str, state: str, error: str = ''):
        if state not in ('queued', 'running', 'attention', 'ready'):
            raise ValueError('账号登录状态无效')
        self._ensure_meta(name)
        with self._conn:
            self._conn.execute('UPDATE accounts_meta SET onboarding_state=?, onboarding_error=? WHERE name=?',
                               (state, error[:300], name))
        self._available.set()

    def set_scheduling(self, name: str, on: bool, *, override_category: bool = False):
        # Only an explicit per-account admin action may bypass a paused category.
        # Read the category switch in the same write as scheduling to avoid stale grants.
        with self._conn:
            self._conn.execute(
                "UPDATE accounts_meta SET scheduling=?, "
                "category_override=CASE WHEN ? AND ? AND NOT COALESCE("
                "(SELECT enabled FROM account_categories WHERE id=accounts_meta.category), 0) "
                "THEN 1 ELSE 0 END, "
                "verification_required=CASE WHEN ? THEN '' ELSE verification_required END WHERE name=?",
                (int(on), on, override_category, on, name))
        self._available.set()

    def mark_birthday_verification(self, name: str):
        self._ensure_meta(name)
        self._conn.execute(
            "UPDATE accounts_meta SET scheduling=0, category_override=0, "
            "verification_required='birthday' WHERE name=?", (name,))
        self._conn.commit()

    def set_email(self, name: str, email: str):
        self._conn.execute(
            "UPDATE accounts_meta SET email=? WHERE name=?", (email, name))
        self._conn.commit()

    def set_login_status(self, name: str, ok: bool, reason: str = ""):
        self._ensure_meta(name)
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=?, login_reason=? WHERE name=?",
            (1 if ok else 0, time.time(), "" if ok else reason[:300], name),
        )
        self._conn.commit()

    def mark_login_expired(self, name: str):
        # Store a fixed, user-facing reason instead of raw exceptions containing credentials.
        self.set_login_status(name, False, "运行中检测到登录失效，请重新登录或导入新 CK。")

    def set_note(self, name: str, note: str):
        self._conn.execute(
            "UPDATE accounts_meta SET note=? WHERE name=?", (note, name))
        self._conn.commit()

    def reset_all_accounts(self, open_scheduling: bool = True,
                           clear_usage: bool = True,
                           clear_credit: bool = False) -> dict:
        """一键恢复所有号为「满状态」。

        - 清除冷却(cooldown)、限流(rate_limited)、额度封锁(quota_blocked) 的时间戳与原因
        - 可选：重新打开所有号的调度(scheduling=1)
        - 可选：清零今日用量(usage 表)，让「今日剩余」恢复满额
        - 可选：清除本地缓存的积分余额(credit_balance=NULL)，下次使用时重新探测

        返回受影响账号数量与操作明细，便于前端提示。
        """
        now = time.time()
        with self._conn:
            # 1) 清除所有限制状态（不管是否过期，一律清零）
            cur = self._conn.execute(
                "UPDATE accounts_meta SET cooldown_until=0, "
                "rate_limited_until=0, limit_reason='', "
                "quota_blocked_until=0, quota_reason='' "
            )
            n_meta = cur.rowcount
            # 2) 重新打开调度
            n_sched = 0
            if open_scheduling:
                cur = self._conn.execute(
                    "UPDATE accounts_meta SET scheduling=1 WHERE scheduling=0 AND verification_required=''")
                n_sched = cur.rowcount
            # 3) 清零今日用量
            n_usage = 0
            if clear_usage:
                cur = self._conn.execute(
                    "DELETE FROM usage WHERE day=?", (_today_key(),))
                n_usage = cur.rowcount
            # 4) 清除本地积分缓存
            n_credit = 0
            if clear_credit:
                cur = self._conn.execute(
                    "UPDATE accounts_meta SET credit_balance=NULL, credit_checked_at=0 "
                    "WHERE credit_balance IS NOT NULL")
                n_credit = cur.rowcount
        total = len(self.accounts)
        return {
            "total_accounts": total,
            "cleared_meta": n_meta,
            "opened_scheduling": n_sched,
            "cleared_usage_rows": n_usage,
            "cleared_credit": n_credit,
        }

    @property
    def trash_dir(self) -> Path:
        # 以 "." 开头：accounts 扫描会自动忽略，不会把回收站当成号
        return self.accounts_dir / ".trash"

    def delete_account(self, name: str, soft: bool = True):
        """删除账号。

        默认「软删除」：把 profile 目录移到 accounts/.trash/ 而不是物理删除，
        误删后可用 restore_account() 找回（用户曾因掉登录误判被批量删号）。
        只有显式 soft=False 时才真正 rmtree。
        """
        lock = self._locks.get(name)
        if lock and lock.locked():
            raise RuntimeError("账号正在出片，不能删除")
        d = self.accounts_dir / name
        if d.exists():
            if soft:
                self.trash_dir.mkdir(parents=True, exist_ok=True)
                backup = self.trash_dir / f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
                backup.mkdir(parents=True, exist_ok=True)
                shutil.move(str(d), str(backup / "profile"))
                (backup / "name.txt").write_text(name, encoding="utf-8")
            else:
                shutil.rmtree(d)
        self._conn.execute("DELETE FROM accounts_meta WHERE name=?", (name,))
        self._conn.commit()

    def list_trash(self) -> list:
        """列出软删除的账号备份。name 为原名，backup 为回收站目录名。"""
        if not self.trash_dir.exists():
            return []
        out = []
        for p in sorted(self.trash_dir.iterdir()):
            if not p.is_dir():
                continue
            name_file = p / "name.txt"
            orig = name_file.read_text(encoding="utf-8").strip() if name_file.exists() else p.name
            try:
                ts = int(p.name.split("_")[0])
            except Exception:
                ts = 0
            out.append({"name": orig, "backup": p.name, "deleted_at": ts})
        return out

    def restore_account(self, backup: str) -> str:
        """把软删除的备份还原为账号，返回还原后的账号名。"""
        src = self.trash_dir / backup
        if not src.exists() or not src.is_dir():
            raise FileNotFoundError(f"备份不存在: {backup}")
        name_file = src / "name.txt"
        orig = name_file.read_text(encoding="utf-8").strip() if name_file.exists() else src.name
        dest = self.accounts_dir / orig
        if dest.exists():
            raise FileExistsError(f"账号 {orig} 已存在，未覆盖")
        prof = src / "profile"
        if prof.exists():
            shutil.move(str(prof), str(dest))
        else:
            # 兼容：整个备份目录就是一个 profile
            shutil.move(str(src), str(dest))
        if src.exists():
            shutil.rmtree(src, ignore_errors=True)
        self._ensure_meta(orig)
        return orig

    async def verify_account(self, name: str):
        """通过原 ISP 只读验证登录态并写回缓存。返回三态：

          True  = 有效（写回 login_ok=1）
          False = 确认失效（写回 login_ok=0）
          None  = 无法判断（网络/代理/响应证据不足，不改写 login_ok）

        号忙抛 RuntimeError。
        """
        if name not in self.accounts:
            raise FileNotFoundError(f"profile 不存在: {name}")
        lock = self._locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("账号正在出片，稍后再验证")
        from browser import check_login_state
        if self.scheduler:
            raw = self._submission_proxy({'name': name, **self._proxy_state(name)})
            async with self.scheduler.diagnostic(self.platform, name, raw, browser=False):
                ok = await check_login_state(name)
        else:
            ok = await check_login_state(name)
        # 只有明确的有效/失效才落库；None（网络/代理故障）保持原状态不变，
        # 避免把「页面没打开」误写成「失效」而被 delete-invalid 删号。
        if ok is not None:
            self.set_login_status(name, ok, "验证确认登录失效，请重新登录或导入新 CK。")
        return ok

    # ===== 调度 =====

    def _set_credit_balance(self, account: str, balance: int, source: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET credit_balance=?, credit_checked_at=? WHERE name=?",
            (max(0, int(balance)), time.time(), account),
        )
        # A one-point observation is model-specific, not a global platform lock.
        # Existing/explicit quota locks keep their original expiry and reason.
        if balance < 1:
            self._conn.execute(
                "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=? WHERE name=?",
                (self._next_limit_reset(), source[:300] or "积分不足", account),
            )
        self._conn.commit()

    def _credit_available(self, account: str, required: int = 2) -> bool:
        row = self._meta(account)
        return not row or row["credit_balance"] is None or row["credit_balance"] >= required

    def _schedulable(self, a: dict, model=None) -> bool:
        context = _generation_quota.get()
        quota = self.quota_ledger.snapshot(a['name'],context['kind']) if context and context.get('kind') else None
        free_expert = bool(context and context.get('kind') == 'expert_fast')
        return (a["effective_scheduling"]
                and a.get('proxy_cooling_until', 0) <= time.time()
                and a.get('preparation_retry_until', 0) <= time.time()
                and not a["cooling"] and (quota is not None or not a["rate_limited"])
                and not a.get("proxy_blocked", False)
                and self._submission_proxy(a) not in self._unsafe_proxies
                and not a.get("verification_required")
                and (free_expert or not a["quota_blocked"]) and not a.get("proxy_blocked", False)
                and a.get("login_ok") != 0  # 死号（掉登录）不调度，避免反复白跑
                and (quota['remaining']>0 and not quota['blocked'] if quota is not None else a["used_today"] < a["limit"])
                and (free_expert or a["credit_balance"] is None or
                     a["credit_balance"] >= _minimum_credit_balance(model)))

    @property
    def all_accounts_limited(self) -> bool:
        """所有开启调度且未处于风控冷却的账号都已达到每日上限。"""
        candidates = [a for a in self.list_accounts() if a["effective_scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["rate_limited"] or a["used_today"] >= a["limit"] for a in candidates
        )

    @property
    def all_accounts_quota_blocked(self) -> bool:
        return self.all_accounts_quota_blocked_for_model()

    def all_accounts_quota_blocked_for_model(self, model=None) -> bool:
        candidates = [a for a in self.list_accounts() if a["effective_scheduling"] and not a["cooling"]]
        def lacks_balance(a):
            return a['credit_balance'] is not None and a['credit_balance'] < _minimum_credit_balance(model)
        return bool(candidates) and all(
            a["quota_blocked"] or lacks_balance(a) or a["rate_limited"] or a["used_today"] >= a["limit"]
            for a in candidates
        ) and any(a["quota_blocked"] or lacks_balance(a) for a in candidates)

    @property
    def all_accounts_proxy_blocked(self) -> bool:
        """所有开启调度且未在冷却的账号，其 ISP 额度都用完/被暂停。"""
        candidates = [a for a in self.list_accounts() if a["effective_scheduling"] and not a["cooling"]]
        return bool(candidates) and all(a.get("proxy_blocked", False) for a in candidates)

    @property
    def available(self) -> bool:
        return self.available_for_model()

    def available_for_model(self, model=None, duration=None, duration_profile=None) -> bool:
        if duration is None and duration_profile is None:
            return any(self._schedulable(a, model) for a in self.list_accounts())
        token = _generation_quota.set({'kind':quota_type(model,duration,duration_profile)})
        try:
            return any(self._schedulable(a, model) for a in self.list_accounts())
        finally:
            _generation_quota.reset(token)

    @property
    def cookie_count(self) -> int:  # 兼容 /health 旧字段
        return len(self.accounts)

    def account_status(self) -> list:
        return [{
            "account": a["name"], "used_today": a["used_today"], "limit": a["limit"],
            "rate_limited": a["rate_limited"], "rate_limited_until": a["rate_limited_until"],
            "quota_blocked": a["quota_blocked"], "quota_blocked_until": a["quota_blocked_until"],
            "proxy_server": a.get("proxy_server", ""), "proxy_used": a.get("proxy_used", 0),
            "proxy_limit": a.get("proxy_limit", 0), "proxy_remaining": a.get("proxy_remaining", 0),
            "proxy_paused": a.get("proxy_paused", False), "proxy_blocked": a.get("proxy_blocked", False),
            "proxy_warn": a.get("proxy_warn", False),
        } for a in self.list_accounts()]

    async def resume_video(self, account: str, conversation_id: str, timeout: int,
                           on_poll=None, deadline_at=None, on_event=None, model=None, submitted_prompt=None,
                           accepted=False, entry_mode=None, duration_profile=None,
                           generation_options=None, task_id=None) -> dict:
        profile = duration_profile or entry_mode
        previous = self._conn.execute('''SELECT quota_type,request_id FROM generation_quota
            WHERE account=? AND conversation_id=?''',(account,conversation_id)).fetchone()
        requested_duration = (generation_options or {}).get('requested_duration', 30)
        kind = previous[0] if previous else quota_type(model,requested_duration if profile=='plugin-expert30' else 15,profile)
        identity = previous[1] if previous else task_id or 'recovery:'+conversation_id
        token = _generation_quota.set({'kind':kind,'task_id':identity,'entry_mode':profile})
        try:
            return await self._resume_video(account,conversation_id,timeout,on_poll=on_poll,
                deadline_at=deadline_at,on_event=on_event,model=model,submitted_prompt=submitted_prompt,
                accepted=accepted,entry_mode=profile,generation_options=generation_options,task_id=identity)
        except PlatformDailyLimitError as error:
            self._mark_daily_limit(account, safe_text(error))
            raise
        finally:
            _generation_quota.reset(token)

    async def _resume_video(self, account: str, conversation_id: str, timeout: int,
                           on_poll=None, deadline_at=None, on_event=None, model=None, submitted_prompt=None,
                           accepted=False, entry_mode=None, duration_profile=None,
                           generation_options=None, task_id=None) -> dict:
        """恢复已受理会话；不参与选号，也不因账号当前额度状态跳过。"""
        entry_mode = duration_profile or entry_mode
        quota_context = _generation_quota.get() or {}
        if quota_context.get('kind') == 'ordinary_fast10':
            async with self._task_slot(recovery=True):
                self._recovering.discard(account)
                if self.scheduler: self.scheduler.release_recovery(self.platform,account)
                self._claim_conversation(account,conversation_id)
                proxy_key = self._submission_proxy({'name':account,**self._proxy_state(account)})
                proxy_sem = (self.scheduler.lane_slot(proxy_key) if self.scheduler else
                             self._proxy_slots.setdefault(proxy_key,asyncio.Semaphore(1)))
                def event(phase,details):
                    if phase == 'platform_rejected_no_charge':
                        self.release_no_charge_conversation(account,conversation_id,PlatformNoChargeError(details.get('reply') or ''))
                    if on_event: on_event(phase,details)
                try:
                    return await resume_video(account,conversation_id,timeout,on_poll=on_poll,
                        on_balance=lambda balance,source='':self._set_credit_balance(account,balance,source),
                        deadline_at=deadline_at,on_event=event,model=model,submitted_prompt=submitted_prompt,
                        accepted=accepted,scheduler=self.scheduler,
                        background_lane=SubmissionSlot(proxy_sem,self._available.set))
                except PlatformNoChargeError as error:
                    self.release_no_charge_conversation(account,conversation_id,error)
                    raise
                except LoginExpiredError:
                    self.mark_login_expired(account)
                    raise
                finally:
                    self._available.set()
        if entry_mode == 'plugin-expert30':
            from plugin_expert30 import resume_video as resume_worker
            requested_duration = (generation_options or {}).get('requested_duration', 30)
            token = _generation_quota.set({'kind':quota_type(model,requested_duration,entry_mode),
                                            'task_id':task_id or 'recovery:'+conversation_id})
            try:
                async with self._task_slot(recovery=True):
                    self._recovering.discard(account)
                    if self.scheduler: self.scheduler.release_recovery(self.platform,account)
                    self._claim_conversation(account,conversation_id)
                    try:
                        return await resume_worker(account,conversation_id,timeout,on_poll=on_poll,
                            on_balance=lambda balance,source='':self._set_credit_balance(account,balance,source),
                            deadline_at=deadline_at,on_event=on_event,model=model,
                            submitted_prompt=submitted_prompt,accepted=accepted,
                            scheduler=self.scheduler,generation_options=generation_options or {})
                    except PlatformNoChargeError as error:
                        self.release_no_charge_conversation(account,conversation_id,error)
                        raise
            finally:
                _generation_quota.reset(token)
                self._available.set()
        deadline = deadline_at if deadline_at is not None else time.time() + timeout
        async with self._task_slot(recovery=True):
            lock = self._locks.setdefault(account, asyncio.Lock())
            proxy_key = self._submission_proxy({'name': account, **self._proxy_state(account)})
            proxy_sem = (self.scheduler.lane_slot(proxy_key) if self.scheduler else
                         self._proxy_slots.setdefault(proxy_key, asyncio.Semaphore(1)))
            async with lock, SubmissionSlot(proxy_sem, self._available.set):
                self._recovering.discard(account)
                if self.scheduler:
                    self.scheduler.release_recovery(self.platform, account)
                self._claim_conversation(account, conversation_id)
                def on_balance(balance, source=""):
                    self._set_credit_balance(account, balance, source)
                pending_no_charge_error = None
                def on_resume_event(phase, details):
                    nonlocal pending_no_charge_error
                    if phase == 'platform_rejected_no_charge':
                        pending_no_charge_error = PlatformNoChargeError(details.get('reply') or '')
                        self.release_no_charge_conversation(account, conversation_id, pending_no_charge_error)
                    if on_event:
                        on_event(phase, details)
                try:
                    resume_worker = resume_video
                    if entry_mode == 'pro-fast15':
                        from dola_expert import resume_video as resume_worker
                    result = await resume_worker(account, conversation_id, timeout,
                                                on_poll=on_poll, on_balance=on_balance,
                                                deadline_at=deadline, on_event=on_resume_event,
                                                model=model, submitted_prompt=submitted_prompt, accepted=accepted)
                    self._conn.execute(
                        "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                        (time.time(), account))
                    self._conn.commit()
                    return result
                except PlatformNoChargeError as error:
                    self.release_no_charge_conversation(account, conversation_id, error)
                    raise
                except LoginExpiredError:
                    self.mark_login_expired(account)
                    raise
                except Exception:
                    if pending_no_charge_error:
                        raise pending_no_charge_error
                    raise
                finally:
                    self._available.set()

    async def generate_video(self, prompt: str, ratio: str = None, duration: int = None,
                             model: str = "seedance_v2.0", on_conversation_id=None,
                             on_poll=None, on_balance=None,
                             reference_image_paths: list[str] | None = None,
                             on_prompt_sent=None, on_event=None, excluded_accounts=(), task_id=None,
                             prepare_references=None, entry_mode=None, preferred_account=None,
                             duration_profile=None, generation_options=None) -> dict:
        """Bound in-flight work independently of browser submission slots."""
        entry_mode = duration_profile or entry_mode
        task_id = task_id or uuid.uuid4().hex
        token = _generation_quota.set({'kind':quota_type(model,duration,entry_mode),'task_id':task_id,
                                      'entry_mode':entry_mode})
        try:
            async with self._task_slot():
                return await self._generate_video(
                    prompt, ratio, duration, model, on_conversation_id, on_poll,
                    on_balance, reference_image_paths, on_prompt_sent, on_event, excluded_accounts, task_id,
                    prepare_references, entry_mode, preferred_account, generation_options)
        finally:
            _generation_quota.reset(token)
            self._available.set()

    async def _read_attempt_receipt(self, account, conversation_id, model, prompt):
        # Query the saved original ISP/session only; never send another prompt here.
        async with HttpVideoSession(account, conversation_id) as session:
            poll = await session.poll()
        return conversation_outcome(poll, model=model, submitted_prompt=prompt)

    async def _generate_video(self, prompt, ratio, duration, model,
                              on_conversation_id, on_poll, on_balance, reference_image_paths,
                              on_prompt_sent, on_event, excluded_accounts=(), task_id=None,
                              prepare_references=None, entry_mode=None, preferred_account=None,
                              generation_options=None):
        attempted = set(excluded_accounts or ())
        task_id = task_id or uuid.uuid4().hex
        last_err, last_account, last_conversation, last_reply = None, None, None, ''
        if preferred_account is not None and entry_mode not in ('pro-fast15','plugin-expert30'):
            raise ValueError('指定账号仅用于专家模式单次测试')
        if len(attempted) >= MAX_ACCOUNT_ATTEMPTS:
            raise AccountAttemptsExhaustedError(len(attempted))
        self._require_unsubmitted_intent(task_id)
        candidates = (self._submission_candidates(attempted, model, preferred_account)
                      if preferred_account is not None else self._submission_candidates(attempted, model))
        async for a in candidates:
            account = a['name']
            lock = self._locks.setdefault(account, asyncio.Lock())
            if not self._schedulable(a, model) or lock.locked():
                continue
            proxy_key = self._submission_proxy(a)
            proxy_sem = self._proxy_slots[proxy_key]
            # Each attempt gets fresh permits, including after an HTTP worker released early.
            async with SubmissionSlot(lock, self._available.set) as account_slot, SubmissionSlot(proxy_sem, self._available.set) as proxy_slot, \
                    SubmissionSlot(self.semaphore, self._available.set) as submission:
                eligible = self._rotation_eligible(self.list_accounts(), model, attempted)
                current = next((x for x in eligible if x['name'] == account), None)
                if not current or not self._schedulable(current, model):
                    continue
                if self._proxy_reserved_for_recovery(proxy_key):
                    continue
                if preferred_account is None and self._rotation_usage(current) > min(self._rotation_usage(x) for x in eligible):
                    continue
                quota_context = _generation_quota.get() or {}
                quota_kind = quota_context.get('kind')
                self._require_unsubmitted_intent(task_id)
                if quota_kind and not self.quota_ledger.reserve(account,quota_kind,task_id):
                    continue
                # Reserve the turn before awaiting browser work, not after video completion.
                self._record_dispatch(account)
                attempted.add(account)
                prompt_sent = generation_accepted = False
                send_recorded = False
                pending_no_charge_error = None
                attempt_conversation, attempt_reply = '', ''
                deadline = time.time() + (config.LONG_VIDEO_TIMEOUT if duration == 30 else config.VIDEO_TIMEOUT)

                def record_send_once():
                    nonlocal send_recorded
                    if not send_recorded:
                        self._record_send(account, proxy_key)
                        send_recorded = True

                def on_prompt_sent_pool(account_name):
                    nonlocal prompt_sent
                    prompt_sent = True
                    if quota_kind: self.quota_ledger.mark_sent(account,quota_kind,task_id)
                    record_send_once()
                    if on_prompt_sent:
                        on_prompt_sent(account_name)

                def on_balance_pool(balance, source=''):
                    self._set_credit_balance(account, balance, source)
                    if on_balance:
                        on_balance(balance, source)

                def on_conversation_id_pool(account_name, conversation_id, deadline_at):
                    nonlocal attempt_conversation, deadline
                    record_send_once()
                    attempt_conversation, deadline = conversation_id, deadline_at
                    if on_conversation_id:
                        on_conversation_id(account_name, conversation_id, deadline_at)
                    self._claim_conversation(account_name, conversation_id)

                def on_browser_closed():
                    # Only the browser permit is free. The ISP remains assigned
                    # through polling/download until this attempt exits.
                    submission.release()
                    if (entry_mode == 'plugin-expert30' or quota_kind == 'ordinary_fast10') and attempt_conversation:
                        # The plugin worker has persisted its original session and
                        # confirmed browser closure. It now locks its lane per HTTP IO.
                        proxy_slot.release()
                        account_slot.release()

                def on_worker_event(phase, details):
                    nonlocal generation_accepted, attempt_reply, pending_no_charge_error
                    if phase == 'platform_rejected_no_charge':
                        pending_no_charge_error = PlatformNoChargeError(details.get('reply') or '')
                        self.release_no_charge_conversation(account, attempt_conversation, pending_no_charge_error)
                    if quota_kind and phase in ('sending','submission_unknown'):
                        self.quota_ledger.mark_sent(account,quota_kind,task_id)
                    if quota_kind and phase == 'subscription_required':
                        self.quota_ledger.block(account,quota_kind,None,details.get('reply') or details.get('reason') or '平台要求会员')
                    if phase in ('accepted', 'downloading', 'completed'):
                        generation_accepted = True
                    if details.get('reply'):
                        attempt_reply = safe_text(details['reply'])
                    if on_event:
                        on_event(phase, details)

                def quarantine_browser():
                    # A close failure must not make this physical line or profile reusable.
                    self._unsafe_proxies.add(proxy_key)
                    self._recovering.add(account)
                    if self.scheduler:
                        self.scheduler.reserve_recovery(self.platform, account, proxy_key)
                        # Preserve both permits until a browser has confirmed closure.
                        # The lane quarantine remains shared across platform adapters.
                        submission.held = proxy_slot.held = False

                if on_event:
                    on_event('preparing', {'account': account,
                        'reason': f'第 {len(attempted)}/{MAX_ACCOUNT_ATTEMPTS} 个不同账号'})
                try:
                    attempt_paths = (await prepare_references(account)) if prepare_references else reference_image_paths
                    submit_video = generate_video
                    if entry_mode == 'pro-fast15':
                        from dola_expert import generate_video as submit_video
                    elif entry_mode == 'plugin-expert30':
                        from plugin_expert30 import generate_video as submit_video
                    elif config.PURE_HTTP_VIDEO and not attempt_paths and duration == 30 and '2.5' in model:
                        from video_worker_http import generate_video as submit_video
                    managed = (self.scheduler.managed_account(self.platform, account)
                               if self.scheduler else nullcontext())
                    with managed:
                        result = await submit_video(
                            account, prompt, ratio, duration, model=model,
                            on_conversation_id=on_conversation_id_pool, on_poll=on_poll,
                            on_balance=on_balance_pool, reference_image_paths=attempt_paths,
                            on_browser_closed=on_browser_closed,
                            on_prompt_sent=on_prompt_sent_pool, on_event=on_worker_event,
                            **({'scheduler':self.scheduler,'generation_options':generation_options or {}}
                               if entry_mode == 'plugin-expert30' else
                               {'scheduler':self.scheduler,'background_lane':SubmissionSlot(proxy_sem,self._available.set)}
                               if quota_kind == 'ordinary_fast10' else {}))
                    # A completed worker is also evidence of a send, including
                    # legacy worker integrations without an on_prompt_sent hook.
                    record_send_once()
                    self._conn.execute('UPDATE accounts_meta SET last_used_at=? WHERE name=?',
                                       (time.time(), account))
                    self._conn.commit()
                    return result
                except asyncio.CancelledError as e:
                    if getattr(e, 'browser_cleanup_failed', False):
                        quarantine_browser()
                    raise
                except BrowserCleanupError as error:
                    quarantine_browser()
                    if pending_no_charge_error:
                        pending_no_charge_error.browser_cleanup_failed = True
                        raise pending_no_charge_error from error
                    raise
                except PlatformNoChargeError as error:
                    self.release_no_charge_conversation(account, attempt_conversation, error)
                    if quota_kind and not attempt_conversation:
                        self.quota_ledger.release(account,quota_kind,task_id,proven_unconsumed=True)
                    raise
                except Exception as e:
                    if pending_no_charge_error:
                        raise pending_no_charge_error from e
                    if isinstance(e, LoginExpiredError):
                        self.mark_login_expired(account)
                    elif isinstance(e, BirthdayVerificationRequired):
                        self.mark_birthday_verification(account)
                    elif isinstance(e, PlatformDailyLimitError):
                        self._mark_daily_limit(account, safe_text(e))
                    if quota_kind and isinstance(e, (AccountLimitedError,CreditInsufficientError)):
                        self.quota_ledger.block(account,quota_kind,
                            reset_at_from_reply(getattr(e,'reply','') or str(e)),safe_text(e))
                    # Receipt/video evidence ends account rotation, including later platform failure.
                    if entry_mode in ('pro-fast15','plugin-expert30') or (quota_kind and (prompt_sent or attempt_conversation)) or generation_accepted or isinstance(e, (PlatformVideoError, WatermarkPreferenceError)):
                        if entry_mode == 'plugin-expert30' and prompt_sent and not attempt_conversation and not quota_kind:
                            self.reserve_unconfirmed_submission(account,task_id)
                        raise
                    sent = prompt_sent or bool(attempt_conversation)
                    if not sent:
                        network_paused = self._pause_network_failure(proxy_key, e)
                        if not network_paused and not isinstance(e, (
                                LoginExpiredError, BirthdayVerificationRequired, CreditInsufficientError,
                                AccountLimitedError, CreditError, RiskControlError)):
                            self._pause_preparation_failure(account, e)
                    explicit = isinstance(e, ExplicitNotAcceptedError)
                    reason = '发送前浏览器或账号失败，已关闭该浏览器'
                    if sent and not explicit:
                        reason = '提示词已发出但未确认受理'
                        if attempt_conversation:
                            try:
                                outcome = await asyncio.wait_for(self._read_attempt_receipt(
                                    account, attempt_conversation, model,
                                    build_video_prompt(prompt, duration, model)), timeout=35)
                            except PlatformNoChargeError as error:
                                self.release_no_charge_conversation(account, attempt_conversation, error)
                                raise
                            except Exception as check_error:
                                reason += f'，原会话最后核对失败（{type(check_error).__name__}）'
                            else:
                                attempt_reply = outcome['reply']
                                if on_poll:
                                    on_poll(time.time())
                                if outcome['state'] in ('accepted', 'completed'):
                                    on_worker_event('accepted', {'reply': attempt_reply,
                                        'reason': '换号前核对发现原会话已有回执或视频，继续原会话'})
                                    on_browser_closed()
                                    try:
                                        return await resume_video(account, attempt_conversation,
                                            max(1, int(deadline-time.time())), on_poll=on_poll,
                                            on_balance=on_balance_pool, deadline_at=deadline,
                                            on_event=on_worker_event, model=model,
                                            submitted_prompt=build_video_prompt(prompt, duration, model), accepted=True)
                                    except PlatformNoChargeError as error:
                                        self.release_no_charge_conversation(account, attempt_conversation, error)
                                        raise
                                    except Exception:
                                        if pending_no_charge_error:
                                            raise pending_no_charge_error
                                        raise
                                if outcome['state'] == 'failed':
                                    self.release_no_charge_conversation(account, attempt_conversation, outcome['error'])
                                    raise outcome['error']
                                if outcome['state'] == 'not_accepted':
                                    explicit = True
                        elif prompt_sent:
                            # No conversation ID to deduplicate against; reserve this uncertain send once.
                            self.reserve_unconfirmed_submission(account, task_id)
                    if explicit:
                        self.release_unaccepted_conversation(account, attempt_conversation)
                        attempt_reply = attempt_reply or getattr(e, 'reply', '')
                        reason = '平台明确尚未开始生成，已关闭该浏览器'
                    elif isinstance(e, CreditInsufficientError):
                        self._mark_quota_blocked(account, safe_text(e))
                    elif isinstance(e, AccountLimitedError):
                        self._mark_daily_limit(account, safe_text(e))
                    elif isinstance(e, CreditError) and not sent:
                        self._claim(account)
                    elif isinstance(e, RiskControlError):
                        self._conn.execute('UPDATE accounts_meta SET cooldown_until=? WHERE name=?',
                                           (time.time()+COOLDOWN_SEC, account))
                        self._conn.commit()
                    # The user explicitly permits rotating uncertain sends; keep their quota reservation.
                    last_err, last_account = e, account
                    last_conversation, last_reply = attempt_conversation, attempt_reply
                    suffix = ('已达 5 个不同账号上限' if len(attempted) >= MAX_ACCOUNT_ATTEMPTS
                              else '保留原提示词和参数，换下个不同账号')
                    if on_event:
                        on_event('account_retry', {'account': account, 'conversation_id': attempt_conversation,
                            'reply': attempt_reply, 'reason':
                            f'第 {len(attempted)}/{MAX_ACCOUNT_ATTEMPTS} 次：{reason}；{suffix}；{safe_text(e)[:240]}'})
                    print(f'[pool] {account} 第 {len(attempted)}/{MAX_ACCOUNT_ATTEMPTS} 次：{reason}；{suffix}', flush=True)
                    if len(attempted) >= MAX_ACCOUNT_ATTEMPTS:
                        raise AccountAttemptsExhaustedError(len(attempted), last_err, last_account,
                                                           last_conversation, last_reply) from None
                finally:
                    if quota_kind:
                        # Only a never-sent reservation releases without a proven
                        # platform no-charge response; uncertain sends remain occupied.
                        self.quota_ledger.release(account,quota_kind,task_id,proven_unconsumed=False)
        if attempted:
            raise AccountAttemptsExhaustedError(len(attempted), last_err, last_account,
                                               last_conversation, last_reply)
        if self.all_accounts_proxy_blocked:
            raise AllAccountsProxyBlockedError('429: 所有已开启调度的账号的 ISP 请求额度均已用完或被暂停')
        context = _generation_quota.get() or {}
        if context.get('kind'):
            rows = [a for a in self.list_accounts() if a['effective_scheduling']]
            if rows and all((q:=self.quota_ledger.snapshot(a['name'],context['kind']))['remaining']<=0 or q['blocked'] for a in rows):
                error = AllAccountsLimitedError('429: 当前生成类型的账号额度不足，请等待对应的日／周额度恢复')
                error.quota_type = context['kind']
                resets = [self.quota_ledger.snapshot(a['name'],context['kind'])['next_available_at'] for a in rows]
                error.next_available_at = min((v for v in resets if v),default=None)
                raise error
        if self.all_accounts_quota_blocked_for_model(model):
            raise AllAccountsQuotaBlockedError('429: 所有已开启调度的账号均已知积分不足')
        if self.all_accounts_limited:
            raise AllAccountsLimitedError('429: 所有已开启调度的账号均已达到 Dola 每日视频上限')
        raise RuntimeError('号池无可用账号（调度关闭/冷却/额度用完/浏览器关闭异常）')
