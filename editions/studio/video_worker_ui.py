"""视频生成 worker v2（UI 流 + 滑块求解）——当前主线。

链路：点「创建视频」→ 输入 → 聚焦输入框后回车（前端自带 a_bogus 签名）
     → 若弹 bdcaptcha 滑块：OpenCV 识别缺口 + 拟人轨迹拖动
     → 前端自动重试提交 → 轮询 /im/chain/single 出片 → 下载到 downloads/

为什么走 UI 而不是页面内裸 fetch：裸 fetch 缺 webmssdk 的 a_bogus 签名，
必触发 shark 滑块（710022004）；UI 流由前端自己签名，只需过滑块这一关。

用法：python video_worker_ui.py <账号名> "提示词" [比例] [时长秒]
"""
import asyncio
from contextlib import asynccontextmanager, suppress
import base64
import json
import random
import re
import sys
import time
from pathlib import Path

import aiohttp
from patchright.async_api import async_playwright

from gap import find_gap_x

import config
import proxy_usage
from account_errors import LoginExpiredError
from browser import cookie_value, install_duration_rewriter, launch_account_context
from dola_client import CREDIT_FAIL_PATTERN, CreditError
from video_worker import POLL_JS, RiskControlError, _download, extract_unwatermarked_url
from video_polling import HttpVideoSession, PollingResponseError, save_poll_state, parse_poll_response
from video_prompt import build_video_prompt
from video_capabilities import normalize_model, to_wire_model, validate_video_parameters
from integrations.dola_ref_upload import upload_reference_media, _main_world
from navigation_errors import BrowserCleanupError, BrowserSetupError, terminal_setup_reason, safe_navigation_reason
from video_evidence import observe_submission, safe_text, matches_submitted_prompt, SubmissionUnconfirmedError, submission_audit
from video_metadata import read_video_duration
from video_delivery import dola_download
from video_outcome import (conversation_outcome, VideoQueryTimeout, ExplicitNotAcceptedError, PlatformNoChargeError, GENERATION_FAIL_PATTERN,
                           is_seedance25_receipt as _is_seedance25_receipt, is_fast_receipt as _is_fast_receipt)

_download_slots = asyncio.Semaphore(config.MAX_DOWNLOADS)
VIDEO_SELECTION_JS = (Path(__file__).parent / 'web' / 'dola-video-selection.js').read_text(encoding='utf-8')


