"""视频超分：把已完成任务的 720p 原片交给外部服务放大到 1080p。

服务商（provider）：
  - fal：fal.ai 上的字节官方超分 fal-ai/bytedance-upscaler/upscale/video（按输出秒数计费，
    1080p/30fps 标准档 $0.0072/秒，10 秒约 $0.07）。
  - open：自建开源超分模型（Real-ESRGAN、FlashVSR 等）的 HTTP 服务，接口约定见 UPSCALE.md。

两种服务商都是「提交 → 轮询」：submit() 返回可序列化的 job（存进任务表，服务重启后接着查），
poll() 返回 pending / completed / failed。号池只负责调度和记录，不在本机跑模型（服务器没有 GPU）。
"""
from __future__ import annotations

import json
import os

import aiohttp

import config

PROVIDERS = ("fal", "open", "custom")
PROVIDER_LABELS = {"fal": "fal（字节超分）", "open": "自建开源模型", "custom": "自定义 API"}
FAL_QUEUE = "https://queue.fal.run"
FAL_MODEL = os.getenv("DOLA_FAL_UPSCALE_MODEL", "fal-ai/bytedance-upscaler/upscale/video")
FAL_PRICE_PER_SECOND = 0.0072          # 1080p / 30fps / standard（fal 模型页，2026-10）
TARGETS = ("1080p", "2k", "4k")
PRESETS = ("aigc", "general", "ugc", "short_series", "old_film")
SETTINGS_KEY = "upscale"
AUTO_MODES = ("off", "param", "param_or_size")
_RESOLUTION_ALIASES = {
    "480p": "720p", "480": "720p", "540p": "720p", "720p": "720p", "720": "720p", "hd": "720p",
    "1080p": "1080p", "1080": "1080p", "fhd": "1080p", "fullhd": "1080p",
    "2k": "2k", "1440p": "2k", "1440": "2k", "qhd": "2k",
    "4k": "4k", "2160p": "4k", "2160": "4k", "uhd": "4k",
}

DEFAULTS = {
    "provider": os.getenv("DOLA_UPSCALE_PROVIDER", "fal").strip() or "fal",
    "target": "1080p",
    "fal_key": os.getenv("DOLA_FAL_KEY", "").strip(),
    "fal_preset": "aigc",
    "fal_tier": "standard",
    "fal_fidelity": "high",
    "open_endpoint": os.getenv("DOLA_UPSCALE_OPEN_URL", "").strip().rstrip("/"),
    "open_token": os.getenv("DOLA_UPSCALE_OPEN_TOKEN", "").strip(),
    # link：/v/<任务>-1080p.mp4 302 到服务商的结果地址；download：下载到服务器再提供
    "delivery": os.getenv("DOLA_UPSCALE_DELIVERY", "download").strip() or "download",
    # 按客户请求的分辨率自动超分：off 关闭（全部交原片）；param 只认请求里的 resolution 参数；
    # param_or_size 没有 resolution 时再按 size（如 1920x1080）的短边判断
    "auto": os.getenv("DOLA_UPSCALE_AUTO", "off").strip() or "off",
    # 自定义 API：任意「提交 → 轮询」或「同步返回」的 HTTP JSON 接口，按字段路径取值（见 UPSCALE.md）
    "custom_name": "",
    "custom_submit_url": "",
    "custom_headers": "",          # JSON 对象，如 {"Authorization": "Bearer sk-..."}；按密钥处理
    "custom_body": "",             # JSON 模板，占位符 {{video_url}} {{target}} {{duration}} {{task_id}}
    "custom_target_map": "",       # 目标分辨率改名，如 1080p=1080p,2k=2K,4k=4K
    "custom_id_path": "",          # 提交响应里任务 ID 的路径；留空 = 同步接口，结果直接在提交响应里
    "custom_poll_url": "",         # 查询地址，{{id}} 换成任务 ID
    "custom_status_path": "",
    "custom_success": "success,succeeded,completed,done",
    "custom_failed": "failed,failure,error,cancelled",
    "custom_result_path": "",
    "custom_error_path": "",
    "custom_code_path": "",        # 业务码路径（HTTP 200 但业务失败的接口用），如 code
    "custom_code_ok": "0",
}
SECRET_FIELDS = ("fal_key", "open_token", "custom_headers")
URL_FIELDS = ("open_endpoint", "custom_submit_url", "custom_poll_url")

