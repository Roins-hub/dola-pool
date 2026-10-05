"""Tạo ảnh tham chiếu qua endpoint OpenAI-compatible /images/generations (9router...)."""
from __future__ import annotations

import asyncio
import base64
from typing import Any, Optional

import httpx

from ai_provider import AiProviderCallError, AiProviderNotConfiguredError, sanitize_secret

TIMEOUT_SECONDS = 180.0
MAX_PARALLEL = 4
SIZES = ("1024x1024", "1792x1024", "1024x1792")


async def generate_image(cfg: dict[str, Any], prompt: str, size: str = "1024x1024",
                         client: Optional[httpx.AsyncClient] = None) -> bytes:
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise AiProviderNotConfiguredError("Chưa cấu hình API Key. Vào Cài đặt → Kết nối AI → OpenAI-compatible.")
    url = cfg["base_url"].rstrip("/") + "/images/generations"
    body = {"model": cfg["model"], "prompt": prompt, "n": 1, "size": size, "response_format": "b64_json"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        r = await client.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            raise AiProviderCallError(f"Tạo ảnh lỗi {r.status_code}: {sanitize_secret(r.text[:300], key)}", r.status_code)
        item = (r.json().get("data") or [{}])[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):  # ponytail: chỉ tin URL do chính gateway trả về, không theo redirect ngoài
            img = await client.get(item["url"])
            if img.status_code == 200:
                return img.content
        raise AiProviderCallError("Gateway không trả dữ liệu ảnh")
    except httpx.TimeoutException as exc:
        raise AiProviderCallError("Hết thời gian chờ tạo ảnh") from exc
    except httpx.RequestError as exc:
        raise AiProviderCallError("Lỗi kết nối tới gateway ảnh: " + sanitize_secret(str(exc), key)) from exc
    finally:
        if own:
            await client.aclose()


async def generate_many(cfg: dict[str, Any], prompts: list[str], size: str,
                        client: Optional[httpx.AsyncClient] = None) -> list[dict[str, Any]]:
    """Chạy song song tối đa MAX_PARALLEL; một ảnh lỗi không làm hỏng cả mẻ."""
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def one(p: str) -> dict[str, Any]:
        async with sem:
            try:
                return {"prompt": p, "data": await generate_image(cfg, p, size, client)}
            except Exception as exc:  # noqa: BLE001 - trả lỗi theo từng ảnh
                return {"prompt": p, "error": str(exc)}

    return await asyncio.gather(*(one(p) for p in prompts))