async def _bounded_browser_cleanup(close, resource, timeout=15):
    """Never reuse an ISP after unconfirmed shutdown or hide task cancellation."""
    original_error = sys.exc_info()[1]
    cancelled = original_error if isinstance(original_error, asyncio.CancelledError) else None
    already_unsafe = isinstance(original_error, BrowserCleanupError) or getattr(
        original_error, 'browser_cleanup_failed', False)
    deadline = asyncio.get_running_loop().time() + timeout
    task = asyncio.ensure_future(close())

    def consume_result(finished):
        try:
            finished.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(consume_result)
    try:
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError as exc:
            # Cancellation must not abandon Chromium and let the pool open the
            # next account on this ISP. Keep the same bounded cleanup deadline.
            cancelled = exc
            if already_unsafe:
                cancelled.browser_cleanup_failed = True
            done, _ = await asyncio.wait(
                {task}, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        if not done:
            raise TimeoutError()
        try:
            task.result()
        except asyncio.CancelledError:
            raise RuntimeError('cleanup operation cancelled') from None
    except asyncio.CancelledError as exc:
        exc.browser_cleanup_failed = True
        raise
    except Exception as exc:
        if cancelled is not None:
            cancelled.browser_cleanup_failed = True
            raise cancelled from None
        # Only the exception type is safe to report: transport messages can
        # contain proxy credentials or signed resource URLs.
        raise BrowserCleanupError(
            f'{resource}未确认关闭（{type(exc).__name__}）；已暂停该 ISP 线路提交，请检查遗留浏览器。') from None
    finally:
        if not task.done():
            # wait_for would wait indefinitely for a stuck protocol cancel ack.
            task.cancel()
    if cancelled is not None:
        raise cancelled


async def _close_submission_context(context, timeout=15):
    await _bounded_browser_cleanup(context.close, '提交浏览器', timeout)


@asynccontextmanager
async def _submission_playwright():
    runtime = async_playwright()
    driver = await runtime.__aenter__()
    try:
        yield driver
    finally:
        error = sys.exc_info()
        await _bounded_browser_cleanup(lambda: runtime.__aexit__(*error), '浏览器驱动')


async def _bounded_panel_wait(waiter, timeout=120, stage='视频面板'):
    # DOM calls also take time (and may hang on a reloading renderer), so counting
    # 120 sleeps is not a 120-second deadline. This runs only before prompt input.
    # Patchright cancellation waits for a protocol __abort__ acknowledgement.
    # wait_for() would wait for that too, losing its deadline if the driver hangs.
    task = asyncio.ensure_future(waiter)

    def consume_result(finished):
        try:
            finished.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(consume_result)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if done:
            return task.result()
        task.cancel()
        # Give ordinary cancellation a short cleanup window, never an unbounded
        # await. The caller still closes the browser before releasing its ISP.
        done, _ = await asyncio.wait({task}, timeout=min(.25, max(.01, timeout)))
        if not done:
            task.cancel()
    except asyncio.CancelledError:
        task.cancel()
        raise
    except asyncio.TimeoutError:
        pass
    raise BrowserSetupError(
        f'{stage}加载超过 {timeout:g} 秒（白屏、加载不完整或页面无响应）；'
        '提示词未发送，已停止本次提交，请检查页面资源或 ISP 线路。') from None


async def _set_accessible_duration_slider(page, target):
    """Use the real keyboard handler of the current Dola/Radix slider.

    Its thumb is role=slider rather than input[type=range]. DOM assignments or
    fabricated events cannot update the controlled value behind that thumb.
    The existing composer guard still verifies model, seconds and ratio.
    """
    candidates = page.get_by_role('slider')
    visible = [candidates.nth(index) for index in range(await candidates.count())
               if await candidates.nth(index).is_visible()]
    if len(visible) != 1:
        return None
    slider = visible[0]
    try:
        minimum = float(await slider.get_attribute('aria-valuemin'))
        maximum = float(await slider.get_attribute('aria-valuemax'))
    except (TypeError, ValueError):
        return None
    if not (0 <= minimum < maximum <= 600):
        return None
    async def read_seconds():
        state = await page.evaluate(VIDEO_SELECTION_JS)
        label = str(state.get('durationText') or '') if isinstance(state, dict) else ''
        values = set(re.findall(r'(?<!\d)(\d+)\s*(?:seconds?\b|Sek(?:unden?)?\.?\b|s\b|秒)', label, re.I))
        return int(next(iter(values))) if len(values) == 1 else None

    # Playwright key presses focus this exact visible thumb, allowing the
    # platform's own onValueChange to update state and the displayed chip.
    await slider.press('Home', timeout=3000)
    await page.wait_for_timeout(80)
    try:
        home_index = float(await slider.get_attribute('aria-valuenow'))
    except (TypeError, ValueError):
        return None
    home_seconds = await read_seconds()
    if home_index != minimum or home_seconds is None:
        return None
    # The current widget's 0..6 indices represent 4..10 seconds. Calibrate
    # from its displayed seconds instead of mistaking an index for a duration.
    direction = 'ArrowUp' if await slider.get_attribute('aria-orientation') == 'vertical' else 'ArrowRight'
    await slider.press(direction, timeout=3000)
    await page.wait_for_timeout(80)
    try:
        next_index = float(await slider.get_attribute('aria-valuenow'))
    except (TypeError, ValueError):
        return None
    next_seconds = await read_seconds()
    if next_seconds is None or next_index <= home_index or next_seconds <= home_seconds:
        return None
    seconds_step = next_seconds - home_seconds
    max_steps = round((maximum - minimum) / (next_index - home_index))
    steps = min(max_steps, max(0, round((target - home_seconds) / seconds_step)))
    if max_steps > 120:
        return None
    if steps == 0:
        await slider.press('Home', timeout=3000)
    for _ in range(max(0, steps - 1)):
        await slider.press(direction, timeout=3000)
    await page.wait_for_timeout(80)
    actual = await read_seconds()
    if actual != home_seconds + steps * seconds_step:
        return None
    return {'applied': actual, 'min': home_seconds,
            'max': home_seconds + max_steps * seconds_step, 'step': seconds_step}


async def _verify_fast_native(page, model, duration, *, prepare=False):
    if normalize_model(model) == 'seedance-2.0' and duration in (15, 30):
        native = None
        method = 'prepare' if prepare else 'check'
        # Lazy input modules can appear after the visible controls. Bound both
        # evaluation and waiting; checking after uploads must never re-prepare.
        attempts = 31 if prepare else 8
        deadline = asyncio.get_running_loop().time() + (9 if prepare else 2.4)
        unreadable = False
        for attempt in range(attempts):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                native = await _bounded_panel_wait(
                    _main_world(page, f'() => globalThis.__DOLA_FAST_NATIVE__?.{method}() || null'),
                    timeout=remaining, stage=f'Fast{duration} 原生核验')
                unreadable = not isinstance(native, dict)
            except Exception:
                native = None
                unreadable = True
            if (isinstance(native, dict) and native.get('ready') is True and native.get('pipelineReady') is True
                    and native.get('model') == 'seedance_v2.0' and native.get('duration') == duration):
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if attempt < attempts - 1 and remaining > 0:
                await page.wait_for_timeout(min(300, remaining * 1000))
        state = native if isinstance(native, dict) else {}
        reasons = {
            'runtime_unavailable': '原生运行环境未就绪',
            'input_store_unavailable': '原生输入状态未就绪',
            'video_pipeline_unavailable': '原生视频提交管线未就绪',
            'record_unavailable': '原生视频参数记录未就绪',
            'selection_readback_failed': '原生选项回读未通过',
            'pipeline_read_only': '原生视频提交管线不可配置',
            'native_prepare_failed': '原生配置未完成',
            'pipeline_changed': '签名前提交管线已变化',
            'selection_changed': '原生选项已变化',
        }
        code = state.get('reasonCode')
        reason = reasons.get(code) if isinstance(code, str) else None
        reason = reason or ('原生核验状态不可读' if unreadable else '原生状态尚未就绪')
        # Never expose arbitrary store values, JavaScript exceptions or URLs.
        models = {'seedance_v2.0': 'Fast2.0', 'seedance_v2.0_std': '2.0标准',
                  'seedance_v2.5': '2.5', 'ic_mini': '1.0'}
        selected = state.get('model')
        selected = models.get(selected, '未确认') if isinstance(selected, str) else '未确认'
        seconds = state.get('duration')
        shown_seconds = f'{seconds:g}秒' if type(seconds) in (int, float) and seconds in (5, 10, 15, 30) else '未确认'
        pipeline = '已就绪' if state.get('pipelineReady') is True else '未就绪'
        raise BrowserSetupError(f'Fast{duration} 原生核对未通过（{reason}；模型={selected}，'
                                f'时长={shown_seconds}，签名前管线={pipeline}）；提示词未发送。')


async def _verify_video_selection(page, model, duration, ratio):
    """Fail before Enter if the current composer does not match the request."""
    expected_version = normalize_model(model).split('-', 1)[1]
    await _verify_fast_native(page, model, duration)
    expected_ratio = re.sub(r'\s+', '', ratio or '')
    verify_ratio = expected_ratio not in ('', 'auto', 'default')
    last = {}
    for attempt in range(8):
        try:
            state = await page.evaluate(VIDEO_SELECTION_JS)
        except Exception:
            state = {}
        last = state if isinstance(state, dict) else {}
        model_text = str(last.get('modelText') or '')
        versions = set(re.findall(r'(?<!\d)([12][.,][05])(?!\d)', model_text))
        versions = {value.replace(',', '.') for value in versions}
        model_matches = versions == {expected_version} and 'mini' not in model_text.lower()
        if expected_version == '2.0':
            model_matches = model_matches and bool(re.search(r'fast|schnell|快速|高速', model_text, re.I))
        duration_text = str(last.get('durationText') or '')
        seconds = set(re.findall(r'(?<!\d)(\d+)\s*(?:seconds?\b|Sek(?:unden?)?\.?\b|s\b|秒)', duration_text, re.I))
        if re.fullmatch(r'\d+', duration_text.strip()):
            seconds.add(duration_text.strip())
        duration_matches = duration is None or seconds == {str(duration)}
        ratio_text = re.sub(r'\s+', '', str(last.get('ratioText') or ''))
        ratio_matches = not verify_ratio or set(re.findall(r'\d+:\d+', ratio_text)) == {expected_ratio}
        if model_matches and duration_matches and ratio_matches:
            return
        if attempt < 7:
            await page.wait_for_timeout(300)
    shown = ', '.join(f'{label}={safe_text(last.get(key))[:80] or "未读到"}' for label, key in
                      (('模型', 'modelText'), ('时长', 'durationText'), ('比例', 'ratioText')))
    raise BrowserSetupError(f'发送前选项核对未通过（{shown}）；期望 Seedance {expected_version}、'
                            f'{duration if duration is not None else "默认"} 秒、{ratio or "默认比例"}。'
                            '提示词未发送，已结束本次浏览器提交。')

# Dola 返回的每日上限文案（当前日文 UI 实测：動画生成の 1 日あたりの上限に達しました。）
DAILY_LIMIT_PATTERN = re.compile(
    r"動画生成の\s*1日あたりの上限|每日(?:视频|影片)?生成.*(?:上限|限额|额度)|"
    r"daily.*(?:limit|quota)|(?:limit|quota).*per\s*day|"
    r"Tages(?:limit|kontingent)|(?:Limit|Kontingent).*Tag",
    re.IGNORECASE,
)


class AccountLimitedError(Exception):
    """当前 Dola 账号达到每日视频生成上限，应立即换下一个账号。"""


# dola 后端拒绝超长时长的文案（2026-10-06 实测：30s 提交被拒，
# 聊天内回复「视频只支持4-15秒」/ 德语类似 4 bis 15 Sekunden）。
# 时长限制文字仅保留为证据；上游可能在说明缩短时长后仍启动生成。
DURATION_LIMIT_PATTERN = re.compile(
    r"4\s*[-–~至到]\s*15\s*秒|只支持\s*4|between\s*4\s*and\s*15|"
    r"4\s*(?:bis|à|to)\s*15\s*Sekunden?[sn]?|Video[- ]?Dauer.*(?:4|15)|"
    r"supports?\s+(?:durations?\s+)?(?:from\s+)?4\s*(?:to|[-–])\s*15\s*seconds?|"
    r"maximale\s+unterstützte\s+Videolänge\s+beträgt\s+15\s*Sekunden|"
    r"(?:duration|length).*between\s*4\s*and\s*15",
    re.IGNORECASE,
)



class CreditInsufficientError(Exception):
    """生成前已知积分不足，不提交视频，换下一个账号。"""


class BirthdayVerificationRequired(RuntimeError):
    """需要账号使用者完成真实生日验证，暂停该账号的自动提交。"""


VIDEO_BTN = re.compile(
    # 2026-10 改版：入口按钮变成输入框底部技能条里的「Videos erstellen / Create videos」，
    # 旧文案（Video erstellen 等）保留兼容；德语新版是复数 Videos。
    # 必须整串锚定：否则会命中左侧历史栏里含「生成视频」字样的会话标题（如「账号生成视频」）。
    r"^(?:動画を作成|Videos? erstellen|Videos? generieren|Create videos?|Create Videos?|"
    r"创建视频|生成视频|视频生成|建立影片)$",
    re.IGNORECASE,
)
CAPTCHA_FRAME_KEY = "bdcaptcha.html"   # 字节 verifycenter 滑块 iframe


def find_captcha_frame(page):
    for f in page.frames:
        if CAPTCHA_FRAME_KEY in f.url:
            return f
    return None


async def _fetch_bytes(url: str, account: str | None = None) -> bytes:
    """兜底抓图：真的再发一次请求（会消耗 ISP 额度，所以先预扣 1 次）。"""
    proxy_usage.reserve_for_account(account, 1)
    async with aiohttp.ClientSession() as s:
        async with s.get(url, proxy=config.proxy_url_for(account)) as r:
            return await r.read()


async def _captcha_image_bytes(frame, url: str) -> bytes | None:
    """优先复用浏览器里已经加载好的图片字节：不额外发请求，也就不消耗 ISP 额度。

    跨域图片没有 CORS 头时 canvas 会被污染（toDataURL 抛错），此时返回 None，
    由调用方回退到 _fetch_bytes（会正常计数）。
    """
    try:
        data_url = await frame.evaluate(
            """async (src) => {
                const img = [...document.images].find(im => im.src === src);
                if (!img || !img.complete || !img.naturalWidth) return "";
                try {
                    const c = document.createElement('canvas');
                    c.width = img.naturalWidth;
                    c.height = img.naturalHeight;
                    c.getContext('2d').drawImage(img, 0, 0);
                    return c.toDataURL('image/png');
                } catch (e) { return ""; }
            }""",
            url,
        )
    except Exception:
        return None
    if not data_url or "," not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        return None



async def _dismiss_cookie_banner(page) -> bool:
    """关掉 Dola 的 Cookie 同意弹窗（德语/英语/中文文案都试一遍）。

    弹窗会挡住底部技能条，导致「Videos erstellen」入口既点不到也可能不加载。
    只点一次即可；找不到就返回 False，不报错。
    """
    labels = (
        "Alle ablehnen", "Alle akzeptieren",
        "Reject all", "Accept all", "Decline all",
        "全部拒绝", "全部接受",
    )
    for label in labels:
        try:
            loc = page.get_by_text(label, exact=True).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=5000)
                await page.wait_for_timeout(800)
                return True
        except Exception:
            pass
    # Some regions expose only an acknowledgement. Scope its generic OK label
    # to the visible Cookie notice so another task's confirmation is untouched.
    try:
        notice = page.get_by_text(re.compile(r'^Dola\s+(?:uses cookies|使用\s*Cookie)', re.I)).first
        if await notice.count() and await notice.is_visible():
            container = notice.locator('..')
            for _ in range(3):
                button = container.get_by_role('button', name=re.compile(r'^(OK|我知道了)$')).first
                if await button.count() and await button.is_visible():
                    await button.click(timeout=5000)
                    await page.wait_for_timeout(800)
                    return True
                container = container.locator('..')
    except Exception:
        pass
    return False


