"""视频生成 worker：复用 accounts/<账号> profile 登录态，页面内 fetch 提交视频生成，
轮询 /im/chain/single 出片，拿到 download_url 后下载到 downloads/。

协议字段全部来自 dola_client.py 的逆向成果（_build_video_body / _poll_video），
传输层换成「真实浏览器页面内 fetch」以规避滑块风控。

用法：python video_worker.py <账号名> "提示词" [比例] [时长秒]
示例：python video_worker.py acc1 "一只猫在草地上追蝴蝶" 9:16 5

验收标准：一次完整出片，拿到 dola CDN download_url + 本地视频文件。
"""
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from http.cookies import SimpleCookie

import aiohttp
from yarl import URL
from patchright.async_api import async_playwright

import config
import proxy_usage
from browser import cookie_value, launch_account_context
from dola_client import CREDIT_FAIL_PATTERN, CreditError
from video_probe import SUBMIT_JS
from video_metadata import read_video_duration, InvalidVideoError
from video_delivery import dola_download, NoWatermarkAvailable


class VideoDownloadError(RuntimeError):
    """The existing platform result could not be saved. Never regenerate it."""
    task_phase = 'download_failed'

# 轮询 /im/chain/single（字段对齐 dola_client._request_im_chain / _poll_video）
# 注意：Content-Type 要带 encoding=utf-8，agw-js-conv 用 str（不是 "str, str"）
POLL_JS = r"""
async ({conversationId, msToken, fp}) => {
  // 2026-08 现行协议：body 必须包 uplink_body.pull_singe_chain_uplink_body，
  // anchor_index=MAX_SAFE_INTEGER + direction=1 拉最新；channel=2, version="1"。
  // （dola_client.py 里的扁平 body 是旧协议，返回 712010202）
  const params = new URLSearchParams({
    version_code: "20800", language: "de", device_platform: "web",
    doubao_device_platform: "web", aid: "495671", real_aid: "495671",
    pkg_type: "release_version", pc_version: "3.32.61", doubao_pc_version: "3.32.61",
    region: "DE", sys_region: "DE", samantha_web: "1", web_platform: "browser",
    "use-olympus-account": "1", web_tab_id: crypto.randomUUID(),
  });

  const resp = await fetch("/im/chain/single?" + params.toString(), {
    method: "POST",
    headers: {
      "Content-Type": "application/json; encoding=utf-8",
      "agw-js-conv": "str",
      "Accept": "*/*",
    },
    body: JSON.stringify({
      cmd: 3100,
      uplink_body: {
        pull_singe_chain_uplink_body: {
          conversation_id: conversationId,
          anchor_index: Number.MAX_SAFE_INTEGER,
          conversation_type: 3,
          direction: 1,
          limit: 20,
          ext: {},
          filter: {index_list: []},
          evaluate_ab_params: "",
          evaluate_common_params: "",
        },
      },
      sequence_id: crypto.randomUUID(),
      channel: 2,
      version: "1",
    }),
    credentials: "include",
  });
  if (resp.status === 401 || resp.headers.get('x-tt-agw-login') === '0')
    return {ok: false, loginExpired: true, texts: [], videos: []};
  if (!resp.ok) return {ok: false, status: resp.status, texts: [], videos: []};

  const data = await resp.json();
  const messages =
    (((data.downlink_body || {}).pull_singe_chain_downlink_body) || {}).messages || [];
  const texts = [];
  const videos = [];
  const videoModels = [];
  for (const msg of messages) {
    let content = msg.content;
    if (typeof content === "string") {
      try { content = JSON.parse(content); } catch (e) { continue; }
    }
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      const text = (((block.content || {}).text_block) || {}).text || "";
      if (text) texts.push(text);
      if (block.block_type !== 2074) continue;
      const creations = (((block.content || {}).creation_block) || {}).creations || [];
      for (const cre of creations) {
        if (cre.type !== 2) continue;
        const url = ((cre.video || {}).download_url) || "";
        if (url.startsWith("http")) {
          videos.push(url);
          videoModels.push((cre.video || {}).video_model || "");
        }
      }
    }
  }
  return {ok: true, status: resp.status, texts, videos, videoModels, raw: data};
}
"""

