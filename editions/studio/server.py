"""dola-pool OpenAI 兼容视频服务（FastAPI）+ 管理面板后端。

对外协议（异步两段式）：
  POST /v1/videos/generations   -> {"id","status","model","prompt"}
  GET  /v1/videos/{id}          -> {"id","status","video_url","error"}
  GET  /videos/<file>           -> 出片转存静态服务

管理面板：GET / -> web/index.html；管理接口 /api/admin/*。
"""
import asyncio
import hashlib
import json
import re
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path

import sqlite3

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

import auth
import config
import proxy_usage
from shared_scheduler import SharedScheduler
from add_account import add_account_flow
from ck_import import import_ck, import_ck_batch
from google_account_import import GoogleImportQueue, import_google_accounts
from browser_pool import (
    AllAccountsLimitedError,
    AllAccountsProxyBlockedError,
    AllAccountsQuotaBlockedError,
    BrowserPool,
)
from media import download_reference_images, validate_reference_urls
from reference_uploads import admin_owner, save_image, save_media, attach_images, cleanup_task_images
from store import PendingTaskLimitExceeded, TaskQuotaExceeded, TaskStore
from video_evidence import safe_text, matches_submitted_prompt, SubmissionUnconfirmedError
from task_results import complete_task, fail_task
from video_prompt import build_video_prompt
from video_capabilities import submission_duration, validate_video_parameters, SUPPORTED_DURATIONS_BY_MODEL
from video_outcome import conversation_outcome, VideoQueryTimeout, ExplicitNotAcceptedError
from clean_download import CleanDownloadFiles

Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path("web").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="dola-pool", version="0.9.0")

store = TaskStore(config.DB_PATH)
clean_download_files = CleanDownloadFiles(config.DB_PATH, config.DOWNLOAD_DIR)
shared_scheduler = SharedScheduler(config.MAX_CONCURRENCY, config.MAX_INFLIGHT,
                                  platform_limits={'doubao': config.DOUBAO_MAX_INFLIGHT})
pool = BrowserPool(accounts_dir=config.DOLA_ACCOUNTS_DIR, db_path=config.DOLA_POOL_DB_PATH,
                   max_concurrency=config.MAX_CONCURRENCY, scheduler=shared_scheduler)
pool.quota_ledger.classify_legacy(store.recent_tasks(100000, platform='dola'))
from browser import set_shared_scheduler
google_import_queue = GoogleImportQueue(pool)

app.mount("/videos", StaticFiles(directory=config.DOWNLOAD_DIR), name="videos")

# 后台 jobs（加号/验证），内存态
JOBS: dict[str, dict] = {}
# 最近一次「检测全部」确认失效的账号集合，供 delete-invalid 白名单使用
VERIFY_RUNS: dict[str, dict] = {}
DELETE_INVALID_RUNS: dict[str, dict] = {}  # 用户确认前生成的失效账号名单快照
VERIFY_RUNS_TTL = 3600  # 1 小时
VERIFY_JOB: dict | None = None  # 后台全量检测任务（进度/结果），供前端轮询

SIZE_TO_RATIO = {
    "1280x720": "16:9", "1920x1080": "16:9",
    "720x1280": "9:16", "1080x1920": "9:16",
    "1024x1024": "1:1", "1440x1080": "4:3", "1080x1440": "3:4",
}
# 2026-10-06 实测：dola 后端已拒绝 30s（聊天内回复「视频只支持4-15秒」），
# 30s 扩展注入路径失效，时长档收敛为 5/10/15。
SUPPORTED_DURATIONS = (5, 10, 15, 30)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class KeyConcurrencyLimiter:
    """按 API Key 限制同时运行的任务；0 表示不限。"""

    def __init__(self):
        self._condition = asyncio.Condition()
        self._active: defaultdict[str, int] = defaultdict(int)

    async def acquire(self, api_key_hash: str | None, limit: int):
        if not api_key_hash or limit <= 0:
            return
        async with self._condition:
            while self._active[api_key_hash] >= limit:
                await self._condition.wait()
            self._active[api_key_hash] += 1

    async def release(self, api_key_hash: str | None):
        if not api_key_hash:
            return
        async with self._condition:
            if self._active[api_key_hash] > 0:
                self._active[api_key_hash] -= 1
            if self._active[api_key_hash] == 0:
                self._active.pop(api_key_hash, None)
            self._condition.notify_all()



key_limiter = KeyConcurrencyLimiter()


# ===== 鉴权 =====


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _anonymous_client() -> dict:
    return {
        "api_key_hash": None,
        "api_key_name": "匿名调用",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _env_client(key: str) -> dict:
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": f"环境变量 Key ({key[:8]}…)",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _auth(authorization):
    """返回本次调用的客户策略；没有任何 Key 配置时保持开发模式免鉴权。"""
    if not config.API_KEYS and not store.has_enabled_keys():
        return _anonymous_client()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    key = authorization[7:].strip()
    if not key:
        raise HTTPException(401, "missing bearer token")
    if key in config.API_KEYS:
        return _env_client(key)
    record = store.get_key(key)
    if not record or not store.is_key_valid(key):
        raise HTTPException(401, "invalid api key")
    store.touch_key(key)
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": record["name"] or "未命名客户",
        "daily_limit": record["daily_limit"],
        "concurrency_limit": record["concurrency_limit"],
        "allowed_durations": record["allowed_durations"],
        "owner_user_id": record.get("owner_user_id"),
        "allowed_platforms": record.get('allowed_platforms', ['dola']),
    }


def _session_user(authorization: str | None) -> dict | None:
    """从 Authorization: Bearer <session token> 解析登录用户。"""
    token = auth.parse_bearer(authorization)
    if not token:
        return None
    token_hash = auth.hash_token(token)
    user = store.get_session_user(token_hash)
    if user:
        store.touch_session(token_hash)
    return user


def _require_user(authorization: str | None) -> dict:
    user = _session_user(authorization)
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def _require_admin(authorization: str | None) -> dict:
    user = _require_user(authorization)
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def _admin_auth(x_admin_key: str | None, authorization: str | None = None) -> dict:
    """管理端鉴权：X-Admin-Key 或管理员邮箱会话，任一通过即可。

    ADMIN_KEY 未配置且系统里还没有任何用户时保持开发模式免鉴权。
    """
    if x_admin_key and config.ADMIN_KEY and x_admin_key == config.ADMIN_KEY:
        return {"kind": "admin_key", "role": "admin"}
    user = _session_user(authorization)
    if user and user["role"] == "admin":
        return {"kind": "session", "user": user, "role": "admin"}
    if not config.ADMIN_KEY and store.count_users() == 0:
        return {"kind": "dev", "role": "admin"}
    raise HTTPException(401, "invalid admin credentials")


def _normalize_allowed_durations(values) -> list[int]:
    if values is None:
        return list(SUPPORTED_DURATIONS)
    try:
        normalized = sorted({int(value) for value in values})
    except (TypeError, ValueError):
        raise HTTPException(422, "allowed_durations 必须是 5、10、15、30 的数组")
    if not normalized or any(value not in SUPPORTED_DURATIONS for value in normalized):
        raise HTTPException(422, "allowed_durations 只能包含 5、10、15、30，且至少选择一个")
    return normalized


# ===== 客户 API =====


class VideoGenRequest(BaseModel):
    platform: str = Field('dola', pattern=r'^(dola|doubao)$')
    # 默认模型走 config.DEFAULT_MODEL（.env.local 的 DOLA_DEFAULT_MODEL 控制）。
    # 低峰用 2.5（高质量），高峰用 2.0 fast（约 10 分钟出片），请求未传 model 时用此默认。
    model: str = config.DEFAULT_MODEL
    prompt: str = Field(..., min_length=1)
    size: str | None = None
    ratio: str | None = None
    # 请求档位不代表最终成片时长；下载后读取 MP4 元数据验证。
    duration: int | None = Field(None, ge=1, le=600)
    duration_profile: str | None = None
    reference_images: list[str] = Field(default_factory=list)
    reference_uploads: list[str] = Field(default_factory=list)
    reference_media_uploads: list[str] = Field(default_factory=list, max_length=10)
    resolution: str = Field('auto', pattern=r'^(auto|480p|720p|1080p)$')
    batch_count: int = Field(1, strict=True, ge=1, le=20)
    require_no_watermark: bool = False
    auto_confirm: bool = False
    keep_browser_open: bool = Field(False, strict=True, description='仅管理端或服务端密钥的单次专家测试保留窗口')
    two_part: bool = Field(False, strict=True, description='插件专家30秒申请明确要求两条独立15秒视频')
    lead_prompt: str = Field('', max_length=4000)
    account_category: str | None = Field(None, pattern=r'^(普通账号|标准账号)$')
    idempotency_key: str | None = Field(None, min_length=8, max_length=128, repr=False)
    # Admin-only diagnostic control. The public video API rejects this field.
    expert_account: str | None = Field(None, min_length=1, max_length=128,
                                       pattern=r'^[A-Za-z0-9._-]+$')

    @model_validator(mode='before')
    @classmethod
    def plugin_expert_defaults(cls, values):
        if isinstance(values, dict) and values.get('duration_profile') == 'plugin-expert30':
            values = dict(values)
            if values.get('platform', 'dola') != 'dola':
                raise ValueError('插件专家仅适用于Dola')
            for key, default in (('model', 'seedance-2.5'), ('duration', 30), ('ratio', '16:9'), ('resolution', '720p')):
                if values.get(key) is None or (key == 'resolution' and values.get(key) == 'auto'):
                    values[key] = default
        elif isinstance(values, dict) and values.get('resolution') == '480p':
            raise ValueError('480p仅适用于插件专家')
        return values

    @model_validator(mode='after')
    def validate_two_part(self):
        if self.two_part and (self.duration_profile != 'plugin-expert30' or self.duration != 30):
            raise ValueError('双段规则仅适用于插件专家30秒申请')
        return self

    @model_validator(mode='before')
    @classmethod
    def default_doubao_confirmation(cls, values):
        if isinstance(values,dict) and values.get('platform')=='doubao' and 'auto_confirm' not in values:
            return {**values,'auto_confirm':True}
        return values

    @model_validator(mode='before')
    @classmethod
    def validate_expert_references(cls, values):
        if isinstance(values, dict) and values.get('platform', 'dola') == 'dola' and values.get('duration_profile') == 'pro-fast15':
            if any(values.get(key) for key in ('reference_images', 'reference_uploads', 'reference_media_uploads')):
                raise ValueError('专家模式15秒试验当前仅支持文字提示词，请清空参考素材或选择普通入口。')
        return values

    @model_validator(mode='before')
    @classmethod
    def reject_retired_cast(cls, values):
        # Old clients may still send empty defaults; never silently drop a
        # nonempty role mapping and generate a video with different content.
        if isinstance(values, dict) and (values.get('cast') or values.get('cast_ids')):
            raise HTTPException(422, {'error_code': 'unsupported_cast',
                                      'message': '角色素材库已移除，请使用独立参考素材上传。'})
        return values


class TaskResponse(BaseModel):
    id: str
    status: str
    model: str | None = None
    prompt: str | None = None
    duration: int | None = None
    duration_profile: str | None = None
    video_url: str | None = None
    error: str | None = None
    phase: str | None = None
    failure_code: str | None = None
    conversation_id: str | None = None
    actual_duration: float | None = None
    accepted_at: float | None = None
    replayed: bool = False
    platform: str = 'dola'
    submitted_duration: int | None = None
    clips: list[dict] = Field(default_factory=list)
    total_actual_duration: float | None = None
    batch_id: str | None = None
    batch_index: int | None = None
    deleted_at: float | None = None


def _submission_identity(req, scope):
    if not getattr(req, 'idempotency_key', None):
        return {}
    body = {'model': req.model, 'prompt': req.prompt, 'ratio': _resolve_ratio(req.size, req.ratio),
            'duration': req.duration or 10, 'reference_images': req.reference_images,
            'reference_uploads': req.reference_uploads}
    if req.reference_media_uploads:
        body['reference_media_uploads'] = req.reference_media_uploads
    if getattr(req, 'duration_profile', None) is not None:
        body['duration_profile'] = req.duration_profile
    if getattr(req, 'duration_profile', None) == 'plugin-expert30':
        body['generation_options'] = {'resolution': req.resolution, 'require_no_watermark': True,
                                      'protocol': 'expert', 'two_part': getattr(req, 'two_part', False)}
    if getattr(req, 'batch_count', 1) != 1:
        body['batch_count'] = req.batch_count
    if getattr(req, 'keep_browser_open', False):
        body['keep_browser_open'] = True
    if getattr(req, 'expert_account', None) is not None:
        body['expert_account'] = req.expert_account
    # Preserve legacy Dola hashes, including omitted versus explicit Dola.
    if getattr(req, 'platform', 'dola') != 'dola':
        body['platform'] = req.platform
        from platform_runtime import generation_options
        options = generation_options(req.model_dump() if hasattr(req, 'model_dump') else vars(req))
        if options:
            body['generation_options'] = options
    return dict(idempotency_scope=scope, idempotency_key=req.idempotency_key,
                request_fingerprint=hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest())