# Dola 服务端过载 / 限流提示文案（中 / 英 / 德 / 日）。
OVERLOAD_TEXTS = (
    "目前需求非常高", "需求非常高", "请稍后再试", "稍后再试",
    "too much demand", "high demand", "try again later",
    "Nachfrage", "später erneut", "später noch einmal",
    "需要が非常に高い", "しばらくしてから",
)


async def _page_has_overload(page) -> bool:
    """检测 Dola 是否弹出「需求过高，请稍后再试」的过载提示。

    命中说明服务端正在限流：消息发不出去，也不会创建 conversation，
    继续等只会白等超时，应尽快失败并把原因讲清楚。
    """
    for text in OVERLOAD_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(await loc.count()):
                try:
                    if await loc.nth(i).is_visible():
                        return True
                except Exception:
                    pass
        except Exception:
            pass
    return False


# dola 新版「参数确认」环节：发送视频请求后，dola 会先回一条德语确认消息，
# 列出 Dauer/Seitenverhältnis/Stil 并问「Soll ich es so generieren? Falls ja,
# antworte einfach Ja.」，必须回复 Ja 才真正开始生成。脚本必须识别并自动回复，
# 否则卡在轮询出片 → 超时 → 换号，白白浪费额度。
BIRTHDAY_MODAL_RE = re.compile(r"Geburtstag|Geburtsdatum|date of birth|birthday|生日|出生日期", re.IGNORECASE)


async def _handle_birthday_modal(page, account: str = "") -> bool:
    """可见生日提示和日期输入同时出现时停止，不猜测或提交出生日期。"""
    headings = page.get_by_text(BIRTHDAY_MODAL_RE)
    if not any([await headings.nth(i).is_visible() for i in range(await headings.count())]):
        return False
    inputs = page.locator('input[type="date"], input[placeholder*="yyyy" i]')
    for i in range(await inputs.count()):
        if await inputs.nth(i).is_visible():
            raise BirthdayVerificationRequired("待生日验证：请由账号使用者填写真实出生日期，完成后再开启调度")
    return False


CONFIRM_TEXTS = (
    "Soll ich es so generieren", "antworte einfach", "antworte einfach Ja",
    "Bestätige", "Generieren", "soll ich das Video",
)
CONFIRM_DURATION_RE = re.compile(
    r"Dauer\s*:\s*(\d+)\s*Sekunden?",
    re.IGNORECASE,
)


async def _find_confirmation(page) -> str | None:
    """检测 dola 是否正在等「参数确认」回复。

    命中返回确认消息全文（用于提取 Dauer 校验时长），未命中返回 None。
    """
    for text in CONFIRM_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(await loc.count()):
                try:
                    node = loc.nth(i)
                    if not await node.is_visible():
                        continue
                    inner = ""
                    try:
                        inner = (await node.inner_text()) or ""
                    except Exception:
                        pass
                    if inner and "Soll ich es so generieren" in inner:
                        return inner
                except Exception:
                    pass
        except Exception:
            pass
    return None


# 登录墙文案：未登录时 Dola 弹「登录解锁更多功能」，会盖住底部技能条。
# 「um mehr Features」是德语「更多功能」的精确特征，与登录动作词组合判定，
# 避免把正常导航里出现的「Anmelden/Sign in」误判成掉登录。
LOGIN_WALL_TEXTS = (
    "Melde dich an", "Anmelden", "Sign in", "Log in", "登录", "登入",
)
LOGIN_WALL_FEATURE_TEXTS = (
    "um mehr Features", "更多功能", "unlock more features", "unlock more",
)


async def _dismiss_login_modal(page) -> bool:
    """检测 Dola 的登录弹窗（未登录时出现，会挡住底部技能条）。

    检测到登录墙时抛出 LoginExpiredError（账号已掉登录，由号池标记死号并换号）；
    未检测到返回 False。
    """
    await _handle_birthday_modal(page)
    # 2026-10-07 实测教训：单看「Anmelden/Sign in」会误杀正常号——已登录页面的
    # 隐藏节点/菜单模板里也有这些词。必须「登录动作词 + 登录墙专属特征」同时命中
    # 才判定登录墙（「um mehr Features」=「解锁更多功能」，只出现在登录墙上）。
    feature = False
    for text in LOGIN_WALL_FEATURE_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(await loc.count()):
                try:
                    if await loc.nth(i).is_visible():
                        feature = True
                        break
                except Exception:
                    pass
            if feature:
                break
        except Exception:
            pass
    if not feature:
        return False
    for text in LOGIN_WALL_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(await loc.count()):
                try:
                    if await loc.nth(i).is_visible():
                        # 双条件同时命中，确认是登录墙。
                        raise LoginExpiredError("检测到登录墙（账号已掉登录）")
                except LoginExpiredError:
                    raise
                except Exception:
                    pass
        except LoginExpiredError:
            raise
        except Exception:
            pass
    return False


class PageUnavailableError(RuntimeError):
    """Dola 返回了错误页面，而非可用的聊天页面。"""


async def _page_unavailable_reason(page) -> str | None:
    """识别截图中的整页错误；聊天里引用相同文字不能触发刷新。"""
    errors = page.get_by_text(re.compile(
        r"^(?:该页面暂时不可用|This page is temporarily unavailable)$", re.I))
    if not any([await errors.nth(i).is_visible() for i in range(await errors.count())]):
        return None
    composers = page.locator('textarea, [contenteditable="true"]')
    for i in range(await composers.count()):
        if await composers.nth(i).is_visible():
            return None
    refresh = page.get_by_text(re.compile(r"^(?:刷新页面|Refresh)$", re.I))
    for i in range(await refresh.count()):
        if await refresh.nth(i).is_visible():
            return "Dola 页面暂时不可用（页面显示刷新按钮）"
    return None


async def goto_retry(page, url: str, tries: int = None, timeout: int = None) -> None:
    """带重试的页面跳转：ISP 出口偶发 ERR_CONNECTION_RESET / ERR_NETWORK_CHANGED。

    页面半加载会让底部技能条（Videos erstellen）根本不渲染，所以跳转失败必须重来，
    不能带着残缺页面往下走。超时/重试次数走 config：高峰期经出口代理加载单次可达 80s+，
    旧的 60s 会把其实能加载出来的页面判死。
    """
    tries = tries or config.GOTO_TRIES
    timeout = timeout or config.GOTO_TIMEOUT_MS
    last = None
    for i in range(tries):
        try:
            await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            reason = await _page_unavailable_reason(page)
            if reason:
                raise PageUnavailableError(reason)
            return
        except Exception as e:
            terminal = terminal_setup_reason(e)
            if terminal:
                raise BrowserSetupError(terminal) from None
            last = e
            print(f"  (打开页面失败，第 {i + 1}/{tries} 次: {safe_navigation_reason(e)})", flush=True)
            if i + 1 < tries:
                await page.wait_for_timeout(3000)
    reason = str(last) if isinstance(last, PageUnavailableError) else safe_navigation_reason(last)
    raise BrowserSetupError(f"打开 Dola 页面失败（已尝试 {tries} 次）：{reason}；已结束本次浏览器提交。") from None


async def attach_reference_images(page, image_paths: list[str]) -> None:
    """Legacy name: select all image/audio/video files together, before typing."""
    if not image_paths:
        return
    ok, message = await upload_reference_media(page, image_paths, timeout_s=max(120, len(image_paths)*20))
    if not ok:
        raise BrowserSetupError('参考素材上传失败：' + message)
    print(f'[upload] {message}', flush=True)


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

    # 省额度：先用浏览器已加载的图片（0 次请求），拿不到才重新下载（2 次请求）。
    bg_bytes = await _captcha_image_bytes(frame, bg["src"])
    piece_bytes = await _captcha_image_bytes(frame, piece["src"])
    if bg_bytes is None:
        bg_bytes = await _fetch_bytes(bg["src"], account)
    if piece_bytes is None:
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


async def poll_conversation(account: str, page, context, conversation_id: str,
                            timeout: int, on_poll=None, on_balance=None) -> dict:
    """轮询已受理会话；可被初次提交和服务重启恢复流程共同调用。"""
    cookies = await context.cookies("https://www.dola.com")
    ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
    async def fetch():
        await _dismiss_login_modal(page)
        return await page.evaluate(
            POLL_JS, {"conversationId": conversation_id, "msToken": ms_token, "fp": fp})
    return await _wait_for_video(account, conversation_id, time.time() + timeout,
                                 fetch, lambda url: _download(url, account), on_poll, on_balance)


