import math
from urllib.parse import urlparse
"""dola-pool OpenAI 兼容视频服务（FastAPI）+ 管理面板后端。

对外协议（异步两段式）：
  POST /v1/videos/generations   -> {"id","status","model","prompt"}
  GET  /v1/videos/{id}          -> {"id","status","video_url","error"}
  GET  /videos/<file>           -> 出片转存静态服务

管理面板：GET / -> web/index.html；管理接口 /api/admin/*。
"""

import asyncio
import aiohttp
import base64
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import tempfile
import zipfile
import uuid
from pure_api_gen import ConfirmedVideoFailure
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, ValidationError, StrictInt

import config
if config.TRANSPORT_MODE == "relay":
    import cf_hook
import cred_store
import failure_text
print = failure_text.print_safe
failure_text.install_log_redaction()
import proxy_store
from add_account import add_account_flow
from add_fb_account import add_fb_account_flow
from ai_settings import (
    get_public_settings,
    update_settings,
    AiSettingsError,
    AiSettingsDecryptError,
)
from ai_provider import (
    rewrite_prompt_text,
    preflight_check_prompt,
    AiProviderError,
    AiProviderNotConfiguredError,
    AiProviderCallError,
)
from ai_preflight import (
    record_preflight_result,
    validate_preflight_gate,
    _PREFLIGHT_TOKENS, context_fingerprint,
)
from cookie_login import import_cookie_account, import_cookie_accounts_from_text
from credential_import import allocate_credentials, parse_account_text
from browser_pool import (
    ACTIVE_GROUPS,
    GROUP_ABNORMAL,
    GROUP_BUSY,
    GROUP_COOLING,
    GROUP_FULL,
    GROUP_HALF,
    GROUP_ORDER,
    GROUP_PENDING,
    GROUP_RISK,
    AllAccountsLimitedError,
    AllAccountsQuotaBlockedError,
    ContentRefusalError,
    BrowserPool,
    classify_upstream_failure,
)
from media import (
    cleanup_local_references,
    delete_reference_thumbnails,
    download_reference_images,
    materialize_data_urls,
    save_reference_thumbnails,
    sweep_stale_references,
    validate_reference_urls,
)
from proxy_store import (
    create_proxy,
    delete_proxy,
    get_account_proxy,
    list_proxies,
    set_account_proxy,
    update_proxy,
)
from store import PendingTaskLimitExceeded, TaskQuotaExceeded, TaskStore
from stress_test import run_stress
from user_store import (
    create_user as user_create,
    delete_user as user_delete,
    get_user as user_get,
    list_users as user_list,
    update_user as user_update,
    verify_login as user_verify,
)

Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path("web").mkdir(parents=True, exist_ok=True)

APP_VERSION = "2.1.3"

app = FastAPI(title="dola-pool", version=APP_VERSION)


@app.exception_handler(StarletteHTTPException)
async def localized_request_error(request, exc):
    raw = exc.detail
    text = raw if isinstance(raw, str) else str(raw.get('message', 'Không thực hiện được yêu cầu')) if isinstance(raw, dict) else 'Không thực hiện được yêu cầu'
    display = failure_text.present(text)
    if re.search(r'[\u3400-\u9fff]', text):
        message = display['message_vi'] if display['category'] != 'unknown' else 'Không thực hiện được yêu cầu; hãy xem chi tiết để kiểm tra dữ liệu hoặc kết nối.'
    else:
        message = failure_text.sanitize_details(text)
    return JSONResponse(status_code=exc.status_code, headers=exc.headers,
        content={'detail': failure_text.sanitize_details(raw) if isinstance(raw, str) else raw,
                 'message_vi': message, 'failure_display': {**display, 'message_vi': message}})


# 浏览器直连（画布这类纯前端）需要 CORS；默认放开所有来源，
# 用 DOLA_CORS_ORIGINS 收紧（逗号分隔）。鉴权依旧是 Bearer Key，放开来源不等于放开权限。
CORS_ORIGINS = [o.strip() for o in os.getenv("DOLA_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

store = TaskStore(config.DB_PATH)
pool = BrowserPool(max_concurrency=config.MAX_CONCURRENCY)

app.mount("/videos", StaticFiles(directory=config.DOWNLOAD_DIR), name="videos")
# 参考图缩略图（任务结束后唯一能回看的副本）；文件名 = <task_id>_<index>.jpg
Path(config.THUMB_DIR).mkdir(parents=True, exist_ok=True)
app.mount("/thumbs", StaticFiles(directory=config.THUMB_DIR), name="thumbs")

# 后台 jobs（加号/验证），内存态
JOBS: dict[str, dict] = {}
# 账号级「测试生成」任务（内存态）：指定某个账号出片，观察是否成功
TEST_JOBS: dict[str, dict] = {}
# 账号登录态自动刷新（防抖：同一账号刷新间隔）
LAST_CRED_REFRESH: dict[str, float] = {}
# 加号并发上限：1.9GB 内存机器上同时开多个浏览器登录会 OOM，批量添加时逐个排队登录
ADD_ACCOUNT_SEM = asyncio.Semaphore(1)
# 浏览器并发压力测试状态（内存态）
STRESS_RUN: dict = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "recommended": None,
    "results": [],
    "logs": [],
}
# 批量操作任务（批量验证等，内存态）
BATCH_JOBS: dict[str, dict] = {}
# 批量验证串行执行：验证会开浏览器/打代理，同时跑多批会互相抢资源
BATCH_VERIFY_SEM = asyncio.Semaphore(1)
# 批量「你好」探测的并发：**0 = 不限（默认，按用户要求全部放开）**，>0 = 限制路数。
# 注意：上游对"同一出口 IP 短时间多次提交"敏感（实测并发 3 路就会被 710022002 整批拒），
# 放开后大概率会看到一批"未完成"——那些不会锁号，只是这次没探到。
BATCH_PROBE_CONCURRENCY = int(os.getenv("DOLA_HELLO_PROBE_CONCURRENCY", "0"))
# 每个探测之间的间隔（秒）：0 = 不等（默认，配合不限并发一起放开）。
BATCH_PROBE_INTERVAL_SECONDS = float(os.getenv("DOLA_HELLO_PROBE_INTERVAL", "0"))
# 软失败后的退避重试间隔（秒）。这不是并发限制，是防误锁：两次都失败才判风控。
BATCH_PROBE_RETRY_SECONDS = float(os.getenv("DOLA_HELLO_PROBE_RETRY_WAIT", "4.0"))


class _NoLimit:
    """并发不限时的空信号量（asyncio.Semaphore(0) 会死锁，所以单独给个 no-op）。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False
# 管理后台会话（token -> 用户信息），重启后需重新登录
ADMIN_SESSIONS: dict[str, dict] = {}
ADMIN_SESSION_TTL = 7 * 86400

SIZE_TO_RATIO = {
    "1280x720": "16:9", "1920x1080": "16:9",
    "720x1280": "9:16", "1080x1920": "9:16",
    "1024x1024": "1:1", "1440x1080": "4:3", "1080x1440": "3:4",
}
VIDEO_MAX_ATTEMPTS = config.VIDEO_MAX_ATTEMPTS  # 0 = 不限次数，在 TASK_DEADLINE 内持续换号
SUPPORTED_DURATIONS = tuple(config.ALL_DURATIONS)  # (5, 10, 15, 30)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


RATIO_TO_DIMENSIONS = {
    "16:9": (1280, 720), "9:16": (720, 1280),
    "1:1": (1024, 1024), "4:3": (1440, 1080), "3:4": (1080, 1440),
}


def _probe_with_ffprobe(path: str) -> dict | None:
    """用 ffprobe 读取真实视频元数据；未安装或解析失败返回 None。"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return None
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    fmt = data.get("format") or {}
    meta = {}
    if stream:
        try:
            meta["video_width"] = int(stream.get("width") or 0) or None
            meta["video_height"] = int(stream.get("height") or 0) or None
        except (TypeError, ValueError):
            pass
        dur = stream.get("duration") or fmt.get("duration")
        try:
            meta["video_duration"] = float(dur) if dur else None
        except (TypeError, ValueError):
            pass
    bit_rate = fmt.get("bit_rate")
    try:
        meta["video_bitrate"] = int(bit_rate) if bit_rate else None
    except (TypeError, ValueError):
        pass
    return {k: v for k, v in meta.items() if v is not None} or None


def _video_metadata(local_path: str, duration: int | None, ratio: str | None) -> dict:
    """出片完成后的视频元数据：优先 ffprobe 真实值，缺失时用任务记录推算。"""
    meta: dict = {}
    try:
        meta["video_size"] = os.path.getsize(local_path)
    except OSError:
        pass
    probed = _probe_with_ffprobe(local_path)
    if probed:
        meta.update(probed)
    else:
        w, h = RATIO_TO_DIMENSIONS.get(ratio or "", (0, 0))
        if w and h:
            meta["video_width"], meta["video_height"] = w, h
        if duration:
            meta["video_duration"] = float(duration)
    if meta.get("video_size") and meta.get("video_duration"):
        meta["video_bitrate"] = int(meta["video_size"] * 8 / meta["video_duration"])
    return meta


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
    }


def _admin_auth(x_admin_key: str | None):
    if not x_admin_key:
        raise HTTPException(401, "Vui lòng đăng nhập trước")
    session = ADMIN_SESSIONS.get(x_admin_key)
    if not session or session.get("expires_at", 0) < time.time():
        ADMIN_SESSIONS.pop(x_admin_key, None)
        raise HTTPException(401, "Phiên đăng nhập đã hết hạn; hãy đăng nhập lại")
    user = user_get(session.get("username", ""))
    if not user or not user.get("enabled"):
        ADMIN_SESSIONS.pop(x_admin_key, None)
        raise HTTPException(401, "Tài khoản không khả dụng; hãy đăng nhập lại")


def _normalize_allowed_durations(values) -> list[int]:
    if values is None:
        return list(SUPPORTED_DURATIONS)
    try:
        normalized = sorted({int(value) for value in values})
    except (TypeError, ValueError):
        raise HTTPException(422, f"allowed_durations must be an array of {list(SUPPORTED_DURATIONS)}")
    if not normalized or any(value not in SUPPORTED_DURATIONS for value in normalized):
        raise HTTPException(
            422, f"allowed_durations 只能包含 {list(SUPPORTED_DURATIONS)}，且至少选择一个"
        )
    return normalized


# ===== 客户 API =====


class VideoGenRequest(BaseModel):
    model: str = "seedance-2.0"
    prompt: str = Field(..., min_length=1)
    size: str | None = None
    ratio: str | None = None
    duration: int | None = Field(None, ge=5, le=30)
    # 时长别名：new-api 的 sora/vinted 任务插件会把画布传来的 duration 统一改写成
    # `seconds` 再转发到本服务，所以这里必须一起认；否则 30 秒请求会被当成「没带时长」
    # 而落到默认 10 秒（画布「选 30 秒却生成 10 秒」就是这个原因）。
    seconds: int | None = Field(None, ge=5, le=30)
    duration_seconds: int | None = Field(None, ge=5, le=30)
    # 参考图：兼容各画布工具的常见字段形态（字符串/数组/{"url": ...}）
    reference_images: list[str] = Field(default_factory=list)
    image_url: Any = None
    image: Any = None
    input_image: Any = None
    input_images: Any = None


class TestGenRequest(BaseModel):
    """账号级「测试生成」：用指定账号跑一条真实出片，看该号能否成功。"""
    prompt: str = Field(..., min_length=1)
    model: str = "seedance-2.0"
    duration: int = Field(10, ge=5, le=30)
    ratio: str | None = None


class TaskResponse(BaseModel):
    id: str
    status: str
    model: str | None = None
    prompt: str | None = None
    video_url: str | None = None
    error: str | None = None
    # 排队/进度说明：客户端可以拿它显示「排队中，预计 N 分钟」。
    # 字段是附加的（老客户端忽略即可，不影响原有 status/video_url 语义）。
    progress: dict | None = None


def _eta_text(seconds: int) -> str:
    if seconds <= 45:
        return "不到 1 分钟"
    minutes = max(1, int(round(seconds / 60.0)))
    return f"约 {minutes} 分钟"


def _progress_payload(*, status: str, created_at: float | None, duration: int | None) -> dict:
    info = store.queue_progress(created_at=created_at, duration=duration)
    labels = {"queued": "Chờ tài khoản", "processing": "Dola đang tạo video",
              "completed": "Hoàn tất", "unknown": "Chưa lấy được kết quả — có thể Dola vẫn đang tạo",
              "failed": "Không tạo được video"}
    return {**info, "workers": int(config.MAX_CONCURRENCY or 0),
            "eta_seconds": info["typical_seconds"] if status == "queued" else None,
            "state": {"processing":"running", "completed":"done"}.get(status,status), "position": info["ahead"] + 1 if status == "queued" else None,
            "percent": 100 if status == "completed" else None,
            "message": labels.get(status, status)}


def _resolve_ratio(size, ratio):
    if size and size in SIZE_TO_RATIO:
        return SIZE_TO_RATIO[size]
    return ratio


def _model_version(model) -> str | None:
    """识别请求里显式声明的 seedance 版本；无法识别返回 None（交给时长规则选择）。"""
    m = (model or "").lower().replace("_", "-")
    if "2.5" in m or "2-5" in m:
        return "seedance-2.5"
    if "2.0" in m or "2-0" in m or "v2.0" in m:
        return "seedance-2.0"
    return None


def _resolve_model_for_duration(model, duration) -> str | None:
    """模型-时长组合归一化；无法组合时返回 None。

    - 请求里写明了 2.5/2.0 版本 → 只允许该版本支持的时长，避免悄悄换型号。
    - 模型名无法识别（如 __schema_probe_invalid_model__）→ 按时长挑一个能生成的版本：
      5/10/15 秒走 seedance-2.0，30 秒与非原生时长走 seedance-2.5；
      10 秒两种都支持时默认 seedance-2.0（消耗点数更低）。
    """
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        return None
    version = _model_version(model)
    if version is None:
        if duration in config.V20_DURATIONS:
            version = "seedance-2.0"
        else:
            # 30 秒一直是 2.5；任意时长（非原生档位）也只有 2.5 能出。
            version = "seedance-2.5"
    if not _duration_supported(version, duration):
        return None
    return version


def _supported_durations_for(version: str) -> list[int]:
    """某个 seedance 版本当前能下发的时长。

    用 config.all_durations() 而不是模块级常量 ALL_DURATIONS：后者是 import 时
    求值的快照，运行期调整 NATIVE_DURATION_MAX 不会反映出来。
    """
    if version == "seedance-2.5":
        return config.all_durations()
    return list(config.V20_DURATIONS)


def _duration_supported(version: str, duration: int) -> bool:
    """时长在当前白名单下是否可下发。

    原生档位**永远放行** —— 尤其 30 秒，绝不能被 NATIVE_DURATION_MAX 夹掉
    （夹了就是把 30 秒片悄悄变成 15 秒片，线上主力流量会崩）。
    非原生档位只在开了任意时长、落在区间内、且走 2.5 时才放行。
    """
    if duration in config.NATIVE_DURATIONS:
        return True
    if config.NATIVE_DURATION_MAX <= 0:
        return False
    if not (config.MIN_DURATION <= duration <= config.NATIVE_DURATION_MAX):
        return False
    return version == "seedance-2.5"


def _normalize_model(model: str) -> str:
    """宽松归一化：显式 2.5 → seedance-2.5，其余一律 seedance-2.0。"""
    return _model_version(model) or "seedance-2.0"


def _duration_cost(model: str, duration: int) -> int:
    """单次出片消耗的额度点数：只看模型，不看时长（对齐 dola-pool-cookie）。

    未知模型按最贵的 DEFAULT_CREDIT_COST 算。旧实现是「矩阵查不到就返回 1 点」，
    属于**少扣**：一旦放开任意时长，每个未登记的 (模型, 时长) 组合都会被按 1 点
    记账，一个号一天能跑 4 条。duration 参数保留仅为兼容调用方签名。
    """
    return config.MODEL_COSTS.get(_normalize_model(model), config.DEFAULT_CREDIT_COST)


def _pick_duration(requested, allowed: list[int]) -> int:
    """把 Ark 请求的时长映射到允许档位，优先取不小于请求值的档位。"""
    try:
        requested = max(1, int(requested or 0))
    except (TypeError, ValueError):
        requested = 10
    allowed = sorted(allowed) or list(SUPPORTED_DURATIONS)
    for duration in allowed:
        if duration >= requested:
            return duration
    return allowed[-1]


def _normalize_image_refs(*fields) -> list[str]:
    """兼容各画布工具常见的图片字段形态（字符串/列表/{"url": ...}）。"""
    out: list[str] = []
    for value in fields:
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                out.append(value.strip())
        elif isinstance(value, dict):
            url = value.get("url")
            if isinstance(url, str) and url.strip():
                out.append(url.strip())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    out.append(item.strip())
                elif isinstance(item, dict):
                    url = item.get("url")
                    if isinstance(url, str) and url.strip():
                        out.append(url.strip())
    seen = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def _deep_image_urls(value, out: list[str] | None = None, depth: int = 0) -> list[str]:
    """递归抓取请求里任意层级的图片 URL（兼容画布工具的各种嵌套格式）。"""
    if out is None:
        out = []
    if depth > 8 or len(out) >= 20 or value is None:
        return out
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("http://", "https://", "data:image/")) and s not in out:
            out.append(s)
        return out
    if isinstance(value, dict):
        keys = [str(k).lower() for k in value]
        has_image_ctx = (
            any("image" in k or "ref" in k or "参考" in k for k in keys)
            or "image" in str(value.get("type", "")).lower()
        )
        for k, v in value.items():
            kk = str(k).lower()
            if isinstance(v, str):
                s = v.strip()
                if ((has_image_ctx or kk in ("url", "uri", "image", "image_url", "file_url", "src"))
                        and s.startswith(("http://", "https://", "data:image/")) and s not in out):
                    out.append(s)
            elif isinstance(v, (dict, list)):
                _deep_image_urls(v, out, depth + 1)
        return out
    if isinstance(value, list):
        for x in value:
            _deep_image_urls(x, out, depth + 1)
    return out


