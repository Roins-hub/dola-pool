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
from dola_client import CREDIT_FAIL_PATTERN, CreditError
from video_worker import POLL_JS, RiskControlError, _download, extract_unwatermarked_url

# Dola 返回的每日上限文案（当前日文 UI 实测：動画生成の 1 日あたりの上限に達しました。）
DAILY_LIMIT_PATTERN = re.compile(
    r"動画生成の\s*1日あたりの上限|每日(?:视频|影片)?生成.*(?:上限|限额|额度)|"
    r"daily.*(?:limit|quota)|(?:limit|quota).*per\s*day",
    re.IGNORECASE,
)

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
    """构造时长选择的回复：
    格式1（A. 5 秒で生成 / B. 10 秒で生成 / C. 15 秒で生成）→ 回完整选项文本（实测裸 "B" 会报错，
    "B. 10秒" 才被接受）；
    格式2（例：・16:9、10秒）→ 回「比例、N秒」。
    """
    if not text or not duration:
        return None
    letter_matches = list(DURATION_CHOICE_RE.finditer(text))
    if letter_matches:
        for m in letter_matches:
            if int(m.group(2)) == duration:
                return f"{m.group(1).upper()}. {duration}秒"
        return None  # 有字母选项但没有匹配时长 → 无法满足，交给终结检测快速失败
    # 新格式（2026-09-01）：「動画生成は現在4～15秒に対応しています。…別の秒数をご希望でしたら指定してください。」
    # 没有字母选项也没有「例：16:9、10秒」，直接回「N秒」即可。
    if re.search(r"対応しています|別の秒数|秒数をご希望|秒数を指定", text, re.IGNORECASE):
        return f"{duration}秒"
    return f"{ratio or '16:9'}、{duration}秒"


class AccountLimitedError(Exception):
    """当前 Dola 账号达到每日视频生成上限，应立即换下一个账号。"""


class CreditInsufficientError(Exception):
    """生成前已知积分不足，不提交视频，换下一个账号。"""


