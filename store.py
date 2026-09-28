"""任务状态持久化（SQLite）+ API 密钥管理。单进程 asyncio 服务够用，用锁保护。"""
import config
import datetime
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

_LOCK = threading.Lock()
# 时长白名单只认 config 一个口径（这里原先硬编码 (10, 15, 30)，
# 会让 2.5 的 5 秒档在新建/编辑 API Key 时被静默过滤掉）。
SUPPORTED_DURATIONS = tuple(config.ALL_DURATIONS)
DEFAULT_ALLOWED_DURATIONS = list(SUPPORTED_DURATIONS)
_ALLOWED_DURATIONS_SQL = json.dumps(DEFAULT_ALLOWED_DURATIONS)


class TaskQuotaExceeded(RuntimeError):
    """API Key 达到每日任务额度。"""


class PendingTaskLimitExceeded(RuntimeError):
    """服务端待处理任务总数达到上限。"""


class TaskStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init()

    def _init(self):
        with _LOCK:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    model TEXT,
                    prompt TEXT,
                    ratio TEXT,
                    duration INTEGER,
                    status TEXT,
                    video_url TEXT,
                    error TEXT,
                    created_at REAL,
                    updated_at REAL,
                    conversation_id TEXT,
                    deadline_at REAL,
                    last_poll_at REAL,
                    failure_code TEXT,
                    reference_images TEXT,
                    api_key_hash TEXT,
                    api_key_name TEXT,
                    started_at REAL,
                    finished_at REAL,
                    client_concurrency_limit INTEGER DEFAULT 0
                )
                """
            )
            # 旧库兼容：逐列补齐任务恢复、客户用量和耗时统计字段。
            for column, definition in (
                ("account", "TEXT"),
                ("conversation_id", "TEXT"),
                ("deadline_at", "REAL"),
                ("last_poll_at", "REAL"),
                ("failure_code", "TEXT"),
                ("reference_images", "TEXT"),
                ("reference_thumbs", "TEXT"),
                ("api_key_hash", "TEXT"),
                ("api_key_name", "TEXT"),
                ("started_at", "REAL"),
                ("finished_at", "REAL"),
                ("client_concurrency_limit", "INTEGER DEFAULT 0"),
                ("video_size", "INTEGER"),
                ("video_width", "INTEGER"),
                ("video_height", "INTEGER"),
                ("video_duration", "REAL"),
                ("video_bitrate", "INTEGER"),
                ("attempt", "INTEGER DEFAULT 0"),
                ("attempted_accounts", "TEXT"),
            ):
                try:
                    self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key TEXT PRIMARY KEY,
                    name TEXT,
                    enabled INTEGER DEFAULT 1,
                    created_at REAL,
                    last_used_at REAL DEFAULT 0,
                    daily_limit INTEGER DEFAULT 0,
                    concurrency_limit INTEGER DEFAULT 0,
                    allowed_durations TEXT DEFAULT '{_ALLOWED_DURATIONS_SQL}',
                    expires_at REAL DEFAULT 0
                )
                """
            )
            # 旧库兼容：给已经存在的 API Key 增加客户级策略字段。
            for column, definition in (
                ("daily_limit", "INTEGER DEFAULT 0"),
                ("concurrency_limit", "INTEGER DEFAULT 0"),
                ("allowed_durations", f"TEXT DEFAULT '{_ALLOWED_DURATIONS_SQL}'"),
                ("expires_at", "REAL DEFAULT 0"),
            ):
                try:
                    self._conn.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            self._conn.commit()

    @staticmethod
    def hash_api_key(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_allowed_durations(raw) -> list[int]:
        if isinstance(raw, list):
            values = raw
        else:
            try:
                values = json.loads(raw or "[]")
            except (TypeError, json.JSONDecodeError):
                values = []
        result = set()
        for value in values if isinstance(values, list) else []:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value in SUPPORTED_DURATIONS:
                result.add(value)
        return sorted(result) or list(DEFAULT_ALLOWED_DURATIONS)

    @classmethod
    def _key_row(cls, row):
        if not row:
            return None
        data = dict(row)
        data["enabled"] = bool(data.get("enabled"))
        data["allowed_durations"] = cls._parse_allowed_durations(data.get("allowed_durations"))
        data["daily_limit"] = max(0, int(data.get("daily_limit") or 0))
        data["concurrency_limit"] = max(0, int(data.get("concurrency_limit") or 0))
        data["expires_at"] = float(data.get("expires_at") or 0)
        return data

    # ===== tasks =====

    # 单列文本上限：库里任何一格都不允许无限大 —— 列表接口要一次性序列化整表，
    # 一格几 MB 就够把单进程 uvicorn 顶成 MemoryError（2026-09-22 面板卡死事故）。
    # 这是最后一道兜底：不管调用方塞什么进来，写库前一律截断。
    _FIELD_LIMITS = {
        "prompt": 20000,
        "error": 2000,
        "reference_images": 8000,
        "reference_thumbs": 4000,
        "attempted_accounts": 4000,
        "conversation_id": 200,
        "video_url": 4000,
        "model": 64,
        "ratio": 32,
        "api_key_name": 128,
        "failure_code": 32,
    }

    @classmethod
    def _clamp_fields(cls, fields: dict) -> dict:
        out = {}
        for key, value in fields.items():
            limit = cls._FIELD_LIMITS.get(key)
            if limit and isinstance(value, str) and len(value) > limit:
                out[key] = value[:limit] + "…(truncated)"
            else:
                out[key] = value
        return out

    def create(
        self,
        task_id,
        model,
        prompt,
        ratio,
        duration,
        account=None,
        reference_images=None,
        api_key_hash=None,
        api_key_name=None,
        daily_limit=0,
        concurrency_limit=0,
        max_pending=0,
    ):
        now = time.time()
        clamped = self._clamp_fields({
            "model": model,
            "prompt": prompt,
            "ratio": ratio,
            "reference_images": reference_images or "[]",
            "api_key_name": api_key_name,
        })
        with _LOCK:
            if max_pending > 0:
                pending = self._conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','processing')"
                ).fetchone()[0]
                if pending >= max_pending:
                    raise PendingTaskLimitExceeded(
                        f"待处理任务已达到服务上限（{max_pending}）"
                    )
            if daily_limit > 0 and api_key_hash:
                day = datetime.date.today().isoformat()
                used = self._conn.execute(
                    "SELECT COUNT(*) FROM tasks "
                    "WHERE api_key_hash=? AND date(created_at,'unixepoch','localtime')=?",
                    (api_key_hash, day),
                ).fetchone()[0]
                if used >= daily_limit:
                    raise TaskQuotaExceeded(
                        f"API Key 今日额度已用完（{daily_limit} 个任务）"
                    )
            self._conn.execute(
                "INSERT INTO tasks ("
                "id,model,prompt,ratio,duration,status,account,created_at,updated_at,"
                "conversation_id,deadline_at,last_poll_at,failure_code,reference_images,"
                "api_key_hash,api_key_name,started_at,finished_at,client_concurrency_limit"
                ") VALUES (?,?,?,?,?,'queued',?,?,?,NULL,NULL,0,NULL,?,?,?,?,?,?)",
                (
                    task_id,
                    clamped["model"],
                    clamped["prompt"],
                    clamped["ratio"],
                    duration,
                    account,
                    now,
                    now,
                    clamped["reference_images"],
                    api_key_hash,
                    clamped["api_key_name"],
                    None,
                    None,
                    max(0, int(concurrency_limit or 0)),
                ),
            )
            self._conn.commit()

    def update(self, task_id, **fields):
        if not fields:
            return
        fields = self._clamp_fields(fields)
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [task_id]
        with _LOCK:
            self._conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", vals)
            self._conn.commit()

    def get(self, task_id):
        with _LOCK:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def delete_task(self, task_id: str) -> bool:
        """彻底删除一条任务记录（删除视频时连记录一并清理）。"""
        with _LOCK:
            cur = self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def prune_finished(self, retention_days: float) -> list[dict]:
        """删掉保留期之外的已结束任务，返回被删记录（含 video_url，供调用方清理文件）。

        retention_days <= 0 表示不清理。分批发删除语句，避免 SQLite 参数上限。
        """
        if not retention_days or retention_days <= 0:
            return []
        cutoff = time.time() - float(retention_days) * 86400
        with _LOCK:
            rows = self._conn.execute(
                "SELECT id, video_url, account, reference_thumbs FROM tasks"
                " WHERE status IN ('completed','failed')"
                "   AND COALESCE(finished_at, updated_at) < ?"
                " ORDER BY COALESCE(finished_at, updated_at) ASC",
                (cutoff,),
            ).fetchall()
            for start in range(0, len(rows), 500):
                chunk = [r["id"] for r in rows[start:start + 500]]
                self._conn.execute(
                    "DELETE FROM tasks WHERE id IN (%s)" % ",".join("?" for _ in chunk),
                    chunk,
                )
            if rows:
                self._conn.commit()
        return [dict(r) for r in rows]

    def vacuum(self) -> None:
        """回收删行后的空间（已结束任务清完库会膨胀，定期 VACUUM 才还盘）。"""
        with _LOCK:
            self._conn.execute("VACUUM")

    def storage_stats(self) -> dict:
        """给 /health 用的存储概况，方便提前发现膨胀。"""
        with _LOCK:
            count = self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            running = self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','processing')"
            ).fetchone()[0]
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += os.path.getsize(self.db_path + suffix)
            except OSError:
                pass
        return {"tasks": count, "running": running, "db_bytes": size}

    def get_for_client(self, task_id, api_key_hash: str | None):
        """只返回属于当前 API Key 的任务；匿名开发模式按 NULL hash 隔离。"""
        with _LOCK:
            if api_key_hash:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id=? AND api_key_hash=?",
                    (task_id, api_key_hash),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id=? AND api_key_hash IS NULL",
                    (task_id,),
                ).fetchone()
        return dict(row) if row else None

    def recoverable_tasks(self) -> list:
        """服务重启后恢复已拿到 conversation_id 的任务，不重复提交 prompt。"""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status IN ('queued','processing') "
                "AND conversation_id IS NOT NULL AND account IS NOT NULL "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def recoverable_queued_tasks(self) -> list:
        """服务重启后需要**重新受理**（而不是续轮询）的任务。

        与 `recoverable_tasks` 互补：只要「没会话」或「没账号」就该重新派号。
        - 还没拿到账号就被中断的（原有语义）；
        - 已经提交过上游、但被服务重启取消的：`_run_task` 的 CancelledError 分支会写回
          `status='queued' + account=NULL`，会话仍留在库里。老判据（要求会话为空）漏掉
          这一半 —— 两个恢复入口都匹配不上，任务只能干等看门狗按「排队超时」判死，
          而它其实已经在上游生成过一版了。
        """
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status IN ('queued','processing') "
                "AND (conversation_id IS NULL OR account IS NULL OR account='') "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def pending_task_count(self) -> int:
        with _LOCK:
            return self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','processing')"
            ).fetchone()[0]

    def queue_progress(self, *, created_at: float | None, duration: int | None) -> dict:
        """排队情况 + 预计耗时（给客户端显示「排队中，预计 N 分钟」）。

        - ahead：排在我前面、还没轮到的任务数
        - running：正在跑的任务数
        - typical_seconds：同档位近期平均耗时（近 3 天已完成任务，样本不足时回落到配置表）
        """
        with _LOCK:
            if created_at:
                ahead = self._conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status='queued' AND created_at < ?",
                    (created_at,),
                ).fetchone()[0]
            else:
                ahead = self._conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]
            running = self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='processing'").fetchone()[0]
            typical = None
            if duration:
                row = self._conn.execute(
                    "SELECT AVG(finished_at - started_at) AS avg_seconds, COUNT(*) AS n"
                    " FROM tasks WHERE status='completed' AND duration=?"
                    "   AND finished_at > ? AND started_at > 0 AND finished_at > started_at",
                    (int(duration), time.time() - 3 * 86400),
                ).fetchone()
                if row and (row["n"] or 0) >= 3 and row["avg_seconds"]:
                    typical = float(row["avg_seconds"])
        if typical is None:
            typical = float(config.ETA_FALLBACK_SECONDS.get(
                int(duration or 0), config.ETA_DEFAULT_SECONDS))
        return {
            "ahead": int(ahead),
            "running": int(running),
            "typical_seconds": int(typical),
        }

    def stale_pending_tasks(self, *, stale_seconds: float, wait_cap_seconds: float) -> dict:
        """找出「看起来在跑、其实没进展」和「排队等太久」的任务（看门狗用）。

        - stuck：status='processing' 但超过 stale_seconds 没有任何轮询/进度更新
          （典型来源：服务重启把协程取消掉，行还留在 processing）
        - waiting：还停在 queued（= 没在跑，含拿到过 conversation 但被重排的）且**最后一次状态变化**
          至今超过 wait_cap_seconds。用 updated_at 而不是 created_at：跑了大半截、刚被重启打回
          queued 的任务不该立刻被判「排队超时」（旧口径按 created_at 计时，重启必吃这条）
        """
        now = time.time()
        with _LOCK:
            stuck = [dict(r) for r in self._conn.execute(
                "SELECT id, account, conversation_id, status, updated_at,"
                " COALESCE(NULLIF(last_poll_at,0), NULLIF(started_at,0), created_at) AS age_from"
                " FROM tasks WHERE status='processing' AND conversation_id IS NOT NULL"
                "   AND COALESCE(NULLIF(last_poll_at,0), NULLIF(started_at,0), created_at)"
                "       < ?",
                (now - stale_seconds,),
            ).fetchall()]
            waiting = [dict(r) for r in self._conn.execute(
                "SELECT id, account, conversation_id, status, created_at,"
                " COALESCE(NULLIF(updated_at,0), created_at) AS queued_since"
                " FROM tasks WHERE status='queued'"
                "   AND COALESCE(NULLIF(updated_at,0), created_at) < ?",
                (now - wait_cap_seconds,),
            ).fetchall()]
            orphans = [dict(r) for r in self._conn.execute(
                "SELECT id, account, conversation_id, status, started_at, created_at"
                " FROM tasks WHERE status='processing' AND conversation_id IS NULL"
                "   AND COALESCE(NULLIF(last_poll_at,0), NULLIF(started_at,0), created_at) < ?",
                (now - stale_seconds,),
            ).fetchall()]
        return {"stuck": stuck, "waiting": waiting, "orphans": orphans}

    def waiting_unassigned(self) -> list:
        """排队中尚未分配到账号的任务（api-pool job store 语义）。"""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status='queued' AND (account IS NULL OR account='') "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def busy_account_ids(self) -> set:
        """当前有 processing 任务的账号集合（幽灵占位对账用）。"""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT DISTINCT account FROM tasks WHERE status='processing' AND account IS NOT NULL"
            ).fetchall()
        return {r[0] for r in rows}

    def claim(self, account: str) -> dict | None:
        """粘性认领：把最早一个未派号的 queued 任务绑定到 account，置为 processing。

        一次只能被一个账号认领；认领后其它账号 claim 不到同一条。
        """
        with _LOCK:
            row = self._conn.execute(
                "SELECT id FROM tasks WHERE status='queued' AND (account IS NULL OR account='') "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            task_id = row["id"]
            self._conn.execute(
                "UPDATE tasks SET status='processing', account=?, started_at=?, updated_at=? "
                "WHERE id=?",
                (account, time.time(), time.time(), task_id),
            )
            self._conn.commit()
            claimed = dict(self._conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone())
        return claimed

    def recover_runtime_jobs(self) -> list:
        """崩溃重启恢复：把 processing 任务回退为 queued。

        已拿到 conversation_id 的保留 account + conversation_id（后续只轮询不重发）；
        未拿到的清空 account，等待重新派号。
        """
        recovered: list[str] = []
        with _LOCK:
            rows = self._conn.execute(
                "SELECT id, conversation_id FROM tasks WHERE status='processing'"
            ).fetchall()
            for row in rows:
                if row["conversation_id"]:
                    self._conn.execute(
                        "UPDATE tasks SET status='queued', updated_at=? WHERE id=?",
                        (time.time(), row["id"]),
                    )
                else:
                    self._conn.execute(
                        "UPDATE tasks SET status='queued', account=NULL, updated_at=? WHERE id=?",
                        (time.time(), row["id"]),
                    )
                recovered.append(row["id"])
            self._conn.commit()
        return recovered

    def recent_tasks(self, limit: int = 50, api_key_hash: str | None = None) -> list:
        with _LOCK:
            if api_key_hash:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE api_key_hash=? "
                    "ORDER BY created_at DESC LIMIT ?", (api_key_hash, limit)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def video_stats(self) -> dict:
        """视频管理统计：全部已完成记录数、仍保留视频文件的记录数。"""
        with _LOCK:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN video_url IS NOT NULL AND video_url != '' "
                "THEN 1 ELSE 0 END) AS available "
                "FROM tasks WHERE status='completed'"
            ).fetchone()
        return {"total": row["total"] or 0, "available": row["available"] or 0}

    def key_usage(self, api_key_hash: str, day: str | None = None) -> dict:
        day = day or datetime.date.today().isoformat()
        with _LOCK:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(status='completed') AS completed, "
                "SUM(status='failed') AS failed, "
                "SUM(status='processing') AS active, "
                "SUM(status='queued') AS queued "
                "FROM tasks WHERE api_key_hash=? "
                "AND date(created_at,'unixepoch','localtime')=?",
                (api_key_hash, day),
            ).fetchone()
        return {
            "day": day,
            "total": row["total"] or 0,
            "completed": row["completed"] or 0,
            "failed": row["failed"] or 0,
            "active": row["active"] or 0,
            "queued": row["queued"] or 0,
        }

    def stats(self) -> dict:
        """今日完成/失败、成功率、近 7 日趋势、各号累计出片。"""
        days = [(datetime.date.today() - datetime.timedelta(days=i)).isoformat()
                for i in range(6, -1, -1)]
        with _LOCK:
            per_day = []
            for d in days:
                row = self._conn.execute(
                    "SELECT sum(status='completed'), sum(status='failed') FROM tasks "
                    "WHERE date(created_at,'unixepoch','localtime')=?", (d,),
                ).fetchone()
                per_day.append({"day": d[5:], "completed": row[0] or 0, "failed": row[1] or 0})
            t = per_day[-1]
            per_account = self._conn.execute(
                "SELECT account, sum(status='completed') FROM tasks "
                "WHERE account IS NOT NULL GROUP BY account"
            ).fetchall()
        completed, failed = t["completed"], t["failed"]
        total = completed + failed
        return {
            "today_completed": completed,
            "today_failed": failed,
            "success_rate": round(completed / total, 3) if total else None,
            "per_day": per_day,
            "per_account_total": {r[0]: r[1] for r in per_account},
        }

    # ===== api keys =====

    def list_keys(self) -> list:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
        return [self._key_row(r) for r in rows]

    def get_key(self, key: str) -> dict | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key=?", (key,)
            ).fetchone()
        return self._key_row(row)

    def create_key(
        self,
        name: str,
        daily_limit: int = 0,
        concurrency_limit: int = 0,
        allowed_durations: list[int] | None = None,
        expires_at: float | None = None,
    ) -> dict:
        key = "sk-" + secrets.token_hex(16)
        now = time.time()
        allowed = self._parse_allowed_durations(allowed_durations or DEFAULT_ALLOWED_DURATIONS)
        expires = float(expires_at or 0)
        with _LOCK:
            self._conn.execute(
                "INSERT INTO api_keys (key,name,enabled,created_at,last_used_at,"
                "daily_limit,concurrency_limit,allowed_durations,expires_at) "
                "VALUES (?,?,1,?,0,?,?,?,?)",
                (
                    key,
                    name or "",
                    now,
                    max(0, int(daily_limit or 0)),
                    max(0, int(concurrency_limit or 0)),
                    json.dumps(allowed, ensure_ascii=False),
                    expires,
                ),
            )
            self._conn.commit()
        data = self.get_key(key)
        data["last_used_at"] = 0
        data["key"] = key
        return data

    def update_key(self, key: str, **fields):
        if not fields:
            return
        fields = dict(fields)
        if "allowed_durations" in fields:
            fields["allowed_durations"] = json.dumps(
                self._parse_allowed_durations(fields["allowed_durations"]),
                ensure_ascii=False,
            )
        cols = ", ".join(f"{k}=?" for k in fields)
        with _LOCK:
            self._conn.execute(
                f"UPDATE api_keys SET {cols} WHERE key=?",
                list(fields.values()) + [key],
            )
            self._conn.commit()

    def delete_key(self, key: str):
        with _LOCK:
            self._conn.execute("DELETE FROM api_keys WHERE key=?", (key,))
            self._conn.commit()

    def has_enabled_keys(self) -> bool:
        with _LOCK:
            row = self._conn.execute(
                "SELECT 1 FROM api_keys WHERE enabled=1 LIMIT 1"
            ).fetchone()
        return bool(row)

    def is_key_valid(self, key: str) -> bool:
        with _LOCK:
            row = self._conn.execute(
                "SELECT enabled, expires_at FROM api_keys WHERE key=?", (key,)
            ).fetchone()
        if not row or not row["enabled"]:
            return False
        return not row["expires_at"] or row["expires_at"] > time.time()

    def touch_key(self, key: str, min_interval: float = 60):
        """节流更新 last_used_at。"""
        now = time.time()
        with _LOCK:
            row = self._conn.execute(
                "SELECT last_used_at FROM api_keys WHERE key=?", (key,)
            ).fetchone()
            if row and now - (row[0] or 0) >= min_interval:
                self._conn.execute(
                    "UPDATE api_keys SET last_used_at=? WHERE key=?", (now, key)
                )
                self._conn.commit()