def _log_image_fields(raw, endpoint: str) -> None:
    """把疑似带图的请求原始结构打日志，便于定位画布工具实际传图字段。"""
    if not isinstance(raw, dict):
        return
    try:
        sample = json.dumps(raw, ensure_ascii=False)
    except Exception:
        return
    markers = ('"image', "image_url", '"url"', '"content"', '"input', 'data:image', 'reference')
    if not any(m in sample for m in markers):
        return
    keys = sorted(str(k) for k in raw.keys())
    print(
        f"[imgreq] {endpoint} top_keys={json.dumps(keys, ensure_ascii=False)} "
        f"fields={len(keys)}",
        flush=True,
    )


def _map_ark_ratio(ratio) -> str | None:
    """Ark 的 ratio 直接透传；adaptive/未知值回落 dola 默认。"""
    if not ratio:
        return None
    value = str(ratio).strip().lower()
    if value in ("adaptive", "auto", "default"):
        return None
    return value if value in {"16:9", "9:16", "1:1", "4:3", "3:4"} else None


ACTIVE_STUDIO_JOBS: set[str] = set()


def _uncertain_task(task_id, error="Chưa lấy được kết quả — có thể Dola vẫn đang tạo"):
    store.update(task_id, status="unknown", phase="uncertain", error=error, finished_at=None)


def _task_file_exists(row):
    return _stored_video_path(row) is not None


def _target_duration(row):
    seen = set()
    target = row.get("duration") or 30
    while row.get("parent_task_id") and row["parent_task_id"] not in seen:
        seen.add(row["parent_task_id"])
        row = store.get(row["parent_task_id"]) or {}
        target = row.get("duration") or target
    return target


async def _save_video_result(task_id, result):
    row = store.get(task_id) or {}
    path = Path(result["local_path"])
    # Reservation is persisted before child creation; repair a crash between create/link.
    if not row.get("parent_task_id"):
        parent = store.continuation_parent(task_id)
        if parent:
            row["parent_task_id"] = parent["id"]
            store.update(task_id, parent_task_id=parent["id"])
    if row.get("parent_task_id"):
        parent = store.get(row["parent_task_id"])
        first = Path(config.DOWNLOAD_DIR) / Path(urlparse(parent["video_url"]).path).name
        if not first.is_file():
            raise RuntimeError("Video đầu đã mất; giữ đoạn mới, chưa thể ghép")
        from pure_api_gen import _concat_videos
        merged = Path(config.DOWNLOAD_DIR) / f"{task_id}_merged.mp4"
        path = await asyncio.to_thread(_concat_videos, [first, path], merged)
    meta = await asyncio.to_thread(_video_metadata, str(path), row.get("duration"), row.get("ratio"))
    store.update(task_id, status="completed", phase="done", accounting_done=1,
                 video_url=f"{config.PUBLIC_BASE}/videos/{path.name}",
                 account=result.get("account") or row.get("account"),
                 last_poll_at=time.time(), finished_at=time.time(), error="", **meta)
    try:
        await asyncio.to_thread(_copy_video_export, task_id)
    except (OSError, ValueError) as exc:
        # An optional export failure must never hide an already received video.
        store.update(task_id, export_error=failure_text.sanitize_details(str(exc)))


async def _run_task(task_id, model, prompt, ratio, duration, reference_images, client):
    if task_id in ACTIVE_STUDIO_JOBS:
        return
    row = store.get(task_id)
    if row and row.get("conversation_id"):
        return await _resume_task(row)
    if row and (row.get("submission_started_at") or row.get("status") == "unknown"):
        _uncertain_task(task_id, "Chưa xác định Dola đã nhận yêu cầu hay chưa; không tự gửi lại")
        return
    ACTIVE_STUDIO_JOBS.add(task_id)
    api_key_hash = client.get("api_key_hash")
    acquired = False
    reference_root = None
    try:
        # 重新受理：排队时钟（updated_at）与上一轮的报错一起清零。看门狗按「最后一次状态
        # 变化」算排队时长，不清零的话刚被重启捞回来的任务会带着旧的 updated_at 立刻被判
        # 「排队超时」——把刚修好的恢复路径又堵死。
        store.update(task_id, status="queued", phase="waiting", error="")
        while pool.inventory_waiting:
            store.update(task_id, phase="waiting", error="Chờ tài khoản — hãy bật kho hoặc bật tài khoản trong Cài đặt")
            await asyncio.sleep(1)
        store.update(task_id, error="")
        await key_limiter.acquire(api_key_hash, client.get("concurrency_limit", 0))
        acquired = True
        # 这里不能急着标 processing：接下来 pool.generate_video 还可能在全局并发槽 /
        # 账号锁上等待，那段等待必须让客户端看到 queued（排队中）。真正拿到账号
        # （= 拿到并发槽）的那一刻由下面的 on_account_try 置为 processing。

        def on_conversation_id(account, conversation_id, deadline_at):
            store.update(task_id, status="processing", account=account,
                         conversation_id=conversation_id, phase="generating", deadline_at=deadline_at,
                         last_poll_at=time.time())

        def on_poll(now, phase=None):
            store.record_poll(task_id, now, phase)

        tried_accs: list[str] = []

        def on_account_try(account, attempt):
            # 拿到账号即拿到并发槽 —— 此刻才算真正开始生成（等待期间保持 queued）。
            tried_accs.append(account)
            fields = dict(status="processing", phase="sending", submission_started_at=time.time(), account=account, attempt=attempt,
                          attempted_accounts=json.dumps(tried_accs, ensure_ascii=False),
                          last_poll_at=time.time())
            if len(tried_accs) == 1:
                # started_at 只记首次尝试，否则换号会把出片耗时均值（ETA）拉长
                fields["started_at"] = time.time()
            store.update(task_id, **fields)

        reference_root, reference_paths = await download_reference_images(
            reference_images or [], task_id)
        if reference_paths:
            # 趁原图还在（终态会被清理）先存一份缩略图，供面板回看
            thumbs = await asyncio.to_thread(
                save_reference_thumbnails, reference_paths, task_id)
            if thumbs:
                store.update(task_id, reference_thumbs=json.dumps(thumbs))
        # 任务总时限内持续换号；到点不再派发新尝试。兜底超时额外留一次完整出片的等待时间，
        # 避免把已经在 Dola 端生成中的那一次强行中断（会白扣额度）。
        task_deadline = time.time() + config.OBSERVATION_SECONDS
        result = await pool.generate_video(
            prompt, ratio, duration, model,
            on_conversation_id=on_conversation_id, on_poll=on_poll,
            reference_image_paths=reference_paths,
            on_account_try=on_account_try, max_attempts=VIDEO_MAX_ATTEMPTS,
            deadline=task_deadline)
        store.update(task_id, phase="downloading")
        await _save_video_result(task_id, result)
        # 出片目录超限就从旧到新删，避免磁盘被撑满（同样是同步目录遍历）
        await asyncio.to_thread(_prune_downloads)
    except (AllAccountsLimitedError, AllAccountsQuotaBlockedError) as e:
        store.update(task_id, status="failed", error=failure_text.summarize(str(e)),
                     failure_code="429", finished_at=time.time())
    except ContentRefusalError as e:
        store.update(
            task_id,
            status="failed",
            error=failure_text.summarize(str(e)),
            failure_code=e.failure_code or "",
            failure_group=e.failure_group or "content_refusal",
            upstream_request_id=e.upstream_request_id or "",
            finished_at=time.time(),
        )
    except asyncio.CancelledError:
        row = store.get(task_id) or {}
        if row.get("conversation_id"):
            store.update(task_id, status="queued", phase="generating", last_poll_at=0)
        elif row.get("submission_started_at"):
            _uncertain_task(task_id, "Yêu cầu bị gián đoạn khi gửi; không tự gửi lại")
        else:
            store.update(task_id, status="queued", phase="waiting")
        raise
    except ConfirmedVideoFailure as e:
        display = failure_text.present(str(e), code=str(getattr(e, "failure_code", "") or ""),
                                       category=str(getattr(e, "failure_group", "") or ""))
        store.update(task_id, status="failed", phase="failed", error=failure_text.sanitize_details(str(e)),
                     failure_group=display["category"], failure_code=display["upstream_code"], finished_at=time.time())
    except TimeoutError as e:
        _uncertain_task(task_id)
    except Exception as e:
        msg = str(e)
        kind = classify_upstream_failure(msg)
        code = getattr(e, "error_code", None) or getattr(e, "status_code", None) or ""
        req_id = getattr(e, "request_id", None) or getattr(e, "upstream_request_id", None) or ""
        group = failure_text.label_for(msg) or ("content_refusal" if kind == "content" else "")
        row = store.get(task_id) or {}
        if kind != "content" and (row.get("conversation_id") or row.get("submission_started_at")):
            _uncertain_task(task_id)
            return
        store.update(
            task_id,
            status="failed",
            error=failure_text.summarize(msg),
            failure_code=str(code) if code else "",
            failure_group=str(group) if group else "",
            upstream_request_id=str(req_id) if req_id else "",
            finished_at=time.time(),
        )
    finally:
        ACTIVE_STUDIO_JOBS.discard(task_id)
        if reference_root:
            shutil.rmtree(reference_root, ignore_errors=True)
        # 落盘的参考图只在任务**走到终态**时清理：任务被取消/服务重启时它还要留给
        # resume 用（库里存的是路径，文件删早了恢复时就报「参考图文件已丢失」）。
        row = store.get(task_id)
        if row and (row.get("status") == "failed" or (row.get("status") == "completed" and not (row.get("video_duration") and row["video_duration"] < (row.get("duration") or 0) * 0.9))):
            cleanup_local_references(reference_images or [])
            store.update(task_id, reference_images="[]")
        if acquired:
            await key_limiter.release(api_key_hash)


async def _resume_task(row: dict):
    task_id = row["id"]
    account = row.get("account")
    if task_id in ACTIVE_STUDIO_JOBS:
        return
    if not account or not row.get("conversation_id"):
        _uncertain_task(task_id, "Thiếu thông tin phục hồi; không tự gửi lại")
        return
    deadline = row.get("deadline_at") or (time.time() + config.OBSERVATION_SECONDS)
    if deadline <= time.time():
        _uncertain_task(task_id)
        return
    remaining = max(1, int(deadline - time.time()))
    ACTIVE_STUDIO_JOBS.add(task_id)
    api_key_hash = row.get("api_key_hash")
    acquired = False
    try:
        await key_limiter.acquire(
            api_key_hash, int(row.get("client_concurrency_limit") or 0)
        )
        acquired = True
        store.update(task_id, status="processing", last_poll_at=time.time(),
                     started_at=row.get("started_at") or time.time())

        def on_poll(now, phase=None):
            store.record_poll(task_id, now, phase)

        result = await pool.resume_video(
            row["account"], row["conversation_id"], remaining, on_poll=on_poll,
            duration=row.get("duration"),
            ratio=None if row.get("ratio") == "default" else row.get("ratio"),
            cost=0 if row.get("accounting_done") else _duration_cost(row.get("model"), row.get("duration") or 10))
        await _save_video_result(task_id, result)
    except asyncio.CancelledError:
        store.update(task_id, status="queued", phase="generating", last_poll_at=0)
        raise
    except ConfirmedVideoFailure as e:
        display = failure_text.present(str(e), code=str(getattr(e, "failure_code", "") or ""),
                                       category=str(getattr(e, "failure_group", "") or ""))
        store.update(task_id, status="failed", phase="failed", error=failure_text.sanitize_details(str(e)),
                     failure_group=display["category"], failure_code=display["upstream_code"], finished_at=time.time())
    except Exception as e:
        _uncertain_task(task_id)
    finally:
        ACTIVE_STUDIO_JOBS.discard(task_id)
        if acquired:
            await key_limiter.release(api_key_hash)


def _task_client(row: dict) -> dict:
    """从任务快照恢复调度所需的客户上下文，不依赖 Key 当前是否仍存在。"""
    return {
        "api_key_hash": row.get("api_key_hash"),
        "api_key_name": row.get("api_key_name") or "历史任务",
        "daily_limit": 0,
        "concurrency_limit": int(row.get("client_concurrency_limit") or 0),
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _task_reference_thumbs(raw) -> list[str]:
    """任务行里的缩略图文件名列表（坏数据当空）。"""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data if x] if isinstance(data, list) else []


def _task_reference_images(raw) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) else []


_THREAD_POOL: ThreadPoolExecutor | None = None


def open_thread_pool() -> int:
    """把 asyncio 的默认线程池开大：出片/探测都跑在 to_thread 里。

    Python 默认只有 min(32, CPU+4) 个线程 —— 不限并发时它才是真正的瓶颈
    （第 33 个任务只能排队等）。0 = 自动，取 max(64, CPU×8)。
    """
    global _THREAD_POOL
    limit = int(getattr(config, "THREAD_POOL_MAX", 0) or 0)
    workers = limit if limit > 0 else max(64, (os.cpu_count() or 4) * 8)
    _THREAD_POOL = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dola")
    asyncio.get_running_loop().set_default_executor(_THREAD_POOL)
    print(f"[startup] 线程池 {workers} 路（DOLA_THREAD_POOL_MAX={limit}，0=自动）", flush=True)
    return workers


@app.on_event("startup")
async def resume_incomplete_tasks():
    """服务重启后恢复已受理会话，并重新排队尚未开始的 queued 任务。"""
    open_thread_pool()
    # 先处理崩溃时留下的 processing 孤儿任务（有 conversation 保留续轮询，
    # 无 conversation 清空账号重新派号），避免永久卡死。
    stale_refs = sweep_stale_references()
    if stale_refs:
        print(f"[startup] 清理 {stale_refs} 个残留参考图文件", flush=True)
    # 启动也跑一次保留期清理（进程长期不重启时靠每日循环兜着）
    try:
        await asyncio.to_thread(_maintenance)
    except Exception as exc:
        print(f"[startup] 维护任务失败: {exc}", flush=True)
    recovered = store.recover_runtime_jobs()
    if recovered:
        print(f"[startup] 恢复 {len(recovered)} 个运行中任务并重新排队", flush=True)
    for row in store.recoverable_tasks():
        asyncio.create_task(_resume_task(row))
    requeued = store.recoverable_queued_tasks()
    if requeued:
        print(f"[startup] 重新受理 {len(requeued)} 个没有账号的任务"
              "（含被重启取消、只剩会话的）", flush=True)
    for row in requeued:
        ratio = row.get("ratio")
        if ratio == "default":
            ratio = None
        asyncio.create_task(_run_task(
            row["id"], row["model"], row["prompt"], ratio, row["duration"],
            _task_reference_images(row.get("reference_images")), _task_client(row),
        ))
    asyncio.create_task(_cred_refresher_loop())
    asyncio.create_task(_daily_quota_reset_loop())
    asyncio.create_task(_task_watchdog_loop())


WATCHDOG_DISPATCHED: set[str] = set()
WATCHDOG_RETRIES: dict[str, int] = {}


async def _task_watchdog_loop() -> None:
    """看门狗：把「挂着不动」和「排队等太久」的任务收掉，别让客户端一直等。

    背景：服务重启会把 asyncio 里正在跑的 `_run_task` 取消掉（CancelledError 不会被
    `except Exception` 捕获），数据库里那些行就留在 processing，客户端看起来像挂起。
    这里定期巡检：
    - processing 且超过 STALE_TASK_SECONDS 没有任何轮询更新 → 重新派发（最多 3 次）
    - 还没提交上游（没有 conversation）却等了超过 MAX_QUEUE_WAIT_SECONDS → 明确失败
    """
    while True:
        await asyncio.sleep(max(15, config.WATCHDOG_INTERVAL_SECONDS))
        try:
            actions = await asyncio.to_thread(_watchdog_plan)
        except Exception as exc:  # noqa: BLE001
            print(f"[watchdog] 巡检失败: {exc}", flush=True)
            continue
        for action in actions:
            row = action["row"]
            if action["kind"] == "resume":
                asyncio.create_task(_resume_task(row))
            else:
                asyncio.create_task(_run_task(
                    row["id"], row["model"], row["prompt"],
                    None if row.get("ratio") == "default" else row.get("ratio"),
                    row.get("duration"),
                    _task_reference_images(row.get("reference_images")),
                    _task_client(row),
                ))