class LoginExpiredError(Exception):
    """Dola 会话已失效（页面出现登录入口），应立即换下一个账号并标记该号需重新登录。"""


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
    proxy = proxy_store.account_proxy_url(account) if account else (config.PROXY or None)
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
    """
    try:
        vm = json.loads(video_model_str or "{}")
        fb = vm.get("fallback_api") or ""
        if not fb or not fb.startswith("http"):
            return None
        parts = urlsplit(fb)
        q = dict(__import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(
            parts.query, keep_blank_values=True))
        q["codec_type"] = "8"     # h264 original
        q["logo_type"] = "unwatermarked"
        url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                          urlencode(q), parts.fragment))
        async with aiohttp.ClientSession() as s:
            proxies = ([proxy_store.account_proxy_url(account)] if account
                       else ([config.PROXY] if config.PROXY else []))
            for proxy in proxies + [None]:
                try:
                    async with s.get(url, proxy=proxy or None,
                                     timeout=aiohttp.ClientTimeout(total=30)) as r:
                        if r.status != 200:
                            continue
                        data = json.loads(await r.text())
                        vi = (((data.get("video_info") or {}).get("data"))
                              or data.get("data") or data)
                        vl = (vi.get("video_list") or {})
                        best = None
                        for v in vl.values():
                            mu = v.get("main_url") or ""
                            if not mu:
                                continue
                            dec = _decode_main_token(mu, vm.get("key_seed") or "")
                            if not dec.startswith("http"):
                                continue
                            rate = int(v.get("bitrate") or v.get("real_bitrate") or 0)
                            if best is None or rate > best[0]:
                                best = (rate, dec)
                        if best:
                            return best[1]
                except Exception:
                    import traceback
                    traceback.print_exc()
                    continue
    except Exception:
        pass
    return None



async def attach_reference_images(page, image_paths: list[str]) -> None:
    """通过 Dola 原生文件 input 上传参考图片，等待 prepare_upload/TOS 完成。"""
    if not image_paths:
        return
    file_input = page.locator('input[type="file"]').first
    await file_input.wait_for(state="attached", timeout=10000)
    events = []

    def on_response(response):
        url = response.url
        if "/alice/resource/prepare_upload" in url or "/upload/v1/" in url:
            events.append((response.status, url))

    page.on("response", on_response)
    try:
        await file_input.set_input_files(image_paths)
        expected = len(image_paths)
        deadline = time.time() + max(60, expected * 20)
        while time.time() < deadline:
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
        raise TimeoutError(
            f"参考图片上传超时: prepare={prepare_count}/{expected}, "
            f"tos={tos_count}/{expected}, 缩略图={thumb_count}/{expected}"
        )
    finally:
        page.remove_listener("response", on_response)


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
    cookies = await context.cookies("https://www.dola.com")
    ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
    start = time.time()
    deadline = start + timeout  # 出片超时上限（默认 30 分钟，dola.env DOLA_VIDEO_TIMEOUT）
    last_callback = 0.0
    terminal_hits = 0
    hard_error_hits = 0
    last_terminal_text = ""
    last_hard_error_text = ""
    choice_replied = False
    while True:
        await asyncio.sleep(5)
        if time.time() > deadline:
            raise TimeoutError(
                f"出片超时：{int(timeout)}s 内未出片（conversation_id={conversation_id}）"
            )
        try:
            poll = await asyncio.wait_for(page.evaluate(
                POLL_JS, {"conversationId": conversation_id, "msToken": ms_token, "fp": fp}), timeout=30)
        except Exception as e:
            print(f"  轮询异常: {e}", flush=True)
            continue
        now = time.time()
        if on_poll and now - last_callback >= 30:
            on_poll(now)
            last_callback = now
        texts = poll.get("texts") or []
        videos = poll.get("videos") or []
        # ---- 新版 Dola：提交后要求 A/B/C 选择时长，不回复则永远不出片 ----
        if duration and not choice_replied:
            joined = "\n".join(texts)
            if DURATION_CHOICE_QUESTION_RE.search(joined):
                reply = _build_duration_reply(joined, duration, ratio)
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
                        except Exception as e:
                            print(f"[{account}] 回复时长选择失败: {str(e)[:100]}", flush=True)
        terminal_now = False
        acceptance_now = False
        hard_error_now = False
        for text in texts:
            balance, _, source = _parse_balance_texts([text])
            if balance is not None and on_balance:
                on_balance(balance, source)
            if DAILY_LIMIT_PATTERN.search(text):
                raise AccountLimitedError(f"账号每日生成上限: {text[:120]}")
            if CREDIT_FAIL_PATTERN.search(text):
                raise CreditError(f"额度不足: {text[:80]}")
            if ACCEPTANCE_TEXT_PATTERN.search(text):
                acceptance_now = True
            if not terminal_now and any(p.search(text) for p in TERMINAL_TEXT_PATTERNS):
                terminal_now = True
                last_terminal_text = text
            if not hard_error_now and any(p.search(text) for p in HARD_ERROR_TEXT_PATTERNS):
                hard_error_now = True
                last_hard_error_text = text
        # 已回复时长选择后，同一段时长提问文字会一直在对话里，不能把它继续当失败信号；
        # 真失败会走硬错误识别或 30 分钟出片超时。
        if not videos and terminal_now and not acceptance_now:
            if not (choice_replied and DURATION_CHOICE_QUESTION_RE.search(last_terminal_text)):
                terminal_hits += 1
            else:
                terminal_hits = 0
        else:
            terminal_hits = 0
        if terminal_hits >= TERMINAL_TEXT_MAX_HITS:
            raise RuntimeError(
                f"Dola 未真正出片（连续收到终结性文字回复且无视频，疑似提交失败）: "
                f"{last_terminal_text[:150]}"
            )
        if not videos and hard_error_now and not acceptance_now:
            hard_error_hits += 1
        else:
            hard_error_hits = 0
        if hard_error_hits >= HARD_ERROR_TEXT_MAX_HITS:
            raise RuntimeError(
                f"Dola 返回错误（连续 {HARD_ERROR_TEXT_MAX_HITS} 次且无视频）: "
                f"{last_hard_error_text[:150]}"
            )
        if videos:
            video_models = poll.get("videoModels", [])
            if video_models:
                print(
                    f"[{account}] videoModels={str(video_models[0])[:2000]}",
                    flush=True,
                )
            vm_str = video_models[0] if video_models else ""
            # 优先用 fallback_api 的 H.264 original 高码率源（约 3Mbps），
            # 失败再回退默认 bytevc1 低码率源。
            url = await fetch_higher_bitrate_url(vm_str, account) \
                or extract_unwatermarked_url(vm_str, videos[0])
            print(f"[{account}] 出片! 下载中（高码率优先）...", flush=True)
            local = await _download(url, account)
            print(f"[{account}] 已下载 {local}（{local.stat().st_size / 1e6:.1f} MB）", flush=True)
            return {"video_url": url, "local_path": str(local),
                    "conversation_id": conversation_id, "account": account}
        print(f"  ...生成中（{int(time.time() - start)}s）", flush=True)


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
    elif model_key in ("seedance_2.0", "seedance_v2.0", "seedance_20", "seedance_v20"):
        model_key = "seedance_v2.0"
    else:
        raise ValueError(f"不支持的模型: {model}（当前支持 seedance-2.0 / seedance-2.5）")
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
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            await dismiss_popups(page)
            await ensure_login_stable(page, account)
            cookies = await context.cookies("https://www.dola.com")
            ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
            await _preflight_balance(page, ms_token, fp, config.VIDEO_REQUIRED_POINTS)

            # ---- UI 发送 ----
            try:
                await page.click(VIDEO_BTN)
            except Exception:
                await page.get_by_text("動画を作成", exact=False).first.evaluate("(el) => el.click()")
                await page.wait_for_timeout(500)
            await page.wait_for_timeout(1500)
            if reference_image_paths:
                await attach_reference_images(page, reference_image_paths)
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
                try:
                    await current_model.click(timeout=5000)
                except Exception:
                    # radix popper 子元素拦截点击时，用 JS 直接派发点击
                    await current_model.evaluate("(el) => el.click()")
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
            if ratio:
                try:
                    await page.click("text=比率", timeout=3000)
                    await page.wait_for_timeout(500)
                    await page.click(f"text={ratio}", timeout=3000)
                except Exception as e:
                    print(f"  (设置比例失败，用默认: {str(e)[:80]})", flush=True)
            if duration:
                try:
                    await page.click(f"text={duration}s", timeout=3000)
                except Exception:
                    try:  # 先点开时长下拉（当前值按钮如「10s」）再选
                        await page.get_by_text(re.compile(r"^\d+s$")).first.click(timeout=3000)
                        await page.wait_for_timeout(500)
                        await page.click(f"text={duration}s", timeout=3000)
                    except Exception as e:
                        print(f"  (设置时长失败，用默认: {str(e)[:80]})", flush=True)
            await dismiss_popups(page)
            box = await page.query_selector("textarea") or await page.query_selector('[contenteditable="true"]')
            try:
                await box.click()
            except Exception:
                await box.evaluate("(el) => el.focus()")
            await page.keyboard.type(prompt, delay=100)
            await page.wait_for_timeout(600)
            await page.keyboard.press("Enter")
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
