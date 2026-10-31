"""视频生成 worker：复用 accounts/<账号> profile 登录态，页面内 fetch 提交视频生成，
轮询 /im/chain/single 出片，拿到 download_url 后下载到 downloads/。

协议字段全部来自 dola_client.py 的逆向成果（_build_video_body / _poll_video），
传输层换成「真实浏览器页面内 fetch」以规避滑块风控。

用法：python video_worker.py <账号名> "提示词" [比例] [时长秒]
示例：python video_worker.py acc1 "一只猫在草地上追蝴蝶" 9:16 5

验收标准：一次完整出片，拿到 dola CDN download_url + 本地视频文件。
"""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import aiohttp
from patchright.async_api import async_playwright

import config
import proxy_store
from browser import cookie_value, launch_account_context
from dola_client import CREDIT_FAIL_PATTERN, CreditError
from video_probe import SUBMIT_JS

# 轮询 /im/chain/single（字段对齐 dola_client._request_im_chain / _poll_video）
# 注意：Content-Type 要带 encoding=utf-8，agw-js-conv 用 str（不是 "str, str"）
POLL_JS = r"""
async ({conversationId, msToken, fp}) => {
  // 2026-08 现行协议：body 必须包 uplink_body.pull_singe_chain_uplink_body，
  // anchor_index=MAX_SAFE_INTEGER + direction=1 拉最新；channel=2, version="1"。
  // （dola_client.py 里的扁平 body 是旧协议，返回 712010202）
  const params = new URLSearchParams({
    version_code: "20800", language: "ja", device_platform: "web",
    doubao_device_platform: "web", aid: "495671", real_aid: "495671",
    pkg_type: "release_version", pc_version: "3.32.61", doubao_pc_version: "3.32.61",
    region: "JP", sys_region: "JP", samantha_web: "1", web_platform: "browser",
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
  if (!resp.ok) return {ok: false, status: resp.status, texts: [], videos: []};

  const data = await resp.json();
  const messages =
    (((data.downlink_body || {}).pull_singe_chain_downlink_body) || {}).messages || [];
  const texts = [];
  const assistantTexts = [];
  const assistantMessages = [];
  const userIndices = [];
  let userAttachments = 0;
  const videos = [];
  const videoModels = [];
  const addText = (target, text) => {
    if (typeof text !== 'string' || !text.trim()) return;
    const value = text.slice(0, 2000);
    if (!target.includes(value)) target.push(value);
  };
  for (const msg of messages) {
    if (!msg || typeof msg !== 'object') continue;
    // Chain messages use user_type (1=user, 2=assistant), unlike SSE roles.
    const userType = Number(msg.user_type);
    if (userType === 1 && /^\d+$/.test(String(msg.index_in_conv))) userIndices.push(BigInt(msg.index_in_conv));
    const isAssistant = userType === 2 || (userType !== 1 &&
      (msg.role === 'assistant' || (msg.sender || {}).role === 'assistant'));
    const messageTexts = [];
    let legacy = msg.content;
    if (typeof legacy === 'string') {
      try { legacy = JSON.parse(legacy); } catch (e) { legacy = []; }
    }
    // Modern responses may leave content empty and only supply content_block.
    const content = [
      ...(Array.isArray(msg.content_block) ? msg.content_block : []),
      ...(Array.isArray(legacy) ? legacy : []),
    ];
    let hasText = false;
    for (const block of content) {
      if (!block || typeof block !== 'object') continue;
      // 10052 = 我们上传的图片附件（UI 先单独发一条附件消息，再发文字）
      if (userType === 1 && Number(block.block_type) === 10052) userAttachments += 1;
      const text = (((block.content || {}).text_block) || {}).text || "";
      if (typeof text === 'string' && text.trim()) {
        hasText = true;
        addText(texts, text);
        if (isAssistant) { addText(assistantTexts, text); addText(messageTexts, text); }
      }
      if (block.block_type !== 2074) continue;
      const creations = (((block.content || {}).creation_block) || {}).creations || [];
      for (const cre of creations) {
        if (cre.type !== 2) continue;
        const url = ((cre.video || {}).download_url) || "";
        if (url.startsWith("http") && !videos.includes(url)) {
          videos.push(url);
          videoModels.push((cre.video || {}).video_model || "");
        }
      }
    }
    if (isAssistant && !hasText) {
      const brief = msg.brief || (msg.ext || {}).brief;
      addText(texts, brief);
      addText(assistantTexts, brief);
      addText(messageTexts, brief);
    }
    if (isAssistant) {
      // Structured creation outcome (2026-10-29 chain dumps): a finished card has
      // ai_creation_tool_list[].status 4 (video) or 5 (failed, fail_code set).
      const ext = (msg.ext && typeof msg.ext === 'object') ? msg.ext : {};
      let tools = ext.ai_creation_tool_list;
      if (typeof tools === 'string') { try { tools = JSON.parse(tools); } catch (e) { tools = []; } }
      assistantMessages.push({
        id: String(msg.message_id || ''),
        index: /^\d+$/.test(String(msg.index_in_conv)) ? String(msg.index_in_conv) : null,
        text: messageTexts.join('\n'),
        resCode: String(ext.ai_creation_res_code ?? ''),
        finished: String(ext.is_finish ?? '') === '1',
        // 2026-10-31 实测链：受理消息 has_video_gen=1；追问 is_creation_clarifying=1（与措辞、语言无关）
        videoGen: String(ext.has_video_gen ?? '') === '1',
        clarifying: String(ext.is_creation_clarifying ?? '') === '1',
        tools: (Array.isArray(tools) ? tools : []).filter(t => t && typeof t === 'object')
          .map(t => ({status: Number(t.status), failCode: String(t.fail_code ?? '')})),
      });
    }
  }
  // Only an explicitly ordered latest assistant turn may drive automated actions.
  const ordered = assistantMessages.filter(m => m.index !== null);
  ordered.sort((a,b) => BigInt(a.index) < BigInt(b.index) ? -1 : BigInt(a.index) > BigInt(b.index) ? 1 : 0);
  let latestAssistant = ordered.length === assistantMessages.length && ordered.length
    ? ordered[ordered.length - 1] : null;
  // A newer user turn means the earlier assistant question/refusal is stale.
  if (latestAssistant && userIndices.some(index => index >= BigInt(latestAssistant.index))) latestAssistant = null;
  // assistantCount: a daily-limit reply is final only when it is the conversation's only
  // assistant turn (10-28: an earlier turn had already accepted a generation).
  return {ok: true, status: resp.status, texts, assistantTexts, latestAssistant, videos, videoModels,
          assistantCount: assistantMessages.length,
          // anyTool: 会话里任何一条回复挂过生成卡片（受理过）。没有卡片、也没报预计时间、
          // 最新回复已说完却迟迟不动 = dola 在等我们答话（不认识的追问），见 poll_conversation。
          anyTool: assistantMessages.some(m => m.tools.length > 0),
          anyVideoGen: assistantMessages.some(m => m.videoGen),
          userAttachments};
}
"""


