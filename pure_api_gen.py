"""cookie 账号纯 API 出片：复用 dola-pool-cookie 的协议（requests + bdms 签名）。

前提：服务器装有 node；accounts/<账号>/cookie_state.json 为该账号的 cookie 状态。
与浏览器出片不同，这里不启动浏览器，直接 requests 提交/轮询/下载。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import config
from pure_signer import install

log = logging.getLogger("dola.pure_api")

# 30 秒出片（2026-09-15 实测）：seedance-2.5 能直接出 30 秒成片，官方报价 6 点、
# 实际只扣 2 点。但只要我们回一句「拆成两段」，上游就把第二段当成新的「显式 15 秒
# 请求」按 6 点报价，余额不足 6 点时直接拒绝 → 整条 30 秒任务失败。
# 所以本服务**不再协商拆段**：只等上游自己出片；不足 30 秒时由调用方新开一条对话
# 补一段，再用 ffmpeg 本地拼接（见 _generate_capped_pair）。
NO_ACK_SECONDS = 420          # 提交后上游连「将消耗 N 点」都不回时的放弃阈值（秒）
                              # 实测上游 30 秒档回执延迟 8s~3.5min 不等，给足余量；
                              # 超过这个时间仍在「只有提示词」状态就放手换号，别空等到 30 分钟超时。
CONCAT_CONTAINER = "mp4"
FINAL_POLL_STATUSES = (
    "failed", "rate_limited", "quota_exceeded", "generation_voided",
    "submit_retry", "conversation_expired",
)

# 上游回执里的额度口径：官方报价与实际扣点可能不一致（30 秒报价 6 点、实际扣 2 点），
# 所以一律以「今日剩余 N 个视频生成额度」为准，实时回写到号池的账号额度上。
CREDITS_USED_RE = re.compile(r"将消耗\s*(\d+)\s*个视频生成额度")
CREDITS_LEFT_RE = re.compile(r"今日剩余\s*(\d+)\s*个视频生成额度")


class _CreditTracker:
    """从一条会话（或其中一段）的上游回执里提取额度消耗与剩余。

    链路文本每次轮询都会整段返回，所以按文本去重，避免同一句被重复累加。
    """

    def __init__(self, on_balance: Callable | None = None):
        self.on_balance = on_balance
        self.charged = 0
        self.left: int | None = None
        self._seen: set[str] = set()

    def note(self, texts) -> None:
        for raw in texts or []:
            text = str(raw)
            if text in self._seen:
                continue
            self._seen.add(text)
            used = CREDITS_USED_RE.search(text)
            if used:
                self.charged += int(used.group(1))
            left = CREDITS_LEFT_RE.search(text)
            if left:
                value = int(left.group(1))
                if value != self.left:
                    self.left = value
                    if self.on_balance:
                        try:
                            self.on_balance(value, "upstream")
                        except Exception as exc:
                            log.info("credit writeback warning: %s", str(exc)[:150])

    def absorb(self, other: "_CreditTracker") -> None:
        """把另一段（例如兜底补的第二段）的消耗并进来。"""
        self.charged += other.charged
        if other.left is not None:
            self.left = other.left

    def fields(self) -> dict[str, Any]:
        return {"credits_used": self.charged or None, "credits_left": self.left}


class PureApiError(RuntimeError):
    """纯 API 出片失败（含登录态失效/额度/风控等，由调用方决定换号/回退）。"""


class PureLoginExpired(PureApiError):
    """会话失效：请求后上游返回未登录。对应浏览器路径的 LoginExpiredError。"""


class ShortClipError(PureApiError):
    """上游只给了短视频（30 秒请求只回 ~15 秒）：默认不拼接，交给调用方换号重试。"""


# 落地成片短于目标时长的这个比例 → 视为「上游这次只给了短视频」。
SHORT_CLIP_RATIO = 0.8


def clip_is_short(actual: float | None, target: int) -> bool:
    """成片是否明显短于目标时长（例如 30 秒的请求只拿到 15 秒）。"""
    return actual is not None and actual < int(target) * SHORT_CLIP_RATIO


def _map_model(model: str, duration: int) -> str:
    m = (model or "").lower().replace("_", "-")
    if "2.5" in m:
        return "seedance_v2.5"
    return "seedance_v2.0"


def _dola():
    from protocol import dola_pure_api as module
    return module


def _load_state(state_file: Path) -> dict[str, Any]:
    if not state_file or not Path(state_file).exists():
        raise PureApiError(f"cookie 状态文件不存在: {state_file}")
    try:
        data = json.loads(Path(state_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PureApiError(f"cookie 状态文件无效: {state_file}") from exc
    if not isinstance(data, dict) or not data.get("cookies"):
        raise PureApiError(f"cookie 状态无 cookies: {state_file}")
    return data


def _looks_like_proxy_block(exc: Exception) -> bool:
    """异常是不是「代理出口把 CDN 拦了」这类和图片上传有关的问题。"""
    text = ("%s: %s" % (type(exc).__name__, exc)).lower()
    return any(k in text for k in (
        "proxy", "tunnel connection failed", "403", "connecttimeout",
    ))


def _upload_reference_images(dola, session, ctx, headers, paths, *,
                             state_file, region, pc_version) -> list[dict[str, Any]]:
    """把本地参考图逐张上传，返回可直接提交的 uri 列表。

    上传走 2 步（ApplyImageUpload 要打到金山 CDN：imagex-*.bytevcloudapi.com），
    实测部分代理出口对该域名 CONNECT 直接 403。此时若
    DOLA_REFERENCE_UPLOAD_DIRECT_FALLBACK 开启，就用直连重传一次
    （只有上传这一段换出口，生成仍旧走账号代理）。
    """
    imgs = dola.normalize_image_paths(paths)
    uploaded: list[dict[str, Any]] = []
    fallback = None
    try:
        for idx, img in enumerate(imgs, 1):
            try:
                info = dola.upload_image(session, ctx, headers, img)
            except Exception as exc:
                if not config.REFERENCE_UPLOAD_DIRECT_FALLBACK or not _looks_like_proxy_block(exc):
                    raise PureApiError(
                        f"参考图上传失败（{idx}/{len(imgs)} {img.name}）: {str(exc)[:200]}"
                    ) from exc
                log.warning("参考图走代理上传失败，改用直连重试（%s）: %s",
                            type(exc).__name__, str(exc)[:160])
                if fallback is None:
                    fallback = dola.setup_session(
                        region=region, pc_version=pc_version, proxy="",
                        login_pure=True, login_state_file=str(state_file))
                try:
                    info = dola.upload_image(fallback[0], fallback[1], fallback[2], img)
                except Exception as exc2:
                    raise PureApiError(
                        f"参考图直连上传也失败（{idx}/{len(imgs)} {img.name}）: {str(exc2)[:200]}"
                    ) from exc2
            uploaded.append(info)
            log.info("参考图 %s/%s 已上传 %s -> %s",
                     idx, len(imgs), img.name, str(info.get("uri"))[:80])
        return uploaded
    finally:
        if fallback is not None:
            fallback[0].close()


def _media_keys(poll) -> list[str]:
    """轮询结果里的成片标识，一片一个 key。

    优先按 vid 计数：同一支片的 1080p/720p 是两条 url 但只有一个 vid，
    早先 vid/url 混在一起会让「1 片」被当成「2 段」而重复拼接（30 秒变 60 秒）。
    只有拿不到 vid 时才退回 url。
    """
    vids: list[str] = []
    for vid in list(getattr(poll, "vids", None) or []):
        token = f"vid:{vid}"
        if token not in vids:
            vids.append(token)
    if vids:
        return vids
    keys: list[str] = []
    for url in list(getattr(poll, "urls", None) or []):
        token = f"url:{url}"
        if token not in keys:
            keys.append(token)
    return keys


def _clip_seconds(dola, poll) -> float | None:
    """从链路原始数据里读成片时长（秒）；读不到返回 None。"""
    data = getattr(poll, "source_data", None)
    if data is None:
        return None
    seconds: list[float] = []
    try:
        infos = dola.find_video_info_objects(data) or []
    except Exception:
        infos = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        for key, scale in (("duration", 1.0), ("video_duration", 1.0), ("duration_ms", 0.001)):
            value = info.get(key)
            if isinstance(value, (int, float)) and value > 0:
                seconds.append(float(value) * scale)
    return max(seconds) if seconds else None


def _wait_video_same_conversation(
    *,
    dola,
    session,
    ctx,
    headers,
    conversation_id: str,
    account: str,
    duration: int,
    deadline: float,
    poll_interval: int,
    on_poll: Callable | None,
    credits: "_CreditTracker | None" = None,
):
    """轮询出片，**不向上游协商拆段**。

    上游对 30 秒有两种反应（2026-09-15 实测）：
    1. 直接出片 —— 有时就是完整 30.04s（实际只扣 2 点），官方报价却是 6 点；
    2. 先回「视频生成目前支持 4~15 秒」再出 15 秒。
    早先遇到第 2 种就去回一句「拆成两段」，结果上游把第二段当成新的「显式 15 秒请求」
    按 6 点报价，余额不足 6 点直接拒绝，整条 30 秒任务失败。
    现在统一按上游自己的计划等出片：拿到几秒就是几秒，不足 30 秒由调用方
    （generate_pure_video 落地后 ffprobe 复核）新开一条对话补一段再本地拼接。
    """
    ack_deadline = min(deadline, time.time() + NO_ACK_SECONDS)
    acked = False
    split_noted = False
    errors = 0
    while True:
        try:
            poll = dola.inspect_video_once(session, ctx, conversation_id)
            errors = 0
        except Exception as exc:
            errors += 1
            log.info("pure poll: 轮询失败 %s (%s/5): %s", account, errors, str(exc)[:200])
            if errors >= 5 or time.time() >= deadline:
                raise PureApiError(f"{account} 轮询 Dola 任务失败: {str(exc)[:200]}") from exc
            time.sleep(poll_interval)
            continue
        status = str(getattr(poll, "status", "") or "")
        texts = [str(item) for item in (getattr(poll, "texts", None) or [])]
        if credits is not None:
            credits.note(texts)
        if any("视频生成额度" in item for item in texts):
            acked = True
        if on_poll:
            try:
                on_poll(time.time())
            except Exception:
                pass
        if duration < 30:
            return poll, []
        clip = _clip_seconds(dola, poll)
        if clip is not None and clip >= duration * 0.9:
            # 上游直出完整时长（实测 30.042s / 2 点）：不需要补段与拼接。
            log.info("pure poll: %s 上游直出 %.1fs 成片", account, clip)
            return poll, []
        keys = _media_keys(poll)
        if len(keys) >= 2:
            # 上游自己出了两段：交给调用方拼接。
            return poll, keys[-2:]
        if status == "succeeded" or status in FINAL_POLL_STATUSES:
            return poll, []
        if not split_noted and dola.poll_has_duration_split(poll, job_seconds=duration):
            # 只记录，不再回「拆两段」（那会把第二段抬到 6 点报价）。
            split_noted = True
            log.info("pure poll: %s 上游称只支持 4~15 秒，不协商拆段，等上游出片后本地补段", account)
        if not acked and time.time() >= ack_deadline:
            # 提交成功但上游一直不回话（无额度提示、无状态）：早点放手换号，别干等 30 分钟。
            log.info("pure poll: %s 上游 %ds 无任何回执，放弃本次", account, NO_ACK_SECONDS)
            return poll, []
        if time.time() >= deadline:
            return poll, []
        time.sleep(poll_interval)


def _result_from_poll(*, dola, session, ctx, conversation_id: str, poll,
                      remove_watermark: bool, proxy: str) -> dict[str, Any]:
    """把轮询结果整理成与 dola.get_video_result 同构的返回体（含可下载 URL）。"""
    result: dict[str, Any] = {
        "conversation_id": conversation_id,
        "status": poll.status,
        "urls": poll.urls,
        "vids": poll.vids,
        "failure_reasons": poll.failure_reasons,
        "texts": poll.texts,
        "creation_statuses": poll.creation_statuses,
        "wait_minutes": poll.wait_minutes,
    }
    if poll.status != "succeeded":
        return result
    play_info = dola.fetch_play_info_urls(session, ctx, poll.vids) if poll.vids else {}
    play_info_urls = list(play_info.get("urls") or [])
    source_url = (play_info_urls or poll.urls or [""])[0]
    result.update({
        "play_info_urls": play_info_urls,
        "fplay_urls": list(play_info.get("fplay_urls") or []),
        "video_infos_count": len(play_info.get("video_infos") or []),
        "source_url": source_url,
        "download_url": source_url,
        "download_source": "fallback",
        "direct_unwatermarked": False,
        "remove_watermark": bool(remove_watermark),
        "nowatermark_api_used": False,
        "nowatermark_api_error": "",
    })
    if source_url:
        chosen = dola.choose_video_download_url(
            source_url,
            play_info={"chain_data": poll.source_data, "play_info": play_info},
            referer=f"{dola.BASE}/chat/{conversation_id}",
            remove_watermark=remove_watermark,
            proxies=dola.proxy_dict(proxy),
        )
        result.update(chosen)
    return result


# 30 秒在上游是「报价 6 点、实际只扣 2 点」的一档：失败时给出可执行的中文说明，
# 而不是只把上游原文抛给调用方（原文里没有「30 秒」这个上下文）。
SPLIT_CREDIT_HINT = (
    "30 秒本次被上游拒绝（额度/限流/无响应）；"
    "可换当日额度充足的账号重试，或等每日额度刷新（日本时间 11:00）"
)
CREDIT_SHORT_MARKERS = ("视频生成额度", "额度不足", "积分不足")

# 补段路线：显式请求 15 秒会被上游按 12~18 点计费，而「请求 30 秒 → 上游自己压到 15 秒」
# 实测只扣 2 点。所以 30 秒成片不足时，改用独立的「30 秒请求」各拿一段 15 秒，
# 再本地拼成 30 秒（总消耗 2+2=4 点，正好是一个免费号当日额度）。
PART_HINTS = (
    "【这是 30 秒成片的前 15 秒】只生成前 15 秒的画面，结尾保持可自然衔接下一段。",
    "【这是 30 秒成片的后 15 秒】承接上一段同样的画面、人物与镜头运动继续，只生成后 15 秒。",
)


def _credit_short(detail: str) -> bool:
    return any(marker in (detail or "") for marker in CREDIT_SHORT_MARKERS)


def _generate_capped_pair(*, dola, session, ctx, headers, account: str, prompt: str,
                          ratio: str | None, model: str, output_path: str | Path,
                          remove_watermark: bool, proxy: str, timeout: int,
                          poll_interval: int, on_poll: Callable | None = None,
                          on_conversation_id: Callable | None = None,
                          existing_parts: list[Path] | None = None,
                          images: list | None = None,
                          credits: "_CreditTracker | None" = None) -> Path:
    """两段独立出片（各由上游压到 15 秒）后本地拼成 30 秒。

    existing_parts：已经拿到的分段（例如上游静默压时长后落地的第一段），
    只补生成缺的那几段再拼接。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="dola-pair-"))
    try:
        parts: list[Path] = list(existing_parts or [])
        for index, hint in enumerate(PART_HINTS[len(parts):], start=len(parts) + 1):
            part_credits = _CreditTracker(credits.on_balance if credits else None)
            submit = dola.submit_chat_completion(
                session, ctx, headers, images=images or [], audios=[],
                prompt=f"{prompt}\n\n{hint}",
                duration=30, model=model, ratio=ratio,
                timeout_sec=180, video_ability=True,
            )
            if submit.error:
                raise PureApiError(f"{account} 第 {index} 段提交失败: {str(submit.error)[:200]}")
            if not submit.conversation_id:
                raise PureApiError(f"{account} 第 {index} 段未返回 conversation_id")
            if index == 1 and on_conversation_id:
                try:
                    on_conversation_id(account, submit.conversation_id, time.time() + timeout)
                except Exception:
                    pass
            result = dola.get_video_result(
                session, ctx, submit.conversation_id,
                timeout=timeout, interval=poll_interval,
                remove_watermark=remove_watermark, proxy=proxy or "",
            )
            part_credits.note(result.get("texts"))
            part_credits.note(result.get("failure_reasons"))
            if credits is not None:
                credits.absorb(part_credits)
            if result.get("status") != "succeeded":
                detail = " | ".join(
                    str(item) for item in (result.get("failure_reasons") or []) + (result.get("texts") or [])
                )[:300]
                raise PureApiError(
                    f"{account} 第 {index} 段出片未成功 status={result.get('status')}: {detail}"
                )
            url = result.get("download_url") or result.get("source_url") or ""
            if not url:
                raise PureApiError(f"{account} 第 {index} 段没有可下载的视频地址")
            part = tmp_dir / f"part{index}.mp4"
            dola.download_video(
                url, str(part),
                referer=f"{dola.BASE}/chat/{submit.conversation_id}", proxy=proxy or "",
            )
            parts.append(part)
            log.info("pure pair: %s 第 %s 段完成", account, index)
        return _concat_videos(parts, Path(output_path))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _segment_media_url(dola, session, ctx, key: str, remove_watermark: bool, proxy: str) -> str:
    """把轮询里的媒体标识（vid:/url:）解析成可下载地址（尽量取高码率源）。"""
    vid = key[4:] if key.startswith("vid:") else ""
    source = key[4:] if key.startswith("url:") else ""
    play_info: dict[str, Any] = {}
    if vid:
        play_info = dola.fetch_play_info_urls(session, ctx, [vid])
        urls = list(play_info.get("urls") or [])
        source = urls[0] if urls else ""
    if not source:
        return ""
    chosen = dola.choose_video_download_url(
        source,
        play_info={"play_info": play_info},
        referer=f"{dola.BASE}/",
        remove_watermark=remove_watermark,
        proxies=dola.proxy_dict(proxy),
    )
    return str(chosen.get("download_url") or source)