async def _wait_for_video(account, conversation_id, deadline, fetch, download,
                          on_poll=None, on_balance=None, on_event=None, model=None,
                          submitted_prompt=None, accepted=False):
    start = time.time()
    last_callback = 0.0
    first = True
    valid_poll = False
    last_reply = None
    while time.time() < deadline:
        if not first:
            await asyncio.sleep(min(config.POLL_INTERVAL, max(0, deadline - time.time())))
        first = False
        if time.time() >= deadline:
            break
        try:
            poll = await asyncio.wait_for(fetch(), timeout=min(30, deadline - time.time()))
        except (aiohttp.ClientError, TimeoutError, PollingResponseError, ValueError) as e:
            # Avoid logging proxy credentials or signed request URLs from exceptions.
            detail = str(e) if isinstance(e, PollingResponseError) else type(e).__name__
            print(f"[{account}] 后台查询暂时失败，稍后重试: {detail}", flush=True)
            continue
        now = time.time()
        if poll.get('loginExpired'):
            raise LoginExpiredError('查询视频时 Dola 登录已失效，请重新登录或导入新 CK')
        valid_poll = True
        if on_poll and now - last_callback >= 30:
            on_poll(now)
            last_callback = now
        outcome = conversation_outcome(poll, model, submitted_prompt)
        texts = outcome['texts']
        has_generation_receipt = outcome['state'] in ('accepted', 'completed')
        if has_generation_receipt and not accepted:
            accepted = True
            if on_event:
                on_event('accepted', {'reply': '\n\n'.join(texts)})
        reply = outcome['reply']
        if reply and reply != last_reply:
            last_reply = reply
            if on_event:
                on_event('polling' if accepted else 'awaiting_receipt', {'reply': reply})
        if outcome['error'] and not (accepted and isinstance(outcome['error'], ExplicitNotAcceptedError)):
            if isinstance(outcome['error'], PlatformNoChargeError) and on_event:
                on_event('platform_rejected_no_charge', {'reply': outcome['error'].reply, 'reason': str(outcome['error'])})
            raise outcome['error']
        for text in texts:
            balance, _, source = _parse_balance_texts([text])
            if balance is not None and on_balance:
                on_balance(balance, source)
            if not poll.get('videos') and DAILY_LIMIT_PATTERN.search(text):
                raise AccountLimitedError(f"账号每日生成上限: {text[:120]}")
            if not poll.get('videos') and CREDIT_FAIL_PATTERN.search(text):
                raise CreditError(f"额度不足: {text[:80]}")
        if poll.get("videos"):
            video_models = poll.get("videoModels", [])
            source = dola_download(video_models[0] if video_models else "", poll["videos"][0])
            url = source['url']
            print(f"[{account}] 已出片，核对下载来源...", flush=True)
            if on_event:
                if not accepted:
                    on_event('accepted', {'video_count': len(poll['videos'])})
                on_event('downloading', {'video_count': len(poll['videos'])})
            async with _download_slots:
                local = await download(url)
            actual_duration = read_video_duration(local)
            print(f"[{account}] 已下载 {local}（{local.stat().st_size / 1e6:.1f} MB）", flush=True)
            return {"video_url": url, "local_path": str(local),
                    "conversation_id": conversation_id, "account": account, 'actual_duration': actual_duration,
                    'download_source': source['download_source'], 'watermark': source['watermark']}
        print(f"[{account}] {'后台等待出片' if accepted else '会话已建立，尚未确认受理'}（{int(time.time() - start)}s）", flush=True)
    if not accepted and valid_poll:
        raise SubmissionUnconfirmedError(f'原会话未见生成回执或视频（conversation_id={conversation_id}）；未重新提交')
    raise VideoQueryTimeout(f"原会话查询超时（conversation_id={conversation_id}），暂未取得成片；未重新提交")


async def resume_video(account: str, conversation_id: str, timeout: int,
                       on_poll=None, on_balance=None, deadline_at=None, on_event=None,
                       model=None, submitted_prompt=None, accepted=False,
                       scheduler=None, background_lane=None) -> dict:
    """使用原账号和代理在后台查询、下载，恢复时不启动浏览器或重新提交。"""
    deadline = deadline_at if deadline_at is not None else time.time() + timeout
    async with HttpVideoSession(account, conversation_id) as session:
        @asynccontextmanager
        async def lane():
            if background_lane is not None:
                async with background_lane:
                    yield
            elif scheduler is not None:
                async with scheduler.lane_slot(session.proxy_url or 'direct'):
                    yield
            else:
                yield
        async def fetch():
            async with lane():
                return await session.poll()
        async def download(url):
            headers = {'Referer': f'https://www.dola.com/chat/{conversation_id}'}
            if getattr(session, 'state', {}).get('user_agent'):
                headers['User-Agent'] = session.state['user_agent']
            async with lane():
                return await _download(url, account, proxy_url=session.proxy_url, own_isp=session.own_isp,
                    headers=headers, cookies=getattr(session, 'state', {}).get('cookies'))
        return await _wait_for_video(account, conversation_id, deadline, fetch,
                                     download, on_poll, on_balance, on_event, model, submitted_prompt, accepted)


import os as _os
_PAYLOAD_LOG = _os.environ.get("DOLA_PAYLOAD_LOG")


def _install_payload_log(page):
    """诊断用：把 /im/chain/single 等请求体写到 DOLA_PAYLOAD_LOG 指定的文件。"""
    if not _PAYLOAD_LOG:
        return
    def _on_req(req):
        u = req.url
        if re.search(r"/im/chain|skill/pack|action_bar", u):
            try:
                body = req.post_data
            except Exception:
                body = None
            try:
                with open(_PAYLOAD_LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": "req", "m": req.method,
                                        "url": u[:600], "body": (body or "")[:20000]},
                                       ensure_ascii=False) + "\n")
            except Exception:
                pass
    page.on("request", _on_req)


def _prompt_text_matches(actual: str, expected: str) -> bool:
    """Compare the whole prompt, allowing only rendered paragraph separators."""
    def rendered(text):
        text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\xa0', ' ')
        return re.sub(r'\n+', '\n', text).strip()

    return bool(expected.strip()) and rendered(actual) == rendered(expected)


async def _fill_video_prompt(page, text: str):
    """Write and verify one visible editor without ever pressing Send."""
    if not isinstance(text, str) or not text.strip():
        raise BrowserSetupError('提示词为空，已停止发送。')
    for attempt in range(3):
        try:
            await page.keyboard.press('Escape')
            for selector, is_textarea in (('textarea', True), ('[contenteditable="true"]', False)):
                editors = page.locator(selector)
                for index in range(await editors.count()):
                    editor = editors.nth(index)
                    if not await editor.is_visible() or not await editor.is_editable():
                        continue
                    await editor.click(timeout=3000)
                    try:
                        await editor.fill(text, timeout=3000)
                    except Exception as error:
                        terminal = terminal_setup_reason(error)
                        if terminal:
                            raise BrowserSetupError(terminal) from None
                        # Keep the replacement scoped to the same editor; newlines
                        # are inserted as text, never as individual Enter presses.
                        await editor.click(timeout=3000)
                        await editor.press('ControlOrMeta+A', timeout=3000)
                        await page.keyboard.insert_text(text)
                    await page.wait_for_timeout(300)
                    actual = (await editor.input_value() if is_textarea else await editor.inner_text()) or ''
                    if _prompt_text_matches(actual, text):
                        return
        except Exception as error:
            terminal = terminal_setup_reason(error)
            if terminal:
                raise BrowserSetupError(terminal) from None
        if attempt < 2:
            await page.wait_for_timeout(500)
    raise BrowserSetupError('提示词未能完整写入当前可见输入框，已结束本次浏览器提交。')


async def _submit_prompt_once(page, account, on_prompt_sent=None):
    """沿用原生 Enter 发送；重新聚焦已填输入框，结果不明不补发。"""
    await _handle_birthday_modal(page, account)
    await _dismiss_login_modal(page)
    # Parameter checks can steal focus after the draft has been verified.
    # Locator.press focuses that editor; a global keyboard Enter does not.
    # Keep the existing native keyboard path, which also avoids an unrelated
    # download extension's floating panel obscuring the Send button.
    for selector, is_textarea in (('textarea', True), ('[contenteditable="true"]', False)):
        editors = page.locator(selector)
        for index in range(await editors.count()):
            editor = editors.nth(index)
            if not await editor.is_visible() or not await editor.is_editable():
                continue
            text = await editor.input_value() if is_textarea else await editor.inner_text()
            if not text or not text.strip():
                continue
            if on_prompt_sent:
                on_prompt_sent(account)
            await editor.press('Enter', timeout=5000)
            return
    raise BrowserSetupError('未找到已填好的可见输入框，未发送提示词。')


async def _read_page_conversation(page, conversation_id, baseline_media=None, baseline_messages=None):
    poll = await page.evaluate(POLL_JS, {'conversationId': conversation_id, 'msToken': '', 'fp': ''})
    if poll.get('loginExpired'):
        raise LoginExpiredError('读取原会话时 Dola 登录已失效，请重新登录或导入新 CK')
    if poll.get('ok') is False:
        raise PollingResponseError('浏览器会话查询失败，不能作为有效回执或历史基线')
    if poll.get('raw'):
        return parse_poll_response(poll['raw'], conversation_id, baseline_media, baseline_messages)
    return poll


