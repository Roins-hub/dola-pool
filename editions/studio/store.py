"""任务状态持久化（SQLite）+ API 密钥管理。单进程 asyncio 服务够用，用锁保护。"""
import datetime
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from video_capabilities import SUPPORTED_DURATIONS

_LOCK = threading.Lock()
DEFAULT_ALLOWED_DURATIONS = list(SUPPORTED_DURATIONS)


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
                ("api_key_hash", "TEXT"),
                ("api_key_name", "TEXT"),
                ("started_at", "REAL"),
                ("finished_at", "REAL"),
                ("client_concurrency_limit", "INTEGER DEFAULT 0"),
                ("phase", "TEXT DEFAULT 'queued'"),
                ("phase_at", "REAL"),
                ("accepted_at", "REAL"),
                ("actual_duration", "REAL"),
                ("request_evidence", "TEXT"),
                ("dola_reply", "TEXT"),
                ("duration_profile", "TEXT"),
                ("platform", "TEXT NOT NULL DEFAULT 'dola'"),
                ("submitted_duration", "INTEGER"),
                ("platform_reply", "TEXT"),
                ("capability_version", "TEXT"),
                ("generation_options", "TEXT NOT NULL DEFAULT '{}'"),
                ("batch_id", "TEXT"),
                ("batch_index", "INTEGER"),
                ("clips_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("total_actual_duration", "REAL"),
                ("deleted_at", "REAL"),
                ("deleted_by", "TEXT"),
            ):
                try:
                    self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            self._conn.execute("""CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                at REAL NOT NULL, phase TEXT NOT NULL, details TEXT NOT NULL)""")
            self._conn.execute("CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id, id)")
            self._conn.execute('CREATE TABLE IF NOT EXISTS task_idempotency (identity TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, task_id TEXT NOT NULL)')
            self._conn.execute('''CREATE TABLE IF NOT EXISTS task_batches (
                id TEXT PRIMARY KEY, requested_count INTEGER NOT NULL,
                api_key_hash TEXT, status TEXT NOT NULL DEFAULT 'queued',
                reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
            self._conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS tasks_batch_index ON tasks(batch_id,batch_index) WHERE batch_id IS NOT NULL')
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    key TEXT PRIMARY KEY,
                    name TEXT,
                    enabled INTEGER DEFAULT 1,
                    created_at REAL,
                    last_used_at REAL DEFAULT 0,
                    daily_limit INTEGER DEFAULT 0,
                    concurrency_limit INTEGER DEFAULT 0,
                    allowed_durations TEXT DEFAULT '[5, 10, 15, 30]',
                    expires_at REAL DEFAULT 0,
                    owner_user_id INTEGER DEFAULT NULL
                )
                """
            )
            # 旧库兼容：给已经存在的 API Key 增加客户级策略字段。
            for column, definition in (
                ("daily_limit", "INTEGER DEFAULT 0"),
                ("concurrency_limit", "INTEGER DEFAULT 0"),
                ("allowed_durations", "TEXT DEFAULT '[5, 10, 15, 30]'"),
                ("expires_at", "REAL DEFAULT 0"),
                ("owner_user_id", "INTEGER DEFAULT NULL"),
                ("allowed_platforms", "TEXT NOT NULL DEFAULT '[\"dola\"]'"),
            ):
                try:
                    self._conn.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            # Legacy full-catalog keys predate 5s support. Preserve restricted keys.
            for key_row in self._conn.execute('SELECT key,allowed_durations FROM api_keys').fetchall():
                try:
                    previous = json.loads(key_row['allowed_durations'] or '[]')
                    legacy_full = isinstance(previous, list) and set(map(int, previous)) == {10, 15, 30}
                except (TypeError, ValueError, json.JSONDecodeError):
                    legacy_full = False
                if legacy_full:
                    self._conn.execute('UPDATE api_keys SET allowed_durations=? WHERE key=?',
                                       (json.dumps(DEFAULT_ALLOWED_DURATIONS), key_row['key']))
            # 邮箱登录用户（admin / member）
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    status TEXT NOT NULL DEFAULT 'active',
                    note TEXT DEFAULT '',
                    created_at REAL,
                    updated_at REAL,
                    last_login_at REAL DEFAULT 0
                )
                """
            )
            # 登录会话（只存 token 摘要）
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at REAL,
                    expires_at REAL,
                    last_seen_at REAL,
                    user_agent TEXT DEFAULT ''
                )
                """
            )
            self._conn.commit()

    @staticmethod
    def validate_platform(value, *, allow_all=False):
        if value not in (('dola', 'doubao', 'all') if allow_all else ('dola', 'doubao')):
            raise ValueError('不支持的平台')
        return value

    @staticmethod
    def parse_platforms(value):
        if isinstance(value, str):
            value = json.loads(value)
        if value is None:
            return ['dola']
        if not isinstance(value, list) or any(p not in ('dola', 'doubao') for p in value):
            raise ValueError('平台权限仅支持 dola、doubao')
        return list(dict.fromkeys(value))

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
        data['allowed_platforms'] = cls.parse_platforms(data.get('allowed_platforms'))
        data["daily_limit"] = max(0, int(data.get("daily_limit") or 0))
        data["concurrency_limit"] = max(0, int(data.get("concurrency_limit") or 0))
        data["expires_at"] = float(data.get("expires_at") or 0)
        return data

    # ===== tasks =====

    def record_evidence(self, task_id, phase, details=None):
        from video_evidence import PHASES, public_details
        if phase not in PHASES:
            raise ValueError('invalid task phase')
        details = public_details(details)
        serialized = json.dumps(details, ensure_ascii=False, sort_keys=True)
        now = time.time()
        with _LOCK, self._conn:
            row = self._conn.execute('SELECT id,platform FROM tasks WHERE id=? AND deleted_at IS NULL', (task_id,)).fetchone()
            if row is None:
                return
            last = self._conn.execute('SELECT phase,details FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1',
                                      (task_id,)).fetchone()
            if last and last['phase'] == phase and last['details'] == serialized:
                return
            self._conn.execute('INSERT INTO task_events(task_id,at,phase,details) VALUES (?,?,?,?)',
                               (task_id, now, phase, serialized))
            if phase == 'account_retry':
                # Save the previous attempt (explicit rejection or authorized unconfirmed retry)
                # and the next queue boundary together; do not label every retry as a proven no-start.
                self._conn.execute(
                    "UPDATE tasks SET status='queued',phase=?,phase_at=?,updated_at=?,started_at=?,"
                    "account=NULL,conversation_id=NULL,deadline_at=NULL,accepted_at=NULL,actual_duration=NULL,"
                    "request_evidence=NULL,dola_reply=NULL,error=NULL,failure_code=NULL,video_url=NULL,"
                    "finished_at=NULL,last_poll_at=0 WHERE id=?", (phase, now, now, now, task_id))
            else:
                self._conn.execute('UPDATE tasks SET phase=?,phase_at=?,updated_at=? WHERE id=?',
                                   (phase, now, now, task_id))
                if phase == 'platform_rejected_no_charge':
                    self._conn.execute(
                        "UPDATE tasks SET status='failed',failure_code=?,finished_at=COALESCE(finished_at,?),error=COALESCE(error,?) WHERE id=?",
                        (phase, now, details.get('reason') or '平台拒绝输入，本次未计号池额度', task_id))
                if phase == 'accepted':
                    self._conn.execute('UPDATE tasks SET accepted_at=COALESCE(accepted_at,?) WHERE id=?', (now, task_id))
                if 'reply' in details:
                    self._conn.execute('UPDATE tasks SET platform_reply=? WHERE id=?', (details['reply'], task_id))
                    if row['platform'] == 'dola':
                        self._conn.execute('UPDATE tasks SET dola_reply=? WHERE id=?', (details['reply'], task_id))
                if 'matches' in details:
                    self._conn.execute('UPDATE tasks SET request_evidence=? WHERE id=?', (serialized, task_id))
                if 'actual_duration' in details:
                    self._conn.execute('UPDATE tasks SET actual_duration=? WHERE id=?', (details['actual_duration'], task_id))
            # Preserve the five-account budget across restarts even after long polling histories.
            attempt = "(phase='account_retry' OR (phase='preparing' AND COALESCE(json_extract(details, '$.account'), '')<>''))"
            self._conn.execute(f'DELETE FROM task_events WHERE task_id=? AND NOT {attempt} AND id NOT IN '
                               f'(SELECT id FROM task_events WHERE task_id=? AND NOT {attempt} ORDER BY id DESC LIMIT 100)',
                               (task_id, task_id))

    def task_events(self, task_id):
        with _LOCK:
            rows = self._conn.execute('SELECT at,phase,details FROM task_events WHERE task_id=? ORDER BY id', (task_id,)).fetchall()
        return [dict(at=row['at'], phase=row['phase'], details=json.loads(row['details'])) for row in rows]

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
        idempotency_scope=None,
        idempotency_key=None,
        request_fingerprint=None,
        duration_profile=None,
        platform='dola',
        submitted_duration=None,
        capability_version=None,
        generation_options=None,
    ):
        self.validate_platform(platform)
        now = time.time()
        with _LOCK, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            if idempotency_key:
                if not idempotency_scope or not request_fingerprint:
                    raise ValueError('提交标识缺少作用域或内容摘要')
                replay = self._find_idempotent(idempotency_scope, idempotency_key, request_fingerprint)
                if replay:
                    return replay
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
                "api_key_hash,api_key_name,started_at,finished_at,client_concurrency_limit,duration_profile,platform,submitted_duration,capability_version,generation_options"
                ") VALUES (?,?,?,?,?,'queued',?,?,?,NULL,NULL,0,NULL,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    model,
                    prompt,
                    ratio,
                    duration,
                    account,
                    now,
                    now,
                    reference_images or "[]",
                    api_key_hash,
                    api_key_name,
                    None,
                    None,
                    max(0, int(concurrency_limit or 0)),
                    duration_profile,
                    platform,
                    submitted_duration,
                    capability_version,
                    generation_options or '{}',
                ),
            )
            if idempotency_key:
                identity = hashlib.sha256(f'{idempotency_scope}\0{idempotency_key}'.encode()).hexdigest()
                self._conn.execute('INSERT INTO task_idempotency(identity,fingerprint,task_id) VALUES (?,?,?)',
                                   (identity, request_fingerprint, task_id))
            self._conn.commit()

    def _find_idempotent(self, scope, key, fingerprint):
        identity = hashlib.sha256(f'{scope}\0{key}'.encode()).hexdigest()
        row = self._conn.execute('SELECT fingerprint,task_id FROM task_idempotency WHERE identity=?', (identity,)).fetchone()
        if not row:
            return None
        if row['fingerprint'] != fingerprint:
            raise ValueError('相同提交标识对应了不同内容，请使用新的提交标识；未新建任务')
        task = self._conn.execute('SELECT * FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
        return self._task_row(task)

    @staticmethod
    def _task_row(row):
        if row is None:
            return None
        result = dict(row)
        try:
            clips = json.loads(result.get('clips_json') or '[]')
        except (ValueError, TypeError):
            clips = []
        result['clips'] = clips if isinstance(clips, list) else []
        return result

    def create_batch(self, batch_id, tasks, *, api_key_hash=None, daily_limit=0,
                     max_pending=0, idempotency_scope=None, idempotency_key=None,
                     request_fingerprint=None):
        """Create all child intents together, charging member task limits per child."""
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 20:
            raise ValueError('批量申请数量必须是1～20')
        if not isinstance(batch_id, str) or not batch_id:
            raise ValueError('批次编号不能为空')
        normalized = []
        allowed = {'model', 'prompt', 'ratio', 'duration', 'account', 'reference_images',
                   'api_key_hash', 'api_key_name', 'duration_profile', 'platform',
                   'submitted_duration', 'capability_version', 'generation_options'}
        for index, item in enumerate(tasks, 1):
            if not isinstance(item, dict):
                raise ValueError('批次子任务格式不正确')
            identifier = item.get('id') or item.get('task_id')
            if not isinstance(identifier, str) or not identifier:
                raise ValueError('批次子任务编号不能为空')
            values = {key: item[key] for key in allowed if key in item}
            for required in ('model', 'prompt', 'ratio', 'duration'):
                if required not in values:
                    raise ValueError('批次子任务缺少参数：' + required)
            self.validate_platform(values.setdefault('platform', 'dola'))
            if values.get('api_key_hash', api_key_hash) != api_key_hash:
                raise ValueError('同批次任务归属必须一致')
            values.update(id=identifier, api_key_hash=api_key_hash, batch_id=batch_id, batch_index=index,
                          client_concurrency_limit=max(0, int(item.get('concurrency_limit') or 0)))
            normalized.append(values)
        if len({item['id'] for item in normalized}) != len(normalized):
            raise ValueError('批次子任务编号不能重复')
        now = time.time()
        with _LOCK, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            if idempotency_key:
                if not idempotency_scope or not request_fingerprint:
                    raise ValueError('提交标识缺少作用域或内容摘要')
                replay = self._find_idempotent(idempotency_scope, idempotency_key, request_fingerprint)
                if replay:
                    if not replay.get('batch_id'):
                        raise ValueError('相同提交标识已用于单次任务，未创建批次')
                    return {**self._get_batch(replay['batch_id']), 'replayed': True}
            if max_pending > 0:
                pending = self._conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','processing')").fetchone()[0]
                if pending + len(tasks) > max_pending:
                    raise PendingTaskLimitExceeded(f'待处理任务超过服务上限（{max_pending}）')
            if daily_limit > 0 and api_key_hash:
                used = self._conn.execute("SELECT COUNT(*) FROM tasks WHERE api_key_hash=? AND date(created_at,'unixepoch','localtime')=?",
                    (api_key_hash, datetime.date.today().isoformat())).fetchone()[0]
                if used + len(tasks) > daily_limit:
                    raise TaskQuotaExceeded(f'API Key 今日剩余任务额度不足（剩余{max(0,daily_limit-used)}，申请{len(tasks)}）')
            self._conn.execute('INSERT INTO task_batches(id,requested_count,api_key_hash,created_at,updated_at) VALUES (?,?,?,?,?)',
                               (batch_id, len(tasks), api_key_hash, now, now))
            for values in normalized:
                values.update(status='queued', phase='queued', created_at=now, updated_at=now)
                values.setdefault('reference_images', '[]')
                values.setdefault('generation_options', '{}')
                columns = ','.join(values)
                self._conn.execute('INSERT INTO tasks (' + columns + ') VALUES (' + ','.join('?' for _ in values) + ')',
                                   list(values.values()))
            if idempotency_key:
                identity = hashlib.sha256(f'{idempotency_scope}\0{idempotency_key}'.encode()).hexdigest()
                self._conn.execute('INSERT INTO task_idempotency(identity,fingerprint,task_id) VALUES (?,?,?)',
                                   (identity, request_fingerprint, normalized[0]['id']))
            return self._get_batch(batch_id)

    def _get_batch(self, batch_id, *, include_deleted=True):
        row = self._conn.execute('SELECT * FROM task_batches WHERE id=?', (batch_id,)).fetchone()
        if row is None:
            return None
        tasks = [self._task_row(item) for item in self._conn.execute('SELECT * FROM tasks WHERE batch_id=? ORDER BY batch_index', (batch_id,))]
        counts = dict(requested=row['requested_count'], sent=0, conversations=0,
                      completed=0, failed=0, pending=0, videos=0, deleted=0)
        for task in tasks:
            if task.get('conversation_id') or task.get('phase') in ('sending', 'submitted', 'submission_unknown', 'awaiting_receipt', 'accepted', 'polling', 'downloading', 'media_ready', 'completed'):
                counts['sent'] += 1
            counts['conversations'] += bool(task.get('conversation_id'))
            counts['completed'] += task['status'] == 'completed'
            counts['failed'] += task['status'] == 'failed'
            counts['pending'] += task['status'] == 'queued'
            counts['videos'] += len([clip for clip in task['clips'] if clip.get('video_url')]) or bool(task.get('video_url'))
            counts['deleted'] += bool(task.get('deleted_at'))
        status = row['status']
        terminal = counts['completed'] + counts['failed']
        if terminal == row['requested_count'] or status not in ('paused', 'waiting_quota'):
            status = ('completed' if counts['completed'] == row['requested_count'] else 'failed' if counts['failed'] == row['requested_count'] else 'partial' if terminal == row['requested_count'] else
                      'processing' if any(task['status'] == 'processing' or task.get('conversation_id') for task in tasks) else 'queued')
        visible = tasks if include_deleted else [task for task in tasks if not task.get('deleted_at')]
        return {**dict(row), 'id': row['id'], 'batch_id': row['id'], 'status': status,
                'tasks': visible, 'counts': counts, 'progress': counts.copy()}

    def get_batch(self, batch_id, *, include_deleted=True):
        with _LOCK:
            return self._get_batch(batch_id, include_deleted=include_deleted)

    def set_batch_state(self, batch_id, status, reason=None, *, error=None):
        if status not in ('queued', 'processing', 'paused', 'waiting_quota'):
            raise ValueError('不支持的批次状态')
        if error is not None:
            reason = error
        with _LOCK, self._conn:
            self._conn.execute('UPDATE task_batches SET status=?,reason=?,updated_at=? WHERE id=?',
                               (status, reason, time.time(), batch_id))

    def merge_clips(self, task_id, clips):
        """Merge public clip records by stable identity; arrival ordering never renumbers."""
        if not isinstance(clips, list):
            raise ValueError('视频结果必须是列表')
        allowed = {'clip_id', 'index', 'vid', 'video_id', 'creation_id', 'status', 'video_url',
                   'actual_duration', 'error', 'failure_code', 'width', 'height', 'download_source',
                   'watermark', 'sha256', 'bytes', 'source_status'}
        with _LOCK, self._conn:
            row = self._conn.execute('SELECT * FROM tasks WHERE id=? AND deleted_at IS NULL', (task_id,)).fetchone()
            if row is None:
                raise ValueError('任务不存在或已删除')
            current = self._task_row(row)['clips']
            positions = {clip['clip_id']: index for index, clip in enumerate(current)}
            for incoming in clips:
                if not isinstance(incoming, dict):
                    raise ValueError('视频结果格式不正确')
                identity = str(incoming.get('clip_id') or incoming.get('vid') or incoming.get('creation_id') or '')
                if not identity or len(identity) > 256:
                    raise ValueError('视频结果缺少稳定身份')
                matching = [old for old in current if any(incoming.get(key) and old.get(key) == incoming[key]
                            for key in ('vid', 'creation_id', 'video_id'))]
                if matching:
                    if len({old['clip_id'] for old in matching}) != 1:
                        raise ValueError('视频身份对应多个条目，未合并结果')
                    identity = matching[0]['clip_id']
                value = {key: incoming[key] for key in allowed if key in incoming}
                value['clip_id'] = identity
                url = value.get('video_url')
                if url and (not isinstance(url, str) or (urlsplit(url).scheme not in ('', 'http', 'https'))):
                    raise ValueError('项目视频下载地址格式不正确')
                if identity in positions:
                    position = positions[identity]
                    old = current[position]
                    for key in ('vid', 'creation_id', 'video_id'):
                        if old.get(key) and value.get(key) and old[key] != value[key]:
                            raise ValueError('同一结果身份不能绑定其他视频')
                    current[position] = {**old, **value, 'index': old['index']}
                else:
                    value['index'] = len(current) + 1
                    positions[identity] = len(current)
                    current.append(value)
            total = sum(float(clip.get('actual_duration') or 0) for clip in current if clip.get('video_url'))
            self._conn.execute('UPDATE tasks SET clips_json=?,total_actual_duration=?,updated_at=? WHERE id=? AND deleted_at IS NULL',
                               (json.dumps(current, ensure_ascii=False), total or None, time.time(), task_id))
            return self._task_row(self._conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def soft_delete(self, task_id, actor, *, active=False):
        with _LOCK, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            row = self._conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return None
            if row['deleted_at'] is not None:
                return self._task_row(row)
            if active or row['status'] not in ('completed', 'failed'):
                raise ValueError('任务仍在运行或补收，暂时不能删除')
            now = time.time()
            self._conn.execute('UPDATE tasks SET deleted_at=?,deleted_by=?,updated_at=? WHERE id=?',
                               (now, str(actor), now, task_id))
            self._conn.execute('INSERT INTO task_events(task_id,at,phase,details) VALUES (?,?,?,?)',
                               (task_id, now, 'deleted', json.dumps({'actor': str(actor)}, ensure_ascii=False)))
            return self._task_row(self._conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def restore(self, task_id, actor):
        with _LOCK, self._conn:
            row = self._conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return None
            if row['deleted_at'] is not None:
                now = time.time()
                self._conn.execute('UPDATE tasks SET deleted_at=NULL,deleted_by=NULL,updated_at=? WHERE id=?', (now, task_id))
                self._conn.execute('INSERT INTO task_events(task_id,at,phase,details) VALUES (?,?,?,?)',
                                   (task_id, now, 'restored', json.dumps({'actor': str(actor)}, ensure_ascii=False)))
            return self._task_row(self._conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def find_idempotent(self, scope, key, fingerprint):
        with _LOCK:
            return self._find_idempotent(scope, key, fingerprint)

    def update(self, task_id, **fields):
        if not fields:
            return
        if 'platform' in fields:
            raise ValueError('任务平台创建后不可修改')
        if 'deleted_at' in fields or 'deleted_by' in fields:
            raise ValueError('删除和撤销须使用专用任务操作')
        fields["updated_at"] = time.time()
        if fields.get('status') == 'failed' and 'phase' not in fields:
            fields['phase'] = 'failed'
            fields['phase_at'] = fields['updated_at']
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [task_id]
        with _LOCK:
            self._conn.execute(f"UPDATE tasks SET {cols} WHERE id=? AND deleted_at IS NULL", vals)
            self._conn.commit()

    def get(self, task_id, *, include_deleted=False):
        with _LOCK:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?" + ('' if include_deleted else ' AND deleted_at IS NULL'), (task_id,)).fetchone()
        return self._task_row(row)

    def get_for_client(self, task_id, api_key_hash: str | None, *, include_deleted=False):
        """只返回属于当前 API Key 的任务；匿名开发模式按 NULL hash 隔离。"""
        with _LOCK:
            if api_key_hash:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id=? AND api_key_hash=?" + ('' if include_deleted else ' AND deleted_at IS NULL'),
                    (task_id, api_key_hash),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id=? AND api_key_hash IS NULL" + ('' if include_deleted else ' AND deleted_at IS NULL'),
                    (task_id,),
                ).fetchone()
        return self._task_row(row)

    def stalled_processing_tasks(self, cutoff: float) -> list:
        """返回进入 processing 后未派发账号、且已超过 cutoff 的停滞任务。

        用于排队看门狗：account/conversation_id 均为空且 started_at 早于 cutoff
        的任务，说明从未真正派发到任何号，判为排队超时。"""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE deleted_at IS NULL AND status='processing' "
                "AND account IS NULL AND conversation_id IS NULL "
                "AND started_at IS NOT NULL AND started_at < ?",
                (cutoff,),
            ).fetchall()
        return [self._task_row(r) for r in rows]

    def stalled_queued_tasks(self, cutoff: float) -> list:
        """Expire dispatched queue waits; untouched batch children belong to their coordinator."""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE deleted_at IS NULL AND status='queued' AND COALESCE(phase,'')!='waiting_quota' AND COALESCE(started_at,created_at) < ? "
                "AND (batch_id IS NULL OR started_at IS NOT NULL) "
                "AND NOT EXISTS (SELECT 1 FROM task_batches b WHERE b.id=tasks.batch_id AND b.status IN ('paused','waiting_quota'))",
                (cutoff,),
            ).fetchall()
        return [self._task_row(r) for r in rows]

    def fail_unconfirmed_submissions(self, reserve=None):
        """Recover interrupted sends under the authorized distinct-account retry policy.

        Keep this legacy entry point for startup callers. Reserve the uncertain
        send using an idempotent task/account key before atomically requeuing it.
        Existing failed tasks and accepted tasks never enter this recovery path.
        """
        with _LOCK, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE deleted_at IS NULL AND status='processing' AND COALESCE(account,'')<>'' "
                "AND COALESCE(conversation_id,'')='' AND accepted_at IS NULL AND platform='dola'").fetchall()
            for row in rows:
                if reserve:
                    reserve(row['account'], row['id'])
                now = time.time()
                if row['duration_profile'] == 'plugin-expert30' or row['batch_id']:
                    reason = '服务重启前曾尝试发送但未取得会话编号；保留原账号与额度，等待核对，未自动重发。'
                    self._conn.execute("UPDATE tasks SET status='failed',phase='submission_unknown',failure_code='submission_unknown',"
                        "error=?,finished_at=?,phase_at=?,updated_at=? WHERE id=?",
                        (reason, now, now, now, row['id']))
                    self._conn.execute('INSERT INTO task_events(task_id,at,phase,details) VALUES (?,?,?,?)',
                        (row['id'], now, 'submission_unknown', json.dumps({'reason': reason}, ensure_ascii=False)))
                    if row['batch_id']:
                        self._conn.execute("UPDATE task_batches SET status='paused',reason=?,updated_at=? WHERE id=?",
                                           (reason, now, row['batch_id']))
                    continue
                details = json.dumps({'account': row['account'], 'conversation_id': '',
                    'reply': row['dola_reply'] or '',
                    'reason': '服务重启前已尝试发送但未取得会话编号；保留额度占用，换不同账号继续，累计最多五个账号'},
                    ensure_ascii=False, sort_keys=True)
                self._conn.execute('INSERT INTO task_events(task_id,at,phase,details) VALUES (?,?,?,?)',
                                   (row['id'], now, 'account_retry', details))
                self._conn.execute(
                    "UPDATE tasks SET status='queued',phase='account_retry',phase_at=?,updated_at=?,started_at=?,"
                    "account=NULL,conversation_id=NULL,deadline_at=NULL,accepted_at=NULL,actual_duration=NULL,"
                    "request_evidence=NULL,dola_reply=NULL,error=NULL,failure_code=NULL,video_url=NULL,"
                    "finished_at=NULL,last_poll_at=0 WHERE id=?", (now, now, now, row['id']))
            return [dict(row) for row in rows]

    def expire_unknown_platform_tasks(self, now: float) -> list:
        """End overdue Doubao acknowledgement waits without clearing recovery data."""
        with _LOCK, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE deleted_at IS NULL AND platform='doubao' AND status='processing' "
                "AND phase='submission_unknown' AND deadline_at IS NOT NULL AND deadline_at<=? "
                "ORDER BY created_at", (now,)).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE tasks SET status='failed',phase='query_timeout',failure_code='query_timeout',"
                    "error=?,finished_at=?,phase_at=?,updated_at=? WHERE id=? AND status='processing' "
                    "AND phase='submission_unknown' AND deadline_at<=?",
                    ('等待真实视频回执超过时限，已停止等待；保留原会话补收入口，未重新提交。',
                     now, now, now, row['id'], now))
                self._conn.execute('INSERT INTO task_events(task_id,at,phase,details) VALUES (?,?,?,?)',
                    (row['id'], now, 'query_timeout', json.dumps({'reason':'回执等待超时，保留原会话与账号预留次数，未重新发送。'},ensure_ascii=False)))
            return [dict(row) for row in rows]

    def recoverable_tasks(self) -> list:
        """服务重启后恢复已拿到 conversation_id 的任务，不重复提交 prompt。"""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE deleted_at IS NULL AND status IN ('queued','processing') "
                "AND conversation_id IS NOT NULL AND account IS NOT NULL "
                "ORDER BY created_at"
            ).fetchall()
        return [self._task_row(r) for r in rows]

    def recoverable_queued_tasks(self) -> list:
        """服务重启后恢复尚未拿到 conversation_id 的 queued 任务。"""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE deleted_at IS NULL AND status='queued' "
                "AND conversation_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM task_batches b WHERE b.id=tasks.batch_id AND b.status='paused') ORDER BY created_at"
            ).fetchall()
        return [self._task_row(r) for r in rows]

    def audit_mismatch_tasks(self, limit: int = 50) -> list:
        """Only recheck bounded local-audit failures with their original conversation."""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE deleted_at IS NULL AND status='failed' AND failure_code='request_mismatch' AND platform='dola' "
                "AND account IS NOT NULL AND account != '' "
                "AND conversation_id IS NOT NULL AND conversation_id != '' "
                "ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 50),)
            ).fetchall()
        return [self._task_row(r) for r in rows]

    def pending_task_count(self) -> int:
        with _LOCK:
            return self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','processing')"
            ).fetchone()[0]

    def completed_videos(self, limit: int = 200, platform='dola') -> list:
        """One gallery entry per completed clip, retaining the original task identity."""
        self.validate_platform(platform, allow_all=True)
        limit = max(0, int(limit))
        with _LOCK:
            rows = self._conn.execute(
                "SELECT id, platform, prompt, ratio, duration, duration_profile, actual_duration, phase, model, video_url, clips_json, total_actual_duration, batch_id, batch_index, status, "
                "account, created_at, finished_at "
                "FROM tasks WHERE deleted_at IS NULL AND ("
                "(status='completed' AND video_url IS NOT NULL AND video_url!='') OR "
                "(status IN ('completed','failed') AND json_valid(clips_json) AND EXISTS "
                "(SELECT 1 FROM json_each(clips_json) WHERE type='object' AND json_extract(value,'$.status')='completed' "
                "AND COALESCE(json_extract(value,'$.video_url'),'')!=''))) "
                "AND (?='all' OR platform=?) ORDER BY created_at DESC LIMIT ?",
                (platform, platform, limit),
            ).fetchall()
        result = []
        for row in rows:
            task = self._task_row(row)
            clips = [clip for clip in task['clips'] if isinstance(clip, dict)
                     and clip.get('status') == 'completed' and clip.get('video_url')]
            if not clips:
                if task['status'] == 'completed' and task.get('video_url'):
                    result.append(task)
            else:
                base = {key: value for key, value in task.items() if key not in ('clips', 'clips_json')}
                for clip in clips:
                    sequence = clip.get('index')
                    item = {**base, 'task_id': task['id'], 'clip_id': clip['clip_id'],
                            'sequence': sequence, 'clip_index': sequence,
                            'actual_duration': clip.get('actual_duration'), 'video_url': clip['video_url']}
                    item.update({key: clip[key] for key in ('vid', 'creation_id', 'width', 'height', 'watermark', 'download_source')
                                 if key in clip})
                    result.append(item)
                    if len(result) >= limit:
                        break
            if len(result) >= limit:
                break
        return result

    def recent_tasks(self, limit: int = 50, api_key_hash: str | None = None, platform='dola') -> list:
        self.validate_platform(platform, allow_all=True)
        with _LOCK:
            if api_key_hash:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE deleted_at IS NULL AND api_key_hash=? AND (?='all' OR platform=?) "
                    "ORDER BY created_at DESC LIMIT ?", (api_key_hash, platform, platform, limit)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE deleted_at IS NULL AND (?='all' OR platform=?) ORDER BY created_at DESC LIMIT ?", (platform, platform, limit)
                ).fetchall()
        return [self._task_row(r) for r in rows]

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

    def stats(self, platform='dola') -> dict:
        """今日完成/失败、成功率、近 7 日趋势、各号累计出片。"""
        self.validate_platform(platform, allow_all=True)
        days = [(datetime.date.today() - datetime.timedelta(days=i)).isoformat()
                for i in range(6, -1, -1)]
        with _LOCK:
            per_day = []
            for d in days:
                row = self._conn.execute(
                    "SELECT sum(status='completed'), sum(status='failed') FROM tasks "
                    "WHERE date(created_at,'unixepoch','localtime')=? AND (?='all' OR platform=?)", (d, platform, platform),
                ).fetchone()
                per_day.append({"day": d[5:], "completed": row[0] or 0, "failed": row[1] or 0})
            t = per_day[-1]
            per_account = self._conn.execute(
                "SELECT platform, account, sum(status='completed') FROM tasks "
                "WHERE account IS NOT NULL AND (?='all' OR platform=?) GROUP BY platform,account", (platform, platform)
            ).fetchall()
        completed, failed = t["completed"], t["failed"]
        total = completed + failed
        return {
            "today_completed": completed,
            "today_failed": failed,
            "success_rate": round(completed / total, 3) if total else None,
            "per_day": per_day,
            "per_account_total": {(r[0]+':'+r[1] if platform == 'all' else r[1]): r[2] for r in per_account},
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
        allowed_platforms: list[str] | None = None,
    ) -> dict:
        key = "sk-" + secrets.token_hex(16)
        now = time.time()
        allowed = self._parse_allowed_durations(allowed_durations or DEFAULT_ALLOWED_DURATIONS)
        expires = float(expires_at or 0)
        platforms = self.parse_platforms(allowed_platforms)
        with _LOCK:
            self._conn.execute(
                "INSERT INTO api_keys (key,name,enabled,created_at,last_used_at,"
                "daily_limit,concurrency_limit,allowed_durations,expires_at,allowed_platforms) "
                "VALUES (?,?,1,?,0,?,?,?,?,?)",
                (
                    key,
                    name or "",
                    now,
                    max(0, int(daily_limit or 0)),
                    max(0, int(concurrency_limit or 0)),
                    json.dumps(allowed, ensure_ascii=False),
                    expires,
                    json.dumps(platforms),
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
        if 'allowed_platforms' in fields:
            fields['allowed_platforms'] = json.dumps(self.parse_platforms(fields['allowed_platforms']))
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

    # ===== users =====

    @staticmethod
    def _user_row(row, include_hash: bool = False) -> dict | None:
        if not row:
            return None
        data = dict(row)
        if not include_hash:
            data.pop("password_hash", None)
        data["role"] = data.get("role") or "member"
        data["status"] = data.get("status") or "active"
        data["last_login_at"] = float(data.get("last_login_at") or 0)
        data["created_at"] = float(data.get("created_at") or 0)
        return data

    def create_user(self, email: str, password_hash: str, role: str = "member",
                    note: str = "") -> dict:
        now = time.time()
        with _LOCK:
            cur = self._conn.execute(
                "INSERT INTO users (email,password_hash,role,status,note,created_at,"
                "updated_at,last_login_at) VALUES (?,?,?,'active',?,?,?,0)",
                (email, password_hash, role, note or "", now, now),
            )
            self._conn.commit()
            user_id = cur.lastrowid
        return self.get_user(user_id)

    def get_user(self, user_id: int, include_hash: bool = False) -> dict | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return self._user_row(row, include_hash)

    def get_user_by_email(self, email: str, include_hash: bool = False) -> dict | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM users WHERE email=?", (email,)
            ).fetchone()
        return self._user_row(row, include_hash)

    def list_users(self) -> list:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM users ORDER BY id"
            ).fetchall()
        users = [self._user_row(r) for r in rows]
        for user in users:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM api_keys WHERE owner_user_id=?",
                    (user["id"],),
                ).fetchone()
            user["key_count"] = row[0] or 0
        return users

    def count_users(self, role: str | None = None) -> int:
        with _LOCK:
            if role:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role=? AND status='active'", (role,)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return row[0] or 0

    def update_user(self, user_id: int, **fields):
        if not fields:
            return
        allowed = {"email", "password_hash", "role", "status", "note", "last_login_at"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with _LOCK:
            self._conn.execute(
                f"UPDATE users SET {cols} WHERE id=?",
                list(fields.values()) + [user_id],
            )
            self._conn.commit()

    def delete_user(self, user_id: int):
        """删除用户及其会话；其 API Key 保留但解除归属。"""
        with _LOCK:
            self._conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._conn.execute(
                "UPDATE api_keys SET owner_user_id=NULL WHERE owner_user_id=?", (user_id,)
            )
            self._conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            self._conn.commit()

    def set_password(self, user_id: int, password_hash: str):
        self.update_user(user_id, password_hash=password_hash)

    def set_last_login(self, user_id: int):
        now = time.time()
        self.update_user(user_id, last_login_at=now)

    # ===== sessions =====

    def create_session(self, token_hash: str, user_id: int, expires_at: float,
                       user_agent: str = ""):
        now = time.time()
        with _LOCK:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions (token_hash,user_id,created_at,"
                "expires_at,last_seen_at,user_agent) VALUES (?,?,?,?,?,?)",
                (token_hash, user_id, now, expires_at, now, (user_agent or "")[:200]),
            )
            self._conn.commit()

    def get_session_user(self, token_hash: str) -> dict | None:
        now = time.time()
        with _LOCK:
            row = self._conn.execute(
                "SELECT s.user_id, s.expires_at, u.* FROM sessions s "
                "JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
                (token_hash,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        if float(data.get("expires_at") or 0) <= now:
            self.delete_session(token_hash)
            return None
        if data.get("status") != "active":
            return None
        user = self._user_row(data)
        return user

    def touch_session(self, token_hash: str, min_interval: float = 300):
        now = time.time()
        with _LOCK:
            row = self._conn.execute(
                "SELECT last_seen_at FROM sessions WHERE token_hash=?", (token_hash,)
            ).fetchone()
            if row and now - float(row[0] or 0) >= min_interval:
                self._conn.execute(
                    "UPDATE sessions SET last_seen_at=? WHERE token_hash=?",
                    (now, token_hash),
                )
                self._conn.commit()

    def delete_session(self, token_hash: str):
        with _LOCK:
            self._conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            self._conn.commit()

    def delete_user_sessions(self, user_id: int):
        with _LOCK:
            self._conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._conn.commit()

    def purge_expired_sessions(self) -> int:
        with _LOCK:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE expires_at<=?", (time.time(),)
            )
            self._conn.commit()
        return cur.rowcount or 0

    # ===== api key ownership =====

    def keys_for_user(self, user_id: int) -> list:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM api_keys WHERE owner_user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [self._key_row(r) for r in rows]

    def assign_key_owner(self, key: str, user_id: int | None):
        with _LOCK:
            self._conn.execute(
                "UPDATE api_keys SET owner_user_id=? WHERE key=?", (user_id, key)
            )
            self._conn.commit()

    def key_hashes_for_user(self, user_id: int) -> list[str]:
        return [self.hash_api_key(k["key"]) for k in self.keys_for_user(user_id)]

    def recent_tasks_for_hashes(self, hashes: list, limit: int = 50, platform='dola') -> list:
        self.validate_platform(platform, allow_all=True)
        hashes = [h for h in (hashes or []) if h]
        if not hashes:
            return []
        placeholders = ",".join("?" for _ in hashes)
        with _LOCK:
            rows = self._conn.execute(
                f"SELECT * FROM tasks WHERE deleted_at IS NULL AND api_key_hash IN ({placeholders}) AND (?='all' OR platform=?) "
                "ORDER BY created_at DESC LIMIT ?",
                hashes + [platform, platform, limit],
            ).fetchall()
        return [self._task_row(r) for r in rows]

    def usage_for_hashes(self, hashes: list, day: str | None = None) -> dict:
        day = day or datetime.date.today().isoformat()
        hashes = [h for h in (hashes or []) if h]
        if not hashes:
            return {"day": day, "total": 0, "completed": 0, "failed": 0,
                    "active": 0, "queued": 0}
        placeholders = ",".join("?" for _ in hashes)
        with _LOCK:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, SUM(status='completed') AS completed, "
                "SUM(status='failed') AS failed, SUM(status='processing') AS active, "
                "SUM(status='queued') AS queued FROM tasks "
                f"WHERE api_key_hash IN ({placeholders}) "
                "AND date(created_at,'unixepoch','localtime')=?",
                hashes + [day],
            ).fetchone()
        return {
            "day": day,
            "total": row["total"] or 0,
            "completed": row["completed"] or 0,
            "failed": row["failed"] or 0,
            "active": row["active"] or 0,
            "queued": row["queued"] or 0,
        }
