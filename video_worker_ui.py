"""视频生成 worker v2（UI 流 + 滑块求解）——当前主线。

链路：点「创建视频」→ 输入 → 回车（前端自带 a_bogus 签名，协议正确）
     → 若弹 bdcaptcha 滑块：OpenCV 识别缺口 + 拟人轨迹拖动
     → 前端自动重试提交 → 轮询 /im/chain/single 出片 → 下载到 downloads/

为什么走 UI 而不是页面内裸 fetch：裸 fetch 缺 webmssdk 的 a_bogus 签名，
必触发 shark 滑块（710022004）；UI 流由前端自己签名，只需过滑块这一关。

用法：python video_worker_ui.py <账号名> "提示词" [比例] [时长秒]
"""
import asyncio
import base64
import hashlib
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
from patchright.async_api import async_playwright

from gap import find_gap_x

import config
import proxy_store
from browser import cookie_value, launch_account_context
from browser_feedback import BrowserReplies, emit as emit_feedback
from dola_client import CREDIT_FAIL_PATTERN, CreditError
from video_worker import POLL_JS, RiskControlError, _download, extract_unwatermarked_url

# Dola 返回的每日上限文案（当前日文 UI 实测：動画生成の 1 日あたりの上限に達しました。）
DAILY_LIMIT_PATTERN = re.compile(
    r"動画生成の\s*1日あたりの上限|每日(?:视频|影片)?生成.*(?:上限|限额|额度)|"
    r"daily.*(?:limit|quota)|(?:limit|quota).*per\s*day",
    re.IGNORECASE,
)
# 2026-10-29 起的话术：「動画生成の無料利用分（1日あたり）の上限に到達しました。…Dola Plus…」。
# 只用于本次会话的最新回复：生成前的余额预检读的是最近会话里的旧消息、不带时间，
# 用它会让收到过一次上限提示的号在额度刷新后仍被一直挡住。
FREE_LIMIT_REPLY = re.compile(
    r"1日あたり[)）]?\s*の上限に(?:到達|達)|無料利用分.{0,12}上限に(?:到達|達)")

# Dola 提交被拒绝/未真正出片时的终结性文字（2026-08-31 实测：提交后只回文字、永远无视频）：
# - 时长选择提问："A. 5 秒で生成 B. 10 秒で生成 ..." / "秒数は 4～15 秒から選んでください"
# - 无法生成视频："現時点では動画生成はできません" / "supports durations from ..."
# 注意：不要把 "生成された動画：..." 占位文字当终结信号——正常生成期间它也会出现。
TERMINAL_TEXT_PATTERNS = [
    re.compile(
        r"supports durations from|nearest supported duration|対応可能な時間|最寄りの対応可能|"
        r"動画生成には|秒で生成|秒数は|から選んで|選んでください|選んで下さい|A/B/C で選んで|"
        r"動画生成はできません|動画を生成できません|動画生成は現在|video generation (?:is )?not possible",
        re.IGNORECASE,
    ),
]
# Dola 通用失败文案（2026-09-01 实测：提交后对话返回「エラーが発生しました。もう一度お試しください。」
# 但页面一直卡"生成中"——这类通用报错不属于每日上限/额度不足/终结性提问，必须单独识别）。
# 连续 HARD_ERROR_TEXT_MAX_HITS 次命中且无视频 → 判失败（比终结文字 12 次更快）。
HARD_ERROR_TEXT_PATTERNS = [
    re.compile(
        r"エラーが発生しました|エラーが発生し|エラーが発生|もう一度お試しください|"
        r"サーバーエラー|内部エラー|通信エラー|システムエラー|予期しないエラー|"
        r"an error occurred|something went wrong|unexpected error|request failed|"
        r"please try again|try again later",
        re.IGNORECASE,
    ),
]
HARD_ERROR_TEXT_MAX_HITS = 3
# 正常受理文案：出现则说明 Dola 已开始生成，重置终结计数（避免把提问/占位文字误判为失败）
ACCEPTANCE_TEXT_PATTERN = re.compile(
    r"生成されます|5 分後に完成|ポイントを消費|を生成します|ビデオを生成|動画を生成|生成を開始",
    re.IGNORECASE,
)
# 连续 N 次轮询（每次约 5s）都只有终结性文字且无视频 → 判定提交失败，不再无限轮询
TERMINAL_TEXT_MAX_HITS = 12

# Dola 新版提交流程（2026-08-31 实测）：提交提示词后要求回复 A/B/C 选择时长，例如
# "A. 5 秒で生成  B. 10 秒で生成  C. 15 秒で生成  A/B/C で選んでください。"
DURATION_CHOICE_RE = re.compile(r"([A-Da-d])\s*[.．、)）\]]?\s*(\d+)\s*秒", re.IGNORECASE)
DURATION_CHOICE_QUESTION_RE = re.compile(
    r"動画生成には|秒で生成|A/B/C で選んで|supports durations from|nearest supported duration|"
    r"秒数は|から選んで|選んでください|選んで下さい|choose.*duration|select.*duration|"
    r"対応しています|別の秒数|秒数をご希望|秒数を指定",
    re.IGNORECASE,
)


def _build_duration_reply(text: str, duration: int | None, ratio: str | None) -> str | None:
    from upstream_reply import duration_reply
    return duration_reply(text, duration, ratio)


class AccountLimitedError(Exception):
    """当前 Dola 账号达到每日视频生成上限，应立即换下一个账号。"""


class CreditInsufficientError(Exception):
    """生成前已知积分不足，不提交视频，换下一个账号。"""


class LoginExpiredError(Exception):
    """Dola 会话已失效（页面出现登录入口），应立即换下一个账号并标记该号需重新登录。"""


from generation_errors import RegionRestrictedError  # noqa: E402 - re-exported for callers

REGION_RESTRICTED_PATH = "/security/region-restricted"


def raise_if_region_restricted(page, account: str) -> None:
    if REGION_RESTRICTED_PATH in (page.url or ""):
        raise RegionRestrictedError(
            f"{account} 代理出口所在地区被 Dola 限制（地区受限页），尚未提交生成；请检查该号代理的国家设置")


VIDEO_BTN = "text=動画を作成"          # ja-JP locale 下的入口文案
CAPTCHA_FRAME_KEY = "bdcaptcha.html"   # 字节 verifycenter 滑块 iframe