def _media_duration(path: Path) -> float | None:
    """ffprobe 读实际时长；读不到返回 None（不阻断主流程）。"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float((proc.stdout or "").strip())
    except Exception:
        return None


def _video_codec(path: Path) -> str | None:
    """ffprobe 读视频流编码；读不到返回 None（不阻断主流程）。"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return (proc.stdout or "").strip().lower() or None
    except Exception:
        return None


def ensure_web_playable(path: str | Path) -> Path:
    """把上游的 HEVC 成片转成 H.264，浏览器 <video> 才能直接预览。

    上游出片统一是 HEVC（hvc1），Chrome/Edge/Safari 的 <video> 默认解不了，
    画布里只能下载不能预览；落地后统一转一次 H.264/AAC + faststart。
    转码失败时保留原片，不阻断出片。
    """
    out_path = Path(path)
    codec = _video_codec(out_path)
    if codec is None or codec in ("h264", "avc1"):
        return out_path
    tmp_path = out_path.with_name(f"{out_path.stem}.h264{out_path.suffix}")
    ok = _run_ffmpeg([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(out_path),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(tmp_path),
    ])
    if not ok or not tmp_path.exists() or tmp_path.stat().st_size == 0:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        log.info("web-compat: %s 转码失败，保留原片（%s）", out_path.name, codec)
        return out_path
    tmp_path.replace(out_path)
    log.info("web-compat: %s %s -> h264（浏览器可预览）", out_path.name, codec)
    return out_path