# 面板「填入模板」：已核对文档的现成服务。密钥写在请求头里，填完把 sk-... 换成自己的。
CUSTOM_PRESETS = {
    "apiverse": {
        "custom_name": "Apiverse",
        "custom_submit_url": "https://api.apiverse.ai/api/v2/open/aigc/super-resolution",
        "custom_headers": '{"Authorization": "Bearer 你的 Apiverse Token"}',
        "custom_body": '{"videoUrl": "{{video_url}}", "quality": "{{target}}", "duration": "{{duration}}", '
                       '"taskNickname": "{{task_id}}"}',
        "custom_target_map": "", "custom_id_path": "data.taskId",
        "custom_poll_url": "https://api.apiverse.ai/api/v2/open/aigc/{{id}}",
        "custom_status_path": "data.status", "custom_success": "success", "custom_failed": "failed",
        "custom_result_path": "data.result.0", "custom_error_path": "data.errorMsg",
        "custom_code_path": "code", "custom_code_ok": "0",
        "docs_url": "https://docs.apiverse.ai/docs/video-super-resolution/",
    },
    "apibobo": {
        "custom_name": "波波 API",
        "custom_submit_url": "https://apibobo.com/v1/tools/run",
        "custom_headers": '{"Authorization": "Bearer sk-你的波波Key"}',
        "custom_body": '{"provider": "video", "endpoint": "/super-resolution", '
                       '"input": {"video": "{{video_url}}", "resolution": "{{target}}"}}',
        "custom_target_map": "1080p=1080p,2k=2K,4k=4K", "custom_id_path": "",
        "custom_poll_url": "", "custom_status_path": "", "custom_success": "success,succeeded,completed,done",
        "custom_failed": "failed,failure,error,cancelled",
        "custom_result_path": "out", "custom_error_path": "error", "custom_code_path": "", "custom_code_ok": "0",
        "key_url": "https://apibobo.com/console/token",
        "docs_url": "https://apibobo.com/content/tools/video-super-resolution/",
    },
}


class UpscaleError(RuntimeError):
    """配置缺失、服务商拒绝或结果不可用。消息给管理员看，不含密钥。"""


# ---------------------------------------------------------------- 配置

def load_settings(store) -> dict:
    raw = store.get_setting(SETTINGS_KEY) if store else None
    saved = {}
    if raw:
        try:
            saved = json.loads(raw)
        except ValueError:
            saved = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in saved.items() if k in DEFAULTS and v is not None})
    return out


def save_settings(store, patch: dict) -> dict:
    """合并保存。密钥字段：None / 不传 = 保持；空字符串 = 清除。"""
    current = load_settings(store)
    for key, value in patch.items():
        if key not in DEFAULTS or value is None:
            continue
        value = str(value).strip()
        if key == "provider" and value not in PROVIDERS:
            raise UpscaleError(f"provider 只能是 {', '.join(PROVIDERS)}")
        if key == "target" and value not in TARGETS:
            raise UpscaleError(f"target 只能是 {', '.join(TARGETS)}")
        if key == "fal_preset" and value not in PRESETS:
            raise UpscaleError(f"fal_preset 只能是 {', '.join(PRESETS)}")
        if key == "delivery" and value not in ("link", "download"):
            raise UpscaleError("delivery 只能是 link 或 download")
        if key == "auto" and value not in AUTO_MODES:
            raise UpscaleError(f"auto 只能是 {', '.join(AUTO_MODES)}")
        if key in URL_FIELDS:
            value = value.rstrip("/") if key == "open_endpoint" else value
            if value and not value.startswith(("http://", "https://")):
                raise UpscaleError(f"{key} 必须以 http:// 或 https:// 开头")
        if key in ("custom_headers", "custom_body") and value:
            try:
                parsed = json.loads(value)
            except ValueError as exc:
                raise UpscaleError(f"{key} 不是合法的 JSON：{exc}") from exc
            if not isinstance(parsed, dict):
                raise UpscaleError(f"{key} 必须是 JSON 对象")
            if key == "custom_headers" and not all(isinstance(v, str) for v in parsed.values()):
                raise UpscaleError("custom_headers 的值必须都是字符串")
        current[key] = value
    store.set_setting(SETTINGS_KEY, json.dumps(current, ensure_ascii=False))
    return current


def _mask(secret: str) -> str:
    return ("••••" + secret[-4:]) if len(secret) > 8 else ("••••" if secret else "")


def _headers_hint(raw: str) -> str:
    """自定义请求头只显示名字（值里通常是密钥）。"""
    try:
        names = list(json.loads(raw or "{}"))
    except ValueError:
        return ""
    return "、".join(names)


