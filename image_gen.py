"""豆包号的纯 API 生图（Seedream）。

协议（2026-10-19 抓包）：
- 提交：/chat/completion，消息文字「生成图片：<提示词>，<比例>」，
  chat_ability = {"ability_type": 3, "ability_param": "{\"ability_param\":{\"model\":\"Seedream 5.0 Flash\",
  \"ratio\":\"16:9\",\"input_box_content\":{...}},\"ability_type\":1}"}；模型名就是网页显示名。
- 没有确认步骤：图片直接在这次请求的流式回复里回来（creation_block → creations[].image），
  一次 1 张、2K 级分辨率。流里没拿到再去会话里找。
- 下载：官方 /creativity/resource/get_without_watermark（{"uri": [图片 key]}）。号在豆包设置里关了
  「AI 生成水印」就是干净图（复用视频那套自动关水印）；接口失败时退回回复里的正式下载原图（带标识）。

dola 网页的生图同一套协议，但 dola 的下载图带「Dola AI」标识、且没有官方关闭开关，所以不接 dola 生图。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import requests

import config
import sites
from pure_api_gen import PureApiError, PureLoginExpired, _load_state

log = logging.getLogger("dola.image")

# 无水印接口失败时退回的版本：平台给的正式下载原图（带标识）
FALLBACK_VARIANTS = ("image_ori",)
IMAGE_TIMEOUT = 180


class ImageRefusedError(PureApiError):
    """上游没出图、回了一段话（审核拒绝等）：换号也一样，不标记账号。"""


def _site_module(site: str):
    if site != sites.DOUBAO:
        raise PureApiError(f"生图只支持豆包号（收到站点 {site}）")
    from doubao_gen import _site
    return _site()


def _session(dp, site: str, state_file, proxy: str):
    from protocol import doubao_site
    return dp.setup_session(region=doubao_site.REGION, pc_version=doubao_site.PC_VERSION, proxy=proxy or "",
                            login_pure=True, login_state_file=str(state_file))


def image_payload_hook(prompt: str, upstream_model: str, ratio: str | None):
    """把一条普通文字消息改成网页「图像生成」发的请求体。"""
    def hook(payload: dict) -> None:
        # 豆包的智能体从文字里读比例（只放在 ability_param 里它按 1:1 出，2026-10-19 实测），
        # 所以比例同时写进文字；「自动」不写
        text = f"生成图片：{prompt}" + (f"，{ratio}" if ratio and ratio != "auto" else "")
        for message in payload.get("messages") or []:
            for block in message.get("content_block") or []:
                tb = (block.get("content") or {}).get("text_block")
                if isinstance(tb, dict):
                    tb["text"] = text
        param: dict[str, Any] = {"model": upstream_model}
        if ratio:
            param["ratio"] = ratio
        param["input_box_content"] = {"user_input_content": prompt, "reply_message_format": "生成图片：%s"}
        payload["chat_ability"] = {
            "ability_type": 3,
            "ability_param": json.dumps({"ability_param": param, "ability_type": 1},
                                        ensure_ascii=False, separators=(",", ":")),
        }
        ext = payload.setdefault("ext", {})
        ext.pop("use_deep_think", None)
        ext["answer_with_suggest"] = "0"
        ext["sub_conv_firstmet_type"] = "1"
    return hook


def _walk(obj, out: list) -> None:
    if isinstance(obj, str):
        s = obj.strip()
        if s[:1] in "[{" and "creation" in s:
            try:
                _walk(json.loads(s), out)
            except ValueError:
                pass
        return
    if isinstance(obj, dict):
        block = obj.get("creation_block")
        if isinstance(block, dict):
            out.extend(c for c in block.get("creations") or [] if isinstance(c, dict))
        for value in obj.values():
            _walk(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _walk(value, out)


def find_images(obj) -> list[dict]:
    """回复 / 会话里出好的图（按 key 去重、保持顺序）。占位（只有 status/placeholder）不算。"""
    creations: list[dict] = []
    _walk(obj, creations)
    found: dict[str, dict] = {}
    for creation in creations:
        image = creation.get("image")
        if not isinstance(image, dict) or not image.get("key"):
            continue
        urls = {k: v.get("url") for k, v in image.items() if isinstance(v, dict) and v.get("url")}
        if not urls:
            continue
        ori = image.get("image_ori") or {}
        found[str(image["key"])] = {"key": str(image["key"]), "urls": urls,
                                    "width": ori.get("width"), "height": ori.get("height")}
    return list(found.values())


def _reply_text(result) -> str:
    texts = [str(t) for t in (getattr(result, "reply_texts", None) or []) if str(t).strip()]
    return (getattr(result, "final_reply_text", "") or " ".join(texts)).strip()


def _download(url: str, out_dir: Path, stem: str) -> Path:
    resp = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"},
                        proxies={"http": None, "https": None})
    if resp.status_code != 200 or not resp.content:
        raise PureApiError(f"图片下载失败 HTTP {resp.status_code}")
    ctype = (resp.headers.get("Content-Type") or "").lower()
    ext = "png" if "png" in ctype else "webp" if "webp" in ctype else "jpg"
    path = out_dir / f"{stem}.{ext}"
    path.write_bytes(resp.content)
    return path


def doubao_clean_urls(dp, session, ctx, keys: list[str]) -> tuple[dict[str, str], bool]:
    """豆包官方无水印地址（号关了 AI 水印时是干净图）。返回 ({key: url}, without_watermark)。"""
    data = dp.dola_api_post_json(session, ctx, "/creativity/resource/get_without_watermark",
                                 {"uri": keys}, label="get_without_watermark",
                                 referer=f"{dp.BASE}/chat/")
    body = data.get("data") or {}
    urls = {k: str((v or {}).get("url") or "") for k, v in (body.get("download_image") or {}).items()}
    return {k: u for k, u in urls.items() if u}, bool(body.get("without_watermark"))


def generate_image(*, account: str, prompt: str, model: str, cookie_state_path,
                   ratio: str | None = None, size: str | None = None, n: int | None = None,
                   reference_image_paths: list | None = None, proxy: str = "",
                   output_dir: str | Path = "", timeout: int | None = None) -> dict[str, Any]:
    spec = sites.image_spec(model)
    if spec is None:
        raise PureApiError(f"不是生图模型: {model}")
    site, upstream, ratios = spec
    ratio_value = sites.pick_image_ratio(ratio, size, ratios)
    state_file = Path(cookie_state_path)
    _load_state(state_file)
    dp = _site_module(site)
    session, ctx, headers = _session(dp, site, state_file, proxy)
    try:
        no_watermark = None
        if site == sites.DOUBAO:
            from doubao_gen import ensure_no_watermark
            no_watermark = ensure_no_watermark(dp, session, ctx, account)
        uploaded = []
        for path in (dp.normalize_image_paths(reference_image_paths) if reference_image_paths else []):
            try:
                uploaded.append(dp.upload_image(session, ctx, headers, path))
            except Exception as exc:  # noqa: BLE001
                raise PureApiError(f"参考图上传失败（{path.name}）: {str(exc)[:200]}") from exc
        started = time.time()
        result = dp.submit_chat_completion(
            session, ctx, headers, images=uploaded, audios=[], prompt=prompt, duration=5, model="",
            ratio=None, timeout_sec=int(timeout or IMAGE_TIMEOUT), video_ability=False,
            payload_hook=image_payload_hook(prompt, upstream, ratio_value))
        if result.error:
            msg = f"{result.error_code or 'upstream'}: {result.error}"
            if any(k in msg.lower() for k in ("login", "登录", "未登录", "anonymous", "710012001")):
                raise PureLoginExpired(f"{account} 会话失效: {msg[:200]}")
            raise PureApiError(f"{account} 生图提交失败: {msg[:300]}")
        conversation_id = result.conversation_id
        images = find_images([e.get("data") for e in result.events])
        if not images and conversation_id:
            # 流断了 / 图在流结束后才落地：去会话里找（最多再等一会儿）
            deadline = started + int(timeout or IMAGE_TIMEOUT)
            while time.time() < deadline and not images:
                try:
                    images = find_images(dp.fetch_single_chain(session, ctx, conversation_id))
                except Exception as exc:  # noqa: BLE001
                    log.info("image 会话轮询异常 %s: %s", account, str(exc)[:160])
                if not images:
                    time.sleep(5)
        reply = _reply_text(result)
        if not images:
            if reply:
                raise ImageRefusedError(f"{account} 没出图：{reply[:300]}")
            raise PureApiError(f"{account} 生图超时（{int(time.time() - started)} 秒内没有出图）")
        if n:
            images = images[:max(1, int(n))]

        clean: dict[str, str] = {}
        without_watermark = False
        if site == sites.DOUBAO:
            try:
                clean, without_watermark = doubao_clean_urls(dp, session, ctx, [img["key"] for img in images])
            except Exception as exc:  # noqa: BLE001
                log.warning("doubao 无水印图片地址获取失败 %s: %s", account, str(exc)[:160])
        out_dir = Path(output_dir or config.DOWNLOAD_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for idx, img in enumerate(images):
            url = clean.get(img["key"]) or ""
            source = "get_without_watermark" if url else ""
            if not url:
                for variant in FALLBACK_VARIANTS:
                    if img["urls"].get(variant):
                        url, source = img["urls"][variant], variant
                        break
            if not url:
                continue
            path = _download(url, out_dir, f"img_{account}_{uuid.uuid4().hex[:10]}_{idx}")
            saved.append({"local_path": str(path), "width": img.get("width"),
                          "height": img.get("height"), "source": source})
        if not saved:
            raise PureApiError(f"{account} 出图了但拿不到下载地址")
        unwatermarked = bool(no_watermark) and bool(clean) and without_watermark
        return {"images": saved, "account": account, "conversation_id": conversation_id,
                "model": upstream, "site": site, "ratio": ratio_value, "reply": reply[:300],
                "unwatermarked": unwatermarked, "elapsed": round(time.time() - started, 1)}
    finally:
        session.close()