def _run_ffmpeg(cmd: list[str]) -> bool:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except FileNotFoundError as exc:
        raise PureApiError(
            "未安装 ffmpeg，无法把两段 15 秒拼接成 30 秒（apt-get install -y ffmpeg）"
        ) from exc
    except subprocess.SubprocessError:
        return False
    if proc.returncode != 0:
        log.info("ffmpeg failed: %s", (proc.stderr or "")[-400:])
    return proc.returncode == 0


def _concat_videos(parts: list[Path], output_path: Path) -> Path:
    """按顺序拼接多段视频：先流拷贝（无损、快），不兼容再重编码。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = output_path.with_suffix(".concat.txt")
    list_file.write_text(
        "\n".join(f"file '{part.as_posix()}'" for part in parts) + "\n", encoding="utf-8"
    )
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file)]
    ok = _run_ffmpeg([*base, "-c", "copy", str(output_path)])
    if not ok or not output_path.exists() or output_path.stat().st_size == 0:
        ok = _run_ffmpeg([
            *base, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
            str(output_path),
        ])
    try:
        list_file.unlink()
    except OSError:
        pass
    if not ok or not output_path.exists() or output_path.stat().st_size == 0:
        raise PureApiError("两段视频拼接失败（ffmpeg 不可用或素材不兼容）")
    return output_path


def merge_split_segments(*, dola, session, ctx, segment_keys: list[str],
                         output_path: str | Path, account: str,
                         remove_watermark: bool, proxy: str) -> Path:
    """下载两段 15 秒并拼成一条 30 秒成片（上游不提供合成，只能本地拼）。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="dola-split-"))
    try:
        parts: list[Path] = []
        for index, key in enumerate(segment_keys, 1):
            url = _segment_media_url(dola, session, ctx, key, remove_watermark, proxy)
            if not url:
                raise PureApiError(f"{account} 第 {index} 段没有可下载的视频地址")
            part = tmp_dir / f"seg{index}.mp4"
            dola.download_video(url, str(part), referer=f"{dola.BASE}/", proxy=proxy)
            parts.append(part)
        log.info("pure split: %s 两段已下载，开始拼接 -> %s", account, output_path)
        return _concat_videos(parts, Path(output_path))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def probe_login(
    state_file: str | Path,
    proxy: str = "",
    region: str | None = None,
    pc_version: str | None = None,
) -> tuple[bool, str]:
    """纯 API 登录探活（对齐 dola-pool-cookie 的 protocol.probe_login）。

    只发几个 requests（get_web_anon_id / launch），用 x-tt-agw-login 头判断登录态；
    不启动浏览器，因此不占用 account 的浏览器 profile。返回 (是否登录, 原因)。
    """
    dola = _dola()
    region = region or config.PURE_API_REGION
    pc_version = pc_version or config.PURE_API_PC_VERSION
    session = None
    try:
        session, ctx, headers = dola.setup_session(
            region=region,
            pc_version=pc_version,
            proxy=proxy or "",
            login_pure=True,
            login_state_file=str(state_file),
        )
        dola.warmup_login_session(session, headers)
        resp = dola.dola_api_post_response(
            session, ctx, "/alice/user/get_web_anon_id", {}, label="get_web_anon_id"
        )
        data = dola.request_json(resp, "get_web_anon_id")
        dola.assert_ok(data, "get_web_anon_id")
        agw = dola.agw_login_flag(resp)
        if agw != "1":
            return False, f"not logged in (x-tt-agw-login={agw or 'missing'})"
        try:
            dola.dola_api_post_json(session, ctx, "/alice/user/launch", {}, label="launch")
        except Exception:
            pass
        return True, "logged_in"
    except Exception as exc:
        return False, str(exc).replace("\n", " ")[:400]
    finally:
        if session is not None:
            session.close()