def _find_submission(identity):
    if not identity:
        return None
    try:
        row = store.find_idempotent(identity['idempotency_scope'], identity['idempotency_key'], identity['request_fingerprint'])
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if row and row.get('deleted_at'):
        raise HTTPException(409, '原任务已删除，未重新申请；请撤销删除后查看')
    if row and row.get('batch_id'):
        from fastapi.responses import JSONResponse
        from dola_submission import public_batch
        return JSONResponse({**public_batch(globals(), store.get_batch(row['batch_id']),
            admin=identity['idempotency_scope'].startswith('admin:')), 'replayed': True})
    return TaskResponse(**row, replayed=True) if row else None


def _resolve_ratio(size, ratio):
    if size and size in SIZE_TO_RATIO:
        return SIZE_TO_RATIO[size]
    return ratio


async def _run_task(task_id, model, prompt, ratio, duration, reference_images, client):
    row = store.get(task_id)
    if row and row.get('platform', 'dola') != 'dola':
        from platform_runtime import run_platform_task
        return await run_platform_task(globals(), row, client)
    api_key_hash = client.get("api_key_hash")
    acquired = False
    interrupted = False
    reference_root = None
    globals().setdefault('_active_task_ids', set()).add(task_id)
    try:
        await key_limiter.acquire(api_key_hash, client.get("concurrency_limit", 0))
        acquired = True
        # 2026-10-07 修正：排队期间保持 queued，不再提前标 processing。
        # 旧逻辑先标 processing，任务在 pool.generate_video 的 semaphore 上排队时
        # 外观与"生成中"无异（account=NULL），既误导前端又会被看门狗误杀（实测企鹅
        # 任务排队超 600s 被误标失败后"复活"，状态错乱）。真正派上号后由
        # on_prompt_sent 回调先写入 processing + account，受理后再补 conversation_id。
        store.update(task_id, started_at=time.time())
        row = store.get(task_id) or {}
        if row.get('duration_profile') is not None:
            model = model.strip()
        upstream_duration = submission_duration(model, duration, row.get('duration_profile'))

        def on_prompt_sent(account):
            # 先持久化发送边界；即使 Enter 后断线/重启，也不能重新排队补发。
            store.update(task_id, status="processing", account=account, submitted_duration=upstream_duration,
                         last_poll_at=time.time())
            store.record_evidence(task_id, 'sending', {})

        def on_conversation_id(account, conversation_id, deadline_at):
            store.update(task_id, status="processing", account=account,
                         conversation_id=conversation_id, deadline_at=deadline_at,
                         last_poll_at=time.time())
            store.record_evidence(task_id, 'awaiting_receipt', {})
            signal = globals().get('_batch_receipts', {}).get(task_id)
            if signal:
                signal.set()

        def on_poll(now):
            store.update(task_id, last_poll_at=now)

        plugin_options = _plugin_task_options(row) if row.get('duration_profile') == 'plugin-expert30' else None

        async def prepare_references(account):
            nonlocal reference_root
            if reference_root:
                shutil.rmtree(reference_root, ignore_errors=True)
                reference_root = None
            if row.get('batch_id'):
                from batch_references import prepare_batch_references
                reference_root, paths = await prepare_batch_references(row['batch_id'], task_id,
                    reference_images or [], account, upload_root=config.REFERENCE_UPLOAD_DIR,
                    download_references=download_reference_images)
            else:
                reference_root, paths = await download_reference_images(
                    reference_images or [], task_id, account=account)
            if plugin_options is not None:
                plugin_options['reference_media_paths'] = [str(Path(path).resolve()) for path in paths
                    if Path(path).suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp')]
            return paths

        reference_options = {}
        reference_paths = []
        if getattr(config, 'ISP_ONLY', False) or row.get('batch_id'):
            reference_options['prepare_references'] = prepare_references
        else:
            reference_root, reference_paths = await download_reference_images(
                reference_images or [], task_id)
            if plugin_options is not None:
                plugin_options['reference_media_paths'] = [str(Path(path).resolve()) for path in reference_paths
                    if Path(path).suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp')]
        if row.get('duration_profile') in ('pro-fast15', 'plugin-expert30'):
            reference_options['entry_mode'] = row['duration_profile']
        if row.get('duration_profile') == 'plugin-expert30':
            reference_options['duration_profile'] = row['duration_profile']
            reference_options['generation_options'] = plugin_options
        events = store.task_events(task_id)
        if row.get('duration_profile') == 'pro-fast15':
            preferred_account = next((event['details'].get('account') for event in events
                if event['phase'] == 'queued'
                and event['details'].get('reason') == '管理员指定专家测试账号'
                and event['details'].get('account')), None)
            if preferred_account:
                reference_options['preferred_account'] = preferred_account
        excluded_accounts = {event['details']['account'] for event in events
                             if event['phase'] in ('preparing', 'account_retry') and event['details'].get('account')}
        while True:
            try:
                result = await pool.generate_video(
                    prompt, ratio, upstream_duration, model,
                    on_conversation_id=on_conversation_id, on_poll=on_poll, on_prompt_sent=on_prompt_sent,
                    reference_image_paths=reference_paths,
                    excluded_accounts=excluded_accounts,
                    task_id=task_id,
                    on_event=lambda phase, details: store.record_evidence(task_id, phase, details),
                    **reference_options)
                break
            except AllAccountsLimitedError as error:
                if not getattr(error, 'quota_type', None):
                    raise
                store.update(task_id, status='queued', started_at=None, error=None)
                store.record_evidence(task_id, 'waiting_quota', {'reason': '等待对应账号额度，尚未发送申请'})
                if row.get('batch_id'):
                    store.set_batch_state(row['batch_id'], 'waiting_quota', '等待对应账号额度，未超额申请')
                await key_limiter.release(api_key_hash)
                acquired = False
                retry_at = getattr(error, 'next_available_at', None)
                await asyncio.sleep(min(60, max(1, retry_at-time.time())) if retry_at else 60)
                await key_limiter.acquire(api_key_hash, client.get('concurrency_limit', 0))
                acquired = True
                row = store.get(task_id) or row
        complete_task(store, task_id, result, config.PUBLIC_BASE)
    except asyncio.CancelledError:
        # Queued/recoverable tasks still need their private reference files on restart.
        interrupted = True
        raise
    except (AllAccountsLimitedError, AllAccountsQuotaBlockedError,
            AllAccountsProxyBlockedError) as e:
        fail_task(store, task_id, e, '429')
    except ExplicitNotAcceptedError as e:
        # Retried accounts remain in history; expose the last original conversation if the pool is exhausted.
        store.update(task_id, account=getattr(e, 'account', None),
                     conversation_id=getattr(e, 'conversation_id', None), dola_reply=e.reply)
        fail_task(store, task_id, e)
    except Exception as e:
        if getattr(e, 'attempts_exhausted', False):
            # A restart can consume the budget entirely from saved attempts before opening a new browser.
            details = {'account': getattr(e, 'account', None),
                       'conversation_id': getattr(e, 'conversation_id', None), 'reply': getattr(e, 'reply', None)}
            if not details['account']:
                details = next((event['details'] for event in reversed(store.task_events(task_id))
                                if event['phase'] in ('preparing', 'account_retry') and event['details'].get('account')), {})
            store.update(task_id, account=details.get('account'),
                         conversation_id=details.get('conversation_id'), dola_reply=details.get('reply'))
        fail_task(store, task_id, e)
    finally:
        globals().setdefault('_active_task_ids', set()).discard(task_id)
        if reference_root:
            shutil.rmtree(reference_root, ignore_errors=True)
        if not interrupted and any(value.startswith('upload:') for value in reference_images or []):
            cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        if acquired:
            await key_limiter.release(api_key_hash)


def _plugin_task_options(row):
    from multi_clip_results import cached_clips
    try:
        options = json.loads(row.get('generation_options') or '{}')
    except (ValueError, TypeError):
        raise ValueError('插件专家任务参数损坏，未重新提交') from None
    if not isinstance(options, dict):
        raise ValueError('插件专家任务参数损坏，未重新提交')
    def on_clip(clip):
        complete_task(store, row['id'], {'clips': [clip], 'generation_finished': False,
            'observation_finished': False, 'account': row.get('account'),
            'conversation_id': row.get('conversation_id')}, config.PUBLIC_BASE)
    return {**options, 'requested_duration': row.get('duration') or 30,
            'existing_clips': cached_clips(store, row), 'on_clip': on_clip}


async def _resume_task(row: dict):
    # Background polling and manual collection share one task lock.
    lock = _result_checks.setdefault(row['id'], asyncio.Lock())
    async with lock:
        current = store.get(row['id'])
        if current and current['status'] in ('queued', 'processing'):
            await _resume_original_task(current)


async def _resume_original_task(row: dict):
    if row.get('platform', 'dola') != 'dola':
        from platform_runtime import run_platform_task
        return await run_platform_task(globals(), row, _task_client(row), resume=True)
    task_id = row["id"]
    model = row['model'].strip() if row.get('duration_profile') is not None else row['model']
    upstream_duration = submission_duration(model, row['duration'], row.get('duration_profile'))
    deadline = row.get("deadline_at") or (
        time.time() + (config.LONG_VIDEO_TIMEOUT if upstream_duration == 30 else config.VIDEO_TIMEOUT)
    )
    remaining = max(1, int(deadline - time.time()))
    api_key_hash = row.get("api_key_hash")
    acquired = False
    interrupted = False
    retry = False
    try:
        await key_limiter.acquire(
            api_key_hash, int(row.get("client_concurrency_limit") or 0)
        )
        acquired = True
        store.update(task_id, status="processing", last_poll_at=time.time(),
                     started_at=row.get("started_at") or time.time())

        def on_poll(now):
            store.update(task_id, last_poll_at=now)

        result = await pool.resume_video(
            row["account"], row["conversation_id"], remaining, on_poll=on_poll, deadline_at=deadline,
            on_event=lambda phase, details: store.record_evidence(task_id, phase, details),
            model=model, submitted_prompt=build_video_prompt(row['prompt'], upstream_duration, model),
            accepted=bool(row.get('accepted_at')), **(
                {'entry_mode': 'plugin-expert30', 'duration_profile': 'plugin-expert30',
                 'generation_options': _plugin_task_options(row), 'task_id': task_id}
                if row.get('duration_profile') == 'plugin-expert30' else
                {'entry_mode': 'pro-fast15'} if row.get('duration_profile') == 'pro-fast15' else {}))
        complete_task(store, task_id, result, config.PUBLIC_BASE)
    except asyncio.CancelledError:
        interrupted = True
        raise
    except (AllAccountsLimitedError, AllAccountsQuotaBlockedError,
            AllAccountsProxyBlockedError) as e:
        fail_task(store, task_id, e, '429')
    except (ExplicitNotAcceptedError, SubmissionUnconfirmedError) as e:
        if row.get('duration_profile') == 'plugin-expert30' or row.get('batch_id'):
            fail_task(store, task_id, e)
            return
        current = store.get(task_id)
        if not row.get('accepted_at') and not (current and current.get('accepted_at')):
            retry = True
            explicit = isinstance(e, ExplicitNotAcceptedError)
            if explicit:
                pool.release_unaccepted_conversation(row['account'], row['conversation_id'])
            store.record_evidence(task_id, 'account_retry', {
                'account': row['account'], 'conversation_id': row['conversation_id'],
                'reply': getattr(e, 'reply', None) or (current or row).get('dola_reply') or str(e),
                'reason': ('原会话明确尚未开始生成，正在换下个账号' if explicit else
                           '原会话未确认受理，按最多五个不同账号的策略换号；旧会话可能继续出片，保留额度占用')})
        else:
            fail_task(store, task_id, e)
    except Exception as e:
        fail_task(store, task_id, e)
    finally:
        if not interrupted and not retry and 'upload:' in (row.get('reference_images') or ''):
            cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        if acquired:
            await key_limiter.release(api_key_hash)

    if retry:
        ratio = row.get('ratio')
        await _run_task(task_id, row['model'], row['prompt'], None if ratio == 'default' else ratio,
                        row['duration'], _task_reference_images(row.get('reference_images')), _task_client(row))


def _task_client(row: dict) -> dict:
    """从任务快照恢复调度所需的客户上下文，不依赖 Key 当前是否仍存在。"""
    return {
        "api_key_hash": row.get("api_key_hash"),
        "api_key_name": row.get("api_key_name") or "历史任务",
        "daily_limit": 0,
        "concurrency_limit": int(row.get("client_concurrency_limit") or 0),
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _task_reference_images(raw) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) else []


@app.on_event("startup")
async def resume_incomplete_tasks():
    """服务重启后恢复已受理会话，并重新排队尚未开始的 queued 任务。"""
    scheduler = globals().get('shared_scheduler')
    if scheduler is not None:
        from browser import set_shared_scheduler
        set_shared_scheduler(scheduler)
    if config.ISP_ONLY:
        from public_assets import account_asset_cache
        cache = account_asset_cache()
        if hasattr(cache, 'preload'):
            print(f'[network] 全 ISP 模式；已预载 {cache.preload()} 个有效公共资源', flush=True)
    store.fail_unconfirmed_submissions(reserve=pool.reserve_unconfirmed_submission)
    from platform_runtime import expire_unknown_platform_tasks
    expire_unknown_platform_tasks(globals())
    recoveries = store.recoverable_tasks()
    # Reserve both platforms before queued tasks can compete for physical lanes.
    doubao_rows = [r for r in store.recent_tasks(100000, platform='doubao')
                   if r['status'] == 'processing' and r.get('account')]
    if doubao_rows:
        doubao_provider.reserve_recoveries(doubao_rows)
        # A manual verification can be interrupted before the native site
        # assigns a conversation ID. Resume only to settle/reconcile its saved
        # send fence; never send it again or leave the account leased forever.
        recoveries.extend(r for r in doubao_rows if not r.get('conversation_id') and
                          r.get('phase') in ('checking_verification', 'waiting_verification', 'verification_continue'))
    pool.reserve_recoveries([r for r in recoveries if r.get('platform', 'dola') == 'dola'])
    for row in recoveries:
        asyncio.create_task(_resume_task(row))
    asyncio.create_task(_recover_audit_tasks(store.audit_mismatch_tasks()))
    batch_rows = {}
    for row in store.recoverable_queued_tasks():
        if row.get('batch_id'):
            batch_rows.setdefault(row['batch_id'], row)
            continue
        ratio = row.get("ratio")
        if ratio == "default":
            ratio = None
        asyncio.create_task(_run_task(
            row["id"], row["model"], row["prompt"], ratio, row["duration"],
            _task_reference_images(row.get("reference_images")), _task_client(row),
        ))
    from dola_submission import start_batch
    for batch_id, row in batch_rows.items():
        start_batch(globals(), batch_id, _task_client(row))
    asyncio.create_task(_watchdog())


@app.on_event('startup')
async def resume_google_imports():
    google_import_queue.recover()
    google_import_queue.start()


@app.on_event('shutdown')
async def stop_google_imports():
    set_shared_scheduler(None)
    if google_import_queue.task and not google_import_queue.task.done():
        google_import_queue.task.cancel()
        try:
            await google_import_queue.task
        except asyncio.CancelledError:
            pass


async def _watchdog():
    """排队停滞看门狗：周期扫描 processing 但未派发账号的任务，超时判失败。

    背景：_run_task 先标 processing 再调 pool.generate_video，若后者卡在等待
    账号/并发位（或协程被异常中断），任务会永久停在 account=NULL 的 processing
    状态，既不算成功也不算失败。本看门狗兜底：超过 QUEUE_STALL_SECONDS 仍无
    account/conversation_id 的任务，标记为排队超时失败，防止僵尸任务堆积。
    """
    while True:
        try:
            await asyncio.sleep(30)
            now = time.time()
            from platform_runtime import expire_unknown_platform_tasks
            expire_unknown_platform_tasks(globals(), now=now)
            # 规则一：processing 但从未派发账号（真僵尸，正常流程不会再出现）
            for row in store.stalled_processing_tasks(now - config.QUEUE_STALL_SECONDS):
                store.update(
                    row["id"], status="failed",
                    error="排队超时：任务进入 processing 后未派发到任何账号，已由看门狗判失败",
                    failure_code="503", finished_at=now,
                )
                print(f"[watchdog] processing 停滞判失败: {row['id']}", flush=True)
            # 规则二：queued 排队超过 QUEUE_WAIT_SECONDS（并发位长期不足兜底）
            for row in store.stalled_queued_tasks(now - config.QUEUE_WAIT_SECONDS):
                store.update(
                    row["id"], status="failed",
                    error="排队超时：queued 超时仍未获得并发位，已由看门狗判失败",
                    failure_code="503", finished_at=now,
                )
                print(f"[watchdog] queued 超时判失败: {row['id']}", flush=True)
        except Exception as e:
            print(f"[watchdog] 扫描异常: {str(e)[:200]}", flush=True)


@app.post("/v1/videos/generations", response_model=TaskResponse)
async def create_video(req: VideoGenRequest, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
    if req.expert_account is not None:
        raise HTTPException(403, '指定专家账号仅限管理员测试')
    if getattr(req, 'platform', 'dola') == 'dola' and 'dola' not in client.get('allowed_platforms', ['dola']):
        raise HTTPException(403, '当前密钥未获准使用 Dola')
    if getattr(req, 'batch_count', 1) > 1 or getattr(req, 'duration_profile', None) == 'plugin-expert30':
        from dola_submission import create_tasks
        return await create_tasks(globals(), req, client)
    if getattr(req, 'platform', 'dola') != 'dola':
        from platform_runtime import create_platform_task
        return await create_platform_task(globals(), req, client)
    if getattr(req, 'duration_profile', None) is not None:
        if (req.duration or 10) not in client['allowed_durations']:
            raise HTTPException(422, f"当前 API Key 不允许生成 {req.duration or 10} 秒视频")
        try:
            submission_duration(req.model, req.duration, req.duration_profile)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    identity = _submission_identity(req, 'api:' + str(client.get('api_key_hash'))) if getattr(req, 'idempotency_key', None) else {}
    if identity:
        replay = _find_submission(identity)
        if replay:
            return replay
    duration = req.duration or 10
    if duration not in SUPPORTED_DURATIONS:
        raise HTTPException(422, "当前支持 5/10/15/30 秒视频")
    if duration not in client["allowed_durations"]:
        raise HTTPException(422, f"当前 API Key 不允许生成 {duration} 秒视频")
    try:
        validate_video_parameters(req.model, duration)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    from reference_uploads import api_owner
    upload_owner = api_owner(client)
    if (req.reference_uploads or req.reference_media_uploads) and upload_owner is None:
        raise HTTPException(422, '上传参考素材需要使用有效的 API 密钥。')
    try:
        if len(req.reference_images) + len(req.reference_uploads) > config.REFERENCE_IMAGE_MAX_COUNT:
            raise ValueError(f'本地图片和链接合计最多 {config.REFERENCE_IMAGE_MAX_COUNT} 张')
        reference_images = await validate_reference_urls(req.reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # 账号暂时忙时允许任务进入 queued，由 BrowserPool 的全局并发控制实际排队。
    # 只有明确全部达到每日上限/积分不足时才立即拒绝。
    model_available = pool.available or pool.available_for_model(req.model)
    if not model_available and pool.all_accounts_limited:
        raise HTTPException(429, "账号限流：所有已开启调度的账号均已达到 Dola 每日视频上限，请明天再试")
    if not model_available and pool.all_accounts_quota_blocked:
        raise HTTPException(429, "积分不足：所有已开启调度的账号都没有足够积分，请等待额度刷新")
    if not model_available and pool.all_accounts_proxy_blocked:
        raise HTTPException(429, "ISP 额度不足：所有已开启调度的账号的专属 ISP 请求额度已用完或被暂停，请到管理面板处理")
    if not pool.accounts:
        raise HTTPException(503, "no account in pool")
    task_id = "video_" + uuid.uuid4().hex
    ratio = _resolve_ratio(req.size, req.ratio)
    try:
        reference_images += await asyncio.to_thread(attach_images, req.reference_uploads,
            config.REFERENCE_UPLOAD_DIR, upload_owner, task_id,
            media_ids=req.reference_media_uploads,
            media_max_count=getattr(config, 'REFERENCE_MEDIA_MAX_COUNT', 10))
        existing = store.create(
            task_id,
            req.model,
            req.prompt,
            ratio or "default",
            duration,
            reference_images=json.dumps(reference_images, ensure_ascii=False),
            api_key_hash=client["api_key_hash"],
            api_key_name=client["api_key_name"],
            daily_limit=client["daily_limit"],
            concurrency_limit=client["concurrency_limit"],
            max_pending=config.MAX_PENDING_TASKS,
            duration_profile=getattr(req, 'duration_profile', None),
            **identity,
        )
        if existing:
            cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
            return TaskResponse(**existing, replayed=True)
    except ValueError as exc:
        cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        raise HTTPException(409 if identity else 422, str(exc)) from None
    except TaskQuotaExceeded as exc:
        cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        raise HTTPException(429, str(exc)) from exc
    except PendingTaskLimitExceeded as exc:
        cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        raise HTTPException(429, str(exc)) from exc
    except Exception:
        cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        raise HTTPException(500, '参考图片保存失败，请重试。') from None
    asyncio.create_task(_run_task(
        task_id, req.model, req.prompt, ratio, duration, reference_images, client
    ))
    return TaskResponse(id=task_id, status="queued", model=req.model, prompt=req.prompt,
                        duration=duration, duration_profile=getattr(req, 'duration_profile', None))


def _admin_expert_account(req: VideoGenRequest) -> str | None:
    """Pin only an admin's one-off Pro test to an already schedulable ISP account."""
    account = req.expert_account
    if account is None:
        return None
    if req.platform != 'dola' or req.duration_profile != 'pro-fast15':
        raise HTTPException(422, '指定账号仅支持 Dola 专家模式 15 秒测试')
    candidate = next((row for row in pool.list_accounts() if row['name'] == account), None)
    if candidate is None or not pool._schedulable(candidate, req.model):
        raise HTTPException(422, '指定专家账号不存在或当前不可调度')
    raw, source = config.account_proxy_raw(account)
    try:
        bound_proxy = config.require_isp_proxy(config._parse_proxy(raw)) if source in ('file', 'map') else None
        effective_proxy = config.proxy_for(account)
    except (ValueError, TypeError):
        bound_proxy = effective_proxy = None
    if not bound_proxy or not effective_proxy or bound_proxy['server'] != effective_proxy['server']:
        raise HTTPException(422, '指定专家账号没有独立绑定的 ISP 线路')
    return account


@app.post("/api/admin/generate", response_model=TaskResponse)
async def admin_generate(req: VideoGenRequest, x_admin_key: str | None = Header(default=None),
                         authorization: str | None = Header(default=None)):
    """管理面板直接生成视频：走管理端鉴权（X-Admin-Key 或管理员会话），
    以匿名客户端身份入库，不受 API Key 日额度限制。"""
    identity = _admin_auth(x_admin_key, authorization)
    expert_account = _admin_expert_account(req) if req.expert_account is not None else None
    if getattr(req, 'batch_count', 1) > 1 or getattr(req, 'duration_profile', None) == 'plugin-expert30':
        from dola_submission import create_tasks
        return await create_tasks(globals(), req, _anonymous_client(), admin_identity=identity)
    if getattr(req, 'platform', 'dola') != 'dola':
        from platform_runtime import create_platform_task
        return await create_platform_task(globals(), req, _anonymous_client(), admin_identity=identity)
    if getattr(req, 'duration_profile', None) is not None:
        try:
            submission_duration(req.model, req.duration, req.duration_profile)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    submission_identity = _submission_identity(req, 'admin:' + admin_owner(identity)) if getattr(req, 'idempotency_key', None) else {}
    if submission_identity:
        replay = _find_submission(submission_identity)
        if replay:
            return replay
    duration = req.duration or 10
    if duration not in SUPPORTED_DURATIONS:
        raise HTTPException(422, "当前支持 5/10/15/30 秒视频")
    try:
        validate_video_parameters(req.model, duration)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    try:
        if len(req.reference_images) + len(req.reference_uploads) > config.REFERENCE_IMAGE_MAX_COUNT:
            raise ValueError(f'参考图片最多 {config.REFERENCE_IMAGE_MAX_COUNT} 张')
        reference_images = await validate_reference_urls(req.reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not pool.accounts:
        raise HTTPException(503, "no account in pool")
    task_id = "video_" + uuid.uuid4().hex
    ratio = _resolve_ratio(req.size, req.ratio)
    client = _anonymous_client()
    try:
        reference_images += await asyncio.to_thread(attach_images, req.reference_uploads,
            config.REFERENCE_UPLOAD_DIR, admin_owner(identity), task_id,
            media_ids=req.reference_media_uploads,
            media_max_count=getattr(config, 'REFERENCE_MEDIA_MAX_COUNT', 10))
        existing = store.create(
            task_id, req.model, req.prompt, ratio or "default", duration,
            reference_images=json.dumps(reference_images, ensure_ascii=False),
            api_key_hash=client["api_key_hash"], api_key_name=client["api_key_name"],
            daily_limit=0, concurrency_limit=0, max_pending=config.MAX_PENDING_TASKS,
            duration_profile=getattr(req, 'duration_profile', None),
            **submission_identity,
        )
        if existing:
            cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
            return TaskResponse(**existing, replayed=True)
    except TaskQuotaExceeded as exc:
        cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        raise HTTPException(429, str(exc)) from exc
    except PendingTaskLimitExceeded as exc:
        cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        raise HTTPException(429, str(exc)) from exc
    except ValueError as exc:
        cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        raise HTTPException(409 if submission_identity else 422, str(exc)) from exc
    except Exception:
        cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        raise HTTPException(500, '参考图片保存失败，请重试。') from None
    if expert_account is not None:
        # Persist before dispatch so a restart cannot silently choose another account.
        store.record_evidence(task_id, 'queued',
                              {'account': expert_account, 'reason': '管理员指定专家测试账号'})
    asyncio.create_task(_run_task(
        task_id, req.model, req.prompt, ratio, duration, reference_images, client
    ))
    return TaskResponse(id=task_id, status="queued", model=req.model, prompt=req.prompt,
                        duration=duration, duration_profile=getattr(req, 'duration_profile', None))


@app.get('/api/admin/reference-images/config')
async def admin_reference_config(x_admin_key: str | None = Header(default=None),
                                 authorization: str | None = Header(default=None), platform: str = 'dola'):
    _admin_auth(x_admin_key, authorization)
    from platform_runtime import upload_root
    upload_root(config, platform)
    return {'max_count': config.REFERENCE_IMAGE_MAX_COUNT, 'max_bytes': config.REFERENCE_IMAGE_MAX_BYTES}


@app.get('/api/me/reference-images/config')
async def member_reference_config(authorization: str | None = Header(default=None), platform: str = 'dola'):
    _require_user(authorization)
    from platform_runtime import upload_root
    upload_root(config, platform)
    return {'max_count': config.REFERENCE_IMAGE_MAX_COUNT, 'max_bytes': config.REFERENCE_IMAGE_MAX_BYTES}


@app.get('/v1/reference-images/limits')
async def api_reference_limits(authorization: str | None = Header(default=None), platform: str = 'dola'):
    from reference_uploads import api_owner
    client = _auth(authorization)
    if api_owner(client) is None:
        raise HTTPException(401, '需要有效的 API 密钥。')
    if platform not in client.get('allowed_platforms', ['dola']):
        raise HTTPException(403, '当前密钥未获准使用该平台。')
    from platform_runtime import upload_root
    upload_root(config, platform)
    return {'max_count': config.REFERENCE_IMAGE_MAX_COUNT, 'max_bytes': config.REFERENCE_IMAGE_MAX_BYTES}


@app.post('/v1/reference-images', status_code=201)
async def api_reference_upload(request: Request, authorization: str | None = Header(default=None), platform: str = 'dola'):
    from reference_uploads import api_owner
    client = _auth(authorization)
    owner = api_owner(client)
    if owner is None:
        raise HTTPException(401, '需要有效的 API 密钥。')
    if platform not in client.get('allowed_platforms', ['dola']):
        raise HTTPException(403, '当前密钥未获准使用该平台。')
    from platform_runtime import upload_root
    root = upload_root(config, platform)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > config.REFERENCE_IMAGE_MAX_BYTES:
            raise HTTPException(413, '图片超过单张大小限制。')
        chunks.append(chunk)
    try:
        return await asyncio.to_thread(save_image, b''.join(chunks), root, owner, config.REFERENCE_IMAGE_MAX_BYTES)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except OSError:
        raise HTTPException(500, '图片保存失败，请检查服务的存储空间。') from None


@app.post('/api/me/reference-images', status_code=201)
async def member_reference_upload(request: Request, authorization: str | None = Header(default=None), platform: str = 'dola'):
    user = _require_user(authorization)
    from platform_runtime import upload_root
    root = upload_root(config, platform)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > config.REFERENCE_IMAGE_MAX_BYTES:
            raise HTTPException(413, '图片超过单张大小限制。')
        chunks.append(chunk)
    try:
        return await asyncio.to_thread(save_image, b''.join(chunks), root,
                                       'user:' + str(user['id']), config.REFERENCE_IMAGE_MAX_BYTES)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except OSError:
        raise HTTPException(500, '图片保存失败，请检查服务的存储空间。') from None


@app.post('/api/admin/reference-images', status_code=201)
async def admin_reference_upload(request: Request, x_admin_key: str | None = Header(default=None),
                                 authorization: str | None = Header(default=None), platform: str = 'dola'):
    identity = _admin_auth(x_admin_key, authorization)
    from platform_runtime import upload_root
    root = upload_root(config, platform)
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > config.REFERENCE_IMAGE_MAX_BYTES:
            raise HTTPException(413, '图片超过单张大小限制。')
        chunks.append(chunk)
    try:
        return await asyncio.to_thread(save_image, b''.join(chunks), root,
                                       admin_owner(identity), config.REFERENCE_IMAGE_MAX_BYTES)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except OSError:
        raise HTTPException(500, '图片保存失败，请检查服务的存储空间。') from None


def _reference_media_limits():
    return {'max_count': config.REFERENCE_MEDIA_MAX_COUNT,
            'max_bytes': config.REFERENCE_MEDIA_MAX_BYTES,
            'extensions': ['mp3','wav','m4a','aac','flac','ogg','mp4','mov','webm']}


async def _save_reference_media_request(request, owner, filename, platform='dola'):
    from urllib.parse import unquote
    from platform_runtime import upload_root
    root=upload_root(config,platform)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > config.REFERENCE_MEDIA_MAX_BYTES:
            raise HTTPException(413, '音视频素材超过单个文件大小限制。')
        chunks.append(chunk)
    try:
        return await asyncio.to_thread(save_media, b''.join(chunks), unquote(filename),
            root, owner, config.REFERENCE_MEDIA_MAX_BYTES)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except OSError:
        raise HTTPException(500, '素材保存失败，请检查服务的存储空间。') from None


@app.get('/v1/reference-media/config')
async def api_reference_media_config(authorization: str | None = Header(default=None), platform: str = 'dola'):
    from reference_uploads import api_owner
    client = _auth(authorization)
    if api_owner(client) is None:
        raise HTTPException(401, '需要有效的 API 密钥。')
    if platform not in client.get('allowed_platforms', ['dola']):
        raise HTTPException(403, '当前密钥未获准使用该平台。')
    from platform_runtime import upload_root
    upload_root(config, platform)
    return _reference_media_limits()


@app.post('/v1/reference-media', status_code=201)
async def api_reference_media_upload(request: Request, x_file_name: str = Header(default=''),
                                     authorization: str | None = Header(default=None), platform: str = 'dola'):
    from reference_uploads import api_owner
    client = _auth(authorization)
    owner = api_owner(client)
    if owner is None:
        raise HTTPException(401, '需要有效的 API 密钥。')
    if platform not in client.get('allowed_platforms', ['dola']):
        raise HTTPException(403, '当前密钥未获准使用该平台。')
    return await _save_reference_media_request(request, owner, x_file_name, platform)


@app.get('/api/admin/reference-media/config')
async def admin_reference_media_config(x_admin_key: str | None = Header(default=None),
                                     authorization: str | None = Header(default=None), platform: str = 'dola'):
    _admin_auth(x_admin_key, authorization)
    from platform_runtime import upload_root
    upload_root(config,platform)
    return _reference_media_limits()


@app.get('/api/me/reference-media/config')
async def member_reference_media_config(authorization: str | None = Header(default=None), platform: str = 'dola'):
    _require_user(authorization)
    from platform_runtime import upload_root
    upload_root(config,platform)
    return _reference_media_limits()


@app.post('/api/admin/reference-media', status_code=201)
async def admin_reference_media_upload(request: Request, x_file_name: str = Header(default=''),
                                     x_admin_key: str | None = Header(default=None),
                                     authorization: str | None = Header(default=None), platform: str = 'dola'):
    identity = _admin_auth(x_admin_key, authorization)
    return await _save_reference_media_request(request, admin_owner(identity), x_file_name,platform)


@app.post('/api/me/reference-media', status_code=201)
async def member_reference_media_upload(request: Request, x_file_name: str = Header(default=''),
                                      authorization: str | None = Header(default=None), platform: str = 'dola'):
    user = _require_user(authorization)
    return await _save_reference_media_request(request, 'user:' + str(user['id']), x_file_name,platform)


@app.get("/v1/videos/{task_id}", response_model=TaskResponse)
async def get_video(task_id: str, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row:
        raise HTTPException(404, "task not found")
    return TaskResponse(
        id=row["id"], status=row["status"], model=row["model"],
        platform=row.get('platform', 'dola'), submitted_duration=row.get('submitted_duration'),
        duration=row.get('duration'), duration_profile=row.get('duration_profile'),
        prompt=row["prompt"], video_url=row["video_url"], error=row["error"],
        phase=row.get('phase'), actual_duration=row.get('actual_duration'), accepted_at=row.get('accepted_at'),
        failure_code=row.get('failure_code'), conversation_id=row.get('conversation_id'),
        clips=row.get('clips') or [], total_actual_duration=row.get('total_actual_duration'),
        batch_id=row.get('batch_id'), batch_index=row.get('batch_index'),
    )


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": app.version,
        "isp_request_limit_enabled": config.PROXY_REQUEST_LIMIT_ENABLED,
        "network_mode": "isp_only" if getattr(config, 'ISP_ONLY', False) else "legacy",
        "accounts": pool.cookie_count,
        "available": pool.available,
        "pending_tasks": store.pending_task_count(),
        "max_pending_tasks": config.MAX_PENDING_TASKS,
        "submission_concurrency": config.MAX_CONCURRENCY,
        "max_inflight": config.MAX_INFLIGHT,
        "poll_interval_seconds": config.POLL_INTERVAL,
        "download_concurrency": config.MAX_DOWNLOADS,
    }


# ===== 管理面板 API =====


class AdminLogin(BaseModel):
    key: str


class AccountPatch(BaseModel):
    scheduling: bool | None = None
    category: str | None = Field(None, pattern=r'^(long_term|disposable)$')
    note: str | None = None
    email: str | None = None


class AccountAdd(BaseModel):
    name: str
    email: str
    password: str
    totp: str


class CKImport(BaseModel):
    platform: str = Field('dola', pattern=r'^(dola|doubao)$')
    name: str
    ck: str = Field(repr=False)
    proxy: str = Field(default='', repr=False)
    category: str = Field(default='long_term', pattern=r'^(long_term|disposable)$')


class GoogleAccountsImport(BaseModel):
    text: str = Field(repr=False)
    category: str = Field(default='disposable', pattern=r'^(long_term|disposable)$')
    proxy: str = Field(default='', repr=False)


class AccountCategoryPatch(BaseModel):
    enabled: bool = Field(strict=True)


class QuotaPolicyPatch(BaseModel):
    model_config = {'extra': 'forbid'}
    mode: str = Field(strict=True, pattern=r'^(fast|seedance25)$')


class ProxySet(BaseModel):
    raw: str | None = None
    scheme: str | None = None
    host: str | None = None
    port: str | None = None
    username: str | None = None
    password: str | None = None
    save_to: str = "map"


class ProxyTest(BaseModel):
    name: str | None = None
    all: bool = False
    raw: str | None = None
    scheme: str | None = None
    host: str | None = None
    port: str | None = None
    username: str | None = None
    password: str | None = None


class ProxyImport(BaseModel):
    text: str = ""
    mode: str = "one_per_account"  # one_per_account | shared_first | shared_n
    per_proxy: int = Field(1, ge=1, le=1000)  # shared_n 模式：每条代理绑定几个账号
    accounts: list[str] = Field(default_factory=list)


class ProxyQuotaPatch(BaseModel):
    limit: int | None = Field(None, ge=1, le=1_000_000)
    paused: bool | None = None
    reset: bool | None = None
    warn_at: int | None = Field(None, ge=0, le=1_000_000)


class KeyCreate(BaseModel):
    allowed_platforms: list[str] = Field(default_factory=lambda: ['dola'])
    name: str = ""
    daily_limit: int = Field(0, ge=0, le=1_000_000)
    concurrency_limit: int = Field(0, ge=0, le=1_000)
    allowed_durations: list[int] = Field(default_factory=lambda: list(SUPPORTED_DURATIONS))
    expires_at: float | None = Field(None, ge=0)
    owner_user_id: int | None = None


class KeyPatch(BaseModel):
    allowed_platforms: list[str] | None = None
    name: str | None = None
    enabled: bool | None = None
    daily_limit: int | None = Field(None, ge=0, le=1_000_000)
    concurrency_limit: int | None = Field(None, ge=0, le=1_000)
    allowed_durations: list[int] | None = None
    expires_at: float | None = Field(None, ge=0)
    owner_user_id: int | None = None


@app.post("/api/admin/login")
async def admin_login(body: AdminLogin):
    """管理面板登录入口。

    - 已配置 DOLA_ADMIN_KEY：可用管理密码（mode=admin_key），
      也可直接用管理员邮箱会话（mode=session）。
    - 未配置且已有用户：必须走 /login 邮箱登录（mode=session）。
    - 未配置且系统无用户：/login 引导初始化第一个管理员（mode=setup）。
    """
    if config.ADMIN_KEY:
        if body.key and body.key == config.ADMIN_KEY:
            return {"ok": True, "auth_required": True, "mode": "admin_key"}
        if body.key:
            raise HTTPException(401, "管理密码错误")
        # 没传密码：提示去邮箱登录
        return {"ok": False, "auth_required": True, "mode": "session",
                "message": "请使用管理员邮箱登录"}
    if store.count_users() == 0:
        return {"ok": True, "auth_required": False, "mode": "setup"}
    return {"ok": False, "auth_required": True, "mode": "session",
            "message": "未设置管理密码，请使用管理员邮箱登录"}


@app.get("/api/admin/accounts")
async def admin_accounts(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    accounts = pool.list_accounts()
    from dola_submission import quota_summary
    return {"accounts": accounts, "categories": pool.list_categories(accounts),
            "quota_policy": pool.get_quota_policy(), 'quota_summary': quota_summary(accounts)}


@app.patch('/api/admin/quota-policy')
async def admin_quota_policy_patch(body: QuotaPolicyPatch,
                                   x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    try:
        policy = pool.set_quota_policy(body.mode)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    accounts = pool.list_accounts()
    return {"ok": True, "quota_policy": policy, "accounts": accounts,
            "categories": pool.list_categories(accounts)}


@app.patch('/api/admin/account-categories/{category}')
async def admin_account_category_patch(category: str, body: AccountCategoryPatch,
                                       x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    try:
        pool.set_category_enabled(category, body.enabled)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return {"ok": True, "categories": pool.list_categories()}


@app.post("/api/admin/accounts/import-ck", status_code=201)
async def admin_import_ck(body: CKImport, x_admin_key: str | None = Header(default=None),
                          authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    if getattr(body, 'platform', 'dola') == 'doubao':
        return {'account': doubao_provider.import_ck(body.name, body.ck, body.proxy, body.category)}
    name = body.name.strip()
    if JOBS.get(name, {}).get('status') == 'running':
        raise HTTPException(409, '该名称正在执行登录任务，请换一个名称。')
    try:
        result = import_ck(pool.accounts_dir, name, body.ck, body.proxy,
                           proxy_pool=[raw for key, raw in config._load_proxy_map().items() if key != '_default'])
        if not result.get('existing'):
            pool.set_category(result['name'], body.category)
    except FileExistsError:
        raise HTTPException(409, '名称已存在，请换一个名称；已有 CK 不会被覆盖。') from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    except OSError:
        raise HTTPException(500, 'CK 保存失败，请检查账号目录的写入权限。') from None
    return {'ok': True, **result}


@app.post('/api/admin/accounts/import-ck-batch', status_code=201)
async def admin_import_ck_batch(body: CKImport, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    if getattr(body, 'platform', 'dola') != 'dola':
        _admin_auth(x_admin_key, authorization)
        raise HTTPException(422, '豆包批量导入请使用平台专属接口或 ?platform=doubao，并提供 files 列表')
    _admin_auth(x_admin_key, authorization)
    if any(job.get('status') == 'running' for name, job in JOBS.items() if name == body.name or name.startswith(body.name + '_')):
        raise HTTPException(409, '该名称正在执行登录任务，请换一个名称。')
    try:
        result = import_ck_batch(pool.accounts_dir, body.name.strip(), body.ck, body.proxy,
            proxy_pool=[raw for key, raw in config._load_proxy_map().items() if key != '_default'])
        for account in result['results']:
            if not account.get('existing') and not account.get('error'):
                pool.set_category(account['name'], body.category)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return {'ok': True, **result}


@app.post('/api/admin/accounts/import-google', status_code=202)
async def admin_import_google(body: GoogleAccountsImport, x_admin_key: str | None = Header(default=None),
                              authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    try:
        result = import_google_accounts(pool, body.text, body.category, body.proxy,
            proxy_pool=[raw for key, raw in config._load_proxy_map().items() if key != '_default'])
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    except OSError:
        raise HTTPException(500, '账号保存失败，请检查本机目录权限。') from None
    google_import_queue.start()
    return {'ok': True, **result}


@app.post('/api/admin/accounts/{name}/retry-google', status_code=202)
async def admin_retry_google(name: str, x_admin_key: str | None = Header(default=None),
                             authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    try:
        google_import_queue.retry(name)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return {'ok': True, 'state': 'queued'}


@app.patch("/api/admin/accounts/{name}")
async def admin_account_patch(name: str, body: AccountPatch,
                              x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if body.category is not None:
        pool.set_category(name, body.category)
    if body.scheduling is not None:
        pool.set_scheduling(name, body.scheduling, override_category=True)
    if body.note is not None:
        pool.set_note(name, body.note)
    if body.email is not None:
        pool.set_email(name, body.email)
    return {"ok": True, "account": next(a for a in pool.list_accounts() if a["name"] == name)}


@app.delete("/api/admin/accounts/{name}")
async def admin_account_delete(name: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    try:
        pool.delete_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/admin/accounts/reset-all")
async def admin_accounts_reset_all(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    """一键恢复所有号为「满状态」：清除冷却/限流/额度封锁，重新打开调度，清零今日用量。"""
    _admin_auth(x_admin_key, authorization)
    result = pool.reset_all_accounts(open_scheduling=True, clear_usage=True, clear_credit=False)
    return {"ok": True, **result}


@app.post("/api/admin/accounts/{name}/verify")
async def admin_account_verify(name: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    try:
        ok = await pool.verify_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    # ok 为三态：True 有效 / False 确认失效 / None 无法判断（网络、代理、超时）
    status = "valid" if ok is True else "invalid" if ok is False else "unknown"
    return {"ok": ok, "status": status}


async def _verify_all_job(job: dict, targets: list[str], retries: int = 2):
    """后台全量登录态检测：并发 2（贴合两条代理线），无法判断自动重试。

    结果写回 login_ok（verify_account 内部三态落库）；结束后把「明确确认失效」
    名单存进 VERIFY_RUNS 供 delete-invalid 白名单用。
    """
    sem = asyncio.Semaphore(2)

    async def one(name: str):
        async with sem:
            ok = None
            for attempt in range(retries + 1):
                try:
                    ok = await pool.verify_account(name)
                except Exception:
                    ok = None
                if ok is not None:
                    break
                if attempt < retries:
                    await asyncio.sleep(2)  # 网络抖动稍候重试，别误判成死号
            job["results"][name] = ok
            job["done"] += 1
            if ok is True:
                job["valid"] += 1
            elif ok is False:
                job["invalid"].append(name)
            else:
                job["unknown"].append(name)

    try:
        await asyncio.gather(*(one(n) for n in targets))
    except Exception as e:
        job["error"] = str(e)  # 任务崩了也要落终态，前端不能永远等
    finally:
        job["running"] = False
        job["finished_at"] = time.time()
        job["invalid"].sort(key=_nat_key)
        job["unknown"].sort(key=_nat_key)
        token = job["token"]
        now = time.time()
        # 记录本轮「明确确认失效」白名单：delete-invalid 只允许删这批号，
        # 避免把历史误判（旧版网络错误写下的 login_ok=0）一起删掉。
        VERIFY_RUNS[token] = {"at": now, "invalid": set(job["invalid"])}
        # 清理过期记录，并只保留最近 20 轮
        for k in [k for k, v in VERIFY_RUNS.items()
                  if k != token and now - v["at"] > VERIFY_RUNS_TTL]:
            VERIFY_RUNS.pop(k, None)
        if len(VERIFY_RUNS) > 20:
            for k in sorted(VERIFY_RUNS, key=lambda k: VERIFY_RUNS[k]["at"])[:-20]:
                VERIFY_RUNS.pop(k, None)


@app.post("/api/admin/accounts/verify-all")
async def admin_accounts_verify_all(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    """启动后台全量登录态检测，立即返回 token（不阻塞请求）。

    300+ 个号约需 20-40 分钟，同步跑必然请求超时——改为后台任务，
    前端用 GET /api/admin/accounts/verify-progress 轮询进度。
    """
    _admin_auth(x_admin_key, authorization)
    global VERIFY_JOB
    if VERIFY_JOB and VERIFY_JOB.get("running"):
        return {"ok": True, "already_running": True, "token": VERIFY_JOB["token"],
                "total": VERIFY_JOB["total"], "done": VERIFY_JOB["done"]}
    targets = [a["name"] for a in pool.list_accounts() if not a["busy"]]
    token = uuid.uuid4().hex
    VERIFY_JOB = {
        "token": token, "running": True, "started_at": time.time(),
        "total": len(targets), "done": 0, "valid": 0,
        "invalid": [], "unknown": [], "results": {}, "error": "",
    }
    asyncio.create_task(_verify_all_job(VERIFY_JOB, targets))
    return {"ok": True, "already_running": False, "token": token, "total": len(targets)}


@app.get("/api/admin/accounts/verify-progress")
async def admin_accounts_verify_progress(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    """查询最近一次全量检测的进度/结果（不带 token 也返回最近一轮）。

    前端刷新页面后靠它找回 token、恢复进度轮询——检测不再因刷新丢状态。
    """
    _admin_auth(x_admin_key, authorization)
    if not VERIFY_JOB:
        return {"ok": True, "running": False}
    j = VERIFY_JOB
    return {
        "ok": True, "running": j.get("running", False), "token": j.get("token"),
        "total": j.get("total", 0), "done": j.get("done", 0),
        "valid": j.get("valid", 0),
        "invalid": sorted(j.get("invalid", []), key=_nat_key),
        "unknown": sorted(j.get("unknown", []), key=_nat_key),
        "error": j.get("error", ""),
        "started_at": j.get("started_at"), "finished_at": j.get("finished_at"),
    }


@app.post("/api/admin/accounts/delete-invalid/preview")
async def admin_accounts_delete_invalid_preview(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    """列出当前失效账号供用户确认；生成任务标记的失效号也包括在内。"""
    _admin_auth(x_admin_key, authorization)
    if VERIFY_JOB and VERIFY_JOB.get("running"):
        raise HTTPException(409, "账号检测正在进行，请等检测完成后再清理。")
    now = time.time()
    for key in list(DELETE_INVALID_RUNS):
        if now - DELETE_INVALID_RUNS[key]["at"] > VERIFY_RUNS_TTL:
            DELETE_INVALID_RUNS.pop(key, None)
    accounts, skipped, checked_at = [], [], {}
    for a in pool.list_accounts():
        if a.get("login_ok") != 0:
            continue
        if a.get("busy"):
            skipped.append({"name": a["name"], "reason": "账号正在出片"})
        elif a.get("proxy_blocked") or a.get("proxy_exhausted"):
            skipped.append({"name": a["name"], "reason": "专属 ISP 额度耗尽或被暂停，暂不清理"})
        else:
            accounts.append(a["name"])
            checked_at[a["name"]] = a.get("login_checked_at")
    token = uuid.uuid4().hex if accounts else None
    if token:
        DELETE_INVALID_RUNS[token] = {"at": now, "invalid": set(accounts), "checked_at": checked_at}
        for key in sorted(DELETE_INVALID_RUNS, key=lambda k: DELETE_INVALID_RUNS[k]["at"])[:-20]:
            DELETE_INVALID_RUNS.pop(key, None)
    return {"ok": True, "token": token, "accounts": sorted(accounts, key=_nat_key), "skipped": skipped}


@app.post("/api/admin/accounts/delete-invalid")
async def admin_accounts_delete_invalid(token: str | None = None, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    """只软删除已确认名单中仍然失效且状态未变的账号，出片中的自动跳过。"""
    _admin_auth(x_admin_key, authorization)
    if VERIFY_JOB and VERIFY_JOB.get("running"):
        raise HTTPException(409, "账号检测正在进行，请等检测完成后再清理。")
    run = (DELETE_INVALID_RUNS.get(token) or VERIFY_RUNS.get(token)) if token else None
    if not run or time.time() - run["at"] > VERIFY_RUNS_TTL:
        DELETE_INVALID_RUNS.pop(token, None)
        VERIFY_RUNS.pop(token, None)
        raise HTTPException(400, "清理名单已失效，请重新点击「删除失效号」查看并确认名单。")
    DELETE_INVALID_RUNS.pop(token, None)
    VERIFY_RUNS.pop(token, None)
    current = {a["name"]: a for a in pool.list_accounts()}
    checked_at = run.get("checked_at")
    deleted, skipped = [], []
    for name in sorted(run["invalid"], key=_nat_key):
        a = current.get(name)
        if not a:
            skipped.append({"name": name, "reason": "账号已移除"})
            continue
        if a.get("login_ok") != 0:
            skipped.append({"name": name, "reason": "账号已不再标记为失效"})
            continue
        if checked_at is not None and a.get("login_checked_at") != checked_at.get(name):
            skipped.append({"name": name, "reason": "登录状态已更新，请重新查看名单"})
            continue
        if a.get("busy"):
            skipped.append({"name": name, "reason": "账号正在出片"})
            continue
        if a.get("proxy_blocked") or a.get("proxy_exhausted"):
            skipped.append({"name": name, "reason": "专属 ISP 额度耗尽或被暂停，暂不清理"})
            continue
        try:
            pool.delete_account(name)
            deleted.append(name)
        except (RuntimeError, OSError) as e:
            skipped.append({"name": name, "reason": str(e)})
    return {"ok": True, "deleted": sorted(deleted, key=_nat_key), "skipped": skipped}


@app.get("/api/admin/trash")
async def admin_trash_list(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    """列出软删除（回收站）的账号备份。"""
    _admin_auth(x_admin_key, authorization)
    return {"ok": True, "items": sorted(pool.list_trash(), key=lambda x: x["deleted_at"], reverse=True)}


@app.post("/api/admin/trash/restore")
async def admin_trash_restore(backup: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    """把回收站里的账号备份还原。"""
    _admin_auth(x_admin_key, authorization)
    try:
        name = pool.restore_account(backup)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "name": name}


# ===== 代理 / IP 管理 =====


def _proxy_display(raw: str) -> dict:
    """从代理串提取可安全展示的信息（绝不含密码）。"""
    p = config._parse_proxy(raw)
    if not p:
        return {"scheme": "", "host": "", "port": "", "server": "", "auth": False, "user": "", "route_label": ""}
    server = p.get("server", "")
    scheme, rest = "", server
    if "://" in server:
        scheme, rest = server.split("://", 1)
    host, _, port = rest.rpartition(":")
    if not host:
        host, port = rest, ""
    # 文档示例线路；部署时按实际端点配置，未知出口不猜测国家。
    route_label = {
        "192.0.2.11": "英国线路 1",
        "192.0.2.12": "英国线路 2",
        "192.0.2.13": "英国线路 3",
        "192.0.2.14": "德国线路 1",
        "192.0.2.15": "德国线路 2",
    }.get(host, "")
    return {"scheme": scheme, "host": host, "port": port, "server": server,
            "auth": bool(p.get("username")), "user": p.get("username", ""), "route_label": route_label}


def _compose_proxy(body, name: str | None = None) -> str:
    """把请求里的 raw 或分字段合成一行代理串；密码留空时沿用已保存的密码。"""
    if body.raw and body.raw.strip():
        return body.raw.strip()
    host = (body.host or "").strip()
    port = str(body.port or "").strip()
    if not host or not port:
        return ""
    username = (body.username or "").strip()
    password = body.password or ""
    if username and not password and name:
        old = config._parse_proxy(config.account_proxy_raw(name)[0]) or {}
        if old.get("username") == username and old.get("password"):
            password = old["password"]
    return config.build_proxy_raw(body.scheme or "http", host, port, username, password)


async def _probe_proxy(proxy: dict, cost: int = 1) -> dict:
    """通过代理访问公网，返回出口 IP / 国家 / 延迟（不返回任何凭据）。

    检测会真实消耗该 ISP 的请求额度（默认 1 次，单请求同时取 IP + 国家），
    因此先预扣额度；不足则直接拒绝检测，不发任何请求。
    """
    import aiohttp
    if proxy:
        try:
            proxy_usage.usage.reserve(proxy, cost)
        except proxy_usage.ProxyQuotaExceeded as e:
            return {"ok": False, "error": f"额度不足，未检测（本次约需 {cost} 次）：{e}"}
    url = config._proxy_to_url(proxy)
    scheme = (proxy.get("server", "").split("://")[0] or "http").lower()
    t0 = time.time()
    try:
        connector = None
        if scheme.startswith("socks"):
            try:
                from aiohttp_socks import ProxyConnector
            except ImportError:
                return {"ok": False, "error": "缺少 aiohttp_socks 依赖，暂不能检测 SOCKS5"}
            connector = ProxyConnector.from_url(url)
        timeout = aiohttp.ClientTimeout(total=18)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
            kw = {} if connector else {"proxy": url}
            # 省额度：单请求同时拿出口 IP + 国家/城市，比原来两次请求省一半。
            async with sess.get("https://ipinfo.io/json", **kw) as r:
                data = await r.json(content_type=None)
        ip = str(data.get("ip", "")).strip()
        country = str(data.get("country", "")).strip()
        city = str(data.get("city", "")).strip()
        if city:
            country = (country + " " + city).strip()
        latency = int((time.time() - t0) * 1000)
        return {"ok": bool(ip), "ip": ip, "country": country, "latency_ms": latency}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


@app.get("/api/admin/proxies")
async def admin_proxies(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    accs = pool.list_accounts()
    items, groups = [], {}
    configured = 0
    own_proxies: list[dict] = []
    for a in accs:
        raw, source = config.account_proxy_raw(a["name"])
        disp = _proxy_display(raw)
        own = source in ("file", "map")
        proxy = config._parse_proxy(raw) if raw else None
        pkey = proxy_usage.proxy_key(proxy) if (own and proxy) else ""
        quota = proxy_usage.usage.get(proxy) if (own and proxy) else None
        if own:
            configured += 1
            if proxy:
                own_proxies.append(proxy)
        items.append({"name": a["name"], "email": a.get("email", ""), "source": source,
                      "own": own, "proxy_key": pkey, "quota": quota, **disp})
        # 按 proxy_key（含用户名）分组，避免同服务器不同账号被误合并；
        # 非专属（global/map_default）按 server 单独归类。
        gkey = pkey or ("shared:" + (disp["server"] or "none"))
        g = groups.setdefault(gkey, {"proxy_key": pkey, **disp, "sources": set(), "accounts": []})
        g["sources"].add(source)
        g["accounts"].append(a["name"])
        if own and quota:
            g["quota"] = quota
            # 同一条线路可能被写成 http:// 或 socks5://，额度已按线路合并；
            # 面板统一显示额度记录里的地址，避免同一条线路出现两种写法。
            qdisp = _proxy_display(quota.get("server") or "")
            if qdisp["server"]:
                for k in ("scheme", "host", "port", "server", "auth", "user"):
                    g[k] = qdisp[k]
    glist = []
    independent = shared = 0
    for g in groups.values():
        g["sources"] = sorted(g["sources"])
        g["own"] = any(s in ("file", "map") for s in g["sources"])
        g["count"] = len(g["accounts"])
        if g["own"]:
            if g["count"] > 1:
                shared += 1
            else:
                independent += 1
        glist.append(g)
    glist.sort(key=lambda g: (not g["own"], -g["count"], g.get("server", "")))
    # 合并已配置但尚未使用的出口，并统计总计。
    quotas = proxy_usage.usage.list_all(own_proxies)
    total_used = sum(q["used"] for q in quotas)
    unlimited = any(q.get("unlimited") for q in quotas)
    total_limit = None if unlimited else sum(q["limit"] for q in quotas)
    warn_count = sum(1 for q in quotas if q["warn"] and not q["blocked"])
    paused_count = sum(1 for q in quotas if q["paused"])
    exhausted_count = sum(1 for q in quotas if q["exhausted"])
    available_count = sum(1 for q in quotas if not q["blocked"])
    return {"accounts": items, "groups": glist, "total": len(accs),
            "network_mode": "isp_only" if config.ISP_ONLY else "legacy",
            "configured": configured, "independent": independent,
            "shared_groups": shared, "missing": len(accs) - configured,
            "quotas": quotas,
            "summary": {"used": total_used, "limit": total_limit, "unlimited": unlimited,
                        "available": available_count, "warn": warn_count,
                        "paused": paused_count, "exhausted": exhausted_count,
                        "count": len(quotas)},
            "global_proxy": _proxy_display(config.PROXY) if config.PROXY and not config.ISP_ONLY else None}


@app.patch("/api/admin/accounts/{name}/proxy")
async def admin_account_proxy_set(name: str, body: ProxySet,
                                  x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    raw = _compose_proxy(body, name)
    if not raw:
        raise HTTPException(400, "请填写主机和端口")
    try:
        # 先清除旧绑定（含 proxy.txt 与映射表），避免两处冲突。
        config.clear_account_proxy(name)
        config.save_account_proxy(name, raw, body.save_to or "map")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "proxy": _proxy_display(raw)}


@app.delete("/api/admin/accounts/{name}/proxy")
async def admin_account_proxy_clear(name: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    config.clear_account_proxy(name)
    return {"ok": True}


@app.post("/api/admin/proxies/test")
async def admin_proxy_test(body: ProxyTest, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    if (body.raw and body.raw.strip()) or body.host:
        raw = _compose_proxy(body, body.name)
        proxy = config._parse_proxy(raw) if raw else None
        if not proxy:
            raise HTTPException(400, "代理参数不完整")
        return {"results": [{"name": body.name or "临时", **await _probe_proxy(proxy)}]}
    if body.all:
        names = [a["name"] for a in pool.list_accounts()]
    elif body.name:
        names = [body.name]
    else:
        raise HTTPException(400, "需要 name 或 all")
    sem = asyncio.Semaphore(8)

    # 每条 ISP 检测约消耗 1 次额度；多个账号共用同一条出口时只检测一次，避免重复消耗。
    async def one(n: str) -> dict:
        async with sem:
            proxy = config.proxy_for(n)
            if not proxy:
                return {"name": n, "ok": False, "error": "未配置代理"}
            return {"name": n, **await _probe_proxy(proxy)}

    async def run_unique(n: str, proxy: dict) -> dict:
        async with sem:
            return {"name": n, **await _probe_proxy(proxy)}

    if body.all:
        # 按 proxy_key 去重：同一条 ISP 只检测一次。
        unique: dict[str, tuple[str, dict]] = {}
        singles: list[tuple[str, str]] = []
        for n in names:
            raw, source = config.account_proxy_raw(n)
            proxy = config._parse_proxy(raw) if raw else None
            if not proxy:
                singles.append((n, "未配置代理"))
                continue
            k = proxy_usage.proxy_key(proxy)
            if k and k not in unique:
                unique[k] = (n, proxy)
        results = [{"name": n, "ok": False, "error": err} for n, err in singles]
        results += list(await asyncio.gather(*[run_unique(n, px) for n, px in unique.values()]))
        return {"results": results, "tested": len(unique), "skipped_duplicates": len(names) - len(unique)}

    results = await asyncio.gather(*[one(n) for n in names])
    return {"results": results}


def _nat_key(s: str):
    """账号自然排序键：1,2,…,10 而不是 1,10,2。"""
    return [(0, int(t)) if t.isdigit() else (1, t.lower()) for t in re.split(r"(\d+)", str(s))]


@app.patch("/api/admin/proxies/quota/{proxy_key}")
async def admin_proxy_quota(proxy_key: str, body: ProxyQuotaPatch,
                            x_admin_key: str | None = Header(default=None),
                            authorization: str | None = Header(default=None)):
    """调整某条 ISP 的额度：设上限 / 暂停 / 恢复 / 重置。不返回任何凭据。"""
    _admin_auth(x_admin_key, authorization)
    state = proxy_usage.usage.get_by_key(proxy_key)
    if not state["key"]:
        raise HTTPException(404, "未找到该 ISP 额度记录")
    proxy = {"server": state["server"], "username": state["username"]}
    if body.reset:
        out = proxy_usage.usage.reset(proxy)
    else:
        if body.limit is not None:
            out = proxy_usage.usage.set_limit(proxy, body.limit, body.warn_at)
        elif body.warn_at is not None:
            out = proxy_usage.usage.set_warn_at(proxy, body.warn_at)
        if body.paused is not None:
            out = proxy_usage.usage.set_paused(
                proxy, body.paused, reason="manual" if body.paused else "")
        if body.limit is None and body.warn_at is None and body.paused is None:
            out = proxy_usage.usage.get(proxy)
    return {"ok": True, "quota": out}


@app.post("/api/admin/proxies/import")
async def admin_proxy_import(body: ProxyImport, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    """批量导入代理，三种写法都支持：

    - 纯代理串：一行一条，按 mode 分给账号。
      one_per_account = 一号一 IP（每条代理一个账号，默认）
      shared_first    = 几号一 IP（目标账号共用第一条代理）
      shared_n        = N 号一 IP（每条代理绑定 per_proxy 个账号，如 1 条代理 10 个号）
    - 账号 + 代理串：`1 http://...` 或 `1=http://...`，指定账号。
    - JSON：{"1": "http://...", "2": "..."}。
    """
    _admin_auth(x_admin_key, authorization)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "请先粘贴代理")

    explicit: dict[str, str] = {}
    plain: list[str] = []

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except Exception:
            raise HTTPException(400, "JSON 解析失败")
        if not isinstance(data, dict):
            raise HTTPException(400, "JSON 需要是 {账号: 代理串} 结构")
        explicit = {str(k).strip(): str(v).strip() for k, v in data.items()}
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z0-9_\-]+)\s*[=,\s]\s*(\S.*)$", line)
            if m:
                explicit[m.group(1)] = m.group(2).strip()
            else:
                plain.append(line)

    assigned: list[dict] = []
    errors: list[dict] = []
    ok = skip = 0

    # 1) 指定了账号的行 / JSON
    if explicit:
        valid_explicit = {}
        for k, v in explicit.items():
            disp = _proxy_display(v)
            if disp["server"]:
                valid_explicit[k] = v
            else:
                errors.append({"line": k, "error": "无法识别的代理格式"})
                skip += 1
        # 导入前先清除该账号旧绑定（含 proxy.txt），保证新代理真正生效。
        for k in valid_explicit:
            config.clear_account_proxy(k)
        if valid_explicit:
            good, bad = config.import_proxy_map(valid_explicit)
            ok += good
            skip += bad
        for k, v in valid_explicit.items():
            assigned.append({"name": k, "server": _proxy_display(v)["server"], "source": "map"})

    # 2) 纯代理串：按顺序分配给账号
    if plain:
        valid, invalid = [], []
        for raw in plain:
            (valid if config._parse_proxy(raw) else invalid).append(raw)
        for raw in invalid:
            errors.append({"line": raw, "error": "无法识别的代理格式"})
            skip += 1

        if valid:
            all_names = sorted((a["name"] for a in pool.list_accounts()), key=_nat_key)
            wanted = [n for n in (body.accounts or []) if n in all_names]
            targets = wanted or all_names
            if body.mode == "shared_first":
                pairs = [(n, valid[0]) for n in targets]
            elif body.mode in ("shared_n", "grouped") or body.per_proxy > 1:
                # N 号一 IP：把目标账号按 per_proxy 个一组，每组共用一条代理。
                per = max(1, int(body.per_proxy or 1))
                need = (len(targets) + per - 1) // per if targets else 0
                pairs = []
                for i, raw in enumerate(valid[:need]):
                    for n in targets[i * per:(i + 1) * per]:
                        pairs.append((n, raw))
                if len(valid) > need:
                    skip += len(valid) - need
            else:
                pairs = list(zip(targets, valid))
                if len(valid) > len(pairs):
                    skip += len(valid) - len(pairs)
            for name, raw in pairs:
                try:
                    # 先清除旧绑定（含 accounts/<name>/proxy.txt）再写入，避免旧出口继续生效。
                    config.clear_account_proxy(name)
                    config.save_account_proxy(name, raw, "map")
                except Exception as e:
                    errors.append({"name": name, "error": str(e)[:120]})
                    skip += 1
                    continue
                assigned.append({"name": name, "server": _proxy_display(raw)["server"], "source": "map"})
                ok += 1

    if not assigned and not errors:
        raise HTTPException(400, "没有可导入的内容，请检查格式")
    assigned_names = {a["name"] for a in assigned}
    unassigned: list[str] = []
    if plain:
        all_names = sorted((a["name"] for a in pool.list_accounts()), key=_nat_key)
        wanted = [n for n in (body.accounts or []) if n in all_names]
        targets = wanted or all_names
        unassigned = [n for n in targets if n not in assigned_names]
    return {"ok": ok, "skip": skip, "total": ok + skip, "assigned": assigned,
            "unassigned": unassigned, "errors": errors}


async def _run_add_job(name: str, email: str, password: str, totp: str):
    JOBS[name] = {"kind": "add", "status": "running", "error": "", "started_at": time.time()}
    try:
        await add_account_flow(name, email, password, totp)
        pool.set_email(name, email)
        pool.set_login_status(name, True)
        JOBS[name] = {**JOBS[name], "status": "success"}
    except Exception as e:
        JOBS[name] = {**JOBS[name], "status": "failed", "error": str(e)[:300]}


@app.post("/api/admin/accounts", status_code=202)
async def admin_account_add(body: AccountAdd, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "invalid account name")
    if body.name in pool.accounts:
        raise HTTPException(409, "account exists")
    if JOBS.get(body.name, {}).get("status") == "running":
        raise HTTPException(409, "add job running")
    asyncio.create_task(_run_add_job(body.name, body.email, body.password, body.totp))
    return {"ok": True, "job": "running"}


@app.get("/api/admin/jobs")
async def admin_jobs(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    return {"jobs": JOBS}


@app.get("/api/admin/tasks")
async def admin_tasks(limit: int = 50, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None), platform: str = 'dola'):
    _admin_auth(x_admin_key, authorization)
    try:
        return {"tasks": store.recent_tasks(min(max(limit, 1), 200), platform=platform)}
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@app.get('/api/admin/tasks/{task_id}')
async def admin_task_detail(task_id: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    row = store.get(task_id)
    if not row:
        raise HTTPException(404, 'task not found')
    return {'task': row, 'events': store.task_events(task_id)}


_result_checks: dict[str, asyncio.Lock] = {}


async def _query_original_task(row):
    if row.get('platform', 'dola') != 'dola':
        raise HTTPException(409, '该任务须通过原平台适配器核查，未重新提交')
    from video_polling import HttpVideoSession
    from account_errors import LoginExpiredError
    if not row.get('account') or not row.get('conversation_id'):
        raise HTTPException(409, '尚未保存原会话，不能查询或重新取回；不会补发提示词')
    try:
        from contextlib import nullcontext
        scheduler = globals().get('shared_scheduler')
        lease = (scheduler.diagnostic('dola', row['account'], config.proxy_for(row['account']), browser=False)
                 if scheduler is not None else nullcontext())
        async with lease:
            async with HttpVideoSession(row['account'], row['conversation_id']) as session:
                poll = await session.poll()
    except LoginExpiredError as error:
        pool.mark_login_expired(row['account'])
        raise HTTPException(409, safe_text(error)) from None
    except Exception as error:
        raise HTTPException(502, f'原会话查询暂时失败（{type(error).__name__}），未重新发送') from None
    if row.get('duration_profile') == 'pro-fast15':
        from dola_expert import expert_outcome
        return expert_outcome(poll)
    if row.get('duration_profile') == 'plugin-expert30':
        from plugin_expert30 import poll_outcome
        _, outcome = poll_outcome(poll)
        parsed = poll.get('plugin_expert30') or {}
        return {**poll, 'videos': parsed.get('clips') or []}, outcome
    model = row['model'].strip() if row.get('duration_profile') is not None else row['model']
    upstream_duration = submission_duration(model, row['duration'], row.get('duration_profile'))
    submitted = build_video_prompt(row['prompt'], upstream_duration, model)
    return poll, conversation_outcome(poll, model, submitted)


def _complete_existing_video(row):
    """Validate a saved local result before making repeated collection idempotent."""
    if row.get('duration_profile') == 'plugin-expert30':
        from multi_clip_results import cached_clips
        clips = cached_clips(store, row)
        if row.get('status') != 'completed' or not clips or len(clips) != len(row.get('clips') or []):
            return False
        complete_task(store, row['id'], {'clips': clips, 'generation_finished': True,
            'observation_finished': True, 'account': row.get('account'),
            'conversation_id': row.get('conversation_id')}, config.PUBLIC_BASE)
        return (store.get(row['id']) or {}).get('status') == 'completed'
    prefix = config.PUBLIC_BASE.rstrip('/') + '/videos/'
    url = row.get('video_url') or ''
    if not url.startswith(prefix):
        return False
    name = url[len(prefix):]
    root = Path(config.DOWNLOAD_DIR).resolve()
    path = (root / name).resolve()
    if not name or Path(name).name != name or path.parent != root or not path.is_file():
        return False
    try:
        complete_task(store, row['id'], {'local_path': str(path), 'account': row['account']}, config.PUBLIC_BASE)
    except (OSError, ValueError):
        return False
    # Publication can reject an existing preview without raising an exception.
    # Only a result link retained by that provenance check is a cache hit.
    saved = store.get(row['id'])
    return bool(saved and saved.get('video_url') == url)


def _reconcile_original_outcome(row, outcome):
    """Reconcile platform evidence; submission audit remains in request_evidence."""
    task_id = row['id']
    state = outcome['state']
    details = {'reply': outcome['reply'], 'video_count': outcome['video_count']}
    if state == 'failed' and not _complete_existing_video(row):
        if getattr(outcome['error'], 'task_phase', None) == 'platform_daily_limit' and row.get('account'):
            from quota_ledger import quota_type
            kind = quota_type(row.get('model'), row.get('duration'), row.get('duration_profile'))
            if kind:
                pool.quota_ledger.block(row['account'], kind, None, str(outcome['error']))
            else:
                pool._mark_daily_limit(row['account'], str(outcome['error']))
        if (getattr(outcome['error'], 'quota_not_consumed', False)
                and row.get('account') and row.get('conversation_id')):
            pool.release_no_charge_conversation(row['account'], row['conversation_id'], outcome['error'])
        store.record_evidence(task_id, outcome['error'].task_phase, details)
        fail_task(store, task_id, outcome['error'])
    elif state == 'accepted' and row['status'] == 'failed' and (
            row.get('failure_code') in ('request_mismatch', 'not_accepted') or
            (row.get('duration_profile') == 'pro-fast15' and row.get('failure_code') in ('failed', 'query_timeout'))):
        store.record_evidence(task_id, 'accepted', details)
        deadline = row.get('deadline_at')
        if deadline and deadline > time.time():
            store.update(task_id, status='processing', error=None, failure_code=None,
                         finished_at=None, last_poll_at=time.time())
            pool.reserve_recoveries([store.get(task_id)])
            return True
        fail_task(store, task_id, VideoQueryTimeout('原会话已确认受理，但观察期限已到；暂未见成片，未重新发送'))
    else:
        phase = 'accepted' if state == 'accepted' and row['status'] in ('queued', 'processing') else row.get('phase') or 'awaiting_receipt'
        store.record_evidence(task_id, phase, details)
    store.update(task_id, last_poll_at=time.time())
    return False


async def _collect_original_video(row):
    # The caller holds the task lock; resume_video also takes the original account lock.
    if _complete_existing_video(row):
        return
    model = row['model'].strip() if row.get('duration_profile') is not None else row['model']
    upstream_duration = submission_duration(model, row['duration'], row.get('duration_profile'))
    result = await pool.resume_video(row['account'], row['conversation_id'], 60,
        deadline_at=time.time() + 60, model=model,
        submitted_prompt=build_video_prompt(row['prompt'], upstream_duration, model),
        on_event=lambda phase, details: store.record_evidence(row['id'], phase, details), accepted=True,
        **({'entry_mode': 'plugin-expert30', 'duration_profile': 'plugin-expert30',
            'generation_options': _plugin_task_options(row), 'task_id': row['id']}
            if row.get('duration_profile') == 'plugin-expert30' else
            {'entry_mode': 'pro-fast15'} if row.get('duration_profile') == 'pro-fast15' else {}))
    complete_task(store, row['id'], result, config.PUBLIC_BASE)


async def _recover_audit_task(row):
    """Bounded startup recheck of a saved conversation, never a generation retry."""
    lock = _result_checks.setdefault(row['id'], asyncio.Lock())
    resume = False
    async with lock:
        row = store.get(row['id'])
        if not row or row['status'] != 'failed' or row.get('failure_code') != 'request_mismatch':
            return
        try:
            poll, outcome = await _query_original_task(row)
            resume = _reconcile_original_outcome(row, outcome)
            if poll.get('videos'):
                await _collect_original_video(row)
        except HTTPException as error:
            print(f"[audit-recovery] {row['id']}: original query unavailable ({error.status_code})")
        except Exception as error:
            fail_task(store, row['id'], error)
    if resume:
        asyncio.create_task(_resume_task(row))


async def _recover_audit_tasks(rows):
    # Query historical audits sequentially; resumed polling runs independently.
    for row in rows:
        await _recover_audit_task(row)


@app.get('/api/admin/tasks/{task_id}/conversation')
async def admin_task_conversation(task_id: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    row = store.get(task_id)
    if not row:
        raise HTTPException(404, 'task not found')
    if row.get('platform', 'dola') != 'dola':
        from platform_runtime import inspect_platform_task
        return await inspect_platform_task(globals(), row)
    lock = _result_checks.setdefault(task_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, '原会话正在轮询或取回，请等待当前任务')
    async with lock:
        row = store.get(task_id)
        poll, outcome = await _query_original_task(row)
        resume = _reconcile_original_outcome(row, outcome)
        if poll.get('videos') and row['status'] == 'failed' and row.get('failure_code') in ('request_mismatch', 'not_accepted'):
            try:
                await _collect_original_video(row)
            except Exception as error:
                fail_task(store, task_id, error)
                raise HTTPException(502, safe_text(error)) from None
    if resume:
        asyncio.create_task(_resume_task(row))
    return {'reply': outcome['reply'], 'video_count': outcome['video_count'],
            'conversation_id': row['conversation_id'], 'state': outcome['state']}


@app.post('/api/admin/tasks/{task_id}/download')
async def admin_task_download(task_id: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    """Only collect a video already returned by the original conversation; never submit."""
    _admin_auth(x_admin_key, authorization)
    row = store.get(task_id)
    if not row:
        raise HTTPException(404, 'task not found')
    if row.get('platform', 'dola') != 'dola':
        from platform_runtime import inspect_platform_task
        return await inspect_platform_task(globals(), row, download=True)
    lock = _result_checks.setdefault(task_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, '正在取回同一任务，请等待')
    async with lock:
        row = store.get(task_id)
        if _complete_existing_video(row):
            return {'task': store.get(task_id)}
        if row['status'] in ('queued', 'processing'):
            raise HTTPException(409, '任务仍在运行，请等待当前轮询或下载完成')
        poll, outcome = await _query_original_task(row)
        if not poll.get('videos'):
            resume = _reconcile_original_outcome(row, outcome)
            if resume:
                asyncio.create_task(_resume_task(row))
            raise HTTPException(409, '原会话暂时没有视频结果，未重新发送或生成')
        # Take the pool's original-account lock; this path never selects another account.
        try:
            _reconcile_original_outcome(row, outcome)
            await _collect_original_video(row)
        except Exception as error:
            fail_task(store, task_id, error)
            raise HTTPException(502, safe_text(error)) from None
        return {'task': store.get(task_id)}


def _clean_download_task(task_id: str) -> dict:
    row = store.get(task_id)
    if not row:
        raise HTTPException(404, 'task not found')
    if row.get('platform', 'dola') != 'dola':
        raise HTTPException(422, '此入口仅处理 Dola 视频')
    return row


@app.get('/api/admin/tasks/{task_id}/clean-download/status')
async def admin_clean_download_status(task_id: str, x_admin_key: str | None = Header(default=None),
                                      authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    _clean_download_task(task_id)
    return clean_download_files.status(task_id)


@app.get('/api/admin/tasks/{task_id}/clean-download/file')
async def admin_clean_download_file(task_id: str, kind: str,
                                    x_admin_key: str | None = Header(default=None),
                                    authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    _clean_download_task(task_id)
    if kind != 'official':
        raise HTTPException(422, '仅支持官方无水印原片')
    path = clean_download_files.download_path(task_id, kind)
    if path is None:
        raise HTTPException(404, '请求的视频版本尚不可下载')
    return FileResponse(path, media_type='video/mp4', filename=f'{task_id}_official_original.mp4',
                        headers={'Cache-Control': 'private, no-store', 'X-Video-Provenance': 'official_original'})


@app.get("/api/admin/gallery")
async def admin_gallery(limit: int = 200, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None), platform: str = 'dola'):
    """视频画廊：返回所有已出片视频（completed 且带 video_url），按时间倒序。"""
    _admin_auth(x_admin_key, authorization)
    try:
        return {"videos": store.completed_videos(min(max(limit, 1), 500), platform=platform)}
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@app.get("/api/admin/stats")
async def admin_stats(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None), platform: str = 'dola'):
    _admin_auth(x_admin_key, authorization)
    if platform != 'dola':
        if platform not in ('doubao', 'all'):
            raise HTTPException(422, '不支持的平台')
        st = store.stats(platform=platform)
        accs = [{**a, 'platform':'doubao'} for a in doubao_provider.list_accounts()]
        if platform == 'all':
            accs += [{**a, 'platform':'dola'} for a in pool.list_accounts()]
        totals = st.pop('per_account_total', {})
        st.update(platform=platform, total_accounts=len(accs), available_accounts=sum(
            bool(a.get('enabled', a.get('effective_scheduling'))) and
            a.get('quota_remaining', a.get('remaining')) != 0 for a in accs),
            total_remaining=None,
            per_account=[{**a, 'completed_total': totals.get(
                a['platform']+':'+a['name'] if platform == 'all' else a['name'], 0)} for a in accs])
        return st
    st = store.stats()
    accs = pool.list_accounts()
    sched = [a for a in accs if a["effective_scheduling"] and not a["cooling"]]
    st["total_accounts"] = len(accs)
    st["available_accounts"] = sum(1 for a in sched if a["remaining"] > 0)
    st["total_remaining"] = sum(a["remaining"] for a in sched)
    from dola_submission import quota_summary
    st['quota_summary'] = quota_summary(accs)
    totals = st.pop("per_account_total", {})
    st["per_account"] = [{**a, "completed_total": totals.get(a["name"], 0)} for a in accs]
    return st


@app.get("/api/admin/keys")
async def admin_keys(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    keys = []
    for key in store.list_keys():
        usage = store.key_usage(store.hash_api_key(key["key"]))
        keys.append({**key, **{
            "today_total": usage["total"],
            "today_completed": usage["completed"],
            "today_failed": usage["failed"],
            "today_active": usage["active"],
            "today_queued": usage["queued"],
        }})
    return {"keys": keys, "env_keys": len(config.API_KEYS)}


@app.post("/api/admin/keys")
async def admin_key_create(body: KeyCreate, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    try:
        platforms = store.parse_platforms(body.allowed_platforms)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    allowed = _normalize_allowed_durations(body.allowed_durations)
    created = store.create_key(
        body.name,
        daily_limit=body.daily_limit,
        concurrency_limit=body.concurrency_limit,
        allowed_durations=allowed,
        expires_at=body.expires_at,
        allowed_platforms=platforms,
    )
    if body.owner_user_id:
        store.assign_key_owner(created["key"], body.owner_user_id)
    return {"created": created}


@app.patch("/api/admin/keys/{key}")
async def admin_key_patch(key: str, body: KeyPatch,
                          x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    fields = {}
    if body.allowed_platforms is not None:
        try:
            fields['allowed_platforms'] = store.parse_platforms(body.allowed_platforms)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    if body.name is not None:
        fields["name"] = body.name
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if body.daily_limit is not None:
        fields["daily_limit"] = body.daily_limit
    if body.concurrency_limit is not None:
        fields["concurrency_limit"] = body.concurrency_limit
    if body.allowed_durations is not None:
        fields["allowed_durations"] = _normalize_allowed_durations(body.allowed_durations)
    if body.expires_at is not None:
        fields["expires_at"] = body.expires_at
    if fields:
        store.update_key(key, **fields)
    if body.owner_user_id is not None:
        store.assign_key_owner(key, body.owner_user_id or None)
    return {"ok": True}


@app.delete("/api/admin/keys/{key}")
async def admin_key_delete(key: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    store.delete_key(key)
    return {"ok": True}


# ===== 邮箱登录 / 成员系统 =====


class RegisterBody(BaseModel):
    email: str
    password: str
    invite_code: str | None = None


class LoginBody(BaseModel):
    email: str
    password: str


class UserCreate(BaseModel):
    email: str
    password: str
    role: str = "member"
    note: str = ""
    create_key: bool = False
    daily_limit: int = Field(0, ge=0, le=1_000_000)
    concurrency_limit: int = Field(0, ge=0, le=1_000)
    allowed_durations: list[int] | None = None


class UserPatch(BaseModel):
    role: str | None = None
    status: str | None = None
    note: str | None = None
    password: str | None = None
    create_key: bool | None = None
    daily_limit: int | None = Field(None, ge=0, le=1_000_000)
    concurrency_limit: int | None = Field(None, ge=0, le=1_000)
    allowed_durations: list[int] | None = None


def _public_user(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "role": user["role"]}


def _issue_session(user: dict, user_agent: str | None) -> str:
    token = auth.new_session_token()
    ttl = max(1, int(config.SESSION_TTL_DAYS)) * 86400
    store.create_session(auth.hash_token(token), user["id"], time.time() + ttl,
                         user_agent or "")
    store.set_last_login(user["id"])
    return token


def _registration_open() -> bool:
    return bool(config.ALLOW_SELF_REGISTER)


@app.get("/api/auth/status")
async def auth_status():
    total = store.count_users()
    return {
        "needs_setup": total == 0,
        "has_admin": store.count_users(role="admin") > 0,
        "registration_open": _registration_open() or total == 0,
        "admin_key_mode": bool(config.ADMIN_KEY),
    }


@app.post("/api/auth/register")
async def auth_register(body: RegisterBody, user_agent: str | None = Header(default=None)):
    email = auth.normalize_email(body.email)
    if not auth.is_valid_email(email):
        raise HTTPException(422, "邮箱格式不正确")
    problem = auth.validate_password(body.password)
    if problem:
        raise HTTPException(422, problem)
    first_user = store.count_users() == 0
    if not first_user:
        if not _registration_open():
            raise HTTPException(403, "注册未开放，请联系管理员创建账号")
        if config.INVITE_CODE and body.invite_code != config.INVITE_CODE:
            raise HTTPException(403, "邀请码不正确")
    if store.get_user_by_email(email):
        raise HTTPException(409, "该邮箱已注册")
    role = "admin" if first_user else "member"
    try:
        user = store.create_user(email, auth.hash_password(body.password), role)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "该邮箱已注册")
    token = _issue_session(user, user_agent)
    return {"token": token, "user": _public_user(user), "first_user": first_user}


@app.post("/api/auth/login")
async def auth_login(body: LoginBody, user_agent: str | None = Header(default=None)):
    email = auth.normalize_email(body.email)
    raw = store.get_user_by_email(email, include_hash=True)
    if not raw or not auth.verify_password(body.password, raw.get("password_hash")):
        raise HTTPException(401, "邮箱或密码错误")
    if raw.get("status") != "active":
        raise HTTPException(403, "账号已被禁用")
    token = _issue_session(raw, user_agent)
    return {"token": token, "user": _public_user(raw)}


@app.post("/api/auth/logout")
async def auth_logout(authorization: str | None = Header(default=None)):
    token = auth.parse_bearer(authorization)
    if token:
        store.delete_session(auth.hash_token(token))
    return {"ok": True}


@app.get("/api/me")
async def me(authorization: str | None = Header(default=None)):
    user = _require_user(authorization)
    keys = store.keys_for_user(user["id"])
    hashes = [store.hash_api_key(k["key"]) for k in keys]
    return {
        "user": _public_user(user),
        "keys": [
            {
                "key": k["key"],
                "name": k["name"],
                "enabled": k["enabled"],
                "daily_limit": k["daily_limit"],
                "concurrency_limit": k["concurrency_limit"],
                "allowed_durations": k["allowed_durations"],
                "expires_at": k["expires_at"],
                "created_at": k["created_at"],
                "last_used_at": k["last_used_at"],
                "usage": store.usage_for_hashes([store.hash_api_key(k["key"])]),
            }
            for k in keys
        ],
        "usage": store.usage_for_hashes(hashes),
        "api_base": config.PUBLIC_BASE,
        "supported_durations": list(SUPPORTED_DURATIONS),
        "supported_durations_by_model": SUPPORTED_DURATIONS_BY_MODEL,
    }


def _member_task(row: dict) -> dict:
    """Expose personal task content without account, client or proxy internals."""
    fields = ("id", "platform", "model", "prompt", "ratio", "duration", "duration_profile", "status",
              "created_at", "updated_at", "video_url", "phase", "actual_duration", "accepted_at",
              "clips", "total_actual_duration", "batch_id", "batch_index", "deleted_at")
    result = {field: row.get(field) for field in fields}
    error = str(row.get("error") or "")
    if row.get('phase') == 'platform_rejected_no_charge' and row.get('status') == 'failed':
        result['error'] = '平台内容政策限制，生成失败；本次未计号池额度，请修改提示词或参考素材后手动提交。'
    elif row.get('failure_code') == 'partial_results':
        result['error'] = '本次申请部分完成；已核验的视频可分别下载，其余结果可从原会话补收。'
    elif row.get('phase') == 'subscription_required' or row.get('failure_code') == 'subscription_required':
        result['error'] = '平台提示此生成能力需要会员，本次已停止申请。'
    elif not error:
        result["error"] = None
    elif "超时" in error or "timeout" in error.lower() or "未出片" in error:
        result["error"] = "任务等待超时，暂未获得视频结果。请联系管理员并提供任务 ID。"
    elif "积分" in error or "额度" in error:
        result["error"] = "生成资源暂不可用。请稍后再试，或联系管理员并提供任务 ID。"
    else:
        result["error"] = "这条视频任务未能完成。请联系管理员并提供任务 ID。"
    return result


@app.get("/api/me/tasks")
async def me_tasks(limit: int = 50, authorization: str | None = Header(default=None), platform: str = 'dola'):
    user = _require_user(authorization)
    keys = store.keys_for_user(user["id"])
    hashes = [store.hash_api_key(k["key"]) for k in keys]
    try:
        rows = (store.recent_tasks_for_hashes(hashes, min(max(limit, 1), 200)) if platform == 'dola'
                else store.recent_tasks_for_hashes(hashes, min(max(limit, 1), 200), platform=platform))
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {"tasks": [_member_task(row) for row in rows]}


# ===== 管理员：成员管理 =====


@app.get("/api/admin/users")
async def admin_users(x_admin_key: str | None = Header(default=None),
                      authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    users = store.list_users()
    for user in users:
        keys = store.keys_for_user(user["id"])
        hashes = [store.hash_api_key(k["key"]) for k in keys]
        user["usage"] = store.usage_for_hashes(hashes)
        user["keys"] = [
            {"key": k["key"], "name": k["name"], "enabled": k["enabled"],
             "daily_limit": k["daily_limit"], "created_at": k["created_at"]}
            for k in keys
        ]
    return {"users": users}


@app.post("/api/admin/users")
async def admin_user_create(body: UserCreate,
                            x_admin_key: str | None = Header(default=None),
                            authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    email = auth.normalize_email(body.email)
    if not auth.is_valid_email(email):
        raise HTTPException(422, "邮箱格式不正确")
    problem = auth.validate_password(body.password)
    if problem:
        raise HTTPException(422, problem)
    if body.role not in ("admin", "member"):
        raise HTTPException(422, "role 只能是 admin 或 member")
    if store.get_user_by_email(email):
        raise HTTPException(409, "该邮箱已注册")
    try:
        user = store.create_user(email, auth.hash_password(body.password), body.role,
                                 body.note or "")
    except sqlite3.IntegrityError:
        raise HTTPException(409, "该邮箱已注册")
    created = None
    if body.create_key:
        created = store.create_key(
            name=email,
            daily_limit=body.daily_limit or 0,
            concurrency_limit=body.concurrency_limit or 0,
            allowed_durations=_normalize_allowed_durations(body.allowed_durations),
        )
        store.assign_key_owner(created["key"], user["id"])
    return {"user": user, "created": created}


@app.patch("/api/admin/users/{user_id}")
async def admin_user_patch(user_id: int, body: UserPatch,
                           x_admin_key: str | None = Header(default=None),
                           authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    user = store.get_user(user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    fields = {}
    if body.role is not None:
        if body.role not in ("admin", "member"):
            raise HTTPException(422, "role 只能是 admin 或 member")
        fields["role"] = body.role
    if body.status is not None:
        if body.status not in ("active", "disabled"):
            raise HTTPException(422, "status 只能是 active 或 disabled")
        fields["status"] = body.status
    if body.note is not None:
        fields["note"] = body.note
    if body.password:
        problem = auth.validate_password(body.password)
        if problem:
            raise HTTPException(422, problem)
        fields["password_hash"] = auth.hash_password(body.password)
    if fields:
        store.update_user(user_id, **fields)
    created = None
    if body.create_key:
        created = store.create_key(
            name=user["email"],
            daily_limit=body.daily_limit or 0,
            concurrency_limit=body.concurrency_limit or 0,
            allowed_durations=_normalize_allowed_durations(body.allowed_durations),
        )
        store.assign_key_owner(created["key"], user_id)
    if body.status == "disabled":
        store.delete_user_sessions(user_id)
    return {"ok": True, "created": created}


@app.post("/api/admin/users/{user_id}/keys")
async def admin_user_create_key(user_id: int,
                                x_admin_key: str | None = Header(default=None),
                                authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    user = store.get_user(user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    created = store.create_key(name=user["email"])
    store.assign_key_owner(created["key"], user_id)
    return {"created": created}


@app.delete("/api/admin/users/{user_id}")
async def admin_user_delete(user_id: int,
                            x_admin_key: str | None = Header(default=None),
                            authorization: str | None = Header(default=None)):
    _admin_auth(x_admin_key, authorization)
    user = store.get_user(user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user["role"] == "admin" and store.count_users(role="admin") <= 1:
        raise HTTPException(409, "至少保留一个管理员")
    store.delete_user(user_id)
    return {"ok": True}


# ===== 页面入口 =====


@app.get("/login")
async def page_login():
    return FileResponse("web/login.html")


@app.get("/member")
async def page_member():
    return FileResponse("web/member.html")


@app.get("/admin")
async def page_admin():
    return FileResponse("web/index.html")


# Register platform routes before the static catch-all.
from task_deletion import register_routes as register_task_routes
register_task_routes(globals())
from dola_submission import register_routes as register_batch_routes
register_batch_routes(globals())
from platform_runtime import register_platform_routes
register_platform_routes(app, globals())

# 面板单文件前端（放最后，保证上面的路由优先）
app.mount("/", StaticFiles(directory="web", html=True), name="web")