def extract_unwatermarked_url(video_model_str: str, fallback_url: str) -> str:
    """从 video_model.video_list.*.main_url 提取无水印视频地址（base64）。

    无水印版本与普通 download_url 是同一内容的不同签名链接，
    查询参数 lr=unwatermarked（普通链接为 lr=cici_ai）。
    失败时回退 download_url。
    """
    try:
        vm = json.loads(video_model_str or "{}")
        video_list = vm.get("video_list") or {}
        candidates = []
        for v in video_list.values():
            if not isinstance(v, dict):
                continue
            main_url = v.get("main_url") or ""
            if not main_url:
                continue
            try:
                decoded = base64.b64decode(main_url).decode("utf-8", "ignore")
            except Exception:
                continue
            if decoded.startswith("http"):
                candidates.append((int(v.get("bitrate") or v.get("real_bitrate") or 0), decoded))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
    except Exception:
        pass
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


async def _download(url: str, account: str) -> Path:
    """下载视频到 DOWNLOAD_DIR（走代理，CDN 可能也限地区）。返回本地路径。"""
    dl_dir = Path(config.DOWNLOAD_DIR)
    dl_dir.mkdir(parents=True, exist_ok=True)
    fname = dl_dir / f"{account}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    timeout = aiohttp.ClientTimeout(total=300)
    from account_transport import http_proxy
    from generation_errors import UpstreamReplyError
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, proxy=http_proxy(account)) as resp:
                resp.raise_for_status()
                with open(fname, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        f.write(chunk)
    except asyncio.CancelledError:
        fname.unlink(missing_ok=True)
        raise
    except Exception as exc:
        fname.unlink(missing_ok=True)
        raise UpstreamReplyError("网络失败",
            f"视频已生成但下载失败（{type(exc).__name__}）；已保留原会话，未换号重新生成") from None
    return fname


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
                await asyncio.sleep(5)
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
                    url = videos[0]
                    print(f"[{account}] 出片成功 download_url={url[:100]}...", flush=True)
                    local = await _download(url, account)
                    print(f"[{account}] 已下载 {local}（{local.stat().st_size / 1e6:.1f} MB）", flush=True)
                    return {
                        "video_url": url,
                        "local_path": str(local),
                        "conversation_id": conv_id,
                        "account": account,
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