POLL_JS = (POLL_JS
    .replace('language: "de"', f'language: "{config.BROWSER_LANGUAGE}"')
    .replace('region: "DE", sys_region: "DE"',
             f'region: "{config.BROWSER_REGION}", sys_region: "{config.BROWSER_REGION}"'))

def extract_unwatermarked_url(video_model_str: str, fallback_url: str) -> str:
    """Compatibility URL selector; only dola_download carries source evidence.

    Poll parsers may use this to discover a result. A discovered URL alone
    never qualifies it for publication as a no-watermark delivery.
    """
    try:
        return dola_download(video_model_str, fallback_url)['url']
    except NoWatermarkAvailable:
        return fallback_url


class RiskControlError(Exception):
    """触发滑块/风控（710022004 slide / 710022002 限流），该账号+IP 需冷却或换号"""


def _check_submit(result: dict) -> str:
    """检查 SUBMIT_JS 结果，返回 conversation_id；风控/异常直接抛。"""
    status = result.get("status")
    if status != 200:
        raise RiskControlError(f"提交失败 HTTP {status}: {json.dumps(result.get('events', []), ensure_ascii=False)[:300]}")

    for err in result.get("errors", []):
        if "710022004" in err or "slide" in err or "shark" in err:
            raise RiskControlError(f"触发滑块风控: {err[:300]}")
        if "710022002" in err:
            raise RiskControlError(f"触发限流: {err[:300]}")
        raise Exception(f"提交返回错误事件: {err[:300]}")

    conv_id = result.get("convId") or ""
    if not conv_id:
        raise Exception(
            "视频受理未返回 conversation_id: "
            + json.dumps(result.get("events", []), ensure_ascii=False)[:300]
        )
    return conv_id


_CURRENT_PROXY = object()


def download_cookie_jar(cookies):
    jar = aiohttp.CookieJar()
    for item in cookies or []:
        domain = str(item.get('domain') or 'www.dola.com').lstrip('.').lower()
        if domain != 'dola.com' and not domain.endswith('.dola.com'):
            continue
        if not item.get('name') or not item.get('value'):
            continue
        cookie = SimpleCookie()
        cookie[item['name']] = item['value']
        cookie[item['name']]['domain'] = item.get('domain') or 'www.dola.com'
        cookie[item['name']]['path'] = item.get('path') or '/'
        cookie[item['name']]['secure'] = bool(item.get('secure'))
        jar.update_cookies(cookie, response_url=URL('https://www.dola.com'))
    return jar


async def _download(url: str, account: str, *, proxy_url=_CURRENT_PROXY, own_isp=None,
                    headers=None, cookies=None) -> Path:
    """Download an existing result on its original ISP; validate before atomic publication."""
    dl_dir = Path(config.DOWNLOAD_DIR)
    dl_dir.mkdir(parents=True, exist_ok=True)
    fname = dl_dir / f"{account}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.mp4"
    temporary = fname.with_suffix('.mp4.part')
    if proxy_url is _CURRENT_PROXY:
        proxy_url = config.proxy_url_for(account)
        if config.ISP_ONLY:
            own_isp = True
    config.check_saved_isp(proxy_url, own_isp)
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers, cookie_jar=download_cookie_jar(cookies), trust_env=False) as session:
        for attempt in range(3):
            if proxy_url:
                if own_isp is None:
                    proxy_usage.reserve_for_account(account, 1)
                elif own_isp:
                    proxy_usage.usage.reserve(config._parse_proxy(proxy_url), 1)
            try:
                async with session.get(url, proxy=proxy_url) as resp:
                    if resp.status != 200:
                        if resp.status >= 500 and attempt < 2:
                            await asyncio.sleep(attempt + 1)
                            continue
                        raise VideoDownloadError(f'视频下载 HTTP {resp.status}；可从原会话重试取回，未重新生成')
                    length = resp.content_length
                    with temporary.open('wb') as output:
                        async for chunk in resp.content.iter_chunked(1 << 16):
                            output.write(chunk)
                    if length is not None and temporary.stat().st_size != length:
                        raise InvalidVideoError('视频下载长度不完整')
                    read_video_duration(temporary)
                    temporary.replace(fname)
                    return fname
            except (aiohttp.ClientError, TimeoutError, InvalidVideoError) as error:
                if attempt >= 2:
                    raise VideoDownloadError(f'视频下载未通过完整性检查（{type(error).__name__}）；可从原会话重试取回，未重新生成') from None
                await asyncio.sleep(attempt + 1)
            finally:
                temporary.unlink(missing_ok=True)