async def _wait_for_generation_receipt(page, account, conversation_id, submitted_prompt, timeout=120,
                                       model='seedance_v2.5', on_event=None, baseline_media=None, baseline_messages=None):
    """Keep the visible browser until receipt evidence; a conversation ID alone is insufficient."""
    deadline = time.monotonic() + timeout
    last_reply = None
    while time.monotonic() < deadline:
        await _dismiss_login_modal(page)
        try:
            poll = await _read_page_conversation(page, conversation_id, baseline_media, baseline_messages)
        except LoginExpiredError:
            raise
        except Exception as exc:
            terminal = terminal_setup_reason(exc)
            if terminal:
                raise BrowserSetupError(terminal) from None
            await page.wait_for_timeout(2000)
            continue
        if poll.get('loginExpired'):
            raise LoginExpiredError('等待生成回执时 Dola 登录已失效，请重新登录或导入新 CK')
        outcome = conversation_outcome(poll, model, submitted_prompt)
        texts = outcome['texts']
        reply = outcome['reply']
        if reply and reply != last_reply:
            last_reply = reply
            if on_event:
                on_event('awaiting_receipt', {'reply': reply})
        if outcome['error']:
            if isinstance(outcome['error'], PlatformNoChargeError) and on_event:
                on_event('platform_rejected_no_charge', {'reply': outcome['error'].reply, 'reason': str(outcome['error'])})
            raise outcome['error']
        if outcome['state'] in ('accepted', 'completed'):
            print(f'[{account}] 已确认视频生成回执或成片', flush=True)
            if on_event:
                on_event('accepted', {'reply': reply, 'video_count': outcome['video_count']})
            return
        for text in texts:
            if DAILY_LIMIT_PATTERN.search(text):
                raise AccountLimitedError('Dola 回复当日视频额度已用完')
            if CREDIT_FAIL_PATTERN.search(text):
                raise CreditError('Dola 回复视频额度不足')
        await page.wait_for_timeout(2000)
    raise SubmissionUnconfirmedError('未确认进入生成：未收到模型和额度的生成回执；未重复发送，请查看原会话')


NEW_CHAT_RE = re.compile(r'^(?:New\s+Chat|Neuer\s+Chat|新对话|新對話|新会话|新會話|新しいチャット)$', re.I)
WELCOME_RE = re.compile(
    r'^(?:How can I (?:assist|help) you today\??|Wie kann ich dir heute helfen\??|'
    r'有什[么麼].*[帮幫].*[吗嗎][？?]?|何かお手伝い.*)$', re.I)


async def _visible_composer(page):
    for selector in ('textarea', '[contenteditable="true"]'):
        editors = page.locator(selector)
        for i in range(await editors.count()):
            editor = editors.nth(i)
            if await editor.is_visible():
                box = await editor.bounding_box()
                if box and box['width'] > 0 and box['height'] > 0:
                    return editor, box, selector == 'textarea'
    return None


async def _find_composer_video_entry(page):
    """History titles can match VIDEO_BTN too; require proximity to the editor."""
    composer = await _visible_composer(page)
    if composer is None:
        return None
    _, editor_box, _ = composer
    entries = page.get_by_text(VIDEO_BTN, exact=True)
    matches = []
    for i in range(await entries.count()):
        entry = entries.nth(i)
        if not await entry.is_visible():
            continue
        box = await entry.bounding_box()
        if not box:
            continue
        x, y = box['x'] + box['width']/2, box['y'] + box['height']/2
        if (editor_box['x'] <= x <= editor_box['x'] + editor_box['width']
                and editor_box['y'] - 45 <= y <= editor_box['y'] + editor_box['height'] + 160):
            matches.append((abs(y - (editor_box['y'] + editor_box['height'])), i, entry))
    return min(matches, key=lambda item: item[:2])[2] if matches else None


async def _ensure_new_chat(page, timeout=30):
    """Only used before preparing a new request, never while resuming a video."""
    from urllib.parse import urlsplit
    deadline = time.monotonic() + timeout
    clicked = False
    while time.monotonic() < deadline:
        if not clicked:
            controls = page.get_by_text(NEW_CHAT_RE, exact=True)
            visible = []
            for i in range(await controls.count()):
                control = controls.nth(i)
                if await control.is_visible():
                    box = await control.bounding_box()
                    if box:
                        visible.append((box['y'], box['x'], i, control))
            if visible:
                # Navigation is above same-named items in the recent-chat list.
                await min(visible, key=lambda item: item[:3])[3].click(timeout=5000)
                clicked = True
        path = urlsplit(page.url).path.rstrip('/')
        if clicked and (path == '/chat' or re.fullmatch(r'/chat/local_[\w-]+', path)):
            composer = await _visible_composer(page)
            if composer:
                editor, editor_box, textarea = composer
                value = await editor.input_value() if textarea else await editor.inner_text()
                if not (value or '').strip():
                    headings = page.get_by_text(WELCOME_RE, exact=True)
                    for i in range(await headings.count()):
                        heading = headings.nth(i)
                        if await heading.is_visible():
                            box = await heading.bounding_box()
                            if box and box['x'] + box['width']/2 >= editor_box['x']:
                                return
        await page.wait_for_timeout(250)
    raise BrowserSetupError('未确认进入空白新会话，提示词未发送；保留原会话与账号登录状态。')