def _watchdog_plan() -> list[dict]:
    """只做数据库判定，返回需要上层重新派发的动作（线程里跑，不碰事件循环）。"""
    info = store.stale_pending_tasks(
        stale_seconds=config.STALE_TASK_SECONDS,
        wait_cap_seconds=config.MAX_QUEUE_WAIT_SECONDS,
    )
    failed_ids: set[str] = set()
    for row in info["waiting"]:
        task_id = row["id"]
        if pool.inventory_waiting and not row.get("submission_started_at"):
            continue
        since = row.get("queued_since") or row["created_at"] or time.time()
        minutes = (time.time() - float(since)) / 60
        print(f"[watchdog] {task_id} 排队 {minutes:.0f} 分钟仍没轮到（上游持续限流），判失败",
              flush=True)
        store.update(task_id, status="failed", finished_at=time.time(),
                     error=f"排队等待超过 {config.MAX_QUEUE_WAIT_SECONDS // 60} 分钟仍未拿到可用账号"
                           "（上游限流），请稍后重试")
        failed_ids.add(task_id)
        WATCHDOG_DISPATCHED.discard(task_id)
        WATCHDOG_RETRIES.pop(task_id, None)

    actions: list[dict] = []
    for row in info["stuck"] + info["orphans"]:
        task_id = row["id"]
        if task_id in failed_ids:
            continue
        if task_id in WATCHDOG_DISPATCHED or task_id in ACTIVE_STUDIO_JOBS:
            continue
        full = store.get(task_id) or {}
        if not full.get("conversation_id") and (full.get("submission_started_at") or full.get("account")):
            _uncertain_task(task_id, "Chưa xác định Dola đã nhận yêu cầu hay chưa; không tự gửi lại")
            continue
        tries = WATCHDOG_RETRIES.get(task_id, 0) + 1
        WATCHDOG_RETRIES[task_id] = tries
        if tries > 3:
            print(f"[watchdog] {task_id} 已重派 {tries - 1} 次仍无进展，判失败", flush=True)
            _uncertain_task(task_id)
            WATCHDOG_RETRIES.pop(task_id, None)
            continue
        full = store.get(task_id)
        if not full:
            continue
        WATCHDOG_DISPATCHED.add(task_id)
        print(f"[watchdog] {task_id} 卡住无进展（第 {tries} 次重派）", flush=True)
        kind = "resume" if (full.get("conversation_id") and full.get("account")) else "submit"
        actions.append({"kind": kind, "row": full})
    # 派发记录只保留仍在排队的任务，避免集合无限增长
    pending_ids = {row["id"] for row in info["stuck"] + info["orphans"] + info["waiting"]}
    WATCHDOG_DISPATCHED.intersection_update(pending_ids)
    return actions


async def _daily_quota_reset_loop() -> None:
    """每天 LIMIT_RESET_HOUR 点（默认日本时间 00:00）自动恢复全池额度。"""
    first = True
    while True:
        delay = max(30.0, pool.next_quota_reset_at() - time.time())
        if first:
            first = False
            print(
                f"[reset] 每日额度重置已排程：{config.LIMIT_RESET_TZ} "
                f"每天 {config.LIMIT_RESET_HOUR}:00，下次 {delay / 3600:.1f} 小时后",
                flush=True,
            )
        await asyncio.sleep(delay)
        try:
            count = pool.reset_daily_quotas()
            print(
                f"[reset] 每日额度重置完成（{config.LIMIT_RESET_TZ} "
                f"{config.LIMIT_RESET_HOUR}:00），恢复 {count} 个账号",
                flush=True,
            )
        except Exception as exc:
            print(f"[reset] 每日额度重置失败: {exc}", flush=True)
        try:
            await asyncio.to_thread(_maintenance)
        except Exception as exc:
            print(f"[maint] 维护任务失败: {exc}", flush=True)


def _prune_downloads() -> dict:
    """出片目录超过体积上限就按 mtime 从旧到新删，避免把磁盘撑满。"""
    limit = config.DOWNLOAD_MAX_BYTES
    root = Path(config.DOWNLOAD_DIR)
    if limit <= 0 or not root.is_dir():
        return {}
    items = []
    for path in root.iterdir():
        try:
            if path.is_file():
                items.append((path.stat().st_mtime, path.stat().st_size, path))
        except OSError:
            continue
    total = sum(size for _, size, _ in items)
    if total <= limit:
        return {}
    removed = 0
    for _, size, path in sorted(items):
        if total <= limit:
            break
        try:
            path.unlink()
            total -= size
            removed += 1
        except OSError:
            continue
    return {"removed": removed, "total": total} if removed else {}


def _maintenance() -> None:
    """保留期清理：删过期任务记录 + 对应视频文件，再 VACUUM 还盘。

    目的：tasks.db 和 downloads/ 都不能无限增长 —— 2026-09-22 就是库被撑到 587MB
    把面板接口顶成 MemoryError 的。
    """
    removed = store.prune_finished(config.TASK_RETENTION_DAYS)
    files = 0
    for row in removed:
        delete_reference_thumbnails(_task_reference_thumbs(row.get("reference_thumbs")))
        name = (row.get("video_url") or "").rsplit("/", 1)[-1]
        if not name:
            continue
        target = Path(config.DOWNLOAD_DIR) / name
        try:
            if target.is_file():
                target.unlink()
                files += 1
        except OSError:
            continue
    if removed:
        store.vacuum()
    stats = store.storage_stats()
    print(
        "[maint] 清理 %d 条过期任务（保留 %s 天）/ %d 个视频文件，"
        "库 %.1f MB / 共 %d 条" % (len(removed), config.TASK_RETENTION_DAYS, files,
                                   stats["db_bytes"] / 1048576, stats["tasks"]),
        flush=True,
    )
    pruned = _prune_downloads()
    if pruned:
        print("[maint] 出片目录超过 %.0f MB，删除 %d 个旧文件"
              % (config.DOWNLOAD_MAX_BYTES / 1048576, pruned["removed"]), flush=True)