def public_settings(settings: dict) -> dict:
    """给面板看的配置：密钥只给是否已设置和末 4 位。"""
    out = {k: v for k, v in settings.items() if k not in SECRET_FIELDS}
    for key in SECRET_FIELDS:
        out[f"{key}_set"] = bool(settings.get(key))
        out[f"{key}_hint"] = (_headers_hint(settings.get(key) or "") if key == "custom_headers"
                              else _mask(settings.get(key) or ""))
    out["custom_presets"] = {name: {k: v for k, v in preset.items() if k != "custom_headers"}
                             | {"custom_headers_example": preset["custom_headers"]}
                             for name, preset in CUSTOM_PRESETS.items()}
    out["ready"] = {p: not missing_config(settings, p) for p in PROVIDERS}
    out["providers"] = [{"id": p, "label": PROVIDER_LABELS[p]} for p in PROVIDERS]
    out["fal_price_per_second"] = FAL_PRICE_PER_SECOND
    out["fal_model"] = FAL_MODEL
    return out


def missing_config(settings: dict, provider: str) -> str:
    """该服务商还缺什么配置；空字符串 = 可用。"""
    if provider == "fal":
        return "" if settings.get("fal_key") else "没有填写 fal API Key"
    if provider == "open":
        return "" if settings.get("open_endpoint") else "没有填写自建超分服务地址"
    if provider == "custom":
        if not settings.get("custom_submit_url") or not settings.get("custom_body"):
            return "自定义 API 还没有填写提交地址和请求体"
        if settings.get("custom_id_path") and not (settings.get("custom_poll_url") and settings.get("custom_status_path")):
            return "自定义 API 是异步接口，还缺查询地址或状态字段路径"
        if not settings.get("custom_result_path"):
            return "自定义 API 还没有填写结果地址的字段路径"
        return ""
    return f"未知的超分服务商 {provider}"


def estimate_cost(provider: str, seconds: float | None) -> float | None:
    if provider != "fal" or not seconds:
        return None
    return round(FAL_PRICE_PER_SECOND * float(seconds), 4)


# ---------------------------------------------------------------- 服务商

def _timeout(total: float = 60) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=total)


async def _json_or_error(resp, what: str) -> dict:
    text = await resp.text()
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {}
    if resp.status >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, list):
            detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
        raise UpscaleError(f"{what} 返回 HTTP {resp.status}：{str(detail or text)[:300]}")
    if not isinstance(data, dict):
        raise UpscaleError(f"{what} 返回的不是 JSON 对象")
    return data


async def submit(settings: dict, provider: str, source_url: str, *, fps: float | None = None,
                 duration: float | None = None, task_id: str = "", target: str | None = None) -> dict:
    """提交一条超分任务，返回可存库的 job 状态（不含密钥）。"""
    problem = missing_config(settings, provider)
    if problem:
        raise UpscaleError(problem)
    target = target or settings.get("target") or "1080p"
    if provider == "custom":
        return await _custom_submit(settings, source_url, target, duration, task_id)
    if provider == "fal":
        payload = {
            "video_url": source_url,
            "target_resolution": target,
            # Dola 原片 24fps：保持原帧率，不插帧（插帧会改动作节奏，且 60fps 价格翻倍）
            "target_fps": float(fps or 24),
            "enhancement_preset": settings.get("fal_preset") or "aigc",
            "enhancement_tier": settings.get("fal_tier") or "standard",
            "fidelity": settings.get("fal_fidelity") or "high",
        }
        async with aiohttp.ClientSession(timeout=_timeout()) as session:
            async with session.post(f"{FAL_QUEUE}/{FAL_MODEL}", json=payload,
                                    headers={"Authorization": f"Key {settings['fal_key']}"}) as resp:
                data = await _json_or_error(resp, "fal 提交")
        if not data.get("status_url") or not data.get("response_url"):
            raise UpscaleError("fal 提交成功但没有返回 status_url / response_url")
        return {"provider": "fal", "request_id": data.get("request_id"),
                "status_url": data["status_url"], "response_url": data["response_url"]}
    endpoint = settings["open_endpoint"]
    async with aiohttp.ClientSession(timeout=_timeout()) as session:
        async with session.post(f"{endpoint}/v1/upscale",
                                json={"video_url": source_url, "target_resolution": target,
                                      "fps": fps or None},
                                headers=_open_headers(settings)) as resp:
            data = await _json_or_error(resp, "自建超分服务提交")
    job_id = data.get("id") or data.get("job_id")
    if not job_id:
        raise UpscaleError("自建超分服务没有返回任务 id")
    return {"provider": "open", "id": str(job_id)}


def _open_headers(settings: dict) -> dict:
    token = settings.get("open_token") or ""
    return {"Authorization": f"Bearer {token}"} if token else {}