async def generate_video(account: str, prompt: str, ratio: str = None,
                         duration: int = None, timeout: int = None,
                         model: str = "seedance_v2.0", use_extension: bool = True,
                         on_conversation_id=None, on_poll=None, on_balance=None,
                         reference_image_paths: list[str] | None = None,
                         on_browser_closed=None, on_prompt_sent=None, on_event=None,
                         prepare_only: bool = False, scheduler=None, background_lane=None) -> dict:
    """完整出片流程（UI 流）。返回 {"video_url","local_path","conversation_id","account"}。

    ratio/duration: None = 用 UI 默认（当前默认 10s）。
    异常：RiskControlError（滑块 3 次不过）、CreditError（额度不足）、TimeoutError。
    """
    timeout = timeout or config.VIDEO_TIMEOUT
    if prepare_only and (prompt or reference_image_paths):
        raise ValueError('准备诊断必须使用空提示词且不上传参考素材')
    model, normalized_duration = validate_video_parameters(model, duration)
    background_handoff = (model == 'seedance-2.0' and normalized_duration == 10
                          and (scheduler is not None or background_lane is not None))
    model_key = to_wire_model(model)
    if model_key == 'ic_mini' and duration is None:
        duration = normalized_duration
    # 30 秒视频（尤其 Seedance 2.5）高峰期可能 30~45 分钟才出片，给足等待窗口。
    if duration == 30:
        timeout = max(timeout, config.LONG_VIDEO_TIMEOUT)
    if reference_image_paths:
        timeout = max(timeout, config.REFERENCE_VIDEO_TIMEOUT)
    async with _submission_playwright() as p:
        context = await launch_account_context(
            p, account, headless=False if use_extension else None,
            use_extension=use_extension, duration=duration,
            native_duration=duration if model_key in ('ic_mini', 'seedance_v2.5') or duration in (15, 30) else None,
            native_model=model)
        run = proxy_usage.begin_run(account)
        try:
            if prepare_only:
                # Defense in depth for the diagnostic path: no generation POST
                # can escape even if a native widget unexpectedly tries to send.
                async def block_generation(route):
                    await route.abort('blockedbyclient')
                await context.route('**/chat/completion*', block_generation)
            page = context.pages[0] if context.pages else await context.new_page()
            _install_payload_log(page)
            print(f"[{account}] 提交阶段：打开 Dola 页面（语言 {config.BROWSER_LOCALE}）", flush=True)
            await goto_retry(page, "https://www.dola.com/chat")
            print(f"[{account}] 提交阶段：页面已载入，检查登录", flush=True)
            await page.wait_for_timeout(5000)
            await _handle_birthday_modal(page, account)
            await _dismiss_login_modal(page)
            # 旧会话里的额度回复没有当前有效性，不能据此锁定账号。
            # 本地账本仍在调度时检查；平台额度由本次请求的原会话回复判定。
            from dola_watermark import ensure_page_preference
            await ensure_page_preference(page, account)
            if on_event:
                on_event('preparing', {'reason': '已回读确认本账号的移除水印设置已开启；收片仍核对原片来源'})
            print(f"[{account}] 提交阶段：等待视频入口（最多 {config.ENTRY_WAIT_SECONDS}s）", flush=True)

            # ---- UI 发送 ----
            # 先关掉 Cookie 弹窗，否则它会盖住底部技能条、入口也可能不加载。
            if await _dismiss_cookie_banner(page):
                print(f"[{account}] 已关闭 Cookie 弹窗", flush=True)
            # 未登录时会弹「登录解锁更多功能」，同样会盖住底部技能条，先关掉。
            if await _dismiss_login_modal(page):
                print(f"[{account}] 已关闭登录弹窗", flush=True)
            await _bounded_panel_wait(_ensure_new_chat(page), timeout=32, stage='新会话')
            print(f'[{account}] 已确认空白新会话，开始查找输入框的视频入口', flush=True)

            # 底部技能条是懒加载，先轮询等入口出现再点（新版入口文案：Videos erstellen）。
            # 页面里同一段文案会有隐藏的度量克隆（aria-hidden），必须挑可见的那一个点。
            #
            # 高峰期首屏很慢：入口往往几十秒后才渲染出来。以前只等 60s 就 page.reload()，
            # 结果慢加载刚把入口渲染出来就被刷新清掉，永远卡在「找不到入口」。
            # 改成：先耐心长等，并周期性把页面滚到底触发懒加载；只有等满整段仍没有
            # 才刷新一次，刷新后再同样长等。
            async def _find_entry(max_seconds: int = config.ENTRY_WAIT_SECONDS):
                deadline = time.monotonic() + max_seconds
                next_login_check = 0.0
                next_scroll = time.monotonic() + 15
                while time.monotonic() < deadline:
                    await _handle_birthday_modal(page)
                    if time.monotonic() >= next_login_check:
                        await _dismiss_cookie_banner(page)
                        await _dismiss_login_modal(page)
                        next_login_check = time.monotonic() + 10
                    reason = await _page_unavailable_reason(page)
                    if reason:
                        raise PageUnavailableError(reason)
                    best = await _find_composer_video_entry(page)
                    if best is not None:
                        return best
                    # 每 15s 轻轻滚到底一次，触发底部技能条的懒加载。
                    if time.monotonic() >= next_scroll:
                        try:
                            await page.mouse.wheel(0, 4000)
                        except Exception:
                            pass
                        next_scroll = time.monotonic() + 15
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        await page.wait_for_timeout(min(1000, remaining * 1000))
                return None

            # 尽力等页面 load 事件（超时也继续，不阻塞后面的轮询）。
            try:
                await page.wait_for_load_state("load", timeout=30000)
            except Exception:
                pass
            page_reloaded = False
            entry_error = None
            try:
                btn = await _bounded_panel_wait(
                    # Let a responsive page return None and reach the one reload;
                    # only a stuck DOM operation should hit this hard deadline.
                    _find_entry(), timeout=config.ENTRY_WAIT_SECONDS + 2, stage='视频入口')
            except PageUnavailableError as exc:
                entry_error = str(exc)
                btn = None
            if btn is None:
                # 明确的错误页立即刷新；正常懒加载仍保留完整等待窗口。
                reason = entry_error or f"入口 {config.ENTRY_WAIT_SECONDS}s 未出现"
                print(f"[{account}] {reason}，刷新页面重试...", flush=True)
                page_reloaded = True
                try:
                    await page.reload(timeout=90000, wait_until="domcontentloaded")
                except Exception as exc:
                    terminal = terminal_setup_reason(exc)
                    if terminal:
                        raise BrowserSetupError(terminal) from None
                    await goto_retry(page, "https://www.dola.com/chat")
                try:
                    await page.wait_for_load_state("load", timeout=60000)
                except Exception:
                    pass
                await page.wait_for_timeout(6000)
                await _dismiss_cookie_banner(page)
                await _dismiss_login_modal(page)
                await _bounded_panel_wait(_ensure_new_chat(page), timeout=32, stage='新会话')
                try:
                    btn = await _bounded_panel_wait(
                        _find_entry(), timeout=config.ENTRY_WAIT_SECONDS + 2, stage='视频入口')
                except PageUnavailableError as exc:
                    raise BrowserSetupError(f'{exc}；重试后仍不可用，已结束本次浏览器提交。') from None
            if btn is None:
                await page.screenshot(path=f"_no_entry_{account}.png")
                raise BrowserSetupError("找不到「创建视频」入口，已结束本次浏览器提交，请检查页面或 ISP 线路。")
            await btn.click(timeout=20000)
            print(f"[{account}] 提交阶段：已点击视频入口，等待生成面板", flush=True)
            await page.wait_for_timeout(1500)

            # 只挑可见节点：页面里同一段文案常有隐藏的度量克隆（aria-hidden），
            # 直接用 .first 可能点到隐藏节点。
            async def _first_visible(loc):
                for i in range(await loc.count()):
                    cand = loc.nth(i)
                    try:
                        if await cand.is_visible():
                            return cand
                    except Exception:
                        pass
                return None

            # 2026-10-07 午后改版：模型芯片从「Modell 2.0 Schnell / Modell 2.5」变成
            # 「Modell Dreamina Seedance …」（版本号不再紧跟 Modell），旧正则全部失配，
            # 导致「视频面板未打开」连环误判换号。放宽为任意 Modell/Model/モデル 前缀
            # （页面其余区域没有此前缀，不会误命中）。
            MODEL_RE = re.compile(r"^(?:(?:モデル|Model|Modell)\s|模型\s*)", re.IGNORECASE)
            # 比例选择后合并芯片显示「16:9 · 30s」；自动比例仍为
            # 「Automatisch · 10 Sekunden」。锚定开头，排除历史相对时间。
            DUR_CHIP_RE = re.compile(
                r"^(?:(?:Auto(?:matisch)?|自动|自動|\d+\s*:\s*\d+)\s*[·•・]?\s*)?"
                r"\d+\s*(?:s\b|Sek|秒|seconds?\b)",
                re.IGNORECASE)
            RATIO_CHIP_RE = re.compile(r"^(比率|比例|Ratio|Verhältnis|アスペクト比)")
            VIDEO_PLACEHOLDER_RE = re.compile(
                r"描述.*(?:视频|画面)|describe.*(?:video|scene)|beschreibe.*(?:video|szene)",
                re.IGNORECASE)

            async def _video_mode_on() -> bool:
                # 只认明确的视频文案/控件；语言切换也会改变普通聊天占位符。
                try:
                    el = await page.query_selector("textarea")
                    ph = (await el.get_attribute("placeholder")) if el is not None else None
                    if ph and VIDEO_PLACEHOLDER_RE.search(ph):
                        return True
                except Exception:
                    pass
                return await _panel_ready()

            async def _panel_ready() -> bool:
                # Lazy video-panel JS can fail after the entry was found. The
                # site's English/Chinese error fallback is not a locale change
                # or an unopened panel; stop before re-clicking or submitting.
                reason = await _page_unavailable_reason(page)
                if reason:
                    raise BrowserSetupError(
                        f"{reason}；视频面板加载失败，提示词未发送，已结束本次浏览器提交。")
                # 视频面板就绪标志：出现可见的模型芯片、时长控件（10s / 10 Sekunden /
                # Automatisch · 10 Sekunden，2026-10-07 改版后两样式并存）或比例芯片。
                if await _first_visible(page.get_by_text(MODEL_RE, exact=False)) is not None:
                    return True
                if await _first_visible(page.get_by_text(DUR_CHIP_RE)) is not None:
                    return True
                if await _first_visible(page.get_by_text(RATIO_CHIP_RE)) is not None:
                    return True
                return False

            # 2026-10 观察：技能条收起时第一次点击可能只展开技能条；但不能靠单一信号
            # 反复点击（面板已打开时再点入口会把面板关掉）。因此：
            #   - 已进入视频模式（占位符已切 / 有比例芯片）时只等芯片渲染，绝不再点入口；
            #   - 仍未进入视频模式才补点入口。
            async def _wait_panel(max_seconds=45):
                deadline = time.monotonic() + max_seconds
                for _attempt in range(4):
                    for _sec in range(30):
                        if time.monotonic() >= deadline:
                            return await _panel_ready()
                        if _sec % 5 == 0:
                            await _dismiss_cookie_banner(page)
                            await _dismiss_login_modal(page)
                        if await _panel_ready():
                            return True
                        await page.wait_for_timeout(1000)
                    if await _panel_ready():
                        return True
                    if await _video_mode_on():
                        # Video mode is active: do not click it closed while controls load.
                        await page.wait_for_timeout(2000)
                        continue
                    cand = await _find_composer_video_entry(page)
                    if cand is None:
                        break
                    try:
                        await cand.click(timeout=8000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1500)
                return await _panel_ready()

            try:
                panel_ready = await _bounded_panel_wait(_wait_panel(), timeout=47)
                # Recovery is before references/prompt input; never reload an accepted conversation.
                if not panel_ready and not page_reloaded:
                    page_reloaded = True
                    print(f"[{account}] 视频面板未完整加载，发送提示词前刷新一次重试", flush=True)
                    await _bounded_panel_wait(
                        page.reload(timeout=30000, wait_until='domcontentloaded'),
                        timeout=32, stage='页面刷新')
                    await _bounded_panel_wait(_ensure_new_chat(page), timeout=32, stage='新会话')
                    entry = await _bounded_panel_wait(
                        _find_entry(max_seconds=45), timeout=47, stage='视频入口')
                    if entry is not None:
                        await _bounded_panel_wait(entry.click(timeout=8000), timeout=10, stage='视频入口点击')
                        panel_ready = await _bounded_panel_wait(_wait_panel(), timeout=47)
            except BrowserSetupError:
                with suppress(Exception):
                    await page.screenshot(path=f"_no_panel_{account}.png", timeout=5000)
                raise
            if not panel_ready:
                with suppress(Exception):
                    await page.screenshot(path=f"_no_panel_{account}.png", timeout=5000)
                raise BrowserSetupError("视频面板未打开（未出现模型/时长控件），已结束本次浏览器提交。")
            print(f"[{account}] 提交阶段：设置模型 {model_key}、时长 {duration}s", flush=True)
            # 选择模型。兼容日语 / 英语 / 德语页面文案。
            try:
                current_model = None
                model_labels = (
                    "モデル 2.0高速", "モデル 2.5",
                    "Model 2.0 Fast", "Model 2.5",
                    "Modell 2.0 Schnell", "Modell 2.5",
                    "Seedance 2.0 Fast", "Seedance 2.5",
                    "Model 1.0", "Modell 1.0", "モデル 1.0", "Seedance 1.0",
                )
                async def _find_model_chip():
                    for label in model_labels:
                        c = await _first_visible(page.get_by_text(label, exact=False))
                        if c is not None:
                            return c
                    return await _first_visible(page.get_by_text(MODEL_RE, exact=False))

                current_model = None
                # 慢线（德国 ISP）上模型列表来自 dola API，渲染可能比面板其余部分晚
                # 几十秒；6s 等待实测不够（2026-10-06：「找不到模型选择控件」换号）。
                for _ in range(30):
                    current_model = await _find_model_chip()
                    if current_model is not None:
                        break
                    await page.wait_for_timeout(1000)
                if current_model is None:
                    await page.screenshot(path=f"_no_model_chip_{account}.png")
                    raise RuntimeError("找不到模型选择控件")
                await current_model.click(timeout=8000)
                await page.wait_for_timeout(500)
                options = {
                    'ic_mini': ('Dreamina Seedance 1.0', 'Seedance 1.0 Fast', 'Seedance 1.0', 'Seedance1.0Fast'),
                    'seedance_v2.5': ('Dreamina Seedance 2.5',),
                    'seedance_v2.0': ('Dreamina Seedance 2.0高速', 'Dreamina Seedance 2.0 Fast',
                                     'Dreamina Seedance 2.0', 'Seedance2.0Fast', 'Seedance 2.0 Fast'),
                }[model_key]
                # 点开后下拉选项是异步渲染的（走 dola API），单次检查会撞上未渲染
                # 空窗；轮询等待最长 15s。
                selected = False
                for _ in range(15):
                    for option_text in options:
                        cand = await _first_visible(page.get_by_text(option_text, exact=False))
                        if cand is not None:
                            await cand.click(timeout=5000)
                            selected = True
                            break
                    if selected:
                        break
                    await page.wait_for_timeout(1000)
                if not selected:
                    await page.screenshot(path=f"_no_model_option_{account}.png")
                    raise RuntimeError("模型选项不存在")
                await page.wait_for_timeout(500)
            except Exception as e:
                raise RuntimeError(f"设置模型失败（{model_key}）: {str(e)[:120]}") from e
            # "default"/"auto" = 保持 UI 默认（新 UI 比例已并入「Automatisch · 10 Sek.」面板，
            # 无独立 Verhältnis 芯片可点），跳过点击避免空等 5s 超时。
            if ratio and ratio not in ("default", "auto", ""):
                try:
                    # 比例标签随语言变化（德语 Verhältnis / 英语 Ratio / 日语 比率）
                    await page.get_by_text(
                        re.compile(r"^(比率|比例|Ratio|Verhältnis|アスペクト比)")
                    ).first.click(timeout=5000)
                    await page.wait_for_timeout(600)
                    # 下拉选项渲染在 body 末尾的浮层，取最后一个命中项避免点到芯片本身
                    await page.get_by_text(ratio, exact=True).last.click(timeout=5000)
                except Exception:
                    # 2026-10-07 新 UI：比例在「Automatisch · 10 Sek.」面板的 Seitenverhältnis
                    # 网格里（Automatisch/3:4/4:3/9:16/16:9/1:1/21:9）。点芯片打开面板后直接点。
                    try:
                        rchip = await _first_visible(page.get_by_text(DUR_CHIP_RE))
                        if rchip is None:
                            raise RuntimeError("找不到比例/时长芯片")
                        await rchip.click(timeout=5000)
                        await page.wait_for_timeout(1000)
                        await page.get_by_text(ratio, exact=True).last.click(timeout=5000)
                        await page.wait_for_timeout(500)
                        try:
                            await page.keyboard.press("Escape")
                            await page.wait_for_timeout(300)
                        except Exception:
                            pass
                        print(f"  (比例已在新面板设置: {ratio})", flush=True)
                    except Exception as e2:
                        print(f"  (设置比例失败，用默认: {str(e2)[:80]})", flush=True)
            if duration:
                # 2026-10-07 两次改版兼容（实测 dump）：
                #   旧 UI：芯片「10s」→ 下拉选项列表（「10s」等）。
                #   新 UI：芯片「Automatisch · 10 Sek.」（NBSP+\xa0+带点）→ 点开是
                #          「Seitenverhältnis 比例网格 + Dauer 滑杆」，滑杆实测 4~10 Sek.，
                #          已无可点的选项列表。
                # 策略：a) 芯片当前值已等于期望 → 直接用（快路径，覆盖默认 10s 场景）；
                #       b) 面板有 input[type=range] 滑杆 → JS 原生 setter 设值（clamp+step 取整）；
                #       c) 有期望时长的可点选项（新旧文案都试，含「10 Sek.」带点）→ 点它；
                #       d) 全失败 → 发送前核对会停止任务，不能用文本说明替代页面选项。
                _dur_opts = (f"{duration}s", f"{duration} Sek.", f"{duration} Sekunden",
                             f"{duration} Sek", f"{duration} 秒", f"{duration}秒",
                             f"{duration} seconds", f"{duration}")

                def _dur_ok(shown: str) -> bool:
                    return bool(re.search(
                        rf"(?<!\d){duration}\s*(?:s\b|Sek|秒|seconds?\b)|(?:^|\s){duration}(?:$|\s)",
                        shown or "", re.IGNORECASE))

                async def _read_chip_text() -> str:
                    c = await _first_visible(page.get_by_text(re.compile(r"^\d+s$")))
                    if c is None:
                        c = await _first_visible(page.get_by_text(DUR_CHIP_RE))
                    return ((await c.inner_text()) or "").strip() if c is not None else ""

                async def _set_slider_value(target: int):
                    # React 受控 input[type=range]：必须用原型原生 setter 设值再派发事件，
                    # 直接赋 value 会被 React 状态覆写回去。只取可见滑杆（面板渲染在 body 末尾浮层）。
                    native = await page.evaluate(
                        """(v) => {
                            const sliders = Array.from(document.querySelectorAll('input[type=range]'))
                                .filter(s => s.offsetParent !== null);
                            if (!sliders.length) return null;
                            const s = sliders[0];
                            const min = parseFloat(s.min || '0'), max = parseFloat(s.max || '100');
                            const step = parseFloat(s.step || '1') || 1;
                            let t = Math.min(max, Math.max(min, v));
                            t = min + Math.round((t - min) / step) * step;
                            t = Math.min(max, Math.max(min, t));
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            setter.call(s, String(t));
                            s.dispatchEvent(new Event('input', {bubbles: true}));
                            s.dispatchEvent(new Event('change', {bubbles: true}));
                            return {applied: t, min: min, max: max, step: step};
                        }""",
                        target,
                    )
                    if native is not None:
                        return native
                    return await _set_accessible_duration_slider(page, target)

                for _dur_try in range(3):
                    try:
                        # 先关掉可能遗留的菜单，再操作，保证状态干净。
                        try:
                            await page.keyboard.press("Escape")
                            await page.wait_for_timeout(300)
                        except Exception:
                            pass
                        # a) 快路径：芯片当前值已满足期望（如默认「Automatisch · 10 Sek.」+ 要 10s）
                        chip_text_before = await _read_chip_text()
                        if chip_text_before and _dur_ok(chip_text_before):
                            print(f"  (芯片已是期望时长: {chip_text_before})", flush=True)
                            break
                        chip = await _first_visible(page.get_by_text(re.compile(r"^\d+s$")))
                        if chip is None:
                            chip = await _first_visible(page.get_by_text(DUR_CHIP_RE))
                        if chip is None:
                            raise RuntimeError("找不到时长芯片")
                        await chip.click(timeout=5000)
                        await page.wait_for_timeout(1200)
                        # b) 新 UI：Dauer 滑杆直接设值
                        slider_info = None
                        try:
                            slider_info = await _set_slider_value(duration)
                        except Exception:
                            slider_info = None
                        if slider_info:
                            print(f"  (滑杆设值 {slider_info.get('applied')} Sek.，"
                                  f"范围 {slider_info.get('min')}~{slider_info.get('max')})", flush=True)
                        else:
                            # c) 旧 UI：选项列表逐个文案试
                            picked = False
                            for _opt_wait in range(8):
                                for opt_text in _dur_opts:
                                    loc = page.get_by_text(opt_text, exact=True)
                                    if await _first_visible(loc) is not None:
                                        await loc.last.click(timeout=3000)
                                        picked = True
                                        break
                                if picked:
                                    break
                                await page.wait_for_timeout(800)
                            if not picked:
                                raise RuntimeError("时长选项未渲染且无滑杆")
                        await page.wait_for_timeout(800)
                        # 校验前先关面板：面板开着时可能读到面板内标签而非芯片值。
                        try:
                            await page.keyboard.press("Escape")
                            await page.wait_for_timeout(300)
                        except Exception:
                            pass
                        shown = await _read_chip_text()
                        if not _dur_ok(shown):
                            raise RuntimeError(f"面板仍显示 {shown}")
                        print(f"  (时长已确认: {shown})", flush=True)
                        break
                    except Exception as e:
                        print(f"  (设置时长第 {_dur_try + 1} 次失败: {str(e)[:80]})", flush=True)
                        if _dur_try == 2:
                            print("  (时长设置未通过，发送前必须再次核对当前页面选项)", flush=True)
            # Fast 5/10 秒的旧扩展仍读取该属性；Fast 15/30 秒和 2.5 由原生页面发送，
            # 不安装正文改写器，也不能拿该属性作为选中目标时长的证据。
            try:
                await page.evaluate(
                    "(v) => { const el = document.documentElement;"
                    " if (v == null) { el.removeAttribute('data-dola-duration'); }"
                    " else { el.setAttribute('data-dola-duration', String(v)); } }",
                    duration,
                )
            except Exception as e:
                print(f"  (同步期望时长失败: {str(e)[:80]})", flush=True)
            # 固定指令和生成参数放在前面，用户原文完整保留在最后。
            previous_conversation = re.search(r'/chat/(\d+)', page.url)
            baseline_media, baseline_messages = [], []
            if previous_conversation:
                try:
                    baseline = await _read_page_conversation(page, previous_conversation.group(1))
                    baseline_media = baseline.get('mediaKeys', [])
                    baseline_messages = baseline.get('messageKeys', [])
                except LoginExpiredError:
                    raise
                except Exception:
                    raise BrowserSetupError('无法核对当前会话已有内容，已停止发送，防止认错旧视频') from None
            # Configure model first: changing the skill/model can clear existing attachments.
            await _verify_fast_native(page, model, duration, prepare=True)
            if prepare_only:
                await _verify_video_selection(page, model, duration, ratio)
                return {'prepared': True, 'account': account, 'model': model_key,
                        'duration': duration, 'url': page.url,
                        'screenshot': await page.screenshot(timeout=5000)}
            if reference_image_paths:
                await attach_reference_images(page, reference_image_paths)
            request_evidence = observe_submission(page, model_key, duration, ratio,
                                                  build_video_prompt(prompt, duration, model), on_event)
            send_text = build_video_prompt(prompt, duration, model)
            await page.screenshot(path=f"_before_send_{account}.png")
            try:
                await _fill_video_prompt(page, send_text)
            except BrowserSetupError:
                with suppress(Exception):
                    await page.screenshot(path=f"_not_typed_{account}.png")
                raise
            await _verify_video_selection(page, model, duration, ratio)
            await _submit_prompt_once(page, account, on_prompt_sent)
            print(f"[{account}] 已在输入框发送一次，等待平台回执，不再补发", flush=True)

            # ---- dola 参数确认环节处理（2026-10-07）----
            # 发送后 dola 可能先回确认消息（「Soll ich es so generieren...」）等用户
            # 回复 Ja 才真正生成。这里检测并自动回复 Ja；若确认消息里的 Dauer 与期望
            # 时长不符，回复里带上纠正（如 Ja, aber mit Dauer: 30 Sekunden）。
            async def _reply_confirm(reply_text: str) -> None:
                box = await page.query_selector("textarea") or await page.query_selector('[contenteditable="true"]')
                if box is None:
                    await page.wait_for_timeout(1000)
                    box = await page.query_selector("textarea") or await page.query_selector('[contenteditable="true"]')
                if box is None:
                    return
                try:
                    await box.click(timeout=3000)
                except Exception:
                    pass
                try:
                    await box.fill("")
                    await box.fill(reply_text)
                except Exception:
                    await page.keyboard.type(reply_text, delay=80)
                await page.wait_for_timeout(300)
                await _submit_prompt_once(page, account)

            def _confirm_reply(confirm_text: str | None) -> str:
                want = f"{duration} Sekunden" if duration else ""
                if confirm_text:
                    m = CONFIRM_DURATION_RE.search(confirm_text)
                    if m and m.group(1) == str(duration):
                        return "Ja"  # 确认消息里的时长已正确，直接回复 Ja
                if want:
                    return f"Ja, aber mit Dauer: {want}"
                return "Ja"

            # 2.5/30 preserves the user's single-shot request; never agree to a shorter/split video.
            _confirm_handled = duration == 30 and '2.5' in model_key
            for _cf in range(6):
                if _confirm_handled:
                    break
                await page.wait_for_timeout(2000)
                await _dismiss_login_modal(page)
                if await _page_has_overload(page):
                    break
                confirm = await _find_confirmation(page)
                if confirm is None:
                    continue
                _confirm_handled = True
                reply = _confirm_reply(confirm)
                print(f"[{account}] 检测到参数确认消息，自动回复: {reply}", flush=True)
                await _reply_confirm(reply)
                break  # 同一个确认提示只回复一次，后续只等待。

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
            # 慢线实测：Dola 受理并改 URL 可能超过 30s（打字动画持续），只等 30s 会把
            # 已经发送成功的任务白白丢弃（ISP 额度已花、账号配额已扣）。放宽到 120s。
            conv_id = ""
            for _ci in range(config.CONV_ID_WAIT_SECONDS):
                await page.wait_for_timeout(1000)
                tail = page.url.rstrip("/").split("/")[-1]
                if tail.isdigit():
                    conv_id = tail
                    break
                # 2026-10-07：登录墙可能在发送/确认后才懒弹出（URL 永远不会变数字 id），
                # 周期检测（每 10s），命中即抛 LoginExpiredError → 号池标死号换号，
                # 不再干等 120s 超时后按普通失败换号。
                if _ci and _ci % 10 == 0:
                    await _dismiss_login_modal(page)
                # 确认环节：dola 等回复 Ja 才会继续，URL 可能迟迟不变数字 id，
                # 这里兜底再检测一次确认消息并自动回复，避免卡死等超时。
                if not _confirm_handled:
                    confirm = await _find_confirmation(page)
                    if confirm is not None:
                        _confirm_handled = True
                        reply = _confirm_reply(confirm)
                        print(f"[{account}] 确认消息出现，自动回复: {reply}", flush=True)
                        await _reply_confirm(reply)
                        continue
                # 服务端过载会弹「目前需求非常高，请稍后再试」，此时不会建会话，
                # 没必要干等 30s 超时，直接快速失败并说明原因。
                if await _page_has_overload(page):
                    await page.screenshot(path=f"_overload_{account}.png")
                    raise RuntimeError(
                        "Dola 服务端过载：目前需求非常高，请稍后再试（未创建会话）")
            if not conv_id:
                await page.screenshot(path="no_conv.png")
                raise TimeoutError(f"发送后等待 {config.CONV_ID_WAIT_SECONDS}s 仍未拿到 conversation_id，未重复提交")
            print(f"[{account}] conversation_id={conv_id}，保存后台查询会话...", flush=True)

            deadline = time.time() + timeout
            save_poll_state(account, conv_id, await context.cookies("https://www.dola.com"),
                            await page.evaluate('navigator.userAgent'),
                            baseline_media if previous_conversation and previous_conversation.group(1) == conv_id else [],
                            baseline_messages if previous_conversation and previous_conversation.group(1) == conv_id else [])
            if on_conversation_id:
                on_conversation_id(account, conv_id, deadline)
            if duration == 30 and '2.5' in model_key:
                audit = submission_audit(request_evidence)
                if not request_evidence and on_event:
                    on_event('submitted', audit)
                if not audit.get('matches'):
                    print(f'[{account}] 本地请求核对存在疑问，继续核对原会话回执；未重新提交', flush=True)
            if not background_handoff:
                await _wait_for_generation_receipt(page, account, conv_id, send_text, model=model_key, on_event=on_event,
                    baseline_media=baseline_media if previous_conversation and previous_conversation.group(1) == conv_id else [],
                    baseline_messages=baseline_messages if previous_conversation and previous_conversation.group(1) == conv_id else [])
        finally:
            try:
                cost = proxy_usage.finish_run(run)
                if cost:
                    print(f"[{account}] 本次出片消耗 ISP 请求 {cost} 次", flush=True)
            finally:
                # Every failed attempt may now rotate. Never release a usable
                # ISP slot when closing its old browser failed or timed out.
                await _close_submission_context(context)

    # Only release submission slots after Chromium and its driver have exited.
    if on_browser_closed:
        on_browser_closed()
    print(f"[{account}] 提交浏览器已关闭，后台等待出片；提交名额已释放", flush=True)
    return await resume_video(account, conv_id, timeout, on_poll, on_balance, deadline_at=deadline,
                              on_event=on_event, model=model_key, submitted_prompt=send_text, accepted=not background_handoff,
                              **({'scheduler':scheduler,'background_lane':background_lane} if background_handoff else {}))


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