async def generate_video(account: str, prompt: str, ratio: str = "9:16",
                         duration: int = 5, timeout: int = None) -> dict:
    """完整出片流程：提交 → 轮询 → 下载。

    返回 {"video_url": dola CDN 临时链接, "local_path": 本地文件, "conversation_id": ...}
    异常：RiskControlError（风控/限流）、CreditError（额度不足，换号）、
         TimeoutError（超时未出片）、FileNotFoundError（profile 不存在）
    """
    timeout = timeout or config.VIDEO_TIMEOUT
    async with async_playwright() as p:
        context = await launch_account_context(p, account)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            cookies = await context.cookies("https://www.dola.com")
            sessionid = cookie_value(cookies, "sessionid")
            if not sessionid:
                raise CreditError(f"{account} 登录态失效（无 sessionid），请重跑 login.py {account}")
            ms_token = cookie_value(cookies, "msToken")
            fp = cookie_value(cookies, "s_v_web_id")

            from dola_watermark import ensure_page_preference
            await ensure_page_preference(page, account)

            print(f"[{account}] 提交视频生成: {prompt[:40]} | {ratio} | {duration}s", flush=True)
            # evaluate 无 timeout 参数，用 wait_for 兜底（SSE 流读完即返回）
            result = await asyncio.wait_for(page.evaluate(
                SUBMIT_JS,
                {"prompt": prompt, "ratio": ratio, "duration": duration,
                 "msToken": ms_token, "fp": fp},
            ), timeout=180)
            conv_id = _check_submit(result)
            print(f"[{account}] 已受理 conversation_id={conv_id}，轮询出片中...", flush=True)

            start = time.time()
            while time.time() - start < timeout:
                # 省额度：自适应轮询（前 30s 每 5s，之后 10s，120s 后 15s）。
                elapsed = time.time() - start
                interval = 5 if elapsed < 30 else (10 if elapsed < 120 else 15)
                await asyncio.sleep(interval)
                try:
                    poll = await asyncio.wait_for(page.evaluate(
                        POLL_JS,
                        {"conversationId": conv_id, "msToken": ms_token, "fp": fp},
                    ), timeout=30)
                except Exception as e:
                    print(f"[{account}] 轮询异常（继续重试）: {e}", flush=True)
                    continue
                if not poll.get("ok"):
                    continue

                for text in poll.get("texts", []):
                    if CREDIT_FAIL_PATTERN.search(text):
                        raise CreditError(f"额度不足: {text[:80]}")

                videos = poll.get("videos", [])
                if videos:
                    models = poll.get('videoModels') or []
                    source = dola_download(models[0] if models else '', videos[0])
                    url = source['url']
                    print(f"[{account}] 出片成功 download_url={url[:100]}...", flush=True)
                    local = await _download(url, account)
                    print(f"[{account}] 已下载 {local}（{local.stat().st_size / 1e6:.1f} MB）", flush=True)
                    return {
                        "video_url": url,
                        "local_path": str(local),
                        "conversation_id": conv_id,
                        "account": account,
                        "download_source": source['download_source'],
                        "watermark": source['watermark'],
                    }
                print(f"[{account}] ...生成中（已等 {int(time.time() - start)}s）", flush=True)

            raise TimeoutError(f"{timeout}s 内未出片（conversation_id={conv_id}）")
        finally:
            await context.close()


async def _main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    prompt = sys.argv[2] if len(sys.argv) > 2 else "一只猫在草地上追蝴蝶"
    ratio = sys.argv[3] if len(sys.argv) > 3 else "9:16"
    duration = int(sys.argv[4]) if len(sys.argv) > 4 else 5

    try:
        result = await generate_video(account, prompt, ratio, duration)
    except (RiskControlError, CreditError, TimeoutError) as e:
        print(f"\n失败: {e}")
        sys.exit(1)
    print("\n=== 结果 ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