async def poll(settings: dict, job: dict) -> dict:
    """查一次：{"state": pending|completed|failed, "url", "error", "billed_seconds", "size"}。"""
    if job.get("provider") == "custom":
        return await _custom_poll(settings, job)
    if job.get("provider") == "fal":
        headers = {"Authorization": f"Key {settings.get('fal_key') or ''}"}
        async with aiohttp.ClientSession(timeout=_timeout()) as session:
            async with session.get(job["status_url"], headers=headers) as resp:
                status = await _json_or_error(resp, "fal 查询")
            state = str(status.get("status") or "").upper()
            if state in ("IN_QUEUE", "IN_PROGRESS"):
                return {"state": "pending", "queue_position": status.get("queue_position")}
            if state != "COMPLETED":
                return {"state": "pending"}
            if status.get("error"):
                return {"state": "failed", "error": f"fal：{str(status['error'])[:300]}"}
            async with session.get(job["response_url"], headers=headers) as resp:
                try:
                    result = await _json_or_error(resp, "fal 取结果")
                except UpscaleError as exc:
                    return {"state": "failed", "error": str(exc)}
        video = result.get("video") or {}
        if not video.get("url"):
            return {"state": "failed", "error": "fal 结果里没有视频地址"}
        return {"state": "completed", "url": video["url"], "size": video.get("file_size"),
                "billed_seconds": result.get("duration")}
    endpoint = settings.get("open_endpoint") or ""
    if not endpoint:
        return {"state": "failed", "error": "自建超分服务地址已被清空，无法继续查询"}
    async with aiohttp.ClientSession(timeout=_timeout()) as session:
        async with session.get(f"{endpoint}/v1/upscale/{job['id']}", headers=_open_headers(settings)) as resp:
            data = await _json_or_error(resp, "自建超分服务查询")
    state = str(data.get("status") or "").lower()
    if state in ("completed", "succeeded", "success"):
        if not data.get("video_url"):
            return {"state": "failed", "error": "自建超分服务结果里没有 video_url"}
        return {"state": "completed", "url": data["video_url"], "size": data.get("size"),
                "billed_seconds": data.get("duration")}
    if state in ("failed", "error", "cancelled"):
        return {"state": "failed", "error": f"自建超分服务：{str(data.get('error') or state)[:300]}"}
    return {"state": "pending"}


# ---------------------------------------------------------------- 自定义 API

def json_path(data, path: str):
    """按 a.b.0.c 取值；取不到返回 None。"""
    cur = data
    for part in [p for p in (path or "").split(".") if p != ""]:
        if isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _target_name(settings: dict, target: str) -> str:
    for pair in (settings.get("custom_target_map") or "").split(","):
        key, _, value = pair.partition("=")
        if key.strip().lower() == target.lower() and value.strip():
            return value.strip()
    return target


def render_template(template: str, values: dict):
    """JSON 模板填值：整个字符串就是 "{{duration}}" 时换成原类型（数字），其余按文本替换。"""
    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            for name, value in values.items():
                token = "{{" + name + "}}"
                if node == token:
                    return value
                node = node.replace(token, str(value))
        return node
    return walk(json.loads(template))


def _custom_headers(settings: dict) -> dict:
    headers = json.loads(settings.get("custom_headers") or "{}")
    return {"Content-Type": "application/json", **headers}


def _custom_check_code(settings: dict, data: dict, what: str) -> None:
    path = settings.get("custom_code_path")
    if not path:
        return
    code = json_path(data, path)
    if str(code) != str(settings.get("custom_code_ok") or "0"):
        message = json_path(data, settings.get("custom_error_path") or "") or data.get("msg") or data.get("message")
        raise UpscaleError(f"{what} 业务码 {code}：{str(message or '')[:200]}")


def _custom_words(raw: str) -> set[str]:
    return {w.strip().lower() for w in (raw or "").split(",") if w.strip()}


def _custom_result(settings: dict, data: dict, what: str) -> dict:
    url = json_path(data, settings.get("custom_result_path") or "")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise UpscaleError(f"{what} 里按 {settings.get('custom_result_path')} 取不到视频地址")
    return {"state": "completed", "url": url, "size": None, "billed_seconds": None}