async def is_logged_out(page) -> bool:
    """Dola 登录态检测：未登录时页面右上角出现 ログイン/登录 按钮，正常会话没有。"""
    try:
        current = (page.url or "").lower()
        if "/login" in current or "/signin" in current:
            return True
    except Exception:
        pass
    try:
        loc = page.locator('[class*="login-btn-header"]').first
        if await loc.count() and await loc.is_visible():
            return True
    except Exception:
        pass
    try:
        loc = page.get_by_text("ログイン", exact=True).first
        if await loc.count() and await loc.is_visible():
            return True
    except Exception:
        pass
    return False


async def dismiss_popups(page):
    """关闭 Cookie 同意、桌面应用推广等可能拦截点击的弹层。"""
    for sel in ("OK", "同意する", "Accept All", "閉じる", "Close"):
        try:
            loc = page.get_by_text(sel, exact=True).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=1500)
                break
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(500)


async def ensure_login_stable(page, account: str, min_wait: int = 22,
                              max_wait: int = 38) -> None:
    """Dola 对失效会话不会立即显示登录入口（实测约 15-25s 后才出现“ログイン”）。

    提交前必须等待稳定窗口：min_wait 秒内持续无登录入口、输入框就绪才算有效；
    一旦出现登录入口立即判失效，避免把“匿名普通聊天”当成视频任务提交。
    """
    start = time.time()
    clean_since = None
    while time.time() - start < max_wait:
        await page.wait_for_timeout(2000)
        raise_if_region_restricted(page, account)   # 受限页没有输入框，别等到超时误判掉登录
        if await is_logged_out(page):
            raise LoginExpiredError(
                f"{account} 登录态失效（页面出现登录入口），请重新登录该账号")
        has_chat = bool(await page.evaluate(
            """() => !!(document.querySelector('textarea')
                    || document.querySelector('[contenteditable="true"]')
                    || document.querySelector('input[type="text"]'))"""
        ))
        now = time.time()
        if has_chat:
            if clean_since is None:
                clean_since = now
            elif now - clean_since >= 6 and now - start >= min_wait:
                return
        else:
            clean_since = None
    raise LoginExpiredError(
        f"{account} 登录态不稳定（{int(max_wait)}s 内未确认有效），请重新登录该账号")


# 生成前只读检查：读取最近会话中的“剩余 N points/ポイント/积分”文案。
# 新账号如果没有历史余额文案，返回 balance=null，交给实际提交流程判断，不误拦截。
BALANCE_JS = r"""
async ({msToken, fp}) => {
  const params = new URLSearchParams({
    version_code: "20800", language: "ja", device_platform: "web",
    doubao_device_platform: "web", aid: "495671", real_aid: "495671",
    pkg_type: "release_version", pc_version: "3.32.62", doubao_pc_version: "3.32.62",
    region: "JP", sys_region: "JP", samantha_web: "1", web_platform: "browser",
    "use-olympus-account": "1", web_tab_id: crypto.randomUUID(),
  });
  if (msToken) params.set("msToken", msToken);
  if (fp) params.set("fp", fp);
  const headers = {
    "Content-Type": "application/json; encoding=utf-8",
    "agw-js-conv": "str", "Accept": "*/*",
  };
  const recent = await fetch("/im/chain/recent_conv?" + params.toString(), {
    method: "POST", headers,
    body: JSON.stringify({
      cmd: 3200,
      uplink_body: {pull_recent_conv_chain_uplink_body: {
        limit: 20, message_count_per_conv: 10, api_version: 1, conv_version: 0,
        direction: 3,
        option: {not_need_message: false, need_complete_conversation: true,
          need_coco_conversation: true, need_coco_bot: true,
          need_pc_pin_chain: true, pc_pin_query_type: 0},
      }},
      sequence_id: crypto.randomUUID(), channel: 2, version: "1",
    }), credentials: "include",
  });
  if (!recent.ok) return {ok: false, texts: []};
  const recentData = await recent.json();
  const body = recentData.downlink_body || {};
  const down = body.pull_recent_conv_chain_downlink_body || {};
  const cells = down.cells || [];
  const ids = cells.map(c => (c.conversation || {}).conversation_id || c.id)
    .filter(Boolean).slice(0, 10);
  if (!ids.length) return {ok: true, texts: []};

  const batch = await fetch("/im/chain/batch_single?" + params.toString(), {
    method: "POST", headers,
    body: JSON.stringify({
      cmd: 3101,
      uplink_body: {batch_pull_singe_chain_uplink_body: {
        conversation_type: 3, direction: 3, limit: 1,
        params: ids.map(conversation_id => ({conversation_id})),
        evaluate_ab_params: "", evaluate_common_params: "", ext: {},
      }},
      sequence_id: crypto.randomUUID(), channel: 2, version: "1",
    }), credentials: "include",
  });
  if (!batch.ok) return {ok: true, texts: []};
  const data = await batch.json();
  const texts = [];
  const seen = new Set();
  const walk = (v) => {
    if (typeof v === "string") {
      if ((v.includes("ポイント") || v.includes("积分") || v.toLowerCase().includes("points")
        || v.includes("上限") || v.includes("limit")) && v.length < 1200 && !seen.has(v)) {
        seen.add(v); texts.push(v);
      }
      try {
        const t = v.trim();
        if (t.startsWith("{") || t.startsWith("[")) walk(JSON.parse(t));
      } catch (e) {}
      return;
    }
    if (!v || typeof v !== "object") return;
    if (Array.isArray(v)) { for (const x of v) walk(x); return; }
    for (const x of Object.values(v)) walk(x);
  };
  walk(data);
  return {ok: true, texts: texts.slice(-80)};
}
""";


def find_captcha_frame(page):
    for f in page.frames:
        if CAPTCHA_FRAME_KEY in f.url:
            return f
    return None


async def _fetch_bytes(url: str, account: str | None = None) -> bytes:
    proxy = __import__("account_transport").http_proxy(account) if account else (config.PROXY or None)
    async with aiohttp.ClientSession() as s:
        async with s.get(url, proxy=proxy or None) as r:
            return await r.read()


QAAB_SALT_HEX = (
    "4dd4c2e6b83162090e52b3c7a6733ba4"
    "1cb2462b829ab58a196b39db57177524"
    "f49baf7f08e8d68d26a72e37c1a95a2f"
    "1f05a51892aef2949732b62a38aadd58"
)


def _b64_decode_loose(text: str) -> bytes | None:
    t = (text or "").strip()
    if not t:
        return None
    variants = [
        t,
        t.replace("$", "_").replace("@", "/").replace("#", "."),
        t.replace("$", "+").replace("@", "/").replace("#", "="),
    ]
    pad = lambda s: s + "=" * ((4 - len(s) % 4) % 4)  # noqa: E731
    for cand in variants:
        if not cand:
            continue
        try:
            return base64.b64decode(pad(cand))
        except Exception:
            continue
    return None