@app.post("/v1/videos/generations", response_model=TaskResponse)
async def create_video(request: Request, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        # 画布（infinite-canvas 之类）走 Sora 风格 multipart，字段名和 JSON 路径不一样
        raw = await _raw_from_multipart(request)
    else:
        try:
            raw = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
    _log_image_fields(raw, "openai")
    try:
        req = VideoGenRequest.model_validate(raw)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    # 时长来源优先级：duration → seconds → duration_seconds（new-api 的 sora 插件只发 seconds）。
    duration = req.duration or req.seconds or req.duration_seconds or 10
    ratio = _resolve_ratio(req.size, req.ratio)
    refs = _normalize_image_refs(
        req.reference_images, req.image_url, req.image,
        req.input_image, req.input_images,
    )
    refs = list(dict.fromkeys(refs + _deep_image_urls(raw)))
    if refs:
        print(f"[api] 收到参考图 {len(refs)} 张: {refs[0][:80]}", flush=True)
    return await _submit_task(client, req.model, req.prompt, ratio, duration, refs)


def _data_url_from_upload(filename: str, data: bytes) -> str:
    """画布上传的参考图折成 data: URL —— 下游 validate_reference_urls 本来就支持这种格式。"""
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(suffix, "png")
    return "data:image/%s;base64,%s" % (mime, base64.b64encode(data).decode())


async def _raw_from_multipart(request: Request) -> dict[str, Any]:
    """把 Sora/画布风格的 multipart 表单折成和 JSON 路径同构的 dict。

    - model / prompt 直取；
    - seconds（画布的时长字段，字符串）→ duration；
    - size → 交给 _resolve_ratio 归一；
    - image[] / image / input_image / first_frame / last_frame 文件 → data: URL 进 reference_images；
    - video[] / audio[] 本项目暂不支持，记日志忽略。
    """
    form = await request.form()
    raw: dict[str, Any] = {}
    for key in ("model", "prompt"):
        value = form.get(key)
        if isinstance(value, str) and value.strip():
            raw[key] = value.strip()
    for key in ("duration", "seconds", "duration_seconds"):
        value = form.get(key)
        if isinstance(value, str) and value.strip():
            try:
                raw["duration"] = int(float(value))
                break
            except ValueError:
                continue
    for key in ("size", "ratio", "aspect_ratio"):
        value = form.get(key)
        if isinstance(value, str) and value.strip():
            raw[key] = value.strip()
    refs: list[str] = []
    for key in ("image[]", "image", "input_image", "first_frame", "last_frame"):
        for item in form.getlist(key):
            if hasattr(item, "read"):
                data = await item.read()
                if data:
                    refs.append(_data_url_from_upload(
                        getattr(item, "filename", "") or "", data))
            elif isinstance(item, str) and item.strip():
                refs.append(item.strip())
    ignored = [key for key in ("video[]", "audio[]") if form.getlist(key)]
    if ignored:
        print(f"[api] 画布 multipart 带了 {ignored}，本项目暂不支持参考视频/音频，已忽略", flush=True)
    if refs:
        raw["reference_images"] = refs
    return raw


@app.post("/v1/videos", response_model=TaskResponse, status_code=202)
async def create_video_vinted_alias(request: Request,
                                    authorization: str | None = Header(default=None)):
    """Vinted/New API 兼容入口：与 OpenAI 路径同逻辑，但返回 202 Accepted。"""
    return await create_video(request, authorization)


async def _submit_task(client, model, prompt, ratio, duration, reference_images, task_id=None, input_fingerprint=None, source_task_id=None):
    """公共受理逻辑：校验模型/时长/账号池 -> 入队 -> 后台跑，返回 TaskResponse。"""
    resolved = _resolve_model_for_duration(model, duration)
    if resolved is None:
        version = _model_version(model)
        if version is not None:
            supported = _supported_durations_for(version)
            raise HTTPException(
                422,
                f"{version} 仅支持 {'/'.join(map(str, supported))} 秒视频"
                f"（收到 {duration} 秒）",
            )
        raise HTTPException(
            422,
            "不支持的模型/时长组合："
            f"模型={model!r}，时长={duration}秒"
            f"（seedance-2.5 支持 {_supported_durations_for('seedance-2.5')} 秒，"
            f"seedance-2.0 仅支持 {list(config.V20_DURATIONS)} 秒）",
        )
    model = resolved
    if task_id:
        existing = store.get(task_id)
        if existing:
            if (existing["prompt"], existing["model"], existing["duration"], existing["ratio"]) != (prompt, model, duration, ratio or "default"):
                raise HTTPException(409, "Yêu cầu đã có nhưng bản nháp khác; hãy tạo yêu cầu mới")
            return TaskResponse(**{k: existing.get(k) for k in ("id","status","model","prompt","video_url","error")})
    if duration not in client["allowed_durations"]:
        raise HTTPException(422, f"Current API key does not allow {duration}s video generation")
    try:
        reference_images = await validate_reference_urls(reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # data: URL（客户端内联的参考图）先落盘：几 MB 的 base64 绝不能进 tasks.db，
    # 否则任务列表接口要序列化几百 MB，直接把进程顶成 MemoryError / 卡死。
    try:
        reference_images = await asyncio.to_thread(materialize_data_urls, reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # 账号暂时忙时允许任务进入 queued，由 BrowserPool 的全局并发控制实际排队。
    # 只有明确全部达到每日上限/积分不足时才立即拒绝。
    if not pool.available and pool.all_accounts_limited:
        raise HTTPException(429, "Rate limit: All scheduled accounts have reached Dola daily limit. Please try again tomorrow.")
    if not pool.available and pool.all_accounts_quota_blocked:
        raise HTTPException(429, "Insufficient credits: All scheduled accounts have insufficient points. Please wait for quota reset.")
    if not pool.accounts:
        raise HTTPException(503, "no account in pool")
    task_id = task_id or "video_" + uuid.uuid4().hex
    if len(prompt or "") > config.MAX_PROMPT_CHARS:
        raise HTTPException(
            422, f"提示词过长（上限 {config.MAX_PROMPT_CHARS} 字符，收到 {len(prompt)}）")
    existing = store.get(task_id)
    if existing:
        cleanup_local_references(reference_images)
        return TaskResponse(**{k: existing.get(k) for k in ("id","status","model","prompt","video_url","error")})
    try:
        store.create(
            task_id,
            model,
            prompt,
            ratio or "default",
            duration,
            reference_images=json.dumps(reference_images, ensure_ascii=False),
            api_key_hash=client["api_key_hash"],
            api_key_name=client["api_key_name"],
            daily_limit=client["daily_limit"],
            concurrency_limit=client["concurrency_limit"],
            max_pending=config.MAX_PENDING_TASKS,
            source_task_id=source_task_id,
        )
    except TaskQuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except PendingTaskLimitExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    if input_fingerprint:
        store.update(task_id, input_fingerprint=input_fingerprint)
    asyncio.create_task(_run_task(
        task_id, model, prompt, ratio, duration, reference_images, client
    ))
    return TaskResponse(
        id=task_id, status="queued", model=model, prompt=prompt,
        progress=_progress_payload(status="queued", created_at=time.time(),
                                   duration=duration),
    )


@app.get("/v1/videos/{task_id}", response_model=TaskResponse)
async def get_video(
    task_id: str,
    authorization: str | None = Header(default=None),
    x_admin_key: str | None = Header(default=None),
):
    if x_admin_key:
        _admin_auth(x_admin_key)
        row = store.get(task_id)
    else:
        client = _auth(authorization)
        row = store.get_for_client(task_id, client["api_key_hash"])
    if not row:
        raise HTTPException(404, "task not found")
    return TaskResponse(
        id=row["id"], status=row["status"], model=row["model"],
        prompt=row["prompt"], video_url=row["video_url"], error=row["error"],
        progress=_progress_payload(status=row["status"], created_at=row["created_at"],
                                   duration=row["duration"]),
    )



@app.get("/v1/videos/{task_id}/content")
async def get_video_content(task_id: str, authorization: str | None = Header(default=None)):
    """new-api 任务插件兼容端点：把已完成的视频文件内容返回给客户端。"""
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row:
        raise HTTPException(404, "task not found")
    if row["status"] != "completed" or not row["video_url"]:
        raise HTTPException(409, "video not ready")
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(row["video_url"]) as resp:
            if resp.status != 200:
                raise HTTPException(502, "failed to fetch video from upstream")
            content_type = resp.headers.get("Content-Type", "video/mp4")
            data = await resp.read()
    return Response(content=data, media_type=content_type)



@app.get("/v1/models")
async def list_models():
    """OpenAI 风格模型列表，供 new-api 等上游获取模型用。"""
    return {
        "object": "list",
        "data": [
            {"id": "seedance-2.0", "object": "model", "owned_by": "dola-pool", "created": 0},
            {"id": "seedance-2.5", "object": "model", "owned_by": "dola-pool", "created": 0},
        ],
    }


# ===== 火山 Ark 任务式协议兼容端点（画布工具） =====

ARK_STATUS_MAP = {
    "queued": "queued",
    "processing": "running",
    "completed": "succeeded",
    "failed": "failed",
}


async def ark_create_task(body: dict, authorization: str | None = Header(default=None)):
    """POST .../contents/generations/tasks：Ark 风格创建，复用 dola 出片流程。"""
    client = _auth(authorization)
    body = body or {}
    _log_image_fields(body, "ark")
    prompt_parts: list[str] = []
    reference_images: list[str] = []
    for item in body.get("content") or []:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").lower()
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if text:
                prompt_parts.append(text)
        elif item_type in ("image_url", "image"):
            url_obj = item.get("image_url")
            url = url_obj.get("url") if isinstance(url_obj, dict) else url_obj
            if isinstance(url, str) and url.strip():
                reference_images.append(url.strip())
    # 顶层兼容：部分画布工具把图片放在顶层 image_url/image 字段
    reference_images = _normalize_image_refs(
        reference_images, body.get("image_url"), body.get("image"),
        body.get("input_image"), body.get("input_images"),
    )
    reference_images = list(dict.fromkeys(reference_images + _deep_image_urls(body)))
    prompt = "\n".join(prompt_parts).strip()
    if not prompt:
        raise HTTPException(422, "Missing text prompt in content")
    requested_model = body.get("model")
    ratio = _map_ark_ratio(body.get("ratio"))
    if reference_images:
        print(f"[api] Ark 收到参考图 {len(reference_images)} 张: {reference_images[0][:80]}", flush=True)
    version = _model_version(requested_model)
    if version is not None:
        # 显式指定了版本：只在该版本支持的时长里就近取档。
        allowed = [
            d for d in client["allowed_durations"]
            if d in _supported_durations_for(version)
        ]
    else:
        # 模型名无法识别：先按请求时长取档，再由时长挑一个支持的 seedance 版本。
        allowed = [
            d for d in client["allowed_durations"]
            if d in SUPPORTED_DURATIONS
        ]
    # 部分画布工具用 seconds 传时长（new-api 的 sora 插件也会改写成 seconds）。
    duration = _pick_duration(body.get("duration") or body.get("seconds"), allowed)
    model = _resolve_model_for_duration(requested_model, duration)
    if model is None:
        raise HTTPException(
            422,
            "不支持的模型/时长组合："
            f"模型={requested_model!r}，时长={duration}秒"
            f"（seedance-2.5 支持 {_supported_durations_for('seedance-2.5')} 秒，"
            f"seedance-2.0 仅支持 {list(config.V20_DURATIONS)} 秒）",
        )
    resp = await _submit_task(client, model, prompt, ratio, duration, reference_images)
    return {"id": resp.id, "status": resp.status}


async def ark_get_task(task_id: str, authorization: str | None = Header(default=None)):
    """GET .../contents/generations/tasks/{id}：Ark 风格查询。"""
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row:
        raise HTTPException(404, "task not found")
    result = {
        "id": row["id"],
        "model": row["model"],
        "status": ARK_STATUS_MAP.get(row["status"], row["status"]),
        "created_at": int(row["created_at"]),
        "updated_at": int(row.get("updated_at") or row["created_at"]),
    }
    if row["status"] == "completed" and row.get("video_url"):
        result["content"] = {"video_url": row["video_url"]}
    if row.get("error"):
        result["error"] = row["error"]
    return result


ARK_TASK_PATHS = (
    "/v1/video/contents/generations/tasks",
    "/api/v3/contents/generations/tasks",
    "/v1/contents/generations/tasks",
    "/seedance/v3/contents/generations/tasks",
)
for _ark_path in ARK_TASK_PATHS:
    app.add_api_route(_ark_path, ark_create_task, methods=["POST"])
    app.add_api_route(_ark_path + "/{task_id}", ark_get_task, methods=["GET"])


@app.get("/health")


async def health():
    meta = pool.engine_snapshot()
    storage = store.storage_stats()
    downloads = 0
    download_root = Path(config.DOWNLOAD_DIR)
    if download_root.is_dir():
        for item in download_root.iterdir():
            try:
                if item.is_file():
                    downloads += item.stat().st_size
            except OSError:
                continue
    storage["downloads_bytes"] = downloads
    storage["retention_days"] = config.TASK_RETENTION_DAYS
    queue_info = store.queue_progress(created_at=None, duration=None)
    workers = int(config.MAX_CONCURRENCY or 0)   # 0 = 不限并发
    queue = {
        "queued": store.pending_task_count() - queue_info["running"],
        "running": queue_info["running"],
        "workers": workers,
        "typical_seconds": queue_info["typical_seconds"],
        "eta_text": _eta_text(queue_info["typical_seconds"]),
    }
    return {
        "ok": True,
        "version": APP_VERSION,
        "accounts": pool.account_status(),
        "pool": meta,
        "storage": storage,
        "queue": queue,
        "available": pool.available,
        "pending_tasks": store.pending_task_count(),
        "max_pending_tasks": config.MAX_PENDING_TASKS,
    }


# ===== 管理面板 API =====


class AdminLogin(BaseModel):
    username: str
    password: str


class AdminUserCreate(BaseModel):
    username: str
    password: str
    role: str = "admin"


class AdminUserUpdate(BaseModel):
    username: str | None = None
    password: str | None = None
    enabled: bool | None = None


class ProxyCreate(BaseModel):
    name: str
    protocol: str = "http"
    # 动态IP（mode="extract"）不填网关地址也行：出口由节点池里的节点决定
    host: str = ""
    port: int = Field(0, ge=0, le=65535)
    username: str = ""
    password: str = ""
    remark: str = ""
    # static = 一条记录一个固定出口；dynamic = 隧道/旋转网关，按 username 里的
    # sticky session 决定出口 IP，每个号自动分配一个独立 session。
    mode: str = "static"
    session_template: str = ""
    sticky_ttl: int = Field(0, ge=0)
    rotate_on_risk: bool = True
    rotate_min_interval: int = Field(0, ge=0)
    # mode="extract" 时用：调这个链接拿一批可直接使用的 host:port:user:pass
    extract_url: str = ""
    extract_interval: int = Field(0, ge=0)
    # mode="extract" 时也可以直接把节点粘进来（一行一个 host:port:user:pass），
    # 与提取链接二选一即可，都填就先建代理再把粘进来的节点入池。
    nodes_text: str = ""


class ProxyUpdate(BaseModel):
    name: str | None = None
    protocol: str | None = None
    host: str | None = None
    port: int | None = Field(None, ge=0, le=65535)
    username: str | None = None
    password: str | None = None
    remark: str | None = None
    enabled: bool | None = None
    mode: str | None = None
    session_template: str | None = None
    sticky_ttl: int | None = Field(None, ge=0)
    rotate_on_risk: bool | None = None
    rotate_min_interval: int | None = Field(None, ge=0)
    extract_url: str | None = None
    extract_interval: int | None = Field(None, ge=0)


class ProxyNodesIn(BaseModel):
    # 手工粘贴的节点：一行一个 host:port:user:pass（兼容 user:pass@host:port 等）
    text: str = ""
    # 有效期（秒）：None = 带 sessiontime-N 的按它算，其余默认不过期；0 = 全部不过期
    ttl: int | None = None


class AccountProxyBind(BaseModel):
    proxy_id: str | None = None


class StressRequest(BaseModel):
    max_concurrency: int = Field(8, ge=1, le=12)
    hold_seconds: float = Field(5.0, ge=3.0, le=15.0)


class AccountPatch(BaseModel):
    pool_id: str | None = None
    scheduling: bool | None = None
    note: str | None = None
    email: str | None = None


class RecoverBody(BaseModel):
    """人工恢复：kind=risk（风控组）| abnormal（异常组）；批量恢复时带 names。"""

    kind: str = "risk"
    names: list[str] = []


class PreferBody(BaseModel):
    preferred: bool = True


class WeightBody(BaseModel):
    weight: int = Field(1, ge=1, le=100)


class BindProxiesBody(BaseModel):
    proxies: list[str]
    force: bool = False
    mode: str = "sticky"


class BatchAccountsBody(BaseModel):
    """批量操作（勾选账号后一键处理）。"""
    names: list[str] = Field(default_factory=list)


class BatchProxyBody(BaseModel):
    names: list[str] = Field(default_factory=list)
    proxy_id: str | None = None


class ImportTextBody(BaseModel):
    text: str = ""
    skip_verify: bool = False


class AccountAdd(BaseModel):
    name: str
    email: str = ""
    password: str = ""
    totp: str = ""
    cookies: str = ""
    cookie_skip_verify: bool = False
    facebook: str = ""


class AiSettingsUpdate(BaseModel):
    active_provider: str | None = None
    gemini: dict[str, Any] | None = None
    openai: dict[str, Any] | None = None


class PromptRewriteRequest(BaseModel):
    prompt: str
    instructions: str = Field(default="", max_length=4000)
    duration: int | None = None
    ratio: str | None = None
    model: str | None = None


class PromptPreflightRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=config.MAX_PROMPT_CHARS)
    model: str | None = None
    duration: int | None = None
    ratio: str | None = None
    reference_roles: list[str] = Field(default_factory=list, max_length=config.REFERENCE_IMAGE_MAX_COUNT)
    reference_images: list[str] = Field(default_factory=list, max_length=config.REFERENCE_IMAGE_MAX_COUNT)
    upstream_task_id: str | None = None


class KeyCreate(BaseModel):
    name: str = ""
    daily_limit: int = Field(0, ge=0, le=1_000_000)
    concurrency_limit: int = Field(0, ge=0, le=1_000)
    allowed_durations: list[int] = Field(default_factory=lambda: list(SUPPORTED_DURATIONS))
    expires_at: float | None = Field(None, ge=0)


class KeyPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    daily_limit: int | None = Field(None, ge=0, le=1_000_000)
    concurrency_limit: int | None = Field(None, ge=0, le=1_000)
    allowed_durations: list[int] | None = None
    expires_at: float | None = Field(None, ge=0)


@app.post("/api/admin/login")
async def admin_login(body: AdminLogin):
    user = user_verify(body.username.strip(), body.password)
    if not user:
        raise HTTPException(401, "Invalid username or password")
    token = secrets.token_urlsafe(32)
    ADMIN_SESSIONS[token] = {
        "username": user["username"],
        "role": user["role"],
        "expires_at": time.time() + ADMIN_SESSION_TTL,
    }
    return {
        "ok": True,
        "auth_required": True,
        "token": token,
        "username": user["username"],
        "role": user["role"],
    }


@app.get("/api/admin/accounts")
async def admin_accounts(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    accounts = pool.list_accounts(processing_accounts=store.busy_account_ids())
    sessions = proxy_store.sessions_snapshot()
    for acc in accounts:
        acc["group_reason_display"] = failure_text.present_account_group(acc.get("group") or "", acc.get("group_reason") or "")
        acc["note_display"] = failure_text.present_note(acc.get("note") or "")
        acc["note"] = failure_text.sanitize_details(acc.get("note") or "")
        info = get_account_proxy(acc["name"])
        acc["proxy_id"] = info["id"] if info else None
        acc["proxy_name"] = info["name"] if info else None
        acc["proxy_mode"] = info.get("mode") if info else None
        sess = sessions.get(acc["name"]) or {}
        acc["ip_session"] = sess.get("session", "")
        acc["exit_ip"] = sess.get("last_exit_ip", "")
        acc["exit_probe_at"] = sess.get("last_probe_at", 0)
        acc["exit_probe_ok"] = bool(sess.get("last_probe_ok"))
        acc["exit_probe_detail"] = sess.get("last_probe_detail", "")
    return {
        "accounts": accounts,
        "engine": pool.engine_snapshot(),
        "next_reset_at": pool.next_quota_reset_at(),
        "reset_tz": config.LIMIT_RESET_TZ,
        "reset_hour": config.LIMIT_RESET_HOUR,
        **_group_summary(accounts),
    }


def _group_summary(accounts: list) -> dict:
    """分组计数 + 【正常/有效】组的剩余总额度（面板按钮与「剩余额度」用）。"""
    counts = {name: 0 for name in GROUP_ORDER}
    for acc in accounts:
        group = acc.get("group")
        if group in counts:
            counts[group] += 1
    return {
        "groups": counts,
        "group_order": list(GROUP_ORDER),
        "all_count": len(accounts),
        "valid_count": sum(counts[g] for g in ACTIVE_GROUPS),
        "remaining_points": sum(
            int(acc.get("remaining") or 0) for acc in accounts
            if acc.get("group") in ACTIVE_GROUPS),
    }


def _running_add_jobs() -> list[str]:
    return [name for name, job in JOBS.items()
            if job.get("status") == "running"]


async def _run_stress_job(max_concurrency: int, hold_seconds: float):
    try:
        result = await run_stress(max_concurrency, hold_seconds)
        STRESS_RUN.update(
            status="completed",
            finished_at=time.time(),
            recommended=result["recommended"],
            results=result["results"],
            logs=result["logs"],
        )
    except Exception as exc:
        STRESS_RUN.update(
            status="failed",
            finished_at=time.time(),
            logs=STRESS_RUN.get("logs", []) + [f"压测异常: {exc}"],
        )


@app.get("/api/admin/stress")
async def admin_stress_status(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {
        **STRESS_RUN,
        "busy_tasks": store.pending_task_count(),
        "running_jobs": len(_running_add_jobs()),
    }


@app.post("/api/admin/stress")
async def admin_stress_start(body: StressRequest,
                             x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if STRESS_RUN.get("status") == "running":
        raise HTTPException(409, "Stress test in progress, please wait until complete")
    busy = store.pending_task_count()
    if busy:
        raise HTTPException(
            409, f"当前有 {busy} 个视频任务在排队/运行，请先等待任务结束再压测"
        )
    adding = _running_add_jobs()
    if adding:
        raise HTTPException(
            409, "当前有账号登录任务进行中：" + "、".join(adding[:5])
        )
    STRESS_RUN.clear()
    STRESS_RUN.update(
        status="running",
        started_at=time.time(),
        finished_at=None,
        recommended=None,
        results=[],
        logs=["压力测试开始：从 1 个浏览器逐级加压…"],
    )
    asyncio.create_task(_run_stress_job(body.max_concurrency, body.hold_seconds))
    return {**STRESS_RUN, "busy_tasks": 0, "running_jobs": 0}


class InventoryPoolBody(BaseModel):
    id: str | None = None
    name: str | None = None
    enabled: bool | None = None


@app.get("/api/admin/account-pools")
async def admin_inventory_pools(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"pools": pool.list_inventory_pools()}


@app.post("/api/admin/account-pools")
@app.patch("/api/admin/account-pools/{pool_id}")
async def admin_inventory_pool_set(body: InventoryPoolBody, pool_id: str = "",
                                   x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        return pool.set_inventory_pool(pool_id or body.id or "", name=body.name, enabled=body.enabled)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.patch("/api/admin/accounts/{name}")
async def admin_account_patch(name: str, body: AccountPatch,
                              x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if body.pool_id is not None:
        try:
            pool.assign_inventory_pool([name], body.pool_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if body.scheduling is not None:
        pool.set_scheduling(name, body.scheduling)
    if body.note is not None:
        pool.set_note(name, body.note)
    if body.email is not None:
        pool.set_email(name, body.email)
    return {"ok": True}


def _account_row(name: str) -> dict | None:
    """单个账号的面板视图（含分组），用于恢复/探测后回显新状态。"""
    return next((a for a in pool.list_accounts(processing_accounts=store.busy_account_ids())
                 if a["name"] == name), None)


@app.post("/api/admin/accounts/{name}/recover")
async def admin_account_recover(name: str, body: RecoverBody,
                                x_admin_key: str | None = Header(default=None)):
    """把号从【风控】/【异常】组放回正常流程（风控恢复后回到【待激活】，要重新探测）。"""
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if body.kind not in ("risk", "abnormal"):
        raise HTTPException(400, "kind must be either risk or abnormal")
    result = pool.recover_account(name, body.kind)
    return {**result, "account": _account_row(name)}


@app.post("/api/admin/accounts/{name}/probe-hello")
async def admin_account_probe_hello(name: str,
                                    x_admin_key: str | None = Header(default=None)):
    """手动发一句「你好」判风控（强制真探，不吃缓存），返回回复原文与登录标记。"""
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    probe = await pool.probe_hello_async(name, force=True)
    return {"ok": True, "probe": probe, "account": _account_row(name)}


class RenameAccount(BaseModel):
    new_name: str


@app.post("/api/admin/accounts/{name}/rename")
async def admin_account_rename(name: str, body: RenameAccount,
                               x_admin_key: str | None = Header(default=None)):
    """给账号改名（profile 目录、代理绑定、今日额度、登录凭据一起搬）。"""
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    try:
        result = pool.rename_account(name, body.new_name)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, **result}


@app.delete("/api/admin/accounts/{name}")
async def admin_account_delete(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    try:
        pool.delete_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/admin/accounts/{name}/verify")
async def admin_account_verify(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        result = await _verify_account_once(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        # 验证流程里浏览器/playwright/SQLite 等其它异常不再冒泡成 500：
        # 统一转 409 并给出原因（不落地 login_ok，避免误标，非破坏）。
        raise HTTPException(409, f"Verification failed (server exception): {str(e)[:200]}")
    return result


async def _verify_account_once(name: str) -> dict:
    """单账号验证（登录态失效时用加密凭据自动重登一次）。

    返回 {"ok": bool, "refreshed": bool, "reason": str}；账号忙 / 不存在等异常向上抛。
    reason 是失败原因（成功时为空串），面板用它显示「为什么失效」。
    """
    ok, reason = await pool.verify_account_detail(name)
    refreshed = False
    if not ok:
        creds = cred_store.load(name)
        if creds and creds.get("password"):
            try:
                async with ADD_ACCOUNT_SEM:
                    await add_account_flow(
                        name, creds["email"], creds["password"], creds.get("totp") or "",
                        force_login=True)
                pool.set_email(name, creds["email"])
                pool.set_login_status(name, True)
                ok, reason = await pool.verify_account_detail(name)
                refreshed = True
            except Exception as exc:
                ok = False
                refreshed = False
                reason = f"自动重登失败: {str(exc)[:160]}"
    return {"ok": ok, "refreshed": refreshed, "reason": reason}


def _clean_batch_names(names: list[str]) -> list[str]:
    """去空、去重、保持勾选顺序；上限 500 个防止误提交超大请求。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out[:500]


async def _run_batch_verify_job(job_id: str, names: list[str]) -> None:
    """逐个验证勾选的账号，进度写回 BATCH_JOBS 供面板轮询。"""
    job = BATCH_JOBS[job_id]
    try:
        async with BATCH_VERIFY_SEM:
            for name in names:
                job["current"] = name
                item = {"name": name, "ok": None, "message": ""}
                try:
                    if name not in pool.accounts:
                        item["message"] = "账号不存在"
                    else:
                        result = await _verify_account_once(name)
                        item["ok"] = bool(result["ok"])
                        item["message"] = "有效" if item["ok"] else "失效"
                        if result["refreshed"]:
                            item["message"] += "（已自动重登）"
                except Exception as exc:
                    item["message"] = str(exc)[:200] or "验证失败"
                job["results"].append(item)
                job["done"] = len(job["results"])
                job["ok_count"] = sum(1 for r in job["results"] if r["ok"])
                job["failed_count"] = sum(1 for r in job["results"] if r["ok"] is False)
        job["status"] = "completed"
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)[:300]
    finally:
        job["current"] = ""
        job["finished_at"] = time.time()


async def _run_batch_probe_job(job_id: str, names: list[str]) -> None:
    """批量「你好」探测：并发受 BATCH_PROBE_SEM 限制，进度写回 BATCH_JOBS 供面板轮询。

    结论口径（与单号探测一致）：
      ok         → 登录有效且有回复；
      logged_out → 被登出 → 该号进【风控】组；
      no_reply   → 一句回复都没有 → 进【风控】组；
      error      → 探测没跑成（网络不通 / 上游 710022002 拒绝）→ **不锁号**，只记"未完成"；
      skipped    → 非纯 API 账号（浏览器登录号）。
    """
    job = BATCH_JOBS[job_id]
    # 并发 0 = 不限（全部放开）；>0 才建信号量。
    # 信号量在协程内部创建：模块级 asyncio 原语会绑死在"第一个"事件循环上。
    sem = (asyncio.Semaphore(BATCH_PROBE_CONCURRENCY)
           if BATCH_PROBE_CONCURRENCY > 0 else _NoLimit())
    label = {
        "ok": "通过",
        "logged_out": "被登出 → 已进【风控】组",
        "no_reply": "没回复 → 已进【风控】组",
        "error": "探测未完成（网络/上游拒绝，不判风控）",
        "skipped": "非纯 API 账号，跳过",
    }

    async def probe_one(name: str) -> None:
        item = {"name": name, "ok": None, "status": "", "message": "", "reply": "",
                "group": "", "attempts": 0}
        try:
            if name not in pool.accounts:
                item["message"] = "账号不存在"
            else:
                async with sem:
                    job["current"] = name
                    await asyncio.sleep(BATCH_PROBE_INTERVAL_SECONDS)   # 错峰，别打爆上游
                    # 第一次只记结论、不判风控（单次"没回复"多半是上游抖动，一次就锁会误封好号）
                    result = await pool.probe_hello_async(name, force=True, apply_risk=False)
                    item["attempts"] = 1
                    if result.get("status") == "logged_out":
                        # 「被登出」是强信号，一次就判风控（重试也救不回来）
                        pool.apply_probe_risk(name, result)
                    elif result.get("status") in ("error", "no_reply"):
                        # 上游限流/网络抖动 / 没回复 → 退避重试一次，仍失败才判风控（两振出局）
                        await asyncio.sleep(BATCH_PROBE_RETRY_SECONDS)
                        result = await pool.probe_hello_async(name, force=True, apply_risk=True)
                        item["attempts"] = 2
                item["status"] = str(result.get("status") or "")
                item["ok"] = bool(result.get("ok"))
                item["reply"] = str(result.get("reply") or "")[:40]
                item["message"] = label.get(item["status"], str(result.get("reason") or "")[:120])
        except Exception as exc:
            item["message"] = str(exc)[:200] or "探测失败"
        row = _account_row(name)
        item["group"] = (row or {}).get("group", "")
        job["results"].append(item)
        job["done"] = len(job["results"])
        job["ok_count"] = sum(1 for r in job["results"] if r["ok"])
        # 只有"硬信号"才算未通过（进风控）；软失败（网络/上游拒绝）单列成"未完成"，
        # 免得把一批限流噪音显示成"一堆号被风控"。
        job["failed_count"] = sum(
            1 for r in job["results"] if r.get("status") in ("logged_out", "no_reply"))
        job["skipped_count"] = sum(
            1 for r in job["results"] if r.get("status") in ("error", "skipped"))

    try:
        await asyncio.gather(*(probe_one(n) for n in names))
        job["status"] = "completed"
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)[:300]
    finally:
        job["current"] = ""
        job["finished_at"] = time.time()


async def _run_batch_recover_job(job_id: str, names: list[str], kind: str) -> None:
    """批量把号从【风控】/【异常】组放回正常流程（风控恢复后退回【待激活】）。"""
    job = BATCH_JOBS[job_id]
    for name in names:
        item = {"name": name, "ok": None, "message": "", "group": ""}
        try:
            if name not in pool.accounts:
                item["message"] = "账号不存在"
            else:
                pool.recover_account(name, kind)
                item["ok"] = True
                item["message"] = ("已退出风控，回到【待激活】——要再探一次「你好」确认登录"
                                   if kind == "risk" else "已退出异常组")
        except Exception as exc:
            item["ok"] = False
            item["message"] = str(exc)[:200] or "恢复失败"
        row = _account_row(name)
        item["group"] = (row or {}).get("group", "")
        job["results"].append(item)
        job["done"] = len(job["results"])
        job["ok_count"] = sum(1 for r in job["results"] if r["ok"])
        job["failed_count"] = sum(1 for r in job["results"] if r["ok"] is False)
    job["status"] = "completed"
    job["current"] = ""
    job["finished_at"] = time.time()


@app.post("/api/admin/accounts/batch-recover")
async def admin_batch_recover(body: RecoverBody,
                              x_admin_key: str | None = Header(default=None)):
    """批量恢复：把选中的号从【风控】/【异常】组放出来。

    风控恢复后登录态会被清空（回到【待激活】），所以建议接着点一次「你好探测」确认登录。
    """
    _admin_auth(x_admin_key)
    kind = body.kind or "risk"
    if kind not in ("risk", "abnormal"):
        raise HTTPException(400, "kind must be either risk or abnormal")
    names = _clean_batch_names(body.names)
    if not names:
        raise HTTPException(400, "No accounts to recover. Please select accounts first.")
    running = [jid for jid, item in BATCH_JOBS.items() if item.get("status") == "running"]
    if running:
        raise HTTPException(409, "Another batch task is already in progress, please wait")
    job_id = "batch_" + uuid.uuid4().hex[:12]
    BATCH_JOBS[job_id] = {
        "kind": "recover",
        "status": "running",
        "total": len(names),
        "done": 0,
        "ok_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "results": [],
        "current": "",
        "started_at": time.time(),
        "finished_at": None,
        "error": "",
    }
    asyncio.create_task(_run_batch_recover_job(job_id, names, kind))
    return {"ok": True, "job_id": job_id, "total": len(names), "names": names}


@app.post("/api/admin/accounts/batch-probe-hello")
async def admin_batch_probe_hello(body: BatchAccountsBody,
                                  x_admin_key: str | None = Header(default=None)):
    """批量发「你好」探风控：被登出 / 没回复 → 进【风控】组；软失败不锁号。"""
    _admin_auth(x_admin_key)
    names = _clean_batch_names(body.names)
    if not names:
        raise HTTPException(400, "No accounts to probe. Please select accounts first.")
    running = [jid for jid, item in BATCH_JOBS.items() if item.get("status") == "running"]
    if running:
        raise HTTPException(409, "Another batch task is already in progress, please wait")
    job_id = "batch_" + uuid.uuid4().hex[:12]
    BATCH_JOBS[job_id] = {
        "kind": "probe",
        "status": "running",
        "total": len(names),
        "done": 0,
        "ok_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "results": [],
        "current": "",
        "started_at": time.time(),
        "finished_at": None,
        "error": "",
    }
    asyncio.create_task(_run_batch_probe_job(job_id, names))
    return {"ok": True, "job_id": job_id, "total": len(names), "names": names}


@app.post("/api/admin/accounts/batch-verify")
async def admin_batch_verify(body: BatchAccountsBody,
                             x_admin_key: str | None = Header(default=None)):
    """一键验证：对勾选的账号逐个验证登录态（后台任务，面板轮询进度）。"""
    _admin_auth(x_admin_key)
    names = _clean_batch_names(body.names)
    if not names:
        raise HTTPException(400, "Please select at least one account")
    running = [jid for jid, job in BATCH_JOBS.items() if job.get("status") == "running"]
    if running:
        raise HTTPException(409, "Batch verification already in progress, please wait")
    job_id = "batch_" + uuid.uuid4().hex[:12]
    BATCH_JOBS[job_id] = {
        "kind": "verify",
        "status": "running",
        "total": len(names),
        "done": 0,
        "ok_count": 0,
        "failed_count": 0,
        "results": [],
        "current": "",
        "started_at": time.time(),
        "finished_at": None,
        "error": "",
    }
    asyncio.create_task(_run_batch_verify_job(job_id, names))
    return {"ok": True, "job_id": job_id, "total": len(names), "names": names}


@app.get("/api/admin/batch/{job_id}")
async def admin_batch_status(job_id: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    job = BATCH_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Batch task not found or expired")
    return job


@app.post("/api/admin/accounts/batch-proxy")
async def admin_batch_proxy(body: BatchProxyBody,
                            x_admin_key: str | None = Header(default=None)):
    """一键更改代理：把勾选账号统一绑定到指定代理（proxy_id 为空 = 恢复默认代理）。"""
    _admin_auth(x_admin_key)
    names = _clean_batch_names(body.names)
    if not names:
        raise HTTPException(400, "Please select at least one account")
    if body.proxy_id and body.proxy_id not in {p["id"] for p in list_proxies()}:
        raise HTTPException(400, "Proxy not found")
    busy = {acc["name"] for acc in pool.list_accounts() if acc.get("busy")}
    updated: list[str] = []
    failed: list[dict] = []
    for name in names:
        if name not in pool.accounts:
            failed.append({"name": name, "error": "账号不存在"})
            continue
        if name in busy:
            failed.append({"name": name, "error": "账号正在出片，未改动"})
            continue
        try:
            set_account_proxy(name, body.proxy_id)
            updated.append(name)
        except Exception as exc:
            failed.append({"name": name, "error": str(exc)[:150]})
    return {"ok": True, "updated": updated, "failed": failed,
            "proxy_id": body.proxy_id}


@app.post("/api/admin/accounts/reset-quotas")
async def admin_reset_quotas(x_admin_key: str | None = Header(default=None)):
    """手动触发一次额度重置（与每天 LIMIT_RESET_HOUR 点自动重置同一套逻辑）。"""
    _admin_auth(x_admin_key)
    count = pool.reset_daily_quotas()
    return {"ok": True, "reset": count, "next_reset_at": pool.next_quota_reset_at()}


@app.post("/api/admin/accounts/{name}/test-generate")
async def admin_test_generate(name: str, body: TestGenRequest,
                              x_admin_key: str | None = Header(default=None)):
    """账号级测试生成：用指定账号跑一条真实出片，返回 task_id（异步轮询结果）。"""
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if JOBS.get(name, {}).get("status") == "running":
        raise HTTPException(409, "Account is currently busy executing a task. Please try again later.")
    task_id = "test_" + uuid.uuid4().hex
    TEST_JOBS[task_id] = {
        "account": name,
        "prompt": body.prompt,
        "model": body.model,
        "duration": body.duration,
        "ratio": body.ratio,
        "status": "running",
        "started_at": time.time(),
        "result": None,
        "error": "",
    }
    asyncio.create_task(_run_test_generate(
        task_id, name, body.prompt, body.ratio, body.duration, body.model))
    return {"task_id": task_id, "account": name}


async def _run_test_generate(task_id: str, name: str, prompt: str,
                             ratio: str | None, duration: int, model: str) -> None:
    try:
        # 测试生成同样消耗账号额度，因此写入正式任务库，便于在任务列表追踪结果。
        store.create(task_id, model, prompt, ratio, duration, account=name)
        store.update(task_id, status="processing", started_at=time.time())
        result = await pool.test_generate(name, prompt, ratio, duration, model)
        TEST_JOBS[task_id]["result"] = result
        TEST_JOBS[task_id]["status"] = "done"
        err = result.get("error", "") if not result.get("ok") else ""
        TEST_JOBS[task_id]["error"] = err
        if result.get("ok"):
            store.update(task_id, status="completed",
                         video_url=result.get("video_url"),
                         finished_at=time.time())
        else:
            status = "风控" if _is_risk_error(err) else "failed"
            store.update(task_id, status=status, error=failure_text.summarize(err),
                         finished_at=time.time())
    except Exception as exc:
        TEST_JOBS[task_id]["status"] = "failed"
        TEST_JOBS[task_id]["error"] = failure_text.summarize(str(exc), limit=600)
        try:
            status = "风控" if _is_risk_error(str(exc)) else "failed"
            store.update(task_id, status=status, error=failure_text.summarize(str(exc)),
                         finished_at=time.time())
        except Exception:
            pass


def _is_risk_error(err: str) -> bool:
    """仅当测试生成的失败原因是「登录态失效」时，才将任务状态标为「风控」。"""
    return "登录态失效" in (err or "")


@app.get("/api/admin/test-gen/{task_id}")
async def admin_test_gen_status(task_id: str,
                                x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    job = TEST_JOBS.get(task_id)
    if not job:
        raise HTTPException(404, "test task not found")
    return job


@app.post("/api/admin/accounts/{name}/proxy")
async def admin_account_proxy_set(name: str, body: AccountProxyBind,
                                  x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    try:
        set_account_proxy(name, body.proxy_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.get("/api/admin/diagnostics/events")
async def admin_diagnostic_events(task_id: str = "", account: str = "", level: str = "",
                                  limit: int = 100, x_admin_key: str | None = Header(default=None),
                                  after: float | None = None, before: float | None = None, kind: str = ""):
    _admin_auth(x_admin_key)
    if level not in ('', 'info', 'error'):
        raise HTTPException(422, "Mức nhật ký không hợp lệ")
    if kind not in ('', 'failure', 'connection', 'state', 'dola') or any(v is not None and (not math.isfinite(v) or v < 0) for v in (after, before)) or (after is not None and before is not None and after > before):
        raise HTTPException(422, 'Khoảng thời gian hoặc loại sự kiện không hợp lệ')
    rows = [store.get(task_id)] if task_id else store.recent_tasks(200)
    events = []
    for row in rows:
        if not row or (account and row.get('account') != account):
            continue
        base = {'task_id': row['id'], 'account': row.get('account') or ''}
        if row.get('error'):
            display = failure_text.present(row['error'], code=row.get('failure_code') or '', category=row.get('failure_group') or '')
            events.append({**base, 'time': row.get('finished_at') or row.get('updated_at') or row['created_at'],
                           'kind': 'failure' if row['status'] == 'failed' else 'connection', 'level': 'error',
                           'message_vi': display['message_vi'], 'details': display['details']})
        events.append({**base, 'time': row.get('updated_at') or row['created_at'], 'kind': 'state', 'level': 'info',
                       'message_vi': {'queued': 'Chờ tài khoản', 'processing': 'Đang tạo', 'completed': 'Hoàn tất',
                                      'failed': 'Tạo video thất bại', 'unknown': 'Chưa lấy được kết quả — có thể Dola vẫn đang tạo'}.get(row['status'], 'Trạng thái đã cập nhật'),
                       'details': ''})
        for update in _task_display(row)['upstream_display']:
            events.append({**base, 'time': update.get('at') or row['created_at'], 'kind': 'dola',
                           'level': 'error' if update.get('connection') == 'interrupted' else 'info',
                           'message_vi': update['message_vi'], 'details': update['details']})
    events = [e for e in events if (not level or e['level'] == level) and (not kind or e['kind'] == kind)
              and (after is None or (e['time'] or 0) >= after) and (before is None or (e['time'] or 0) <= before)]
    events.sort(key=lambda e: e['time'] or 0, reverse=True)
    return {'events': events[:min(max(limit, 1), 200)],
            'scope_vi': 'Trạng thái và thông báo đã lưu của 200 tác vụ gần nhất; lọc mã tác vụ để xem tác vụ cũ.'}


@app.get("/api/admin/route")
async def admin_route(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {**pool.preview_route(), 'transport': config.TRANSPORT_MODE,
            'account_proxy_used': config.TRANSPORT_MODE != 'relay',
            'route_vi': 'Relay cho kết nối Dola; Proxy tài khoản không dùng cho yêu cầu Dola.' if config.TRANSPORT_MODE == 'relay'
                        else 'Kết nối trực tiếp hoặc Proxy đã gán theo tài khoản.'}


@app.post("/api/admin/accounts/{name}/prefer")
async def admin_account_prefer(name: str, body: PreferBody,
                               x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    pool.set_preferred(name if body.preferred else None)
    return {"ok": True, "preferred": name if body.preferred else None}


@app.post("/api/admin/accounts/{name}/weight")
async def admin_account_weight(name: str, body: WeightBody,
                               x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    pool.set_weight(name, body.weight)
    return {"ok": True, "weight": body.weight}


@app.post("/api/admin/accounts/rebalance-proxies")
async def admin_rebalance_proxies(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        return pool.rebalance_proxy_bindings()
    except Exception as exc:
        code = getattr(exc, "code", "rebalance_failed")
        raise HTTPException(400, f"{code}: {exc}") from exc


@app.post("/api/admin/accounts/bind-proxies")
async def admin_bind_proxies(body: BindProxiesBody,
                             x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        return pool.bind_proxies(body.proxies, force=body.force, mode=body.mode)
    except Exception as exc:
        code = getattr(exc, "code", "bind_failed")
        raise HTTPException(400, f"{code}: {exc}") from exc


@app.post("/api/admin/accounts/import-cookies")
async def admin_import_cookies(body: ImportTextBody,
                               x_admin_key: str | None = Header(default=None)):
    """Cookie 头文本 / Cookie JSON 批量导入（对齐 api-pool 的 import-cookies）。

    text 形如：一行一个 Cookie 头，或浏览器扩展导出的 Cookie JSON 数组；自动跳过注释/空行/邮箱行，按 sessionid 去重。
    """
    _admin_auth(x_admin_key)
    if not (body.text or "").strip():
        raise HTTPException(400, "text cannot be empty")
    try:
        result = await import_cookie_accounts_from_text(
            pool, body.text, require_login=not body.skip_verify
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)[:300]) from exc
    return {"ok": True, **result}


def _preview_account_import(account_pool, text: str) -> dict:
    from credential_import import parse_account_line
    from add_fb_account import parse_fb_input
    rows, seen = [], set()
    existing_fb = {j.get("facebook_uid") for j in JOBS.values() if j.get("status") == "running"} | {r["facebook_uid"] for r in account_pool._conn.execute("SELECT facebook_uid FROM accounts_meta") if r["facebook_uid"]}
    # ponytail: scan saved cookie JSON on preview; persisted UID already covers new imports.
    for name in account_pool.accounts:
        for filename in ('state.json', 'cookie_state.json'):
            path = account_pool.accounts_dir / name / filename
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                state = json.loads(path.read_text())
                cookies = state.get('cookies', []) if isinstance(state, dict) else []
                for cookie in cookies if isinstance(cookies, list) else []:
                    if isinstance(cookie, dict) and cookie.get('name') == 'c_user' and 'facebook.com' in str(cookie.get('domain', '')) and str(cookie.get('value', '')).isdigit():
                        existing_fb.add(str(cookie['value']))
            except (OSError, ValueError):
                pass
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        identity, kind, state = "", "google", "ready"
        if "c_user=" in line:
            kind = "facebook"
            cookie, email, _ = parse_fb_input(line)
            cookies = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
            identity = cookies.get("c_user", "").strip()
            if not identity or not cookies.get("xs"):
                state = "invalid"
            elif identity in existing_fb or ("@" in email and account_pool.find_accounts_by_email(email)):
                state = "duplicate"
        else:
            cred = parse_account_line(line)
            if not cred or not cred.password:
                state = "invalid"
            else:
                identity = cred.email.lower()
                if account_pool.find_accounts_by_email(identity) or any(j.get("status") == "running" and (j.get("email") or "").lower() == identity for j in JOBS.values()):
                    state = "duplicate"
        key = (kind, identity)
        if state == "ready" and key in seen:
            state = "duplicate"
        seen.add(key)
        rows.append({"line": number, "kind": kind, "identity": identity, "status": state})
    return {"rows": rows, **{state: sum(r["status"] == state for r in rows) for state in ("ready", "duplicate", "invalid")}}


@app.post("/api/admin/accounts/import-preview")
async def admin_import_preview(body: ImportTextBody, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if len(body.text) > 1_000_000:
        raise HTTPException(422, "Danh sách quá lớn (tối đa 1 MB)")
    return _preview_account_import(pool, body.text)


@app.post("/api/admin/accounts/import-start", status_code=202)
async def admin_import_start(body: ImportTextBody, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if len(body.text) > 1_000_000:
        raise HTTPException(422, "Danh sách quá lớn (tối đa 1 MB)")
    preview = _preview_account_import(pool, body.text)
    lines = body.text.splitlines()
    accepted, errors = [], []
    taken = set(pool.accounts) | set(JOBS)
    from credential_import import parse_account_line
    for row in preview['rows']:
        if row['status'] != 'ready':
            continue
        line = lines[row['line'] - 1]
        prefix = 'fb' if row['kind'] == 'facebook' else 'acc'
        index = 1
        while f"{prefix}{index}" in taken:
            index += 1
        name = f"{prefix}{index}"
        taken.add(name)
        if row['kind'] == 'facebook':
            data = AccountAdd(name=name, facebook=line)
        else:
            cred = parse_account_line(line)
            data = AccountAdd(name=name, email=cred.email, password=cred.password, totp=cred.totp)
        try:
            await admin_account_add(data, x_admin_key=x_admin_key)
            JOBS[name]['import_line'] = row['line']
            if row['kind'] == 'facebook':
                JOBS[name]['facebook_uid'] = row['identity']
            accepted.append({'name': name, 'line': row['line']})
        except HTTPException as exc:
            errors.append({'line': row['line'], 'message_vi': failure_text.present(str(exc.detail))['message_vi']})
    return {**preview, 'accepted': accepted, 'errors': errors}


@app.post("/api/admin/accounts/import-accounts")
async def admin_import_accounts(body: ImportTextBody,
                                x_admin_key: str | None = Header(default=None)):
    """多行账号凭据批量登录导入（支持 Google 与 Facebook 两种格式）。"""
    _admin_auth(x_admin_key)
    raw_text = (body.text or "").strip()
    if not raw_text:
        raise HTTPException(400, "text cannot be empty")

    created: list[str] = []
    failed: list[str] = []
    errors: list[str] = []

    # Phân loại dòng Facebook vs Google
    fb_lines: list[str] = []
    gg_lines: list[str] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "c_user=" in line:
            fb_lines.append(line)
        else:
            gg_lines.append(line)

    try:
        async with ADD_ACCOUNT_SEM:
            # Xử lý các dòng Facebook
            if fb_lines:
                existing = set(pool.accounts)
                next_num = 1
                for f_line in fb_lines:
                    while f"fb{next_num}" in existing:
                        next_num += 1
                    name = f"fb{next_num}"
                    existing.add(name)
                    next_num += 1
                    try:
                        res = await add_fb_account_flow(name, f_line)
                        created.append(name)
                    except Exception as exc:
                        failed.append(name)
                        errors.append(f"{name} (Facebook): {str(exc)[:150]}")

            # Xử lý các dòng Google
            if gg_lines:
                records = parse_account_text("\n".join(gg_lines))
                if records:
                    pairs = allocate_credentials(records, pool.accounts, name_prefix="acc")
                    for name, cred in pairs:
                        dups = pool.find_accounts_by_email(cred.email)
                        if dups:
                            failed.append(name)
                            errors.append(f"{name}: 邮箱已存在于 {','.join(dups)}")
                            continue
                        try:
                            await add_account_flow(name, cred.email, cred.password, cred.totp)
                            pool.ensure_account(name)
                            pool.set_email(name, cred.email)
                            pool.set_source(name, "login")
                            pool.set_login_status(name, True)
                            if cred.email:
                                cred_store.store(name, cred.email, cred.password, cred.totp)
                            created.append(name)
                        except Exception as exc:
                            failed.append(name)
                            errors.append(f"{name}: {str(exc)[:150]}")
    except Exception as exc:
        raise HTTPException(400, str(exc)[:300]) from exc
    return {"ok": True, "created": created, "failed": failed, "errors": errors}


@app.get("/api/admin/proxies")
async def admin_proxies(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"proxies": list_proxies(), "default_proxy": config.PROXY or ""}


@app.post("/api/admin/proxies")
async def admin_proxy_create(body: ProxyCreate,
                             x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        created = create_proxy(
            body.name, body.protocol, body.host, body.port,
            body.username, body.password, body.remark,
            mode=body.mode,
            session_template=body.session_template,
            sticky_ttl=body.sticky_ttl,
            rotate_on_risk=body.rotate_on_risk,
            rotate_min_interval=body.rotate_min_interval,
            extract_url=body.extract_url,
            extract_interval=body.extract_interval,
            nodes_text=body.nodes_text,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"created": created}


@app.patch("/api/admin/proxies/{proxy_id}")
async def admin_proxy_update(proxy_id: str, body: ProxyUpdate,
                             x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        updated = update_proxy(proxy_id, **fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not updated:
        raise HTTPException(404, "proxy not found")
    return {"updated": updated}


@app.delete("/api/admin/proxies/{proxy_id}")
async def admin_proxy_delete(proxy_id: str,
                             x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    delete_proxy(proxy_id)
    return {"ok": True}


# ===== 动态代理：出口 IP 探测 / 轮换 =====


@app.post("/api/admin/proxies/{proxy_id}/probe")
async def admin_proxy_probe(proxy_id: str,
                            x_admin_key: str | None = Header(default=None)):
    """探测该代理当前的出口 IP（动态代理用临时 session，不动已绑定的号）。"""
    _admin_auth(x_admin_key)
    return await asyncio.to_thread(proxy_store.probe_proxy_record, proxy_id)


@app.get("/api/admin/proxies/{proxy_id}/nodes")
async def admin_proxy_nodes(proxy_id: str,
                            x_admin_key: str | None = Header(default=None)):
    """提取型代理的节点池概览。"""
    _admin_auth(x_admin_key)
    nodes = proxy_store.list_nodes(proxy_id)
    now = time.time()
    return {
        "nodes": nodes,
        "total": len(nodes),
        "alive": sum(1 for n in nodes
                     if not n["dead"] and (not n["expires_at"] or n["expires_at"] > now)),
        "dead": sum(1 for n in nodes if n["dead"]),
    }


@app.post("/api/admin/proxies/{proxy_id}/nodes")
async def admin_proxy_add_nodes(proxy_id: str, body: ProxyNodesIn,
                                x_admin_key: str | None = Header(default=None)):
    """手工往节点池里粘贴/追加节点（一行一个 host:port:user:pass）。"""
    _admin_auth(x_admin_key)
    result = await asyncio.to_thread(
        proxy_store.add_nodes_from_text, proxy_id, body.text, body.ttl)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Failed to add nodes"))
    return result


@app.post("/api/admin/proxies/{proxy_id}/refresh-nodes")
async def admin_proxy_refresh_nodes(proxy_id: str,
                                    x_admin_key: str | None = Header(default=None)):
    """手动调一次提取接口补节点（注意：GenerateLink 类接口通常按次计费）。"""
    _admin_auth(x_admin_key)
    result = await asyncio.to_thread(proxy_store.fetch_extract_nodes, proxy_id)
    return result


@app.post("/api/admin/proxies/{proxy_id}/prune-nodes")
async def admin_proxy_prune_nodes(proxy_id: str,
                                  x_admin_key: str | None = Header(default=None)):
    """清掉过期/连续失败的节点。"""
    _admin_auth(x_admin_key)
    return {"ok": True, "removed": proxy_store.prune_nodes(proxy_id)}


@app.post("/api/admin/accounts/{name}/rotate-ip")
async def admin_account_rotate_ip(name: str,
                                  x_admin_key: str | None = Header(default=None)):
    """给该号换一个出口 IP（动态代理换 sticky session；静态代理无操作）。"""
    _admin_auth(x_admin_key)
    # 注意：不要 to_thread。BrowserPool 的 sqlite 连接是跨线程共享的，
    # 从 worker 线程调用池方法会和事件循环交错使用同一条连接、留下未结束的事务，
    # 把 pool_usage.db 的写锁焊死（实测会让所有写操作 15s 超时 database is locked）。
    result = pool.rotate_account_ip(name, force=True)
    if not result.get("rotated"):
        raise HTTPException(400, result.get("reason", "Rotation failed"))
    return result


@app.post("/api/admin/accounts/{name}/probe-ip")
async def admin_account_probe_ip(name: str,
                                 x_admin_key: str | None = Header(default=None)):
    """查该号当前真实出口 IP 与归属地。"""
    _admin_auth(x_admin_key)
    return await asyncio.to_thread(proxy_store.probe_account_exit_ip, name)


@app.post("/api/admin/proxies/{proxy_id}/rotate")
async def admin_proxy_rotate_all(proxy_id: str,
                                 x_admin_key: str | None = Header(default=None)):
    """给绑定在该代理上的所有号换出口 IP。"""
    _admin_auth(x_admin_key)
    rotated, skipped = [], []
    for acc in pool.list_accounts():
        if acc.get("proxy_id") != proxy_id:
            continue
        result = pool.rotate_account_ip(acc["name"], force=True)
        (rotated if result.get("rotated") else skipped).append(acc["name"])
    return {"ok": True, "rotated": rotated, "skipped": skipped}


@app.get("/api/admin/users")
async def admin_users(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    session = ADMIN_SESSIONS.get(x_admin_key or "", {})
    return {
        "users": user_list(),
        "current_username": session.get("username"),
    }


@app.post("/api/admin/users")
async def admin_user_create(body: AdminUserCreate,
                            x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        created = user_create(
            body.username, body.password, role=body.role,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"created": created}


@app.patch("/api/admin/users/{username}")
async def admin_user_update(username: str, body: AdminUserUpdate,
                            x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        updated = user_update(
            username,
            new_username=body.username,
            password=body.password,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"updated": updated}


@app.delete("/api/admin/users/{username}")
async def admin_user_delete(username: str,
                            x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    session = ADMIN_SESSIONS.get(x_admin_key or "", {})
    if session.get("username") == username:
        raise HTTPException(400, "Cannot delete currently logged-in account")
    try:
        user_delete(username)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


async def _run_add_job(name: str, email: str, password: str, totp: str,
                       cookies: str = "", cookie_skip_verify: bool = False,
                       facebook: str = "") -> None:
    JOBS[name] = {**JOBS.get(name, {}), "kind": "add", "status": "running", "email": email or "facebook",
                  "error": "", "started_at": time.time(), "stage_vi": "Chờ đăng nhập"}
    try:
        async with ADD_ACCOUNT_SEM:
            JOBS[name]["stage_vi"] = "Đang đăng nhập Facebook" if facebook else "Đang đăng nhập tài khoản"
            if facebook or ("c_user=" in cookies) or ("c_user=" in email):
                fb_line = facebook or cookies or email
                res = await add_fb_account_flow(name, fb_line)
                pool.ensure_account(name)
                pool.set_sessionid(name, res["sessionid"])
                pool.set_source(name, "facebook")
                uid = _preview_account_import(pool, fb_line)['rows'][0]['identity']
                pool._conn.execute("UPDATE accounts_meta SET facebook_uid=? WHERE name=?", (uid, name))
                pool._conn.commit()
                pool.set_login_status(name, True)
                if res.get("email"):
                    pool.set_email(name, res["email"])
            elif cookies:
                result = await import_cookie_account(
                    name, json.loads(cookies),
                    require_login=not cookie_skip_verify,
                )
                pool.ensure_account(name)
                pool.set_email(name, email)
                pool.set_source(name, "cookie")
                pool.set_login_status(name, bool(result.get("verified")))
            else:
                await add_account_flow(name, email, password, totp)
                pool.ensure_account(name)
                pool.set_email(name, email)
                pool.set_source(name, "login")
                pool.set_login_status(name, True)
        JOBS[name] = {**JOBS[name], "status": "success", "stage_vi": "Đã đăng nhập"}
    except Exception as e:
        JOBS[name] = {**JOBS[name], "status": "failed", "stage_vi": "Đăng nhập thất bại", "error": failure_text.sanitize_details(str(e))[:300]}


async def _cred_try_refresh_once() -> None:
    """发现有凭据但登录态失效的账号时，自动用存储的 Google 凭据重新登录刷新。"""
    now = time.time()
    for name in cred_store.all_names():
        if name in JOBS and JOBS[name].get("status") == "running":
            continue
        acct = next((a for a in pool.list_accounts() if a["name"] == name), None)
        if not acct:
            continue
        # cookie 来源账号靠重新导入 cookie 管理，不用 Google 凭据自动重登，
        # 否则会卡 Google OAuth 并占用浏览器 profile（导致验证 profile 锁）。
        if acct.get("source") == "cookie":
            continue
        if acct.get("login_ok") in (1, None):
            continue
        if now - LAST_CRED_REFRESH.get(name, 0) < 180:
            continue
        creds = cred_store.load(name)
        if not creds or not creds.get("email") or not creds.get("password"):
            continue
        LAST_CRED_REFRESH[name] = now
        print(f"[cred-refresh] 尝试自动刷新账号 {name}", flush=True)
        try:
            async with ADD_ACCOUNT_SEM:
                await add_account_flow(
                    name, creds["email"], creds["password"], creds.get("totp") or "",
                    force_login=True)
            pool.set_email(name, creds["email"])
            pool.set_login_status(name, True)
            print(f"[cred-refresh] {name} 刷新成功", flush=True)
        except Exception as e:
            print(f"[cred-refresh] {name} 刷新失败: {str(e)[:200]}", flush=True)


async def _cred_refresher_loop() -> None:
    while True:
        try:
            await _cred_try_refresh_once()
        except Exception:
            pass
        await asyncio.sleep(60)


@app.post("/api/admin/accounts", status_code=202)
async def admin_account_add(body: AccountAdd, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "invalid account name")
    if body.name in pool.accounts:
        raise HTTPException(409, "Account name already exists: " + body.name)
    if JOBS.get(body.name, {}).get("status") == "running":
        raise HTTPException(409, "add job running")
    email = (body.email or "").strip()
    cookies = (body.cookies or "").strip()
    facebook = (body.facebook or "").strip()
    if not email and not cookies and not facebook:
        raise HTTPException(400, "Vui lòng nhập Email, Cookie hoặc thông tin tài khoản Facebook")
    if email and not facebook and "c_user=" not in email:
        cred_store.store(body.name, email, body.password, body.totp)
        dups = pool.find_accounts_by_email(email)
        if dups:
            raise HTTPException(409, "Email already exists in account(s): " + ", ".join(dups))
        norm = email.lower()
        for n, job in JOBS.items():
            if (job.get("kind") == "add" and job.get("status") == "running"
                    and (job.get("email") or "").strip().lower() == norm):
                raise HTTPException(409, "Email is currently being added (" + n + "), please do not submit duplicates")
    if cookies and not facebook and "c_user=" not in cookies:
        try:
            json.loads(cookies)
        except Exception:
            raise HTTPException(422, "Invalid cookie JSON format")
    JOBS[body.name] = {"kind": "add", "status": "running", "email": email or "facebook",
                       "error": "", "started_at": time.time()}
    asyncio.create_task(_run_add_job(
        body.name, email, body.password, body.totp,
        cookies=cookies, cookie_skip_verify=body.cookie_skip_verify,
        facebook=facebook,
    ))
    return {"ok": True, "job": "running"}


@app.get("/api/admin/jobs")
async def admin_jobs(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"jobs": {name: {**job, "error": failure_text.sanitize_details(job.get("error") or ""),
                            "failure_display": failure_text.present(job.get("error") or "")} for name,job in JOBS.items()}}


def _task_display(row: dict) -> dict:
    """Presentation is derived on read, so historical records remain recoverable."""
    try:
        updates = json.loads(row.get("upstream_updates") or "[]")
    except (TypeError, ValueError):
        updates = []
    if not isinstance(updates, list):
        updates = []
    safe_updates = []
    for u in updates[-20:]:
        if not isinstance(u, dict):
            continue
        safe = {key: failure_text.sanitize_details(u.get(key) or "")
                for key in ("status", "connection", "wait_minutes")}
        safe["at"] = u.get("at") if isinstance(u.get("at"), (int, float)) else None
        for key in ("texts", "failure_reasons", "creation_statuses"):
            values = u.get(key) or []
            safe[key] = [failure_text.sanitize_details(x) for x in values[:10]] if isinstance(values, list) else []
        safe_updates.append(safe)
    return {"source_task_id": row.get("source_task_id"), "error": failure_text.sanitize_details(row.get("error") or ""),
            "upstream_updates": json.dumps(safe_updates, ensure_ascii=False),
            "failure_display": failure_text.present(row.get("error") or "",
                code=row.get("failure_code") or "", category=row.get("failure_group") or ""),
            "upstream_display": [failure_text.present_update(u) for u in updates[-20:] if isinstance(u, dict)]}


@app.get("/api/admin/tasks")
async def admin_tasks(limit: int = 50, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"tasks": [{**row, **_task_display(row), "target_duration": _target_duration(row), "file_exists": _task_file_exists(row)} for row in store.recent_tasks(min(max(limit, 1), 200))]}


@app.get("/api/admin/tasks/{task_id}")
async def admin_get_task(task_id: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    row = store.get(task_id)
    if not row:
        raise HTTPException(404, "task not found")
    prog = _progress_payload(status=row["status"], created_at=row["created_at"], duration=row.get("duration", 30))
    return {
        **_task_display(row),
        "id": row["id"],
        "status": row["status"],
        "model": row["model"],
        "prompt": row["prompt"],
        "video_url": row["video_url"],
        "file_exists": _task_file_exists(row),
        "export_error": failure_text.sanitize_details(row.get("export_error") or ""),
        "error": failure_text.sanitize_details(row["error"]),
        "failure_code": row.get("failure_code"),
        "failure_group": row.get("failure_group"),
        "upstream_request_id": row.get("upstream_request_id"),
        "progress": prog,
        "created_at": row["created_at"],
        "duration": row.get("duration", 30),
        "target_duration": _target_duration(row),
        "ratio": row.get("ratio"),
        "phase": row.get("phase"),
        "batch_id": row.get("batch_id"),
        "batch_index": row.get("batch_index"),
        "last_poll_at": row.get("last_poll_at"),
        "submission_started_at": row.get("submission_started_at"),
        "upstream_updates": _task_display(row)["upstream_updates"],
        "video_duration": row.get("video_duration"),
        "video_width": row.get("video_width"),
        "video_height": row.get("video_height"),
        "continuation_task_id": row.get("continuation_task_id") if store.get(row.get("continuation_task_id") or "") else None,
        "can_check_result": bool(row.get("conversation_id") and row.get("account")),
    }


@app.post("/api/admin/tasks/{task_id}/check-result")
async def admin_check_result(task_id: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    row = store.get(task_id)
    if not row:
        raise HTTPException(404, "Không tìm thấy video")
    if task_id in ACTIVE_STUDIO_JOBS or row["status"] == "completed":
        return {"id": task_id, "status": row["status"]}
    if not row.get("conversation_id") or not row.get("account"):
        raise HTTPException(409, "Thiếu conversation hoặc tài khoản; không thể tự gửi lại an toàn")
    # One user-requested read, with a fresh bounded polling window; no new submission.
    store.update(task_id, status="processing", phase="generating", error="",
                 deadline_at=time.time() + config.OBSERVATION_SECONDS)
    asyncio.create_task(_resume_task(store.get(task_id)))
    return {"id": task_id, "status": "processing"}


class ContinueVideoRequest(BaseModel):
    confirm_cost: bool = False
    confirm_review: bool = False


@app.post("/api/admin/tasks/{task_id}/continue")
async def admin_continue_video(task_id: str, body: ContinueVideoRequest,
                               x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    parent = store.get(task_id)
    if not parent:
        raise HTTPException(404, "Không tìm thấy video")
    if not body.confirm_cost:
        raise HTTPException(400, "Cần xác nhận dùng thêm điểm để tạo đoạn tiếp")
    if parent.get("continuation_task_id") and store.get(parent["continuation_task_id"]):
        child = store.get(parent["continuation_task_id"])
        return {"id": child["id"], "status": child["status"]}
    actual = parent.get("video_duration")
    target = _target_duration(parent)
    if parent["status"] != "completed" or not actual or actual >= target * 0.9:
        raise HTTPException(409, "Chỉ tạo tiếp cho video đã nhận nhưng thiếu thời lượng")
    first = Path(config.DOWNLOAD_DIR) / Path(urlparse(parent["video_url"]).path).name
    if not first.is_file():
        raise HTTPException(409, "Không còn file video đầu để ghép")
    remaining = target - actual
    options = config.V20_DURATIONS if '2.0' in parent["model"] else config.NATIVE_DURATIONS
    duration = next((d for d in sorted(options) if d >= remaining), max(options))
    prompt = parent["prompt"] + "\nTiếp tục hành động của cảnh trước, giữ nhân vật, bối cảnh và ánh sáng; chuyển động nối tiếp tự nhiên."
    check = await preflight_check_prompt(prompt)
    if check["status"] == "block":
        raise HTTPException(422, check.get("reason") or "Nội dung cần chỉnh sửa")
    if check["status"] == "review" and not body.confirm_review:
        raise HTTPException(409, {"status": "review", "reason": check.get("reason")})
    # After await, re-read. No await between reservation/create/dispatch (single process).
    parent = store.get(task_id)
    child_id = parent.get("continuation_task_id") or str(uuid.uuid4())
    if store.get(child_id):
        return {"id": child_id, "status": store.get(child_id)["status"]}
    store.update(task_id, continuation_task_id=child_id)
    client = _task_client(parent)
    store.create(child_id, parent["model"], prompt, parent["ratio"], duration,
                 reference_images=parent.get("reference_images") or "[]")
    store.update(child_id, parent_task_id=task_id)
    asyncio.create_task(_run_task(child_id, parent["model"], prompt,
        None if parent["ratio"] == "default" else parent["ratio"], duration,
        _task_reference_images(parent.get("reference_images")), client))
    return {"id": child_id, "status": "queued"}


@app.get("/api/admin/videos")
async def admin_videos(limit: int = 100, x_admin_key: str | None = Header(default=None)):
    """视频管理：已完成任务的列表 + 文件元数据（老任务按需从磁盘补大小）。"""
    _admin_auth(x_admin_key)
    rows = store.recent_tasks(min(max(limit, 1), 500))
    out = []
    for r in rows:
        if r.get("status") != "completed":
            continue
        item = {**r, **_task_display(r)}
        if item.get("video_url"):
            local = Path(config.DOWNLOAD_DIR) / Path(item["video_url"]).name
        else:
            local = None
        if local and local.exists():
            item["file_exists"] = True
            try:
                item["file_size"] = os.path.getsize(local)
            except OSError:
                item["file_size"] = item.get("video_size")
            if not item.get("video_size"):
                item["video_size"] = item["file_size"]
        else:
            item["file_exists"] = False
        out.append(item)
    out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    stats = store.video_stats()
    return {
        "videos": out,
        "total": stats["total"],
        "available": stats["available"],
    }


@app.delete("/api/admin/videos/{task_id}")
async def admin_video_delete(task_id: str, x_admin_key: str | None = Header(default=None)):
    """删除视频文件并同步删除对应的任务记录。"""
    _admin_auth(x_admin_key)
    row = store.get(task_id)
    if not row or row.get("status") != "completed":
        raise HTTPException(404, "task not found")
    url = row.get("video_url")
    if url:
        local = Path(config.DOWNLOAD_DIR) / Path(url).name
        try:
            if local.exists():
                local.unlink()
        except OSError:
            pass
    delete_reference_thumbnails(_task_reference_thumbs(row.get("reference_thumbs")))
    store.delete_task(task_id)
    return {"ok": True, "deleted": task_id}


@app.get("/api/admin/stats")
async def admin_stats(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    st = store.stats()
    accs = pool.list_accounts(processing_accounts=store.busy_account_ids())
    sched = [a for a in accs if a["scheduling"] and not a["cooling"]]
    st["total_accounts"] = len(accs)
    st["available_accounts"] = sum(1 for a in sched if a["remaining"] > 0)
    st["total_remaining"] = sum(a["remaining"] for a in sched)
    totals = st.pop("per_account_total", {})
    st["per_account"] = [{**a, "completed_total": totals.get(a["name"], 0)} for a in accs]
    st.update(_group_summary(accs))
    return st


@app.get("/api/admin/keys")
async def admin_keys(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
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
    return {
        "keys": keys,
        "env_keys": len(config.API_KEYS),
        # 面板的「允许时长」勾选项必须跟着 NATIVE_DURATION_MAX 走：前端原先硬编码
        # 5/10/15/30，开了任意时长也勾不到新档位，等于功能从面板上用不了。
        "supported_durations": config.all_durations(),
    }


@app.post("/api/admin/keys")
async def admin_key_create(body: KeyCreate, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    allowed = _normalize_allowed_durations(body.allowed_durations)
    return {"created": store.create_key(
        body.name,
        daily_limit=body.daily_limit,
        concurrency_limit=body.concurrency_limit,
        allowed_durations=allowed,
        expires_at=body.expires_at,
    )}


@app.patch("/api/admin/keys/{key}")
async def admin_key_patch(key: str, body: KeyPatch,
                          x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    fields = {}
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
    store.update_key(key, **fields)
    return {"ok": True}


@app.delete("/api/admin/keys/{key}")
async def admin_key_delete(key: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    store.delete_key(key)
    return {"ok": True}


# ===== AI CẤU HÌNH, REWRITE, PREFLIGHT & TẠO VIDEO ADMIN =====

@app.get("/api/admin/studio-info")
async def admin_studio_info(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"transport_mode": config.TRANSPORT_MODE,
            "observation_seconds": config.OBSERVATION_SECONDS,
            "reference_image_max_bytes": config.REFERENCE_IMAGE_MAX_BYTES,
            "reference_image_max_count": config.REFERENCE_IMAGE_MAX_COUNT,
            "reference_total_max_bytes": config.REFERENCE_TOTAL_MAX_BYTES,
            "max_prompt_chars": config.MAX_PROMPT_CHARS}


@app.get("/api/admin/ai-settings")
async def admin_get_ai_settings(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        return get_public_settings()
    except AiSettingsDecryptError as exc:
        raise HTTPException(500, f"Lỗi giải mã cấu hình AI: {exc}")
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.put("/api/admin/ai-settings")
async def admin_put_ai_settings(body: AiSettingsUpdate, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        return update_settings(body.model_dump())
    except (ValueError, AiSettingsError) as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/admin/prompts/rewrite")
async def admin_prompt_rewrite(body: PromptRewriteRequest, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not (body.prompt or "").strip():
        raise HTTPException(422, "Prompt không được để trống")
    try:
        rewritten = await rewrite_prompt_text(
            original_prompt=body.prompt,
            instructions=body.instructions,
            duration=body.duration,
            ratio=body.ratio,
            model=body.model,
        )
        return {
            "ok": True,
            "original_prompt": body.prompt,
            "rewritten_prompt": rewritten,
        }
    except AiProviderNotConfiguredError as exc:
        raise HTTPException(400, str(exc))
    except AiProviderCallError as exc:
        raise HTTPException(502, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


def _validate_image_content(data: bytes, filename: str) -> None:
    """Xác thực magic bytes và tính toàn vẹn của tệp ảnh trước khi lưu trữ."""
    if len(data) < 12:
        raise HTTPException(422, f"Tệp {filename} không phải tệp ảnh hợp lệ (kích thước quá nhỏ)")
    is_png = data.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpeg = data.startswith(b"\xff\xd8\xff")
    is_webp = data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP"
    if not (is_png or is_jpeg or is_webp):
        raise HTTPException(
            422,
            f"Tệp {filename} có phần mở rộng ảnh nhưng nội dung thực tế không phải định dạng JPG, PNG hoặc WEBP hợp lệ"
        )
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
    except Exception as exc:
        raise HTTPException(422, f"Tệp ảnh {filename} bị lỗi hoặc không thể giải mã: {exc}")


async def _reference_context(refs: list[str], roles: list[str]) -> tuple[list[dict], list[str]]:
    if len(roles) > len(refs) or any(role not in ("character", "scene", "style", "other") for role in roles):
        raise HTTPException(422, "Vai trò ảnh tham chiếu không hợp lệ")
    root = None
    try:
        root, paths = await download_reference_images(refs, "check_" + uuid.uuid4().hex[:12])
        context, frozen = [], []
        total = 0
        for index, path in enumerate(paths):
            data = Path(path).read_bytes()
            total += len(data)
            if total > config.REFERENCE_TOTAL_MAX_BYTES:
                raise ValueError("Tổng dung lượng ảnh quá lớn")
            _validate_image_content(data, Path(path).name)
            context.append({"sha256": hashlib.sha256(data).hexdigest(), "role": roles[index] if index < len(roles) else "other"})
            frozen.append(_data_url_from_upload(Path(path).name, data))
        return context, frozen
    except ValueError as exc:
        raise HTTPException(422, failure_text.sanitize_details(str(exc))) from exc
    finally:
        if root:
            shutil.rmtree(root, ignore_errors=True)


def _video_context(model, duration, ratio, references):
    resolved = _resolve_model_for_duration(model, duration)
    if not resolved:
        raise HTTPException(422, "Model không hỗ trợ thời lượng đã chọn")
    return {"model": resolved, "duration": duration, "ratio": ratio, "references": references}


async def _ensure_video_preflight(session_id, prompt, token, confirm_review, context):
    record = _PREFLIGHT_TOKENS.get(token or "")
    ok, message, status = validate_preflight_gate(session_id, prompt, token, confirm_review,
                                                 context=context if record and record.get("context") is not None else None)
    if not ok:
        raise HTTPException(422, message)
    if record.get("context") is not None:
        return record
    try:
        result = await preflight_check_prompt(prompt, context=context)
    except AiProviderNotConfiguredError as exc:
        raise HTTPException(400, str(exc)) from exc
    except AiProviderCallError as exc:
        raise HTTPException(502, str(exc)) from exc
    checked = record_preflight_result(session_id, prompt, result, context=context)
    if checked["status"] == "review":
        raise HTTPException(409, {"message": "Cấu hình thực cần xem xét trước khi gửi", "preflight": checked})
    if checked["status"] == "block":
        raise HTTPException(422, {"message": "Nội dung cần chỉnh sửa; chưa gửi video", "preflight": checked})
    return checked


@app.post("/api/admin/prompts/preflight")
async def admin_prompt_preflight(body: Request, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    refs = []
    try:
        if not isinstance(body, PromptPreflightRequest):
            if "multipart/form-data" in body.headers.get("content-type", ""):
                form = await body.form()
                data = PromptPreflightRequest.model_validate_json(str(form.get("analysis") or "{}"))
                refs = await _admin_reference_images(form)
            else:
                data = PromptPreflightRequest.model_validate(await body.json())
        else:
            data = body
        refs = refs or data.reference_images
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, "Dữ liệu kiểm tra prompt không hợp lệ") from exc
    session_id = hashlib.sha256((x_admin_key or "default").encode()).hexdigest()[:24]
    if not data.prompt.strip():
        raise HTTPException(422, "Prompt không được để trống")
    context = None
    if data.model is not None or data.duration is not None or data.ratio is not None or refs:
        reference_context, _ = await _reference_context(refs, data.reference_roles)
        context = _video_context(data.model or "seedance-2.5", data.duration or 30,
                                 _resolve_ratio(None, data.ratio or "16:9"), reference_context)
    error = None
    if data.upstream_task_id:
        row = store.get(data.upstream_task_id)
        if not row or row.get("status") != "failed":
            raise HTTPException(409, "Chỉ phân tích lỗi của tác vụ đã xác nhận thất bại")
        error = failure_text.sanitize_details(row.get("error") or "")
    try:
        kwargs = {"context": context, "upstream_error": error} if context is not None or error else {}
        result = await preflight_check_prompt(data.prompt, **kwargs)
        record = record_preflight_result(session_id, data.prompt, result, context=context)
        return {"ok": True, **record}
    except AiProviderNotConfiguredError as exc:
        raise HTTPException(400, str(exc)) from exc
    except AiProviderCallError as exc:
        raise HTTPException(502, str(exc)) from exc



async def _admin_reference_images(form):
    refs: list[str] = []
    total = 0
    for field in ("images", "image", "image[]", "reference_images"):
        for item in form.getlist(field):
            if hasattr(item, "read") and hasattr(item, "filename"):
                filename = getattr(item, "filename", "") or "image.png"
                ext = Path(filename).suffix.lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                    raise HTTPException(422, f"Định dạng ảnh không được hỗ trợ ({ext}). Chỉ hỗ trợ JPG, PNG, WEBP.")
                read_fn = getattr(item, "read")
                data = await read_fn()
                if len(data) > config.REFERENCE_IMAGE_MAX_BYTES:
                    raise HTTPException(422, f"Ảnh {filename} vượt quá giới hạn dung lượng ảnh")
                total += len(data)
                if total > config.REFERENCE_TOTAL_MAX_BYTES:
                    raise HTTPException(422, "Tổng dung lượng ảnh của mẻ vượt giới hạn")
                if len(refs) >= config.REFERENCE_IMAGE_MAX_COUNT:
                    raise HTTPException(422, "Số ảnh của mẻ vượt giới hạn")
                if data:
                    _validate_image_content(data, filename)
                    refs.append(_data_url_from_upload(filename, data))
            elif isinstance(item, str) and item.strip():
                refs.append(item.strip())

    return refs


# ponytail: 32 fixed lock stripes bound memory; use per-request locks if unrelated acceptance contention matters.
_STUDIO_ACCEPT_LOCKS = [asyncio.Lock() for _ in range(32)]


def _acceptance_lock(identity: str):
    index = int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16) % len(_STUDIO_ACCEPT_LOCKS)
    return _STUDIO_ACCEPT_LOCKS[index]


@app.get("/api/admin/video-receipts/{request_id}")
async def admin_video_receipt(request_id: str, kind: str = "single",
                              x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id) or kind not in ("single", "batch"):
        raise HTTPException(422, "Mã hoặc loại yêu cầu không hợp lệ")
    username = ADMIN_SESSIONS[x_admin_key]["username"]
    digest = hashlib.sha256(f"studio:{username}:{request_id}".encode()).hexdigest()[:32]
    if kind == "batch":
        batch_id = "batch_" + digest
        if store.batch_fingerprint(batch_id) is None:
            return None
        return {"batch_id": batch_id, "tasks": [{"id": r["id"], "status": r["status"]}
                for r in store.batch_tasks(batch_id)]}
    row = store.get("video_" + digest)
    return {"id": row["id"], "status": row["status"]} if row else None


@app.post("/api/admin/videos/generations", response_model=TaskResponse)
async def admin_create_video(request: Request, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    form = await request.form()
    identity = f"single:{x_admin_key}:{form.get('request_id') or ''}"
    async with _acceptance_lock(identity):
        return await _accept_create_video(request, x_admin_key)


async def _accept_create_video(request: Request, x_admin_key: str | None = None):
    _admin_auth(x_admin_key)
    session_id = hashlib.sha256((x_admin_key or "default").encode("utf-8")).hexdigest()[:24]

    form = await request.form()
    p_val = form.get("prompt")
    prompt = str(p_val) if isinstance(p_val, str) else ""
    if not prompt.strip():
        raise HTTPException(422, "Prompt không được để trống")
    if len(prompt) > config.MAX_PROMPT_CHARS:
        raise HTTPException(
            422, f"Prompt quá dài (tối đa {config.MAX_PROMPT_CHARS} ký tự, nhận được {len(prompt)})"
        )

    m_val = form.get("model")
    model = str(m_val).strip() if m_val and isinstance(m_val, str) else "seedance-2.5"

    d_val = form.get("duration")
    try:
        duration = int(float(str(d_val))) if d_val else 30
    except (ValueError, TypeError):
        duration = 30

    sz = form.get("size")
    rt = form.get("ratio")
    size_str = str(sz) if sz and isinstance(sz, str) else None
    ratio_str = str(rt) if rt and isinstance(rt, str) else "16:9"
    ratio = _resolve_ratio(size_str, ratio_str)

    pt_val = form.get("preflight_token")
    preflight_token = str(pt_val).strip() if pt_val and isinstance(pt_val, str) else None

    cr_val = form.get("confirm_review")
    confirm_review = str(cr_val).lower() in ("true", "1", "yes")

    refs = await _admin_reference_images(form)

    admin_client = {
        "api_key_hash": f"admin_{session_id}",
        "api_key_name": f"Admin ({session_id})",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }

    request_id = str(form.get("request_id") or "").strip()
    task_id = None
    if request_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            raise HTTPException(422, "Mã yêu cầu không hợp lệ")
        username = ADMIN_SESSIONS[x_admin_key]["username"]
        task_id = "video_" + hashlib.sha256(f"studio:{username}:{request_id}".encode()).hexdigest()[:32]
    resolved = _resolve_model_for_duration(model, duration)
    if not resolved:
        raise HTTPException(422, "Model không hỗ trợ thời lượng đã chọn")
    try:
        roles = json.loads(str(form.get("reference_roles") or "[]"))
    except ValueError as exc:
        raise HTTPException(422, "Vai trò ảnh tham chiếu không hợp lệ") from exc
    if not isinstance(roles, list) or len(roles) > len(refs) or any(role not in ("character", "scene", "style", "other") for role in roles):
        raise HTTPException(422, "Vai trò ảnh tham chiếu không hợp lệ")
    input_fp = context_fingerprint({"prompt": prompt, "model": resolved, "duration": duration,
                                    "ratio": ratio, "references": refs, "roles": roles})
    if task_id and (existing := store.get(task_id)):
        if (existing["prompt"], existing["model"], existing["duration"], existing["ratio"]) != (prompt, resolved, duration, ratio or "default"):
            raise HTTPException(409, "Mã yêu cầu đã dùng cho bản nháp khác")
        if existing.get("input_fingerprint") and existing["input_fingerprint"] != input_fp:
            raise HTTPException(409, "Ảnh hoặc cấu hình đã đổi; cần mã yêu cầu mới")
        return TaskResponse(**{k: existing.get(k) for k in ("id", "status", "model", "prompt", "video_url", "error")})
    source_task_id = str(form.get('source_task_id') or '')
    if source_task_id:
        source = store.get(source_task_id)
        if not source or source['status'] != 'failed':
            raise HTTPException(422, 'Chỉ liên kết bản sửa với tác vụ đã xác nhận thất bại')
    reference_context, frozen_refs = await _reference_context(refs, roles)
    context = _video_context(model, duration, ratio, reference_context)
    await _ensure_video_preflight(session_id, prompt, preflight_token, confirm_review, context)
    return await _submit_task(admin_client, resolved, prompt, ratio, duration, frozen_refs,
                              task_id=task_id, input_fingerprint=input_fp, source_task_id=source_task_id)


# 面板单文件前端（放最后，保证上面的路由优先）
class BatchPrompt(BaseModel):
    reference_indices: list[StrictInt] | None = None
    reference_roles: list[str] = Field(default_factory=list)
    group: str = Field(default="", max_length=200)
    prompt: str = Field(min_length=1, max_length=config.MAX_PROMPT_CHARS)
    preflight_token: str = ""
    confirm_review: bool = False


class VideoBatch(BaseModel):
    source_task_id: str | None = Field(default=None, max_length=200)
    name: str = Field(default="", max_length=100)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    prompts: list[BatchPrompt] = Field(min_length=1, max_length=50)
    copies: int = Field(default=1, ge=1, le=10)
    model: str = "seedance-2.5"
    duration: int = 30
    ratio: str = Field(default="16:9", pattern=r"^(16:9|9:16|1:1|4:3|3:4)$")


def _safe_task_filename(row, suffix='.mp4'):
    name = re.sub(r'[^A-Za-z0-9_-]', '_', row['id'])[:80]
    return f"{int(row.get('batch_index') or 0):03d}_{name}_{hashlib.sha256(row['id'].encode()).hexdigest()[:8]}{suffix}"


def _stored_video_path(row):
    url = row.get('video_url') or ''
    path = urlparse(url).path
    if not path.startswith('/videos/'):
        return None
    root = Path(config.DOWNLOAD_DIR).resolve()
    candidate = root / Path(path).name
    try:
        resolved = candidate.resolve(strict=True)
        if candidate.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        return resolved
    except OSError:
        return None


def _build_batch_zip(rows):
    fd, filename = tempfile.mkstemp(prefix='dola-batch-', suffix='.zip')
    os.close(fd)
    try:
        included, skipped = [], []
        with zipfile.ZipFile(filename, 'w', compression=zipfile.ZIP_STORED) as archive:
            for row in rows:
                path = _stored_video_path(row) if row.get('status') == 'completed' else None
                if not path:
                    skipped.append({'id': row['id'], 'reason_vi': 'Video chưa hoàn tất hoặc file không còn khả dụng'})
                    continue
                name = _safe_task_filename(row, path.suffix)
                try:
                    archive.write(path, name)
                    included.append({'id': row['id'], 'file': name})
                except OSError:
                    skipped.append({'id': row['id'], 'reason_vi': 'File không còn đọc được'})
            archive.writestr('ket-qua.json', json.dumps({'included': included, 'skipped': skipped}, ensure_ascii=False, indent=2))
        return filename
    except BaseException:
        Path(filename).unlink(missing_ok=True)
        raise


@app.get("/api/admin/videos/batches/{batch_id}/download")
async def admin_download_batch(batch_id: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if store.batch_fingerprint(batch_id) is None:
        raise HTTPException(404, "Không tìm thấy mẻ")
    path = await asyncio.to_thread(_build_batch_zip, store.batch_tasks(batch_id))
    return FileResponse(path, filename=_batch_download_name(batch_id), media_type='application/zip',
                        background=BackgroundTask(Path(path).unlink, missing_ok=True))


def _validate_export_directory(value):
    if not value:
        return ''
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('Chọn đường dẫn tuyệt đối đến thư mục trên máy chạy Studio')
    try:
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise OSError('not a directory')
        with tempfile.NamedTemporaryFile(dir=path, prefix='.dola-write-check-'):
            pass
    except OSError as exc:
        raise ValueError('Thư mục không tồn tại hoặc Studio không có quyền ghi') from exc
    return str(path)


class StudioOutputBody(BaseModel):
    export_directory: str = Field(default='', max_length=4000)


@app.get('/api/admin/studio-output')
async def admin_output_settings(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {'export_directory': store.studio_option('export_directory')}


@app.put('/api/admin/studio-output')
async def admin_set_output(body: StudioOutputBody, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        path = await asyncio.to_thread(_validate_export_directory, body.export_directory.strip())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    store.set_studio_option('export_directory', path)
    return {'export_directory': path}


@app.post('/api/admin/studio-output/open')
async def admin_open_output(request: Request, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if (not request.client or request.client.host not in ('127.0.0.1','::1')
            or request.url.hostname not in ('127.0.0.1','localhost','::1')
            or any(h in request.headers for h in ('forwarded','x-forwarded-for','x-forwarded-host'))):
        raise HTTPException(403, 'Mở thư mục chỉ dùng khi truy cập trực tiếp trên máy chạy Studio; hãy tải ZIP')
    path = _validate_export_directory(store.studio_option('export_directory'))
    if not path:
        raise HTTPException(422, 'Chưa chọn thư mục xuất')
    await asyncio.to_thread(subprocess.run, ['open', path], check=True, timeout=10)
    return {'ok': True}


def _copy_video_export(task_id):
    value = store.studio_option('export_directory')
    if not value:
        return
    row = store.get(task_id)
    source = _stored_video_path(row)
    if not source:
        raise ValueError('File video gốc không còn khả dụng')
    destination = Path(_validate_export_directory(value))
    fd, temporary = tempfile.mkstemp(dir=destination, prefix='.dola-export-')
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination / _safe_task_filename(row, source.suffix))
    finally:
        Path(temporary).unlink(missing_ok=True)


def _batch_download_name(batch_id):
    label = re.sub(r'[^\w-]+', '_', store.batch_name(batch_id)).strip('_')[:80]
    return (label or 'dola-videos') + '.zip'


@app.get("/api/admin/videos/batches/{batch_id}")
async def admin_get_batch(batch_id: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if store.batch_fingerprint(batch_id) is None:
        raise HTTPException(404,"Không tìm thấy mẻ")
    return {"batch_id":batch_id,"name":store.batch_name(batch_id),"download_name":_batch_download_name(batch_id),"tasks":[{**row,**_task_display(row),"target_duration":_target_duration(row),
        "file_exists":_task_file_exists(row)} for row in store.batch_tasks(batch_id)]}


@app.post("/api/admin/videos/batches", status_code=202)
async def admin_create_batch(request: Request, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    form = await request.form()
    try:
        batch_data = json.loads(str(form.get("batch") or "{}"))
        request_id = batch_data.get("request_id", "") if isinstance(batch_data, dict) else ""
    except ValueError:
        request_id = ""
    async with _acceptance_lock(f"batch:{x_admin_key}:{request_id}"):
        return await _accept_create_batch(request, x_admin_key)


async def _accept_create_batch(request: Request, x_admin_key: str | None = None):
    _admin_auth(x_admin_key)
    form = await request.form()
    raw = str(form.get("batch") or "")
    if len(raw) > 1_100_000:
        raise HTTPException(422, "Danh sách prompt quá lớn")
    try:
        batch = VideoBatch.model_validate_json(raw)
    except ValueError:
        raise HTTPException(422, "Mẻ không hợp lệ: tối đa 50 video, 1–10 bản mỗi prompt; kiểm tra model, thời lượng và tỉ lệ")
    if len(batch.prompts)*batch.copies > 50 or any(not p.prompt.strip() for p in batch.prompts):
        raise HTTPException(422, "Mỗi mẻ có tối đa 50 video và không được có prompt trống")
    model = _resolve_model_for_duration(batch.model, batch.duration)
    if not model:
        raise HTTPException(422, "Model không hỗ trợ thời lượng đã chọn")
    refs = await _admin_reference_images(form)
    selections = []
    for prompt in batch.prompts:
        indices = prompt.reference_indices if prompt.reference_indices is not None else list(range(len(refs)))
        if (len(indices) != len(set(indices)) or any(type(i) is not int or i < 0 or i >= len(refs) for i in indices)):
            raise HTTPException(422, "Danh sách ảnh của nhóm không hợp lệ")
        selections.append(indices)
    username = ADMIN_SESSIONS[x_admin_key]["username"]
    batch_id = "batch_"+hashlib.sha256(f"studio:{username}:{batch.request_id}".encode()).hexdigest()[:32]
    saved_fingerprint = store.batch_fingerprint(batch_id)
    # Legacy receipts bound immutable inline bytes. Preserve them without another AI call.
    legacy_fp = hashlib.sha256(json.dumps({"prompts":[p.prompt.strip() for p in batch.prompts],
        "copies":batch.copies,"model":model,"duration":batch.duration,"ratio":batch.ratio,
        "references":refs},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    if (saved_fingerprint == legacy_fp and all(p.reference_indices is None and all(role == 'other' for role in p.reference_roles) for p in batch.prompts)
            and all(ref.startswith('data:') for ref in refs)):
        return {"batch_id":batch_id,"tasks":[{"id":r["id"],"status":r["status"]} for r in store.batch_tasks(batch_id)]}
    # Download/freeze each uploaded reference once; hash actual bytes before a receipt comparison.
    reference_context, frozen_refs = await _reference_context(refs, [])
    contexts, owned_refs = [], []
    for prompt, indices in zip(batch.prompts, selections):
        if len(prompt.reference_roles) > len(indices) or any(role not in ('character','scene','style','other') for role in prompt.reference_roles):
            raise HTTPException(422, "Vai trò ảnh của nhóm không hợp lệ")
        refs_context = [{**reference_context[i], 'role': prompt.reference_roles[n] if n < len(prompt.reference_roles) else 'other'} for n,i in enumerate(indices)]
        contexts.append(_video_context(model, batch.duration, batch.ratio, refs_context))
        owned_refs.append([frozen_refs[i] for i in indices])
    fingerprint = hashlib.sha256(json.dumps({'prompts':[{'prompt':p.prompt.strip(),'group':p.group,'context':c} for p,c in zip(batch.prompts,contexts)],
        'copies':batch.copies},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if saved_fingerprint is not None:
        if saved_fingerprint != fingerprint:
            raise HTTPException(409, "Mã mẻ đã được dùng cho nội dung khác")
        return {"batch_id":batch_id,"tasks":[{"id":r["id"],"status":r["status"]} for r in store.batch_tasks(batch_id)]}
    if batch.source_task_id:
        source = store.get(batch.source_task_id)
        if not source or source['status'] != 'failed':
            raise HTTPException(422, 'Chỉ liên kết bản sửa với tác vụ đã xác nhận thất bại')
    session_id = hashlib.sha256((x_admin_key or "default").encode()).hexdigest()[:24]
    for prompt, context in zip(batch.prompts, contexts):
        await _ensure_video_preflight(session_id, prompt.prompt.strip(), prompt.preflight_token,
                                      prompt.confirm_review, context)
    if not pool.accounts:
        raise HTTPException(503, "Chưa có tài khoản Dola")
    if not pool.available and (pool.all_accounts_limited or pool.all_accounts_quota_blocked):
        raise HTTPException(429, "Tài khoản đã hết lượt hoặc chưa đủ điểm; mẻ chưa được gửi")
    try:
        refs = await validate_reference_urls(refs)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    client = {"api_key_hash":f"admin_{session_id}","api_key_name":f"Admin ({session_id})",
              "daily_limit":0,"concurrency_limit":0,"allowed_durations":list(SUPPORTED_DURATIONS)}
    jobs=[];created=False
    try:
        for prompt, prompt_refs in zip(batch.prompts, owned_refs):
            for _ in range(batch.copies):
                # Each job owns its references; one finishing must not delete another's input.
                images = await asyncio.to_thread(materialize_data_urls,prompt_refs)
                jobs.append({"id":"video_"+hashlib.sha256(f"{batch_id}:{len(jobs)}".encode()).hexdigest()[:32],
                    "model":model,"prompt":prompt.prompt.strip(),"duration":batch.duration,"ratio":batch.ratio,
                    "reference_images":json.dumps(images,ensure_ascii=False),"api_key_hash":client["api_key_hash"],
                    "api_key_name":client["api_key_name"],"client_concurrency_limit":0,"source_task_id":batch.source_task_id})
        try:
            rows,created = store.create_batch(batch_id,fingerprint,jobs,max_pending=config.MAX_PENDING_TASKS,name=batch.name.strip())
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc
        except PendingTaskLimitExceeded as exc:
            raise HTTPException(429,str(exc)) from exc
        if created:
            for row in rows:
                asyncio.create_task(_run_task(row["id"],model,row["prompt"],batch.ratio,batch.duration,
                    _task_reference_images(row["reference_images"]),client))
        return {"batch_id":batch_id,"tasks":[{"id":r["id"],"status":r["status"]} for r in rows]}
    finally:
        if not created:
            for job in jobs:
                cleanup_local_references(_task_reference_images(job["reference_images"]))


app.mount("/", StaticFiles(directory="web", html=True), name="web")
