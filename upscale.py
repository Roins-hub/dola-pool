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

PROVIDERS = ("fal", "open")
PROVIDER_LABELS = {"fal": "fal（字节超分）", "open": "自建开源模型"}
FAL_QUEUE = "https://queue.fal.run"
FAL_MODEL = os.getenv("DOLA_FAL_UPSCALE_MODEL", "fal-ai/bytedance-upscaler/upscale/video")
FAL_PRICE_PER_SECOND = 0.0072          # 1080p / 30fps / standard（fal 模型页，2026-10）
TARGETS = ("1080p", "2k", "4k")
PRESETS = ("aigc", "general", "ugc", "short_series", "old_film")
SETTINGS_KEY = "upscale"

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
}
SECRET_FIELDS = ("fal_key", "open_token")


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
        if key == "open_endpoint":
            value = value.rstrip("/")
            if value and not value.startswith(("http://", "https://")):
                raise UpscaleError("自建模型地址必须以 http:// 或 https:// 开头")
        current[key] = value
    store.set_setting(SETTINGS_KEY, json.dumps(current, ensure_ascii=False))
    return current


def _mask(secret: str) -> str:
    return ("••••" + secret[-4:]) if len(secret) > 8 else ("••••" if secret else "")


def public_settings(settings: dict) -> dict:
    """给面板看的配置：密钥只给是否已设置和末 4 位。"""
    out = {k: v for k, v in settings.items() if k not in SECRET_FIELDS}
    for key in SECRET_FIELDS:
        out[f"{key}_set"] = bool(settings.get(key))
        out[f"{key}_hint"] = _mask(settings.get(key) or "")
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


async def submit(settings: dict, provider: str, source_url: str, *, fps: float | None = None) -> dict:
    """提交一条超分任务，返回可存库的 job 状态（不含密钥）。"""
    problem = missing_config(settings, provider)
    if problem:
        raise UpscaleError(problem)
    target = settings.get("target") or "1080p"
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


def public_url(task_id: str, target: str | None) -> str:
    return f"{config.PUBLIC_BASE}/v/{task_id}-{target or '1080p'}.mp4"
