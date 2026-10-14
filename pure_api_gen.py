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
import threading
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
# 提交后上游连「将消耗 N 点」都不回时的放弃阈值（秒）。
# 2026-09-23 拍板：改为 5 分钟，且这种「派发后完全没有回应」不再只是换号了事 ——
# 该账号进【异常】组，任务报「生视频过程中出现异常情况，请重试」。
NO_ACK_SECONDS = config.NO_ACK_SECONDS
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


class UnsupportedDurationError(PureApiError):
    """上游明确表示这个时长不支持（如 30 秒只给 4~15 秒）：换号也一样，整个任务直接失败。"""


class PairSegmentShortError(UnsupportedDurationError):
    """两段拼接里有一段上游只给了短片（如「按默认 5 秒」）：拼不出目标时长。

    继承 UnsupportedDurationError → 号池直接失败、不换号：一单 pair 用掉一个满额号一整天的额度，
    换号重试只会再烧一个号。"""


class ShortClipError(PureApiError):
    """上游只给了短视频（30 秒请求只回 ~15 秒）：默认不拼接，交给调用方换号重试。"""


class AbnormalNoAckError(PureApiError):
    """派发后 NO_ACK_SECONDS 内上游没有任何回应：账号判【异常】，任务提示重试。"""


# 异常类失败的统一用户话术（拍板要求）
ABNORMAL_RETRY_HINT = "生视频过程中出现异常情况，请重试"

# 等待出片期间的心跳间隔（秒）：短时长任务走 get_video_result（内部轮询不回传进度），
# 不给任务库刷 last_poll_at 的话，看门狗 10 分钟就会判「卡住」→ 重新派发 →
# 等于把同一条视频重新投一次（白扣额度）。所以这里起一个心跳线程持续刷进度。
PROGRESS_TICK_SECONDS = 60


# 落地成片短于目标时长的这个比例 → 视为「上游这次只给了短视频」。
SHORT_CLIP_RATIO = 0.8


def clip_is_short(actual: float | None, target: int) -> bool:
    """成片是否明显短于目标时长（例如 30 秒的请求只拿到 15 秒）。"""
    return actual is not None and actual < int(target) * SHORT_CLIP_RATIO


def _map_model(model: str, duration: int) -> str:
    """把对外模型名映射成上游模型名。

    非 2.0 原生档位（含 30 秒与任意时长）只有 2.5 能出，所以这里按时长兜底，
    与 protocol.build_ability 的口径保持一致。
    """
    m = (model or "").lower().replace("_", "-")
    if "2.5" in m or "2-5" in m:
        return "seedance_v2.5"
    if int(duration or 0) not in config.V20_DURATIONS:
        return "seedance_v2.5"
    return "seedance_v2.0"


def _dola():
    from protocol import dola_pure_api as module
    # 任意时长上限由 config 注入：protocol 是 vendored 模块，不反向依赖本项目 config。
    module.set_native_duration_max(config.NATIVE_DURATION_MAX)
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
    cap_since = 0.0
    cap_said = ""
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
        _emit_upstream_note(on_poll, dola, texts)
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
        announced = _announced_seconds(dola, texts)
        if (not config.ALLOW_30S_PAIR and announced and announced * 2 < duration):
            # 上游只答应生成 N 秒，连两段都凑不够目标时长（2026-10-14 线上：30 秒任务上游回
            # 「我将为您生成一个5秒的视频…预计等待 45 分钟」）：等下去只会拿到短片，直接失败。
            raise UnsupportedDurationError(
                _cap_message(account, duration, _upstream_note(dola, texts))
                + f"（上游只答应生成 {announced} 秒）")
        if not split_noted and dola.poll_has_duration_split(poll, job_seconds=duration):
            # 只记录，不再回「拆两段」（那会把第二段抬到 6 点报价）。
            split_noted = True
            log.info("pure poll: %s 上游称只支持 4~15 秒，不协商拆段，等上游出片后本地补段", account)
            cap_since = time.time()
            cap_said = _upstream_note(dola, texts)
            if not config.ALLOW_30S_PAIR and "按默认5秒" in re.sub(r"\s+", "", cap_said):
                # 上游明确只给默认 5 秒：等下去也拿不到 30 秒（2026-10-13 线上：回完这句 30 分钟
                # 都没出片，任务白等到超时）。直接失败、不换号重试（换号上游也是同样回答）。
                raise UnsupportedDurationError(_cap_message(account, duration, cap_said))
        if (split_noted and not config.ALLOW_30S_PAIR and config.CAP_30S_GRACE_SECONDS > 0
                and time.time() - cap_since >= config.CAP_30S_GRACE_SECONDS):
            # 上游说只支持 4~15 秒后又等了一段时间仍没出片：不再等到总超时
            raise UnsupportedDurationError(
                _cap_message(account, duration, cap_said)
                + f"（上游表态后 {config.CAP_30S_GRACE_SECONDS // 60} 分钟仍未出片）")
        if not acked and time.time() >= ack_deadline:
            # 提交成功但上游一直不回话（无额度提示、无状态）：按拍板判【异常】，
            # 由调用方把该号放进异常组并换号重试。
            log.info("pure poll: %s 上游 %ds 无任何回执 → 判异常", account, NO_ACK_SECONDS)
            raise AbnormalNoAckError(
                f"{ABNORMAL_RETRY_HINT}（提交后 {NO_ACK_SECONDS} 秒内上游没有任何回执）")
        if time.time() >= deadline:
            return poll, []
        time.sleep(poll_interval)