def _aes_cbc_decrypt(payload: bytes, key: bytes, iv: bytes) -> str | None:
    from Crypto.Cipher import AES

    if len(payload) % 16 != 0 or len(key) not in (16, 24, 32) or len(iv) != 16:
        return None
    try:
        plain = AES.new(key, AES.MODE_CBC, iv).decrypt(payload)
    except Exception:
        return None
    if plain:
        pad = plain[-1]
        if 1 <= pad <= 16 and pad <= len(plain) and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
    url = plain.decode("utf-8", "ignore")
    return url if url.startswith("http") else None


def _decode_qaab_url(token: str, key_seed: str) -> str | None:
    """解密 Dola vod 的 qAAB 加密 main_url（与 dola30 扩展同算法）。"""
    data = _b64_decode_loose(token)
    seed = _b64_decode_loose(key_seed)
    if not data or not seed:
        return None
    digest1 = hashlib.sha512(seed[:32]).digest()
    salt = bytes.fromhex(QAAB_SALT_HEX)
    digest2 = hashlib.sha512(digest1 + salt).digest()
    key = digest2[:16]
    iv = digest2[16:32]
    attempts = []
    if len(data) >= 4 and data[0] == 0xA8 and data[1] == 0x00 and data[2] == 0x01 and data[3] == 0x00:
        payload = data[4:]
        attempts.append((payload, key, iv))
        attempts.append((payload, iv, key))
        if len(data) > 36:
            attempts.append((data[36:], key, data[20:36]))
            attempts.append((data[36:], key, iv))
    else:
        attempts.append((data, key, iv))
    for payload, k, i in attempts:
        url = _aes_cbc_decrypt(payload, k, i)
        if url:
            return url
    return None


def _decode_main_token(mu: str, key_seed: str) -> str | None:
    if not mu:
        return None
    if mu.startswith("qAAB"):
        return _decode_qaab_url(mu, key_seed)
    try:
        dec = base64.b64decode(mu).decode("utf-8", "ignore")
    except Exception:
        return None
    return dec if dec.startswith("http") else None


async def fetch_higher_bitrate_url(video_model_str: str, account: str | None = None) -> str | None:
    """用 Dola vod fallback_api 取更高码率的 H.264 original 源（约 3Mbps）。

    Dola 默认下发 bytevc1/H.265 低码率源（720p 约 1Mbps）。fallback_api 是
    vod-urls 播放接口，把 codec_type 改成 8/0 会返回 h264 original（约 3.3Mbps、
    体积 3 倍）。失败返回 None，由调用方回退默认源。

    2026-10-30：这一步偶发失败（13 条里 2 条），任务就退回低码率源（5 秒 0.4 MB、转码后约 1.3 Mbps）
    且失去直链交付；以前只试一次、异常全吞掉。现在经同一出口重试 HIGH_BITRATE_ATTEMPTS 次，
    失败原因写日志（不含签名 URL）。仍不走直连：账号隔离不变。
    """
    reasons: list[str] = []
    try:
        vm = json.loads(video_model_str or "{}")
        fb = vm.get("fallback_api") or ""
        if not fb or not fb.startswith("http"):
            print(f"[{account}] 高码率原片：video_model 没有 fallback_api，用默认源", flush=True)
            return None
        parts = urlsplit(fb)
        q = dict(__import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(
            parts.query, keep_blank_values=True))
        q["codec_type"] = "8"     # h264 original
        q["logo_type"] = "unwatermarked"
        url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                          urlencode(q), parts.fragment))
        proxy = (__import__("account_transport").http_proxy(account) if account
                 else (config.PROXY or None))
        async with aiohttp.ClientSession() as s:
            for attempt in range(1, max(1, config.HIGH_BITRATE_ATTEMPTS) + 1):
                if attempt > 1:
                    await asyncio.sleep(2 * (attempt - 1))
                try:
                    async with s.get(url, proxy=proxy,
                                     timeout=aiohttp.ClientTimeout(total=30)) as r:
                        if r.status != 200:
                            reasons.append(f"HTTP {r.status}")
                            continue
                        data = json.loads(await r.text())
                    best = _best_decoded_source(data, vm.get("key_seed") or "")
                    if best:
                        if attempt > 1:
                            print(f"[{account}] 高码率原片第 {attempt} 次取到（之前：{'；'.join(reasons)}）",
                                  flush=True)
                        return best
                    reasons.append("回包里没有可解码的源")
                except Exception as exc:  # noqa: BLE001 - 原因写日志，不带签名 URL
                    reasons.append(type(exc).__name__ + (f": {str(exc)[:80]}" if str(exc) else ""))
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"准备请求失败 {type(exc).__name__}: {str(exc)[:80]}")
    print(f"[{account}] 高码率原片获取失败，退回默认低码率源："
          f"{'；'.join(reasons)[:400] or '原因不明'}", flush=True)
    return None


def _best_decoded_source(data: dict, key_seed: str) -> str | None:
    """vod-urls 回包里码率最高、能解码出 http 地址的源。"""
    vi = (((data.get("video_info") or {}).get("data")) or data.get("data") or data)
    best = None
    for v in (vi.get("video_list") or {}).values():
        mu = v.get("main_url") or ""
        if not mu:
            continue
        dec = _decode_main_token(mu, key_seed)
        if not dec or not dec.startswith("http"):
            continue
        rate = int(v.get("bitrate") or v.get("real_bitrate") or 0)
        if best is None or rate > best[0]:
            best = (rate, dec)
    return best[1] if best else None