def generate_pure_video(
    *,
    account: str,
    prompt: str,
    ratio: str | None,
    duration: int,
    model: str,
    cookie_state_path: str | Path,
    proxy: str = "",
    output_dir: str | Path = "",
    region: str | None = None,
    pc_version: str | None = None,
    remove_watermark: bool | None = None,
    timeout: int | None = None,
    poll_interval: int | None = None,
    on_conversation_id: Callable | None = None,
    on_poll: Callable | None = None,
    on_balance: Callable | None = None,
    reference_image_paths: list | None = None,
) -> dict[str, Any]:
    """提交一次视频生成并出片，返回 {'local_path','account','conversation_id',...}。

    on_balance(剩余额度, 来源)：从上游回执「今日剩余 N 个视频生成额度」实时回写账号额度。
    返回体带 credits_used（本单上游实际扣的点数）与 credits_left（上游报的剩余点数）。
    """
    dola = _dola()
    credits = _CreditTracker(on_balance)
    # 安装持久 bdms 签名器（每个 Python 进程只装一次）。
    install()

    region = region or config.PURE_API_REGION
    pc_version = pc_version or config.PURE_API_PC_VERSION
    remove_watermark = (config.PURE_API_REMOVE_WATERMARK
                        if remove_watermark is None else bool(remove_watermark))
    timeout = timeout or config.PURE_API_TIMEOUT
    poll_interval = poll_interval or config.PURE_API_POLL_INTERVAL
    # 30 秒要先出片、落地复核，必要时再补一段并拼接，15 分钟不够。
    if int(duration or 0) >= 30:
        timeout = max(timeout, 1800)

    if reference_image_paths:
        # 带参考图时链路更长（上传 + 生成），按参考图专用超时兜底。
        timeout = max(timeout, config.REFERENCE_VIDEO_TIMEOUT)

    state_file = Path(cookie_state_path)
    _load_state(state_file)
    if not proxy:
        proxy = (config.PROXY or "").strip()

    session, ctx, headers = dola.setup_session(
        region=region,
        pc_version=pc_version,
        proxy=proxy or "",
        login_pure=True,
        login_state_file=str(state_file),
    )
    # 模拟 open_session 的探活/拉活。
    try:
        dola.warmup_login_session(session, headers)
    except Exception as exc:
        log.info("pure warmup_login_session warning %s: %s", account, str(exc)[:200])
    try:
        dola.dola_api_post_json(session, ctx, "/alice/user/get_web_anon_id", {}, label="get_web_anon_id")
    except Exception as exc:
        log.info("pure get_web_anon_id warning %s: %s", account, str(exc)[:200])
    try:
        dola.dola_api_post_json(session, ctx, "/alice/user/launch", {}, label="launch")
    except Exception as exc:
        log.info("pure launch warning %s: %s", account, str(exc)[:200])

    pure_model = _map_model(model, duration)
    # 参考图先上传拿到 uri，再和文本提示一起提交（协议层原本就支持）。
    uploaded_images: list[dict[str, Any]] = []
    if reference_image_paths:
        uploaded_images = _upload_reference_images(
            dola, session, ctx, headers, reference_image_paths,
            state_file=state_file, region=region, pc_version=pc_version)
    submit_prompt = (dola.enrich_multi_image_prompt(prompt, len(uploaded_images))
                     if uploaded_images else prompt)
    submit = dola.submit_chat_completion(
        session, ctx, headers,
        images=uploaded_images, audios=[], prompt=submit_prompt, duration=duration,
        model=pure_model, ratio=ratio, timeout_sec=180, video_ability=True,
    )
    if submit.error:
        msg = f"{submit.error_code or 'upstream'}: {submit.error}"
        if any(k in msg.lower() for k in ("login", "登录", "ログイン", "未登录", "anonymous")):
            raise PureLoginExpired(f"{account} 纯 API 会话失效: {msg[:200]}")
        raise PureApiError(f"{account} 提交失败: {msg[:300]}")
    if not submit.conversation_id:
        raise PureApiError(f"{account} 提交未返回 conversation_id")

    conversation_id = submit.conversation_id
    deadline = time.time() + timeout
    if on_conversation_id:
        try:
            on_conversation_id(account, conversation_id, deadline)
        except Exception:
            pass

    if int(duration) >= 30:
        # 30 秒：等上游出片（可能是完整 30 秒，也可能被压到 15 秒），落地后复核。
        poll, segments = _wait_video_same_conversation(
            dola=dola, session=session, ctx=ctx, headers=headers,
            conversation_id=conversation_id, account=account,
            duration=int(duration), deadline=deadline,
            poll_interval=poll_interval, on_poll=on_poll,
            credits=credits,
        )
        if segments:
            # 上游不会自己做剪辑合成，两段 15 秒齐了就在这里用 ffmpeg 拼成 30 秒。
            out_dir = Path(output_dir or config.DOWNLOAD_DIR)
            out_dir.mkdir(parents=True, exist_ok=True)
            merged_path = out_dir / f"{account}_{uuid.uuid4().hex[:12]}.{CONCAT_CONTAINER}"
            merged = merge_split_segments(
                dola=dola, session=session, ctx=ctx, segment_keys=segments,
                output_path=merged_path, account=account,
                remove_watermark=remove_watermark, proxy=proxy or "",
            )
            return {
                "local_path": str(merged),
                "account": account,
                "conversation_id": conversation_id,
                "download_url": "",
                "duration": int(duration),
                "model": pure_model,
                "pure_api": True,
                "merged_segments": len(segments),
                **credits.fields(),
            }
        result = _result_from_poll(
            dola=dola, session=session, ctx=ctx, conversation_id=conversation_id,
            poll=poll, remove_watermark=remove_watermark, proxy=proxy or "",
        )
    else:
        result = dola.get_video_result(
            session, ctx, conversation_id,
            timeout=timeout, interval=poll_interval,
            remove_watermark=remove_watermark, proxy=proxy or "",
        )
    credits.note(result.get("texts"))
    credits.note(result.get("failure_reasons"))
    status = result.get("status")
    if status != "succeeded":
        reasons = result.get("failure_reasons") or []
        texts = result.get("texts") or []
        detail = " | ".join(str(x) for x in (reasons + texts))[:300]
        if any(str(x).lower() in ("login", "未登录", "ログイン") for x in reasons + texts):
            raise PureLoginExpired(f"{account} 纯 API 轮询发现登录失效: {detail}")
        if int(duration) >= 30 and _credit_short(detail) and config.ALLOW_30S_PAIR:
            # 额度/限流类失败：只有在显式开启两段兜底时，才用「两条独立的 30 秒请求各被上游
            # 压成 15 秒」再本地拼接；默认不拼接，交给调用方换号重试拿原生 30 秒。
            try:
                out_dir = Path(output_dir or config.DOWNLOAD_DIR)
                out_dir.mkdir(parents=True, exist_ok=True)
                merged_path = out_dir / f"{account}_{uuid.uuid4().hex[:12]}.{CONCAT_CONTAINER}"
                merged = _generate_capped_pair(
                    dola=dola, session=session, ctx=ctx, headers=headers, account=account,
                    prompt=prompt, ratio=ratio, model=pure_model, output_path=merged_path,
                    remove_watermark=remove_watermark, proxy=proxy or "",
                    timeout=timeout, poll_interval=poll_interval,
                    on_poll=on_poll, on_conversation_id=on_conversation_id,
                    images=uploaded_images,
                    credits=credits,
                )
                return {
                    "local_path": str(merged),
                    "account": account,
                    "conversation_id": conversation_id,
                    "download_url": "",
                    "duration": int(duration),
                    "model": pure_model,
                    "pure_api": True,
                    "merged_segments": len(PART_HINTS),
                    "merge_mode": "capped_pair",
                    **credits.fields(),
                }
            except PureApiError as fallback_exc:
                raise PureApiError(
                    f"{account} 出片未成功 status={status}: {detail} —— {SPLIT_CREDIT_HINT}"
                    f"；低成本两段兜底也未成功：{fallback_exc}"
                ) from fallback_exc
        raise PureApiError(f"{account} 出片未成功 status={status}: {detail}")

    download_url = result.get("download_url") or result.get("source_url") or ""
    if not download_url:
        raise PureApiError(f"{account} 未解析到下载 URL")

    out_dir = Path(output_dir or config.DOWNLOAD_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{account}_{uuid.uuid4().hex[:12]}.mp4"
    try:
        saved = dola.download_video(
            download_url, str(out_path),
            referer=f"{dola.BASE}/chat/{conversation_id}", proxy="",
        )
    except Exception as exc:
        raise PureApiError(f"{account} 下载失败: {str(exc)[:200]}") from exc

    if int(duration) >= 30:
        # 上游有时「静默」把 30 秒压成 15 秒（不给明确提示），落地后用 ffprobe 复核实际时长。
        # 默认**不拼接**（30 秒要一气呵成）：只拿到短视频就当作本次失败，由调用方换号重试，
        # 直到某个号给出原生 30 秒成片；需要旧的两段兜底时把 DOLA_ALLOW_30S_PAIR 设为 1。
        actual = _media_duration(Path(saved))
        if clip_is_short(actual, int(duration)) and not config.ALLOW_30S_PAIR:
            raise ShortClipError(
                f"{account} 上游本次只给了 {actual:.1f} 秒成片（目标 {int(duration)} 秒），"
                "按「30 秒不拼接」策略换号重试"
            )
        if clip_is_short(actual, int(duration)):
            merged_path = out_dir / f"{account}_{uuid.uuid4().hex[:12]}.{CONCAT_CONTAINER}"
            merged = _generate_capped_pair(
                dola=dola, session=session, ctx=ctx, headers=headers, account=account,
                prompt=prompt, ratio=ratio, model=pure_model, output_path=merged_path,
                remove_watermark=remove_watermark, proxy=proxy or "",
                timeout=timeout, poll_interval=poll_interval,
                on_poll=on_poll, on_conversation_id=on_conversation_id,
                existing_parts=[Path(saved)],
                images=uploaded_images,
                credits=credits,
            )
            return {
                "local_path": str(merged),
                "account": account,
                "conversation_id": conversation_id,
                "download_url": download_url,
                "duration": int(duration),
                "model": pure_model,
                "pure_api": True,
                "merged_segments": len(PART_HINTS),
                "merge_mode": "partial_pair",
                "first_segment_duration": actual,
                **credits.fields(),
            }

    return {
        "local_path": str(saved),
        "account": account,
        "conversation_id": conversation_id,
        "download_url": download_url,
        "duration": int(duration),
        "model": pure_model,
        "pure_api": True,
        **credits.fields(),
    }