def _progress_heartbeat(on_poll, *, interval: float | None = None):
    """等待出片期间的心跳：定期回调 on_poll，避免看门狗把正常等待误判成「卡住」重投。

    用上下文管理器包住可能长时间阻塞的同步调用（如 get_video_result），退出即停。
    """
    interval = PROGRESS_TICK_SECONDS if interval is None else interval

    class _Heartbeat:
        def __enter__(self):
            self._stop = threading.Event()
            if not on_poll:
                return self
            def _tick():
                while not self._stop.wait(interval):
                    try:
                        on_poll(time.time())
                    except Exception:
                        pass
            self._thread = threading.Thread(target=_tick, daemon=True)
            self._thread.start()
            return self

        def __exit__(self, *exc_info):
            stop = getattr(self, "_stop", None)
            if stop is not None:
                stop.set()
            thread = getattr(self, "_thread", None)
            if thread is not None:
                thread.join(timeout=1)
            return False

    return _Heartbeat()



# 取回时遇到这些终态就不用再等了：上游明确不会再出这条视频
RECOVER_FINAL_STATUSES = {"failed", "generation_voided", "quota_exceeded",
                          "conversation_expired", "submit_retry"}


def fetch_conversation_video(cookie_state_path: str | Path, conversation_id: str, *,
                             proxy: str = "", output_dir: str | Path | None = None,
                             remove_watermark: bool | None = None,
                             region: str | None = None, pc_version: str | None = None) -> dict:
    """查一次已有会话有没有出片（超时任务自动取回用）。**只查询，不提交任何新请求。**

    返回 {"status": ..., "local_path": 有片时的本地路径, "final": 上游已判死, "detail": ...}：
      - 出片了 → 下载到 output_dir，返回 local_path；
      - 上游明确失败（额度用完/作废/会话过期…）→ final=True，调用方停止盯守；
      - 其它（还在排队/生成）→ 都为空，调用方过一会儿再查。
    """
    dola = _dola()
    _load_state(Path(cookie_state_path))
    if remove_watermark is None:
        remove_watermark = config.PURE_API_REMOVE_WATERMARK
    session, ctx, _headers = dola.setup_session(
        region=region or config.PURE_API_REGION,
        pc_version=pc_version or config.PURE_API_PC_VERSION,
        proxy=proxy or "", login_pure=True, login_state_file=str(cookie_state_path))
    # timeout=1：wait_for_video 只查一轮就返回（没片时状态是 timeout，表示「还没好」）
    result = dola.get_video_result(session, ctx, conversation_id, timeout=1, interval=1,
                                   remove_watermark=bool(remove_watermark), proxy=proxy or "")
    status = str(result.get("status") or "")
    detail = " | ".join(str(x) for x in ((result.get("failure_reasons") or [])
                                         + (result.get("texts") or [])))[:300]
    url = result.get("download_url") or result.get("source_url") or ""
    if status == "succeeded" and url:
        out_dir = Path(output_dir or config.DOWNLOAD_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"recovered_{uuid.uuid4().hex[:12]}.mp4"
        saved = dola.download_video(url, str(out_path),
                                    referer=f"{dola.BASE}/chat/{conversation_id}", proxy="")
        return {"status": status, "local_path": str(saved), "final": False, "detail": detail,
                "conversation_id": conversation_id}
    return {"status": status, "local_path": "", "final": status in RECOVER_FINAL_STATUSES,
            "detail": detail, "conversation_id": conversation_id}


_ANNOUNCED_SECONDS = re.compile(
    r"(?:生成一个|生成|按默认|按最接近的支持时长|按最接近的时长|按)\s*(\d{1,2})\s*秒")


def _announced_seconds(dola, texts) -> int:
    """上游回执里宣布「要生成几秒」（如「我将为您生成一个5秒的视频」「按默认 5 秒」「按 15 秒生成」）。

    提示词回显不算；「支持 4 到 15 秒」这种范围说明不算（前面是「到」，不是「生成/按」）。
    """
    is_echo = getattr(dola, "is_prompt_echo_text", None) or (lambda _t: False)
    found = 0
    for text in texts or []:
        if is_echo(str(text)):
            continue
        for m in _ANNOUNCED_SECONDS.finditer(str(text)):
            found = int(m.group(1))
    return found


def _cap_message(account: str, duration: int, said: str) -> str:
    return (f"{account} dola 目前只支持 4~15 秒，{duration} 秒无法一次生成"
            f"（dola 原话：{(said or '')[:120]}）。需要 30 秒可把 DOLA_ALLOW_30S_PAIR 设为 1，"
            "用 2×15 秒本地拼接")


def _upstream_note(dola, texts) -> str:
    """上游回执里给人看的话（去掉提示词回显、去重），面板「dola：…」用。"""
    seen: list[str] = []
    is_echo = getattr(dola, "is_prompt_echo_text", None) or (lambda _t: False)
    for text in texts or []:
        text = str(text).strip()
        if not text or text in seen or is_echo(text):
            continue
        seen.append(text)
    return " ".join(seen)[:300]


def _emit_note_text(on_poll, note: str) -> None:
    """回传号池自己的进度说明（不是 dola 原话，如 30 秒的出片进度）。

    走 progress=：面板显示在「⏳」行，不进「dola：」，也不会把 dola 的原话挤掉。
    老的 on_poll(now, note=, wait_seconds=) 不认 progress 时退回 note=。
    """
    if not on_poll or not note:
        return
    try:
        on_poll(time.time(), progress=note)
    except TypeError:
        try:
            on_poll(time.time(), note=note, wait_seconds=0)
        except Exception:
            pass
    except Exception:
        pass


def _emit_upstream_note(on_poll, dola, texts) -> None:
    """把上游原话和它报的「预计等待」回传给调用方（落库，面板显示）。"""
    if not on_poll:
        return
    note = _upstream_note(dola, texts)
    if not note:
        return
    try:
        wait = (_wait_seconds_from_texts(dola, texts)
                if hasattr(dola, "extract_wait_minutes_from_text") else 0)
        on_poll(time.time(), note=note, wait_seconds=wait)
    except TypeError:
        pass        # 老调用方的 on_poll(now) 不收这两个参数：不回传即可
    except Exception:
        pass


def _wait_seconds_from_texts(dola, texts, *, latest: bool = False) -> int:
    """从上游回执里解析「预计等待 N 分钟 / N 小时 / 30-40 分钟」，返回秒数（取区间上限）。

    latest=True 从后往前找（dola 中途改口时取最新的预计）。
    """
    if not hasattr(dola, "extract_wait_minutes_from_text"):
        return 0
    for text in reversed(list(texts or [])) if latest else (texts or []):
        raw = dola.extract_wait_minutes_from_text(str(text))
        if not raw:
            continue
        seconds = 0
        hours = re.search(r"(\d+)(?:-(\d+))?小时", raw)
        if hours:
            seconds += int(hours.group(2) or hours.group(1)) * 3600
        minutes = re.search(r"(\d+)(?:-(\d+))?分钟", raw)
        if minutes:
            seconds += int(minutes.group(2) or minutes.group(1)) * 60
        if seconds:
            return seconds
    return 0


def _timeout_for_upstream_wait(base_timeout: int, wait_seconds: int) -> int:
    """按 dola 的「预计等待」动态定出片等待：X × FACTOR + MARGIN，不短于原超时、不超过 WAIT_HARD_MAX。"""
    if wait_seconds <= 0:
        return int(base_timeout)
    want = int(wait_seconds * config.UPSTREAM_WAIT_FACTOR) + int(config.UPSTREAM_WAIT_MARGIN)
    cap = max(int(base_timeout), int(config.WAIT_HARD_MAX))
    return min(max(int(base_timeout), want), cap)


def _wait_for_video(dola, session, ctx, conversation_id: str, *, account: str, started_at: float,
                    base_timeout: int, wait_seconds: int, poll_interval, remove_watermark: bool,
                    proxy: str, on_poll=None, on_deadline=None) -> dict:
    """等 dola 出片，上限跟着 dola 的预计走；到点时再读一次 dola 最新的预计，变长了就顺延。

    返回 get_video_result 的结果（到点仍没出片时 status=timeout）。
    """
    deadline = started_at + _timeout_for_upstream_wait(base_timeout, wait_seconds)
    while True:
        with _progress_heartbeat(on_poll):
            result = dola.get_video_result(
                session, ctx, conversation_id,
                timeout=max(60, int(deadline - time.time())), interval=poll_interval,
                remove_watermark=remove_watermark, proxy=proxy or "",
            )
        if result.get("status") != "timeout":
            return result
        newer = _wait_seconds_from_texts(dola, result.get("texts") or [], latest=True)
        extended = started_at + _timeout_for_upstream_wait(base_timeout, newer)
        if extended > deadline + 60 and extended > time.time() + 60:
            log.info("pure: %s dola 改口预计等待 %d 分钟，出片等待顺延到 %d 分钟",
                     account, newer // 60, int(extended - started_at) // 60)
            deadline = extended
            if on_deadline:
                try:
                    on_deadline(deadline)
                except Exception:
                    pass
            continue
        return result


def _await_first_ack(*, dola, session, ctx, account: str, conversation_id: str,
                     ack_deadline: float, poll_interval: float,
                     credits: "_CreditTracker | None" = None, on_poll=None) -> list[str]:
    """等「第一次回执」——拍板的 5 分钟规则，**所有时长都要过这一关**。

    提交成功后，上游必须在 NO_ACK_SECONDS 内有**任何**回应（额度播报/状态说明/报错），
    否则判【异常】：账号进异常组，任务提示「生视频过程中出现异常情况，请重试」。
    拿到回执就立刻返回，后续出片仍按原有超时继续等 —— 只约束"有没有人答话"。
    提示词回显（自己发的那段话）不算回执。
    """
    while time.time() < ack_deadline:
        try:
            poll = dola.inspect_video_once(session, ctx, conversation_id)
        except Exception as exc:
            log.info("first-ack: %s 轮询异常（继续等）: %s", account, str(exc)[:160])
            time.sleep(poll_interval)
            continue
        texts = [str(item) for item in (getattr(poll, "texts", None) or [])]
        reasons = [str(item) for item in (getattr(poll, "failure_reasons", None) or [])]
        if credits is not None:
            credits.note(texts)
        if on_poll:
            try:
                on_poll(time.time())
            except Exception:
                pass
        evidence = [t for t in (texts + reasons) if t.strip() and not dola.is_prompt_echo_text(t)]
        status = str(getattr(poll, "status", "") or "")
        if evidence or status not in ("", "pending"):
            _emit_upstream_note(on_poll, dola, evidence)
            return evidence
        time.sleep(poll_interval)
    raise AbnormalNoAckError(
        f"{ABNORMAL_RETRY_HINT}（派发后 {NO_ACK_SECONDS} 秒内上游没有任何回应）")



def _result_from_poll(*, dola, session, ctx, conversation_id: str, poll,
                      remove_watermark: bool, proxy: str) -> dict[str, Any]:
    """把轮询结果整理成与 dola.get_video_result 同构的返回体（含可下载 URL）。"""
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
# 分段提示词不写具体秒数，免得上游照着提示词里的数字理解时长（时长由请求参数决定）
PART_HINTS = (
    "【这是完整成片的前半段】只生成前半段的画面，结尾保持可自然衔接下一段。",
    "【这是完整成片的后半段】承接上一段同样的画面、人物与镜头运动继续，只生成后半段。",
)


def _credit_short(detail: str) -> bool:
    return any(marker in (detail or "") for marker in CREDIT_SHORT_MARKERS)


# 上游对「请求 30 秒」有时不直接受理，而是反问「只支持 4~15 秒，是否按 15 秒生成？请回复：确认生成」，
# 不回话它就一直等（stable-dola-pool 2.1.22 实测，长分镜提示词常见）。在同一会话回这句它才出片。
DURATION_CAP_CONFIRM_REPLY = "确认生成"
# 第一段成片达到请求时长的这个比例 → 上游这次原样出了 30 秒（10-02 实测 30.04 秒、只扣 2 点），直接交付
NATIVE_FULL_RATIO = 0.9
_QUESTION_MARKERS = ("是否", "请回复", "请确认", "需要你确认", "需要确认", "确认后", "要不要",
                     "可以吗", "？", "?", "doyouwant", "wouldyoulike", "shalli")


def _awaiting_confirmation(dola, texts) -> bool:
    """上游在等我们答话（反问时长 / 要不要拆段），而不是已经受理出片。

    已经播报「将消耗 N 个视频生成额度」的算受理，**不回话**：此时再回「确认生成」可能被当成新请求再扣一次。
    """
    is_echo = getattr(dola, "is_prompt_echo_text", None) or (lambda _t: False)
    accepted = getattr(dola, "looks_like_generation_accepted", None) or (lambda _t: False)
    confirm = getattr(dola, "looks_like_duration_confirm", None) or (lambda _t: False)
    capped = getattr(dola, "looks_like_duration_capped", None) or (lambda _t: False)
    own = [str(t) for t in texts or [] if str(t).strip() and not is_echo(str(t))]
    if not own or any(accepted(t) for t in own):
        return False
    for text in own:
        compact = re.sub(r"\s+", "", text).lower()
        if confirm(text) or (capped(text) and any(m in compact for m in _QUESTION_MARKERS)):
            return True
    return False


def _chat_reply(*, dola, session, ctx, headers, conversation_id: str, text: str,
                account: str = "") -> bool:
    """在**同一个会话**里回一句纯文本（video_ability=False），返回是否发出去（移植自 stable-dola-pool）。"""
    if not text or not conversation_id:
        return False
    section_id = ""
    last_index = None
    try:
        chain = dola.fetch_single_chain(session, ctx, conversation_id)
        section_id = dola.find_latest_section_id(chain) or ""
        last_index = dola.find_latest_message_index(chain)
    except Exception as exc:  # noqa: BLE001
        log.info("chat reply: %s 拉会话链失败（用空 section 继续）: %s", account, str(exc)[:120])
    try:
        result = dola.submit_chat_completion(
            session, ctx, headers, images=[], audios=[], prompt=text,
            duration=5, model="", ratio=None, timeout_sec=180,
            video_ability=False, conversation_id=conversation_id,
            section_id=section_id, last_message_index=last_index,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("chat reply: %s 同会话回话失败: %s", account, str(exc)[:180])
        return False
    return not getattr(result, "error", None)


def _await_acceptance(*, dola, session, ctx, account: str, conversation_id: str,
                      poll_interval: float, credits: "_CreditTracker | None" = None,
                      on_poll=None) -> list[str]:
    """回了「确认生成」之后，等上游真正受理（播报额度 / 出媒体 / 进终态）。"""
    accepted = getattr(dola, "looks_like_generation_accepted", None) or (lambda _t: False)
    deadline = time.time() + NO_ACK_SECONDS
    while time.time() < deadline:
        try:
            poll = dola.inspect_video_once(session, ctx, conversation_id)
        except Exception as exc:
            log.info("confirm: %s 轮询异常（继续等）: %s", account, str(exc)[:160])
            time.sleep(poll_interval)
            continue
        texts = [str(t) for t in (getattr(poll, "texts", None) or [])]
        if credits is not None:
            credits.note(texts)
        status = str(getattr(poll, "status", "") or "")
        if (any(accepted(t) for t in texts) or _media_keys(poll)
                or status in ("succeeded",) + FINAL_POLL_STATUSES):
            _emit_upstream_note(on_poll, dola, texts)
            return texts
        time.sleep(poll_interval)
    raise PureApiError(f"{account} 回了「{DURATION_CAP_CONFIRM_REPLY}」后 {NO_ACK_SECONDS} 秒上游仍未受理")


def _segment_cost(model: str, segment_duration: int) -> int:
    m = (model or "").replace("_", ".")
    version = "seedance-2.0" if "2.0" in m else "seedance-2.5"
    return config.credit_cost(version, segment_duration)


def _generate_capped_pair(*, dola, session, ctx, headers, account: str, prompt: str,
                          ratio: str | None, model: str, output_path: str | Path,
                          remove_watermark: bool, proxy: str, timeout: int,
                          poll_interval: int, on_poll: Callable | None = None,
                          on_conversation_id: Callable | None = None,
                          existing_parts: list[Path] | None = None,
                          images: list | None = None,
                          credits: "_CreditTracker | None" = None,
                          segment_duration: int = 30,
                          stats: dict | None = None) -> Path:
    """30 秒：先原样请求一次 30 秒，出满了直接交付；只出了半截再补后半段、本地拼接。

    依据（2026-10-14 AWS 实测 4 个会话）：请求 30 秒时 dola 回执说「按最接近的支持时长 15 秒生成，
    将消耗 2 个」，实际成片却是 **30.08 秒**（24fps、721 帧）。回执里的秒数不可信，只认落地时长。
    流程：
    1. 第 1 段用原提示词请求 segment_duration（默认 30）秒；dola 反问「是否按 15 秒生成」→ 同会话回「确认生成」。
    2. 等它出片、ffprobe 量时长：≥27 秒直接交付（只扣一次，2 点）；不到 13 秒判失败。
    3. 13~27 秒（真被压成 15 秒）→ 余额够就补「后半段」再请求一次，两段拼成 30 秒（共 4 点）。
    existing_parts：已经拿到的分段（例如上游静默压时长后落地的第一段），只补生成缺的那几段。
    stats：调用方传 dict 进来，回填 segments（实际提交了几次 = 扣了几次）与 native（是否原样 30 秒）。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="dola-pair-"))
    min_seconds = float(config.PAIR_MIN_SEGMENT_SECONDS)
    full_seconds = float(segment_duration) * NATIVE_FULL_RATIO
    total = len(PART_HINTS)
    if stats is not None:
        stats.update(segments=0, native=False)
    try:
        parts: list[Path] = list(existing_parts or [])
        pending: list[tuple[int, str]] = []
        plan: dict[str, tuple[float, int]] = {}      # 会话 → (提交时间, dola 预计等待秒数)

        def submit(index: int, *, whole: bool) -> str:
            # whole=True：第一次按完整提示词请求（多半直接出满 30 秒）；补段时才带「后半段」提示
            text = prompt if whole else f"{prompt}\n\n{PART_HINTS[index - 1]}"
            result = dola.submit_chat_completion(
                session, ctx, headers, images=images or [], audios=[],
                prompt=text, duration=segment_duration, model=model, ratio=ratio,
                timeout_sec=180, video_ability=True,
            )
            if result.error:
                raise PureApiError(f"{account} 第 {index} 段提交失败: {str(result.error)[:200]}")
            if not result.conversation_id:
                raise PureApiError(f"{account} 第 {index} 段未返回 conversation_id")
            if index == 1 and on_conversation_id:
                try:
                    on_conversation_id(account, result.conversation_id, time.time() + timeout)
                except Exception:
                    pass
            if stats is not None:
                stats["segments"] += 1
            pending.append((index, result.conversation_id))
            plan[result.conversation_id] = (time.time(), 0)
            return result.conversation_id

        def acknowledge(index: int, conv: str) -> None:
            """等第一次回执；dola 在反问时长就回「确认生成」。回执里说几秒不作数，只认落地时长。"""
            ack = _await_first_ack(
                dola=dola, session=session, ctx=ctx, account=account, conversation_id=conv,
                ack_deadline=time.time() + NO_ACK_SECONDS, poll_interval=poll_interval,
                credits=credits, on_poll=on_poll) or []
            if _awaiting_confirmation(dola, ack):
                replied = _chat_reply(dola=dola, session=session, ctx=ctx, headers=headers,
                                      conversation_id=conv, text=DURATION_CAP_CONFIRM_REPLY,
                                      account=account)
                log.info("pure pair: %s 第 %s 段 dola 在问时长 → 回「%s」(ok=%s)",
                         account, index, DURATION_CAP_CONFIRM_REPLY, replied)
                if not replied:
                    raise PureApiError(
                        f"{account} 第 {index} 段 dola 在等确认（{_upstream_note(dola, ack)[:120]}），但回话失败")
                _emit_note_text(on_poll, f"30 秒：第 {index} 段 dola 问要不要按 15 秒生成 → 已回「确认生成」")
                ack = _await_acceptance(dola=dola, session=session, ctx=ctx, account=account,
                                        conversation_id=conv, poll_interval=poll_interval,
                                        credits=credits, on_poll=on_poll)
            plan[conv] = (plan.get(conv, (time.time(), 0))[0], _wait_seconds_from_texts(dola, ack))

        first = len(parts) + 1
        if first <= total:
            acknowledge(first, submit(first, whole=not parts))
            if not parts:
                _emit_note_text(on_poll, "30 秒：已原样请求 30 秒（dola 常说「按 15 秒」，实际多数出满 30 秒），等出片")
        i = 0
        while i < len(pending):
            index, conv = pending[i]
            i += 1
            part_credits = _CreditTracker(credits.on_balance if credits else None)
            _emit_note_text(on_poll, f"30 秒：等第 {index} 段出片")
            started, wait = plan.get(conv, (time.time(), 0))
            result = _wait_for_video(
                dola, session, ctx, conv, account=account, started_at=started,
                base_timeout=timeout, wait_seconds=wait, poll_interval=poll_interval,
                remove_watermark=remove_watermark, proxy=proxy or "", on_poll=on_poll,
                on_deadline=(lambda d: on_conversation_id(account, conv, d))
                if (index == 1 and on_conversation_id) else None)
            part_credits.note(result.get("texts"))
            _emit_upstream_note(on_poll, dola, result.get("texts") or [])
            part_credits.note(result.get("failure_reasons"))
            if credits is not None:
                credits.absorb(part_credits)
            if result.get("status") != "succeeded":
                detail = " | ".join(
                    str(item) for item in (result.get("failure_reasons") or []) + (result.get("texts") or [])
                )[:300]
                if result.get("status") == "timeout" and index == 1 and not existing_parts:
                    # dola 还在做、只是我们等到上限：按「等待超时」抛，号池不换号重提，
                    # 服务端接着在后台盯这条会话（任务记的会话就是第 1 段），出片后自动交付
                    raise TimeoutError(
                        f"{account} 30 秒等待出片超时：已等 {int(time.time() - started) // 60} 分钟 "
                        f"dola 仍未出片，转后台继续盯原会话（dola：{detail[:160]}）")
                raise PureApiError(
                    f"{account} 第 {index} 段出片未成功 status={result.get('status')}: {detail}"
                )
            url = result.get("download_url") or result.get("source_url") or ""
            if not url:
                raise PureApiError(f"{account} 第 {index} 段没有可下载的视频地址")
            part = tmp_dir / f"part{index}.mp4"
            dola.download_video(
                url, str(part),
                referer=f"{dola.BASE}/chat/{conv}", proxy=proxy or "",
            )
            seconds = _media_duration(part)
            if seconds is not None and seconds >= full_seconds:
                # 原样出满了：直接交付这一段，绝不再拼（否则 60 秒）
                log.info("pure pair: %s 第 %s 段原样出了 %.1f 秒 → 直接交付（共提交 %s 次）",
                         account, index, seconds, len(pending))
                out_path = Path(output_path)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(part, out_path)
                if stats is not None:
                    stats["native"] = True
                return out_path
            if seconds is not None and seconds < min_seconds:
                raise PairSegmentShortError(
                    f"{account} 第 {index} 段只出了 {seconds:.1f} 秒（至少要 {min_seconds:g} 秒），"
                    "拼不出 30 秒；本单直接失败，不换号重试")
            parts.append(part)
            log.info("pure pair: %s 第 %s 段完成（%s 秒）", account, index,
                     f"{seconds:.1f}" if seconds is not None else "?")
            if i == len(pending) and len(parts) < total:
                # 真被压成了半截：余额够就补后半段
                nxt = len(parts) + 1
                need = _segment_cost(model, segment_duration)
                left = credits.left if credits is not None else None
                if left is not None and left < need:
                    raise PureApiError(
                        f"{account} 第 {index} 段只出了 {seconds or 0:.1f} 秒，账号今日只剩 {left} 点，"
                        f"补第 {nxt} 段要 {need} 点，拼不出 30 秒")
                _emit_note_text(on_poll, f"30 秒：第 {index} 段只出了 {seconds or 0:.1f} 秒，补第 {nxt} 段再拼接")
                acknowledge(nxt, submit(nxt, whole=False))
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


def hello_probe(
    cookie_state_path: str | Path,
    proxy: str = "",
    region: str | None = None,
    pc_version: str | None = None,
    prompt: str | None = None,
    timeout_sec: int = 60,
) -> dict[str, Any]:
    """发一句「你好」做风控探测，返回可判定的结构化结果（纯 API，秒级，不消耗视频额度）。

    判据（按拍板的规则）：
      - 被登出（`x-tt-agw-login != 1`）→ ok=False；
      - 没回复（上游报错 / 超时 / 空回复）→ ok=False；
      - 有回复且登录标记为 1 → ok=True。

    ⚠️ 实测结论：**只看"有没有回复"不够**——把 sessionid/sid_tt 等会话 cookie 全换成死值后，
    匿名会话照样回「你好呀！」。所以这里必须同时读登录标记，否则会把已经登出的号判成健康号。

    返回 {"ok", "status", "login_ok", "replied", "reason", "reply", "elapsed"}：
      - status="ok"          → 登录有效且有回复；
      - status="logged_out"  → 登录标记不是 1（**硬信号**：判风控）；
      - status="no_reply"    → 登录有效但一句回复都没有（**硬信号**：按拍板判风控）；
      - status="error"       → 探测本身没跑成功（网络/上游限流 710022002 等，**软失败**：
        不能据此判风控 —— 实测健康号也会被上游拒绝一次，误判会把好号永久锁死）。
    """
    dola = _dola()
    region = region or config.PURE_API_REGION
    pc_version = pc_version or config.PURE_API_PC_VERSION
    prompt = (prompt or config.HELLO_PROBE_PROMPT or "你好").strip() or "你好"
    started = time.time()
    session = None
    login_ok = False
    replied = False
    reply = ""
    reason = ""
    status = "error"
    try:
        _load_state(Path(cookie_state_path))
        session, ctx, headers = dola.setup_session(
            region=region, pc_version=pc_version, proxy=proxy or "",
            login_pure=True, login_state_file=str(cookie_state_path),
        )
        try:
            dola.warmup_login_session(session, headers)
        except Exception as exc:
            log.info("hello_probe warmup warning: %s", str(exc)[:200])
        # 登录标记：登出/会话失效时 x-tt-agw-login != "1"
        resp = dola.dola_api_post_response(
            session, ctx, "/alice/user/get_web_anon_id", {}, label="get_web_anon_id")
        data = dola.request_json(resp, "get_web_anon_id")
        dola.assert_ok(data, "get_web_anon_id")
        agw = dola.agw_login_flag(resp)
        login_ok = agw == "1"
        if not login_ok:
            status = "logged_out"
            reason = f"被登出（x-tt-agw-login={agw or 'missing'}）"
        else:
            status = "no_reply"      # 先假定没回复；拿到回复再改成 ok
            try:
                dola.dola_api_post_json(session, ctx, "/alice/user/launch", {}, label="launch")
            except Exception as exc:
                log.info("hello_probe launch warning: %s", str(exc)[:200])
            result = dola.submit_chat_completion(
                session, ctx, headers, images=[], audios=[], prompt=prompt,
                duration=0, model="", ratio=None, timeout_sec=timeout_sec,
                video_ability=False,
            )
            reply = str(getattr(result, "final_reply_text", "") or "").strip()
            if not reply:
                texts = [str(t).strip() for t in (getattr(result, "reply_texts", None) or [])]
                reply = next((t for t in texts if t), "")
            if getattr(result, "error", ""):
                err = f"{result.error_code or 'upstream'}: {result.error}"
                if any(k in err.lower() for k in ("login", "登录", "未登录", "ログイン", "anonymous")):
                    login_ok = False
                    status = "logged_out"
                else:
                    # 上游拒绝/网络问题：软失败，不判风控
                    status = "error"
                reason = err[:200]
            if reply:
                replied = True
                if login_ok:
                    status = "ok"
            elif not reason:
                reason = f"{timeout_sec} 秒内没有回复"
    except Exception as exc:
        reason = str(exc).replace("\n", " ")[:200] or exc.__class__.__name__
    finally:
        if session is not None:
            session.close()

    ok = bool(login_ok and replied)
    if ok:
        status = "ok"
        reason = reason or "replied"
    return {
        "ok": ok,
        "status": status,
        "login_ok": bool(login_ok),
        "replied": bool(replied),
        "reason": reason,
        "reply": reply[:200],
        "elapsed": round(time.time() - started, 2),
    }


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
    if int(duration or 0) > 15 and config.LONG_VIDEO_MODE == "pair":
        # 先原样请求 30 秒（dola 嘴上说「按 15 秒」，实测多数出满 30 秒、只扣 2 点）；
        # 只出了半截再补后半段、本地拼接（见 _generate_capped_pair）。
        out_dir = Path(output_dir or config.DOWNLOAD_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        merged_path = out_dir / f"{account}_{uuid.uuid4().hex[:12]}.{CONCAT_CONTAINER}"
        pair_stats: dict = {}
        merged = _generate_capped_pair(
            dola=dola, session=session, ctx=ctx, headers=headers, account=account,
            prompt=submit_prompt, ratio=ratio, model=pure_model, output_path=merged_path,
            remove_watermark=remove_watermark, proxy=proxy or "",
            timeout=max(config.PURE_API_TIMEOUT, config.PURE_API_TIMEOUT_MAX,
                        config.LONG_VIDEO_TIMEOUT_MAX),
            poll_interval=poll_interval, on_poll=on_poll, on_conversation_id=on_conversation_id,
            images=uploaded_images, credits=credits,
            segment_duration=config.PAIR_SEGMENT_SECONDS, stats=pair_stats,
        )
        return {
            "local_path": str(merged), "account": account, "conversation_id": "",
            "download_url": "", "duration": int(duration), "model": pure_model,
            "pure_api": True, "merged_segments": 1 if pair_stats.get("native") else len(PART_HINTS),
            "merge_mode": "native_30s" if pair_stats.get("native") else "pair_15s",
            # 实际向上游提交了几次（= 扣了几次）：原样 30 秒只扣一次，补了后半段扣两次
            "pair_segments_charged": pair_stats.get("segments") or len(PART_HINTS),
            **credits.fields(),
        }
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
    submitted_at = time.time()
    deadline = submitted_at + timeout
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
        # 拍板的 5 分钟规则：短时长任务同样先等「第一次回执」，没等到就判【异常】，
        # 而不是干等到 15 分钟总超时。
        ack_texts = _await_first_ack(
            dola=dola, session=session, ctx=ctx, account=account,
            conversation_id=conversation_id,
            ack_deadline=time.time() + NO_ACK_SECONDS,
            poll_interval=poll_interval, credits=credits, on_poll=on_poll,
        ) or []
        # 上游说「预计等待 N 分钟」：出片等待跟着放长（封顶 DOLA_PURE_TIMEOUT_MAX），
        # 否则上游排队久一点就会被我们自己判超时（上游其实还在生成，额度照扣）。
        upstream_wait = _wait_seconds_from_texts(dola, ack_texts)
        effective_timeout = _timeout_for_upstream_wait(timeout, upstream_wait)
        if effective_timeout > timeout:
            log.info("pure: %s 上游预计等待 %d 分钟，出片等待放长到 %d 分钟",
                     account, upstream_wait // 60, effective_timeout // 60)

        def _on_deadline(new_deadline):
            if on_conversation_id:
                on_conversation_id(account, conversation_id, new_deadline)

        _on_deadline(submitted_at + effective_timeout)
        # 心跳在 _wait_for_video 里：get_video_result 内部轮询不会回调进度，替它刷 last_poll_at，
        # 免得看门狗把正常等待判成「卡住」而重新派发（重投 = 白扣一次额度）。
        result = _wait_for_video(
            dola, session, ctx, conversation_id, account=account, started_at=submitted_at,
            base_timeout=timeout, wait_seconds=upstream_wait, poll_interval=poll_interval,
            remove_watermark=remove_watermark, proxy=proxy or "", on_poll=on_poll,
            on_deadline=_on_deadline)
    credits.note(result.get("texts"))
    credits.note(result.get("failure_reasons"))
    status = result.get("status")
    if status != "succeeded":
        reasons = result.get("failure_reasons") or []
        texts = result.get("texts") or []
        detail = " | ".join(str(x) for x in (reasons + texts))[:300]
        # 派发后一句回执都没有（连额度播报、状态都没有）→ 按拍板判【异常】，提示重试
        if not reasons and not texts:
            raise AbnormalNoAckError(
                f"{ABNORMAL_RETRY_HINT}（{NO_ACK_SECONDS} 秒内上游没有任何回执，status={status}）")
        if any(str(x).lower() in ("login", "未登录", "ログイン") for x in reasons + texts):
            raise PureLoginExpired(f"{account} 纯 API 轮询发现登录失效: {detail}")
        if status == "timeout":
            # 已经拿到会话、上游接单了，只是我们等到上限还没出片：上游大概率仍在排队/生成。
            # 抛 TimeoutError（号池对它**不换号重提**，否则会重复出片、重复扣额度）。
            waited = int((time.time() - submitted_at) // 60)
            hint = result.get("wait_minutes") or ""
            raise TimeoutError(
                f"{account} 等待出片超时：已等 {waited} 分钟仍未出片"
                + (f"（dola 预计排队 {hint}）" if hint else "")
                + "，超过本服务等待上限；dola 端可能仍在生成，为避免重复扣额度不自动重提。"
                + f" 上游回执：{detail}")
        # 上游对 30 秒请求有两条拒绝路径：额度不足（报价 6 点）和「单条最多 15 秒，
        # 要不要拆两段」的追问。两者在 ALLOW_30S_PAIR 打开时都用本地两段兜底，
        # 而不是直接失败（否则画布那边的 30 秒任务只能靠换号碰运气）。
        duration_refused = bool(
            status == getattr(dola, "DURATION_SPLIT_STATUS", "duration_split")
            or getattr(dola, "looks_like_duration_confirm", lambda _t: False)(detail)
            or getattr(dola, "looks_like_duration_capped", lambda _t: False)(detail)
        )
        if int(duration) >= 30 and config.ALLOW_30S_PAIR and (
                _credit_short(detail) or duration_refused):
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
        if int(duration) >= 30 and duration_refused and not config.ALLOW_30S_PAIR:
            raise PureApiError(
                f"{account} 上游要求 30 秒拆两段（status={status}）：{detail} —— "
                "当前按「30 秒不拼接」策略换号重试；要直接用 2×15 秒本地拼接，"
                "把 DOLA_ALLOW_30S_PAIR 设为 1"
            )
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
    elif int(duration) not in config.NATIVE_DURATIONS:
        # 任意时长（非原生档位）：上游没有「两段拼成 20 秒」这种兜底，
        # 所以只复核实际时长 —— 短了直接失败换号，避免把 15 秒片当 20 秒交付。
        actual = _media_duration(Path(saved))
        if clip_is_short(actual, int(duration)):
            raise ShortClipError(
                f"{account} 上游本次只给了 {actual:.1f} 秒成片（目标 {int(duration)} 秒），"
                "非原生时长无拼接兜底，直接换号重试"
            )

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