async def attach_reference_images(page, image_paths: list[str]) -> None:
    """通过浏览器上传，记录 prepare / Apply / TOS / Commit 各阶段的失败。"""
    import re
    from urllib.parse import parse_qs, urlsplit
    from generation_errors import PreSubmissionUploadError, UploadRouteError
    if not image_paths:
        return
    file_input = page.locator('input[type="file"]').first
    await file_input.wait_for(state="attached", timeout=10000)
    events = []
    checks = set()
    errors = []

    def stage_of(url):
        parts = urlsplit(url)
        action = parse_qs(parts.query).get("Action", [""])[0]
        if "/alice/resource/prepare_upload" in parts.path:
            return "prepare_upload"
        if action in ("ApplyImageUpload", "CommitImageUpload"):
            return action
        if "/upload/v1/" in parts.path:
            return "TOS"
        return None

    async def inspect_storage_api(response, stage):
        try:
            data = await response.json()
            error = (data.get("ResponseMetadata") or {}).get("Error") or {}
            if error:
                errors.append(f"{stage} 返回 {str(error.get('Code') or '服务错误')[:80]}")
            elif stage == "TOS" and data.get("code") not in (None, 0, "0", 2000, "2000"):
                errors.append(f"TOS 返回 {data.get('code')}")
            elif stage == "TOS":
                events.append((response.status, response.url))
        except Exception:
            errors.append(f"{stage} 返回无法解析的响应")

    def watch(coro):
        task = asyncio.create_task(coro)
        checks.add(task)
        task.add_done_callback(checks.discard)

    def on_request_failed(request):
        stage = stage_of(request.url)
        if not stage:
            return
        # Only expose the host and Chromium error code, never signed URLs/credentials.
        code = re.search(r"net::ERR_[A-Z0-9_]+", str(request.failure or ""))
        reason = code.group(0) if code else "网络连接失败"
        errors.append(UploadRouteError(urlsplit(request.url).hostname or "unknown", reason, stage))

    # Cross-origin ImageX calls need a CORS preflight. When it fails Chromium only
    # reports net::ERR_FAILED on the real request; CDP exposes the preflight cause.
    cdp_requests, cdp_failures = {}, []

    def on_cdp_request(params):
        request = params.get("request") or {}
        if stage_of(request.get("url", "")):
            cdp_requests[params.get("requestId")] = (stage_of(request["url"]), params.get("type") or "",
                                                    request.get("method") or "")

    def on_cdp_failed(params):
        known = cdp_requests.get(params.get("requestId"))
        if not known:
            return
        stage, kind, method = known
        cors = (params.get("corsErrorStatus") or {}).get("corsError")
        parts = [f"{'预检' if kind == 'Preflight' or method == 'OPTIONS' else stage}"
                 f" {params.get('errorText') or ''}".strip()]
        if cors:
            parts.append(f"CORS {cors}")
        if params.get("blockedReason"):
            parts.append(f"blocked {params['blockedReason']}")
        cdp_failures.append("，".join(parts)[:160])

    cdp = None
    try:
        cdp = await page.context.new_cdp_session(page)
        cdp.on("Network.requestWillBeSent", on_cdp_request)
        cdp.on("Network.loadingFailed", on_cdp_failed)
        await cdp.send("Network.enable")
    except Exception:
        cdp = None   # diagnostics only; uploads do not depend on CDP

    async def inspect_prepare(response):
        try:
            data = await response.json()
            if data.get("code") not in (None, 0, "0", 2000, "2000"):
                errors.append(f"prepare_upload 返回 {data.get('code')}：{str(data.get('msg') or data.get('message') or '')[:180]}")
                return
            events.append((response.status, response.url))
        except Exception:
            errors.append("prepare_upload 返回无法解析的响应")

    def on_response(response):
        url = response.url
        stage = stage_of(url)
        if not stage or getattr(getattr(response, "request", None), "method", "") == "OPTIONS":
            return
        if stage == "prepare_upload":
            if response.status >= 400:
                errors.append(f"prepare_upload HTTP {response.status}")
            else:
                watch(inspect_prepare(response))
        else:
            if response.status >= 400:
                errors.append(f"{stage} HTTP {response.status}")
            else:
                watch(inspect_storage_api(response, stage))

    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
    import socks_relay
    relay_before = dict(socks_relay.STATS)
    try:
        await file_input.set_input_files(image_paths)
        expected = len(image_paths)
        deadline = time.time() + max(60, expected * 20)
        while time.time() < deadline:
            if errors:
                if isinstance(errors[0], UploadRouteError):
                    if cdp is not None and not cdp_failures:
                        await page.wait_for_timeout(500)   # CDP events may trail requestfailed
                    first = errors[0]
                    # 本地 SOCKS 中转这段时间新增的上游连接失败：区分「代理连不上」和「浏览器侧失败」
                    relay = {k: v - relay_before.get(k, 0) for k, v in socks_relay.STATS.items()
                             if v > relay_before.get(k, 0)}
                    details = list(dict.fromkeys(cdp_failures))
                    if relay:
                        details.append("中转上游失败 " + "、".join(f"{k}×{n}" for k, n in relay.items()))
                    if details:
                        first = UploadRouteError(first.host, first.code, first.stage, "；".join(details)[:300])
                    raise first
                raise PreSubmissionUploadError("参考图片上传未完成（尚未提交生成）：" + errors[0])
            prepare_count = sum("/alice/resource/prepare_upload" in url and 200 <= status < 300
                                for status, url in events)
            tos_count = sum("/upload/v1/" in url and 200 <= status < 300
                            for status, url in events)
            # 缩略图出现且每个 prepare/TOS 都成功，才允许发送。
            thumb_count = await page.locator('img[alt]').count()
            # Dola 会把多张图合并成一次 prepare_upload（实测 2 张图 prepare=1、tos=2），
            # 因此 prepare 只要求至少 1 次，真正以 TOS 上传数和缩略图数为准。
            if prepare_count >= 1 and tos_count >= expected and thumb_count >= expected:
                await page.wait_for_timeout(800)
                print(f"[upload] 参考图片上传完成: {expected} 张", flush=True)
                return
            await page.wait_for_timeout(250)
        raise PreSubmissionUploadError(
            f"参考图片上传超时: prepare={prepare_count}/{expected}, "
            f"tos={tos_count}/{expected}, 页面图片={thumb_count}（页面图片数不代表上传成功）"
        )
    finally:
        page.remove_listener("response", on_response)
        page.remove_listener("requestfailed", on_request_failed)
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass
        pending = list(checks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def _gen_track(distance: float):
    """拟人轨迹：smootherstep 主行程 + 过冲回稳 + y 轴抖动。返回 [(dx,dy,dt_ms),...]"""
    steps = random.randint(45, 65)
    overshoot = random.uniform(3, 9)
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        s = 10 * t**3 - 15 * t**4 + 6 * t**5
        x = (distance + overshoot) * s
        y = random.uniform(-1.5, 1.5) if 0.1 < t < 0.95 else 0
        pts.append((x, y, random.randint(8, 22)))
    for i in range(1, random.randint(3, 5) + 1):
        pts.append((distance + overshoot * (1 - i / 5), random.uniform(-0.8, 0.8), random.randint(15, 30)))
    return pts


async def solve_slider(page, frame, attempt: int, account: str | None = None) -> bool:
    """在 bdcaptcha iframe 内识别缺口并拖动。成功返回 True。"""
    # 等两张图加载完成（naturalWidth>0），否则 scale 算错
    await frame.wait_for_selector("img", timeout=15000)
    await frame.evaluate("""async () => {
        const t0 = Date.now();
        while (Date.now() - t0 < 10000) {
            const imgs = [...document.images];
            if (imgs.length >= 2 && imgs.every(im => im.complete && im.naturalWidth > 0)) return;
            await new Promise(r => setTimeout(r, 200));
        }
        throw new Error("captcha images load timeout");
    }""")
    await page.wait_for_timeout(800)

    imgs = await frame.evaluate("""() => [...document.images].map(im => ({
        src: im.src, w: im.naturalWidth, h: im.naturalHeight,
        bw: im.getBoundingClientRect().width,
        left: im.getBoundingClientRect().left,
    }))""")
    bg = next((i for i in imgs if ".jpeg" in i["src"] or "-2." in i["src"]), None)
    piece = next((i for i in imgs if i is not bg and (".png" in i["src"] or "-1." in i["src"])), None)
    if not bg or not piece:
        print("  ✗ 未找到背景/滑块图", flush=True)
        return False

    bg_bytes = await _fetch_bytes(bg["src"], account)
    piece_bytes = await _fetch_bytes(piece["src"], account)
    Path("dbg_bg.jpg").write_bytes(bg_bytes)
    Path("dbg_piece.png").write_bytes(piece_bytes)

    gap_x, conf = find_gap_x(bg_bytes, piece_bytes)
    scale = bg["bw"] / bg["w"] if bg["w"] else 340 / 552
    distance = (gap_x - (piece["left"] - bg["left"]) / scale) * scale
    print(f"  [solve#{attempt}] gap_x={gap_x} conf={conf:.3f} scale={scale:.2f} distance={distance:.0f}px", flush=True)

    btn = frame.locator(".captcha-slider-btn")
    bb = await btn.bounding_box()
    if not bb:
        print("  ✗ 未找到拖动按钮 .captcha-slider-btn", flush=True)
        return False
    sx, sy = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
    await page.mouse.move(sx, sy)
    await page.wait_for_timeout(random.randint(150, 350))
    await page.mouse.down()
    await page.wait_for_timeout(random.randint(80, 180))
    for dx, dy, dt in _gen_track(distance):
        await page.mouse.move(sx + dx, sy + dy)
        await asyncio.sleep(dt / 1000)
    await page.wait_for_timeout(random.randint(120, 260))
    await page.mouse.up()

    for _ in range(10):
        await page.wait_for_timeout(700)
        if not find_captcha_frame(page):
            return True
    return False



_BALANCE_PATTERNS = (
    re.compile(r"(?:本日は|今日(?:还剩|剩余)?|今天).*?(\d+)\s*(?:ポイント|积分|points?)", re.I),
    re.compile(r"(?:remaining|left)\s*[:：]?\s*(\d+)\s*points?", re.I),
    re.compile(r"(?:还剩|剩余|还有)\s*(\d+)\s*(?:积分|点)", re.I),
)


def _parse_balance_texts(texts: list[str]) -> tuple[int | None, bool, str]:
    for text in texts:
        if DAILY_LIMIT_PATTERN.search(text):
            return None, True, text
    for text in texts:
        for pattern in _BALANCE_PATTERNS:
            match = pattern.search(text)
            if match:
                return int(match.group(1)), False, text
    return None, False, ""


async def _preflight_balance(page, ms_token: str, fp: str, required: int) -> dict:
    """尽力读取已知积分；未知时不误阻止新账号。"""
    try:
        result = await asyncio.wait_for(page.evaluate(
            BALANCE_JS, {"msToken": ms_token, "fp": fp}), timeout=30)
        balance, daily_limited, source = _parse_balance_texts(result.get("texts", []))
        if daily_limited:
            raise AccountLimitedError(f"账号每日生成上限: {source[:120]}")
        if balance is not None and balance < required:
            raise CreditInsufficientError(
                f"生成前积分不足: 当前 {balance}，本次至少需要 {required}（来源: {source[:120]}）"
            )
        return {"balance": balance, "source": source}
    except (AccountLimitedError, CreditInsufficientError):
        raise
    except Exception as e:
        print(f"  余额预检未知（继续提交，不误拦截）: {str(e)[:120]}", flush=True)
        return {"balance": None, "source": ""}


async def poll_conversation(account: str, page, context, conversation_id: str,
                            timeout: int, on_poll=None, on_balance=None,
                            duration: int | None = None,
                            ratio: str | None = None) -> dict:
    """轮询已受理会话直到出片（不限时）；可被初次提交和服务重启恢复流程共同调用。"""
    emit_feedback(on_poll, progress="已取得上游会话，正在查询视频结果")
    cookies = await context.cookies("https://www.dola.com")
    ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
    start = time.time()
    deadline = start + timeout  # 出片超时上限（默认 30 分钟，dola.env DOLA_VIDEO_TIMEOUT）
    last_callback = 0.0
    last_note = ""
    choice_replied = False
    failure_seen = (None, 0)
    final_seen = (None, 0)
    limit_seen = 0
    stall_key, stall_since, stall_noted = None, 0.0, False
    import config as _config
    last_shape = None
    last_wait = 0
    poll_errors = 0
    from upstream_reply import wait_seconds_from_reply
    while True:
        await asyncio.sleep(5)
        if time.time() > deadline:
            raise TimeoutError(
                f"出片超时：{int(timeout)}s 内未出片（conversation_id={conversation_id}）"
            )
        try:
            poll = await asyncio.wait_for(page.evaluate(
                POLL_JS, {"conversationId": conversation_id, "msToken": ms_token, "fp": fp}), timeout=30)
            if poll.get("ok") is False:
                raise ConnectionError(f"上游会话查询 HTTP {poll.get('status', 'unknown')}")
        except Exception as e:
            from generation_errors import UpstreamReplyError
            poll_errors += 1
            if poll_errors >= 3:
                raise UpstreamReplyError("网络失败", "连续三次查询上游会话失败；未重新提交生成，请检查账号代理后查询原会话") from e
            continue
        poll_errors = 0
        now = time.time()
        from upstream_reply import reply_failure, duration_reply, start_confirm_reply
        from generation_errors import UpstreamReplyError
        latest = poll.get("latestAssistant")
        latest_text = (latest or {}).get("text", "")
        # Older test doubles may only expose assistantTexts; real ordered polls never
        # fall back to historical replies when the latest turn is empty/unknown.
        texts = ([latest_text] if latest_text else []) if "latestAssistant" in poll else (poll.get("assistantTexts") or [])
        note = (latest_text if "latestAssistant" in poll else "\n".join(dict.fromkeys(poll.get("assistantTexts") or [])))[:4000]
        if on_poll and (note != last_note or now - last_callback >= 30):
            emit_feedback(on_poll, note=note or None)
            last_callback = now
            last_note = note
            wait = wait_seconds_from_reply(note) if note else 0
            if wait and wait != last_wait:
                last_wait = wait
                emit_feedback(on_poll, progress=f"已取得上游会话，dola 预计约 {max(1, round(wait / 60))} 分钟出片，正在查询视频结果")
        videos = poll.get("videos") or []
        # ---- 新版 Dola：提交后要求 A/B/C 选择时长，不回复则永远不出片 ----
        if duration and not choice_replied:
            joined = "\n".join(texts)
            if latest and not videos:
                reply = duration_reply(joined, duration, ratio)
                # dola 问「按 N 秒直接开始生成吗？」（时长本来就在支持范围内）→ 回确认，否则永远不出片
                confirm = None if reply else start_confirm_reply(joined, duration)
                reply = reply or confirm
                if reply:
                    box = await page.query_selector("textarea") or await page.query_selector('[contenteditable="true"]')
                    if box:
                        try:
                            await box.click()
                            await page.keyboard.type(reply, delay=120)
                            await page.wait_for_timeout(300)
                            await page.keyboard.press("Enter")
                            choice_replied = True
                            print(f"[{account}] 已回复时长选择: {reply}（{duration}s）", flush=True)
                            if confirm:
                                emit_feedback(on_poll, progress=f"dola 询问是否按 {duration} 秒直接生成，已回复确认，等待出片")
                        except Exception as e:
                            print(f"[{account}] 回复时长选择失败: {str(e)[:100]}", flush=True)
        joined = "\n".join(texts)
        if not videos and (joined or latest):
            category = reply_failure(joined)
            if DAILY_LIMIT_PATTERN.search(joined) or FREE_LIMIT_REPLY.search(joined):
                # 定论条件：它是会话里唯一一条助手回复、已结束、没有生成卡片，并连续两轮不变。
                # 10-28 acc148「上限提示后仍出片」：提示词被换行拆成两条，第一条已被接单生成，
                # 第二条才收到上限提示 —— 视频属于更早那条回复。会话里只有这一条回复时，
                # 没有任何生成在进行，换号重提不会重复出片。
                if "latestAssistant" not in poll:
                    raise AccountLimitedError(f"账号每日生成上限: {joined[:1000]}")
                if (poll.get("assistantCount") == 1 and latest.get("finished")
                        and not latest.get("tools")):
                    limit_seen += 1
                    if limit_seen >= 2:
                        raise AccountLimitedError(f"账号每日生成上限: {joined[:1000]}")
                    continue
            limit_seen = 0
            if category == "额度不足":
                raise CreditError(f"额度不足: {joined[:1000]}")
            from upstream_reply import final_generation_failure
            final = final_generation_failure(latest, bool(videos))
            if final:
                # The failed card must be the same latest message on two polls.
                key = ((latest or {}).get("index"), final["code"])
                final_seen = (key, final_seen[1] + 1 if final_seen[0] == key else 1)
                if final_seen[1] >= 2:
                    from generation_errors import UpstreamGenerationFailed
                    raise UpstreamGenerationFailed(
                        final["category"], final["text"] or joined, code=final["code"],
                        conversation_id=conversation_id, retryable=final["retryable"],
                        not_charged=final["not_charged"])
                continue
            final_seen = (None, 0)
            if category:
                previous, hits = failure_seen
                failure_seen = (joined, hits + 1 if previous == joined else 1)
                if failure_seen[1] >= 2:
                    raise UpstreamReplyError(category, joined)
            else:
                failure_seen = (None, 0)
            balance, _, source = _parse_balance_texts(texts)
            if balance is not None and on_balance:
                on_balance(balance, source)
        else:
            failure_seen = (None, 0)
        if latest:
            shape = (latest.get("index"), bool(latest.get("finished")),
                     tuple(t.get("status") for t in latest.get("tools") or []), bool(poll.get("anyTool")), len(videos))
            if shape != last_shape:
                last_shape = shape
                print(f"[{account}] 上游最新回复 idx={shape[0]} finished={shape[1]} tools={list(shape[2])} "
                      f"anyTool={shape[3]} videos={shape[4]}: {latest_text[:80]!r}", flush=True)
        # ---- 兜底：认不出的追问。dola 说完了、整个会话没挂过生成卡片、也没报预计时间，
        # 这样持续 STALL_REPLY_SECONDS 不变 → 它在等我们答话，空等只会等到出片超时。
        # 没有卡片 = 没开始生成、不扣点；结束任务并给出 dola 原话，不替客户乱答。
        stalled = (_config.STALL_REPLY_SECONDS > 0 and latest and latest_text and not videos
                   and latest.get("finished") and not latest.get("tools") and not poll.get("anyTool")
                   and not wait_seconds_from_reply("\n".join(poll.get("assistantTexts") or [latest_text])))
        if stalled:
            key = (latest.get("index"), latest_text)
            if key != stall_key:
                stall_key, stall_since, stall_noted = key, now, False
            elif not stall_noted and now - stall_since >= 30:
                stall_noted = True
                emit_feedback(on_poll, progress="dola 已回复但没有开始生成，可能在等答复；"
                                                f"{_config.STALL_REPLY_SECONDS} 秒内仍无进展将结束任务（不扣点）")
            if now - stall_since >= _config.STALL_REPLY_SECONDS:
                raise UpstreamReplyError(
                    "上游在等待回复",
                    f"dola 回复后 {int(now - stall_since)} 秒没有开始生成（会话里没有生成卡片，未扣点），"
                    f"可能在追问需要补充的信息：{latest_text[:1500]}")
        else:
            stall_key = None
        if videos:
            emit_feedback(on_poll, progress="上游已返回视频，正在获取下载地址")
            video_models = poll.get("videoModels", [])
            vm_str = video_models[0] if video_models else ""
            # 优先用 fallback_api 的 H.264 original 高码率源（约 3Mbps），
            # 失败再回退默认 bytevc1 低码率源。
            hi = await fetch_higher_bitrate_url(vm_str, account)
            import video_link
            if hi and video_link.enabled():
                probe = await asyncio.to_thread(video_link.probe_direct, hi)
                if probe:
                    # 直链交付：不下载到服务器，由 /v/<任务>.mp4 302 到上游
                    emit_feedback(on_poll, progress="视频已生成，直链交付（不经服务器下载）")
                    print(f"[{account}] 出片! 直链交付（不下载）", flush=True)
                    return {"video_url": hi, "local_path": "", "delivery": "link",
                            "video_model": vm_str, "video_size": probe["size"],
                            "link_expires_at": probe["expires_at"],
                            "conversation_id": conversation_id, "account": account}
                print(f"[{account}] 上游直链服务器直连不可用，改为下载", flush=True)
            url = hi or extract_unwatermarked_url(vm_str, videos[0])
            print(f"[{account}] 出片! 下载中（高码率优先）...", flush=True)
            emit_feedback(on_poll, progress="视频已生成，正在下载到服务器")
            local = await _download(url, account)
            emit_feedback(on_poll, progress="视频已下载，正在完成本地处理")
            print(f"[{account}] 已下载 {local}（{local.stat().st_size / 1e6:.1f} MB）", flush=True)
            return {"video_url": url, "local_path": str(local),
                    "conversation_id": conversation_id, "account": account}
        print(f"  ...生成中（{int(time.time() - start)}s）", flush=True)


def single_line_prompt(prompt: str) -> str:
    """The composer sends on Enter: a newline typed as a key press submits the message early.

    2026-10-29: the AI-source note joined to the prompt with a blank line went out as its
    own message; Dola answered both halves with a JSON plan (710082041) and never generated.
    Prompts that carry their own line breaks were split the same way.
    """
    return re.sub(r"\s*[\r\n]+\s*", " ", prompt or "").strip()


async def type_prompt(page, prompt: str, delay: int = 40) -> None:
    # 逐字输入仍用真实按键（React 输入框靠键盘事件同步状态）；100ms/字时 300 多字的剧本要打
    # 30 多秒，所以调快到 40ms。换行一律换成空格，否则回车会把半截提示词提前发出去。
    await page.keyboard.type(single_line_prompt(prompt), delay=delay)


async def enter_video_mode(page, account: str) -> None:
    """Click 動画を作成 and confirm the video actionbar rendered (at most two clicks)."""
    from browser_video_params import video_mode_ready
    from generation_errors import PreSubmissionParametersError
    for click in range(2):
        try:
            await page.click(VIDEO_BTN, timeout=8000)
        except Exception:
            entry = page.get_by_text("動画を作成", exact=False).first
            if await entry.count():
                await entry.evaluate("(el) => el.click()")
        await page.wait_for_timeout(1500)
        # A click is not enough: in plain chat mode images still upload, but the
        # model/params controls never appear and the prompt is not a video job.
        if await video_mode_ready(page, 10000):
            return
        if click:
            raise PreSubmissionParametersError(
                "页面未进入视频创作模式（找不到模型按钮和参数入口），尚未上传参考图或提交生成")
        print(f"[{account}] 点击「動画を作成」后未进入视频模式，重试一次", flush=True)
        await dismiss_popups(page)


async def upload_with_retry(page, account: str, image_paths: list[str], on_poll=None) -> None:
    """Upload references; on a pre-submission failure reload and retry on the same account.

    Proxy links drop 20-40% of single connections (2026-10-29 probes), and one upload
    needs prepare/Apply/TOS/Commit. A reload is far cheaper than switching accounts
    (new browser + ~22s login check), clears the attachment stuck at 0% (screenshot
    verified), and nothing has been submitted, so it cannot duplicate a generation.
    """
    from account_transport import redact_error
    from generation_errors import PreSubmissionUploadError
    tries = 1 + config.UPLOAD_RETRIES
    for upload_try in range(1, tries + 1):
        emit_feedback(on_poll, progress="正在上传参考图（尚未提交生成）" if upload_try == 1
                      else f"正在重新上传参考图（第 {upload_try}/{tries} 次，尚未提交生成）")
        try:
            await attach_reference_images(page, image_paths)
            return
        except PreSubmissionUploadError as exc:
            if upload_try == tries:
                raise
            emit_feedback(on_poll, progress=f"{redact_error(exc)[:200]}；刷新页面后同号重试上传")
        await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await dismiss_popups(page)
        raise_if_region_restricted(page, account)
        if await is_logged_out(page):
            raise LoginExpiredError(f"{account} 重新上传前页面出现登录入口，请重新登录该账号")
        await enter_video_mode(page, account)


async def resume_video(account: str, conversation_id: str, timeout: int,
                       on_poll=None, on_balance=None, duration: int | None = None,
                       ratio: str | None = None) -> dict:
    """服务重启后恢复一个已受理会话，不重新发送 prompt。"""
    async with async_playwright() as p:
        context = await launch_account_context(p, account, headless=False, use_extension=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"https://www.dola.com/chat/{conversation_id}",
                            timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            await dismiss_popups(page)
            await ensure_login_stable(page, account)
            return await poll_conversation(
                account, page, context, conversation_id, timeout, on_poll, on_balance,
                duration, ratio)
        finally:
            await context.close()


async def generate_video(account: str, prompt: str, ratio: str = None,
                         duration: int = None, timeout: int = None,
                         model: str = "seedance_v2.0", use_extension: bool = True,
                         on_conversation_id=None, on_poll=None, on_balance=None,
                         reference_image_paths: list[str] | None = None) -> dict:
    """完整出片流程（UI 流）。返回 {"video_url","local_path","conversation_id","account"}。

    ratio/duration: None = 用 UI 默认（当前默认 10s）。
    异常：RiskControlError（滑块 3 次不过）、CreditError（额度不足）。出片轮询不限时。
    """
    timeout = timeout or config.VIDEO_TIMEOUT
    model_key = model.lower().replace("-", "_")
    if model_key in ("seedance_2.5", "seedance_v2.5", "seedance_25", "seedance_v25"):
        model_key = "seedance_v2.5"
    elif model_key in ("seedance_2.0", "seedance_v2.0", "seedance_20", "seedance_v20",
                       "seedance_2.0_fast", "seedance_v2.0_fast"):
        model_key = "seedance_v2.0"         # dola 网页上叫 Seedance 2.0 Fast
    else:
        raise ValueError(f"不支持的模型: {model}（当前支持 seedance-2.0-fast / seedance-2.5）")
    if duration is not None and duration not in (5, 10, 15, 30):
        raise ValueError("Dola 当前支持 5/10/15 秒和扩展注入的 30 秒")
    if duration == 30 and not use_extension:
        raise ValueError("30 秒生成需要启用 Dola30 扩展")
    # 30 秒视频（尤其 Seedance 2.5）页面会明确提示约 15 分钟，不能沿用 5 分钟默认值。
    if duration == 30:
        timeout = max(timeout, 1800)
    if reference_image_paths:
        timeout = max(timeout, config.REFERENCE_VIDEO_TIMEOUT)
    async with async_playwright() as p:
        context = await launch_account_context(
            p, account, headless=False if use_extension else None,
            use_extension=use_extension)
        replies = None
        try:
            emit_feedback(on_poll, progress="正在打开账号浏览器并检查登录态")
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            raise_if_region_restricted(page, account)
            await dismiss_popups(page)
            await ensure_login_stable(page, account)
            cookies = await context.cookies("https://www.dola.com")
            ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
            emit_feedback(on_poll, progress="登录检查完成，正在检查额度")
            await _preflight_balance(page, ms_token, fp, config.VIDEO_REQUIRED_POINTS)

            # ---- UI 发送 ----
            await enter_video_mode(page, account)
            if reference_image_paths:
                await upload_with_retry(page, account, reference_image_paths, on_poll)
            # 选择模型（尽力而为）：扩展/当前网页端文案 モデル 2.0高速/2.5；
            # 部分账号 UI 变体没有该按钮，找不到就沿用 Dola 默认模型，不阻塞出片。
            try:
                current_model = None
                for label in ("モデル 2.0高速", "モデル 2.5"):
                    loc = page.get_by_text(label, exact=True).first
                    if await loc.count() and await loc.is_visible():
                        current_model = loc
                        break
                if current_model is None:
                    current_model = page.get_by_text(re.compile(r"^モデル "), exact=False).first
                if not await current_model.count():
                    raise RuntimeError("没有模型按钮")   # 不让 evaluate 再盲等 30 秒
                try:
                    await current_model.click(timeout=5000)
                except Exception:
                    # radix popper 子元素拦截点击时，用 JS 直接派发点击
                    await current_model.evaluate("(el) => el.click()", timeout=3000)
                await page.wait_for_timeout(500)
                options = (("Dreamina Seedance 2.5",)
                           if model_key == "seedance_v2.5"
                           else ("Dreamina Seedance 2.0高速", "Dreamina Seedance 2.0", "Seedance2.0Fast"))
                selected = False
                for option_text in options:
                    loc = page.get_by_text(option_text, exact=False).first
                    if await loc.count() and await loc.is_visible():
                        try:
                            await loc.click(timeout=5000)
                        except Exception:
                            # radix popper 子元素拦截点击时，用 JS 直接派发点击
                            await loc.evaluate("(el) => el.click()")
                            await page.wait_for_timeout(300)
                        selected = True
                        break
                if not selected:
                    print(f"  (未找到模型选项，用 Dola 默认: {model_key})", flush=True)
                await page.wait_for_timeout(500)
            except Exception as e:
                print(f"  (设置模型失败，用 Dola 默认: {str(e)[:100]})", flush=True)
            # Confirm both settings in the current combined slider panel (or legacy UI).
            from browser_video_params import select_video_parameters
            emit_feedback(on_poll, progress="正在确认视频模型、时长和比例（尚未提交生成）")
            await select_video_parameters(page, duration, ratio)
            await dismiss_popups(page)
            box = await page.query_selector("textarea") or await page.query_selector('[contenteditable="true"]')
            try:
                await box.click()
            except Exception:
                await box.evaluate("(el) => el.focus()")
            emit_feedback(on_poll, progress=f"正在输入提示词（{len(prompt)} 字，尚未提交生成）")
            await type_prompt(page, prompt)
            await page.wait_for_timeout(600)
            replies = BrowserReplies(page, on_poll)
            replies.start()
            await page.keyboard.press("Enter")
            emit_feedback(on_poll, progress="已发送生成请求，等待上游会话确认")
            print(f"[{account}] UI 已发送: {prompt[:40]}", flush=True)

            # ---- 滑块求解（最多 3 次）----
            solved_or_absent = False
            for attempt in range(1, 4):
                frame = None
                for _ in range(20):
                    await page.wait_for_timeout(1000)
                    frame = find_captcha_frame(page)
                    if frame:
                        break
                if not frame:
                    solved_or_absent = True
                    break
                print(f"[{account}] 滑块出现，第 {attempt} 次求解...", flush=True)
                if await solve_slider(page, frame, attempt, account):
                    print(f"[{account}] 滑块通过 ✓", flush=True)
                    await page.wait_for_timeout(3000)  # 等前端自动重试提交
                    solved_or_absent = True
                    break
                print(f"[{account}] 滑块未过，等刷新重试", flush=True)
            if not solved_or_absent:
                await page.screenshot(path="solve_fail.png")
                raise RiskControlError("滑块 3 次未通过")

            # ---- 等真实 conversation_id（URL 从 local_x 变成数字 id）----
            conv_id = ""
            for _ in range(30):
                await page.wait_for_timeout(1000)
                if await is_logged_out(page):
                    raise LoginExpiredError(
                        f"{account} 登录态失效（发送后页面出现登录入口），请重新登录该账号")
                tail = page.url.rstrip("/").split("/")[-1]
                if tail.isdigit():
                    conv_id = tail
                    break
            if not conv_id:
                await page.screenshot(path="no_conv.png")
                raise TimeoutError("30s 未拿到 conversation_id")
            print(f"[{account}] conversation_id={conv_id}，轮询出片...", flush=True)

            deadline = time.time() + timeout
            if on_conversation_id:
                on_conversation_id(account, conv_id, deadline)
            return await poll_conversation(
                account, page, context, conv_id, timeout, on_poll, on_balance,
                duration, ratio)
        finally:
            try:
                if replies is not None:
                    await replies.close()
            finally:
                await context.close()


async def _main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    prompt = sys.argv[2] if len(sys.argv) > 2 else "一只橘猫在窗台上打盹，阳光洒进来"
    ratio = sys.argv[3] if len(sys.argv) > 3 else None
    duration = int(sys.argv[4]) if len(sys.argv) > 4 else None
    model = sys.argv[5] if len(sys.argv) > 5 else "seedance_v2.0"
    result = await generate_video(account, prompt, ratio, duration, model=model)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception:
        import traceback
        traceback.print_exc()