async def _custom_submit(settings: dict, source_url: str, target: str, duration: float | None,
                         task_id: str) -> dict:
    body = render_template(settings["custom_body"], {
        "video_url": source_url, "target": _target_name(settings, target),
        "duration": max(1, int(-(-float(duration or 0) // 1))) if duration else 60, "task_id": task_id})
    sync = not settings.get("custom_id_path")
    async with aiohttp.ClientSession(timeout=_timeout(900 if sync else 60)) as session:
        async with session.post(settings["custom_submit_url"], json=body, headers=_custom_headers(settings)) as resp:
            data = await _json_or_error(resp, "自定义 API 提交")
    _custom_check_code(settings, data, "自定义 API 提交")
    if sync:   # 同步接口：结果直接在提交响应里
        return {"provider": "custom", "result": _custom_result(settings, data, "自定义 API 提交响应")}
    job_id = json_path(data, settings["custom_id_path"])
    if job_id in (None, ""):
        raise UpscaleError(f"自定义 API 提交响应里按 {settings['custom_id_path']} 取不到任务 ID")
    return {"provider": "custom", "id": str(job_id)}


async def _custom_poll(settings: dict, job: dict) -> dict:
    if job.get("result"):
        return job["result"]
    if not settings.get("custom_poll_url"):
        return {"state": "failed", "error": "自定义 API 的查询地址已被清空，无法继续查询"}
    from urllib.parse import quote
    url = settings["custom_poll_url"].replace("{{id}}", quote(job["id"], safe=""))
    async with aiohttp.ClientSession(timeout=_timeout()) as session:
        async with session.get(url, headers=_custom_headers(settings)) as resp:
            data = await _json_or_error(resp, "自定义 API 查询")
    try:
        _custom_check_code(settings, data, "自定义 API 查询")
    except UpscaleError as exc:
        return {"state": "failed", "error": str(exc)}
    state = str(json_path(data, settings.get("custom_status_path") or "") or "").lower()
    if state in _custom_words(settings.get("custom_success")):
        try:
            return _custom_result(settings, data, "自定义 API 查询结果")
        except UpscaleError as exc:
            return {"state": "failed", "error": str(exc)}
    if state in _custom_words(settings.get("custom_failed")):
        error = json_path(data, settings.get("custom_error_path") or "") or state
        return {"state": "failed", "error": f"自定义 API：{str(error)[:300]}"}
    return {"state": "pending"}


async def download(url: str, dest: str, *, limit_bytes: int = 500 * 1024 * 1024) -> int:
    """把结果下载到服务器（delivery=download）。返回字节数；超限或失败抛 UpscaleError。"""
    tmp = dest + ".part"
    size = 0
    try:
        async with aiohttp.ClientSession(timeout=_timeout(600)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise UpscaleError(f"下载超分结果失败：HTTP {resp.status}")
                with open(tmp, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        size += len(chunk)
                        if size > limit_bytes:
                            raise UpscaleError("超分结果超过 500 MB，已放弃下载")
                        fh.write(chunk)
        os.replace(tmp, dest)
        return size
    except UpscaleError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UpscaleError(f"下载超分结果失败：{type(exc).__name__}") from exc
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def normalize_resolution(value) -> str | None:
    """客户写法 → 720p / 1080p / 2k / 4k；认不出返回 None（按没写处理）。"""
    return _RESOLUTION_ALIASES.get(str(value or "").strip().lower().replace(" ", ""))


def resolution_from_size(size) -> str | None:
    """1920x1080 / 1080*1920 → 按短边判断档位。"""
    import re
    match = re.match(r"^\s*(\d{3,5})\s*[x×*:]\s*(\d{3,5})\s*$", str(size or ""), re.I)
    if not match:
        return None
    short = min(int(match.group(1)), int(match.group(2)))
    return "720p" if short <= 720 else "1080p" if short <= 1080 else "2k" if short <= 1440 else "4k"


def auto_plan(settings: dict, resolution, size=None) -> dict:
    """请求的分辨率 → 出片后要不要自动超分。

    返回 {"requested": 档位或 None, "auto": bool, "target": 档位, "provider": ..., "note": 不超分的原因}。
    720p（或没写）交原片；1080p 及以上且开启了自动超分、服务商可用，才超分到请求的档位。
    """
    mode = settings.get("auto") or "off"
    requested = normalize_resolution(resolution)
    if requested is None and mode == "param_or_size":
        requested = resolution_from_size(size)
    plan = {"requested": requested, "auto": False, "target": requested, "provider": settings.get("provider"),
            "note": ""}
    if requested in (None, "720p"):
        return plan
    if mode == "off":
        plan["note"] = f"请求 {requested}，但后台没有开启自动超分，交付原片 720p"
        return plan
    problem = missing_config(settings, settings.get("provider") or "")
    if problem:
        plan["note"] = f"请求 {requested}，但超分服务不可用（{problem}），交付原片 720p"
        return plan
    plan["auto"] = True
    return plan


def public_url(task_id: str, target: str | None) -> str:
    return f"{config.PUBLIC_BASE}/v/{task_id}-{target or '1080p'}.mp4"
