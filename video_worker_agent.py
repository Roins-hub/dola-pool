"""Seedance 2.0 15 秒：走 Dola 工作模式（Agent-GeneralTask + Creative video 技能）。

普通聊天里 2.0 只出 5/10 秒，工作模式能一次出 15 秒。2026-11-07 实测：
- 同一个号在 patchright 浏览器里被 710022002 拒绝，放进 CloakBrowser 就能过，所以这里
  把账号登录态复制到一个临时 CloakBrowser profile，用完即删，不动账号自己的 profile。
- 被拒时页面会自己调 /passport/web/logout/，放行就真掉线，所以全程拦截；看到它被拦下
  就说明这次被拒绝了。
- 助手一般先要确认：有时是普通回复，有时是输入框上的选择题（侧栏显示「一時停止中」，
  chain 里读不到）；选择题不回也会继续，但要等二十多分钟，所以主动回。要确认就回「确认」，
  问别的就和普通聊天一样走 dola_responder：通用回复两次，再交给 AI 代答（如已开启）。
- 助手可能改写提示词，内容审核拦下时还会换更短的词重试，成片和原词未必完全一致。
"""
import asyncio
import base64
import json
import mimetypes
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

from patchright.async_api import async_playwright

import config
import dola_responder
import failure_text
import proxy_store
from browser import cookie_value, launch_account_context
from browser_feedback import emit as emit_feedback
from generation_errors import (
    PreSubmissionParametersError, PreSubmissionUploadError, UpstreamReplyError,
)
from video_worker import RiskControlError, _download
from video_worker_ui import (
    LoginExpiredError, dismiss_popups, fetch_higher_bitrate_url, is_logged_out,
    raise_if_region_restricted,
)
from work_reply import (
    WORK_DURATION, asks_to_confirm, build_prompt, chain_messages, reply_finished, reply_text,
    session_cookies, tools_ran, uploads_committed, video_models,
)

SKILL_RE = re.compile(r"^(Creative video|创意视频|クリエイティブ動画|クリエイティブビデオ)$")
WORK_TAB_RE = re.compile(r"^(ワーク|工作|Work)$")
PAUSED_RE = re.compile(r"一時停止中|已暂停|Paused")
UPLOAD_ITEM_RE = re.compile(r"ファイルまたは画像|上传文件|Upload file", re.I)
COMPOSER = 'textarea, [contenteditable="true"]'
CONFIRM_OPTION_RE = re.compile(r"确认|確認|開始|开始")
POLL_SECONDS = 10
# 回复写完后等这么多轮还没动静，才算助手在等回答
QUIET_POLLS = 2
# 回答之后至少等这么多轮，再判断它有没有理会
ANSWER_GAP_POLLS = 3
# 工具跑完、回复写完后这么多轮还没有视频 → 这条会话不会再出片
SETTLED_POLLS = 6
# 排队等名额时隔这么久报一次进度：看门狗 10 分钟没进度就把任务判成卡死
SLOT_NOTE_SECONDS = 30
THROTTLE_NOTE = "710022002 工作模式请求被 dola 拒绝，页面随后要退出登录（已拦下）"

QUESTION_JS = r"""(selector) => {
  let node = document.querySelector(selector);
  for (let i = 0; node && i < 6; i++) node = node.parentElement;
  return node ? node.innerText.slice(0, 1500) : '';
}"""

PASTE_JS = r"""async ({selector, files}) => {
  const data = new DataTransfer();
  for (const f of files) {
    const bytes = Uint8Array.from(atob(f.data), c => c.charCodeAt(0));
    data.items.add(new File([bytes], f.name, {type: f.type}));
  }
  const target = document.querySelector(selector);
  if (!target) return false;
  target.focus();
  target.dispatchEvent(new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: data}));
  return true;
}"""

CHAIN_JS = r"""async ({query, conversationId}) => {
  const r = await fetch('/im/chain/single?' + query, {method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json; encoding=utf-8', 'agw-js-conv': 'str', 'Accept': '*/*'},
    body: JSON.stringify({cmd: 3100, uplink_body: {pull_singe_chain_uplink_body: {
      conversation_id: conversationId, anchor_index: Number.MAX_SAFE_INTEGER, conversation_type: 3,
      direction: 1, limit: 20, ext: {}, filter: {index_list: []}}},
      sequence_id: crypto.randomUUID(), channel: 2, version: '1'})});
  return {status: r.status, text: await r.text()};
}"""

# 同一时间只开这么多 CloakBrowser：有头运行，一单要占约 6 分钟
_slots: asyncio.Semaphore | None = None


def _work_slots() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(max(1, config.WORK_15S_CONCURRENCY))
    return _slots


@asynccontextmanager
async def _work_slot(on_poll):
    slots = _work_slots()
    while True:
        try:
            await asyncio.wait_for(slots.acquire(), SLOT_NOTE_SECONDS)
            break
        except TimeoutError:
            emit_feedback(on_poll, progress="工作模式：等前一单跑完（同一时间只跑一单）")
    try:
        yield
    finally:
        slots.release()


class _WorkSession:
    """一次工作模式出片：打开页面、发送、确认、等视频。"""

    def __init__(self, account, page, on_poll, *, ratio=None, attachments=0):
        self.account, self.page, self.on_poll = account, page, on_poll
        self.ratio, self.attachments = ratio, attachments
        self.confirmed = False
        self.nudges = 0
        self.ai_tried = False
        self.tried: list[str] = []
        self.logouts: list[str] = []
        self.sent: list[str] = []
        self.page_query = ""
        self.upload_actions: list[str] = []

    def watch(self):
        def on_request(request):
            parts = urlsplit(request.url)
            if parts.netloc == "www.dola.com" and "device_id=" in request.url and "aid=" in request.url:
                self.page_query = request.url
            if parts.path.endswith("/chat/completion"):
                self.sent.append(request.url)
            action = parse_qs(parts.query).get("Action", [""])[0]
            if action:
                self.upload_actions.append(action)
        self.page.on("request", on_request)

    def rejected(self) -> bool:
        return bool(self.logouts)

    def raise_if_rejected(self):
        if self.rejected():
            raise RiskControlError(THROTTLE_NOTE)

    async def open_chat(self):
        page = self.page
        await page.goto("https://www.dola.com/chat", timeout=120000, wait_until="domcontentloaded")
        for _ in range(45):
            raise_if_region_restricted(page, self.account)
            if await page.locator(COMPOSER).count():
                break
            await page.wait_for_timeout(1000)
        else:
            raise PreSubmissionParametersError(f"{self.account} 工作模式输入框没有加载出来")
        # 账号语言和页面语言不一致时页面会重载一次，等它稳定再操作
        await page.wait_for_timeout(15000)
        raise_if_region_restricted(page, self.account)
        if await is_logged_out(page):
            raise LoginExpiredError(f"{self.account} 登录态失效，工作模式未发送")
        await dismiss_popups(page)

    async def enter_work_mode(self):
        tab = self.page.get_by_text(WORK_TAB_RE)
        if not await tab.count():
            raise PreSubmissionParametersError(f"{self.account} 页面上没有工作模式入口")
        await tab.first.click(timeout=6000)
        await self.page.wait_for_timeout(8000)

    async def _pick_files(self, paths: list[str]) -> bool:
        """上传按钮 → 菜单 → 文件选择框。菜单有时没展开，等一等再重点。"""
        page = self.page
        button = page.locator('[data-testid="upload_file_button"]')
        item = page.locator("[role=menuitem],[role=option],[class*=menu] li,[class*=dropdown] div").filter(
            has_text=UPLOAD_ITEM_RE)
        for _ in range(3):
            try:
                await button.first.click(timeout=4000)
                for _ in range(6):
                    await page.wait_for_timeout(1000)
                    if await item.count():
                        async with page.expect_file_chooser(timeout=6000) as chooser:
                            await item.last.click(timeout=4000)
                        await (await chooser.value).set_files(paths)
                        return True
            except Exception as exc:  # noqa: BLE001
                print(f"[{self.account}] 工作模式上传菜单失败：{type(exc).__name__}", flush=True)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1500)
        return False

    async def _paste_files(self, paths: list[str]) -> None:
        files = []
        for path in paths:
            data = await asyncio.to_thread(Path(path).read_bytes)
            files.append({"name": Path(path).name, "data": base64.b64encode(data).decode(),
                          "type": mimetypes.guess_type(path)[0] or "image/png"})
        if not await self.page.evaluate(PASTE_JS, {"selector": COMPOSER, "files": files}):
            raise PreSubmissionUploadError(f"{self.account} 工作模式没有可以粘贴图片的输入框")

    async def attach_references(self, paths: list[str]):
        page = self.page
        if not await self._pick_files(paths):
            await self._paste_files(paths)
        for _ in range(60):
            if uploads_committed(self.upload_actions) >= len(paths):
                break
            await page.wait_for_timeout(1000)
        else:
            raise PreSubmissionUploadError(f"{self.account} 工作模式参考图 60 秒内没有传完")
        await page.wait_for_timeout(2500)

    async def choose_skill(self):
        page = self.page
        skill = page.get_by_text(SKILL_RE)
        for _ in range(3):
            await page.locator(COMPOSER).first.click(timeout=8000)
            await page.keyboard.type("/", delay=120)
            for _ in range(10):
                await page.wait_for_timeout(1000)
                if await skill.count():
                    await skill.first.click(timeout=5000)
                    await page.wait_for_timeout(1500)
                    return
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(2500)
        raise PreSubmissionParametersError(f"{self.account} 技能列表里没有 Creative video")

    async def send(self, text: str) -> str:
        page = self.page
        await page.keyboard.insert_text(text)
        await page.wait_for_timeout(1500)
        await page.keyboard.press("Enter")
        for _ in range(15):
            await page.wait_for_timeout(1000)
            if self.sent:
                break
        else:
            raise PreSubmissionParametersError(f"{self.account} 工作模式请求没有发出去")
        for _ in range(60):
            self.raise_if_rejected()
            match = re.search(r"/chat/(\d+)", page.url)
            if match:
                return match.group(1)
            await page.wait_for_timeout(1000)
        self.raise_if_rejected()
        raise TimeoutError(f"{self.account} 工作模式 60 秒没有拿到会话，可能已在生成，不换号重提")

    async def next_answer(self, question: str) -> tuple[str | None, str]:
        """这次该怎么回：要确认就确认；问别的走通用回复，用完再交给 AI 代答。"""
        if not self.confirmed and asks_to_confirm(question):
            self.confirmed = True
            return "确认", "确认"
        if self.nudges < dola_responder.GENERIC_LIMIT:
            reply = dola_responder.generic_reply(self.ratio, WORK_DURATION, self.attachments,
                                                 self.nudges, question)
            self.nudges += 1
            return reply, "通用回复"
        if not self.ai_tried and dola_responder.enabled():
            self.ai_tried = True
            emit_feedback(self.on_poll, progress="工作模式：助手仍在追问，正在请 AI 代答")
            return await dola_responder.ai_reply(question, self.ratio, WORK_DURATION,
                                                 self.attachments), "AI 代答"
        return None, ""

    async def answer(self, reply: str):
        page = self.page
        await page.locator(COMPOSER).first.click(timeout=8000)
        await page.keyboard.insert_text(reply)
        await page.wait_for_timeout(1000)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(6000)
        if await self.paused():
            # 选择题没收下打字回复：点带「确认」的选项
            option = page.get_by_text(CONFIRM_OPTION_RE)
            if await option.count():
                await option.last.click(timeout=4000)

    async def question(self) -> str:
        return await self.page.evaluate(QUESTION_JS, COMPOSER)

    async def paused(self) -> bool:
        return bool(await self.page.get_by_text(PAUSED_RE).count())

    async def read_chain(self, query: str, conversation_id: str):
        result = await self.page.evaluate(CHAIN_JS, {"query": query, "conversationId": conversation_id})
        if result["status"] != 200:
            return None
        try:
            return json.loads(result["text"])
        except ValueError:
            return None

    async def wait_for_video(self, conversation_id: str, deadline: float) -> str:
        cookies = await self.page.context.cookies("https://www.dola.com")
        query = dict(parse_qsl(urlsplit(self.page_query or self.sent[0]).query, keep_blank_values=True))
        query.pop("a_bogus", None)
        query["msToken"] = cookie_value(cookies, "msToken") or query.get("msToken", "")
        query_text = urlencode(query)
        stable, last, since_answer = 0, "", ANSWER_GAP_POLLS
        answered = set()
        while time.time() < deadline:
            await self.page.wait_for_timeout(POLL_SECONDS * 1000)
            self.raise_if_rejected()
            if await is_logged_out(self.page):
                raise LoginExpiredError(f"{self.account} 工作模式生成中登录态失效")
            chain = await self.read_chain(query_text, conversation_id)
            emit_feedback(self.on_poll, progress="工作模式：助手处理中")
            if chain is None:
                continue
            found = video_models(chain)
            if found:
                return found[0]
            bots = [m for m in chain_messages(chain) if str(m.get("user_type")) == "2"]
            signature = json.dumps([(m.get("index_in_conv"), len(reply_text(m))) for m in bots])
            stable = stable + 1 if signature == last else 0
            last = signature
            since_answer += 1
            latest = bots[-1] if bots else None
            finished = bool(latest) and reply_finished(latest)
            if finished and any(tools_ran(m) for m in bots):
                if stable >= SETTLED_POLLS:
                    reply = reply_text(latest)[-300:]
                    raise UpstreamReplyError(failure_text.label_for(reply) or "生成失败", reply,
                                             generation_started=True)
                continue
            question = ""
            if await self.paused():
                question = await self.question()
            elif finished and stable >= QUIET_POLLS and (
                    latest.get("index_in_conv") not in answered or stable >= SETTLED_POLLS):
                question = reply_text(latest)
                answered.add(latest.get("index_in_conv"))
            if question and since_answer >= ANSWER_GAP_POLLS:
                reply, how = await self.next_answer(question)
                if not reply:
                    raise UpstreamReplyError(
                        "上游在等待回复",
                        f"工作模式助手在追问，已尝试 {len(self.tried)} 次回复仍未开始生成：{question[-800:]}",
                        generation_started=False)
                self.tried.append(how)
                emit_feedback(self.on_poll, progress=f"工作模式：已{how}：{reply[:60]}")
                await self.answer(reply)
                stable, since_answer = 0, 0
        raise TimeoutError(f"{self.account} 工作模式等待超时，可能仍在生成，不换号重提")


async def _deliver(account: str, vm_str: str, conversation_id: str, on_poll) -> dict:
    url = await fetch_higher_bitrate_url(vm_str, account)
    if not url:
        raise UpstreamReplyError("网络失败", "工作模式已出片，但没有拿到无水印地址；已保留原会话",
                                 generation_started=True)
    import video_link
    if video_link.enabled():
        probe = await asyncio.to_thread(video_link.probe_direct, url)
        if probe:
            emit_feedback(on_poll, progress="视频已生成，直链交付（不经服务器下载）")
            return {"video_url": url, "local_path": "", "delivery": "link", "video_model": vm_str,
                    "video_size": probe["size"], "link_expires_at": probe["expires_at"],
                    "conversation_id": conversation_id, "account": account}
    emit_feedback(on_poll, progress="视频已生成，正在下载到服务器")
    local = await _download(url, account)
    return {"video_url": url, "local_path": str(local),
            "conversation_id": conversation_id, "account": account}


async def _copied_session(p, account: str) -> list[dict]:
    source = await launch_account_context(p, account, headless=True)
    try:
        cookies = session_cookies(await source.cookies())
    finally:
        await source.close()
    if not any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
        raise LoginExpiredError(f"{account} 没有可用的登录态，工作模式未发送")
    return cookies


async def _launch_cloak(account: str, profile: str):
    try:
        from cloakbrowser import launch_persistent_context_async
    except ImportError as exc:
        raise PreSubmissionParametersError("工作模式需要 CloakBrowser，当前环境没有安装") from exc
    proxy = proxy_store.proxy_url_for(account)
    if not proxy:
        raise PreSubmissionParametersError(f"{account} 没有绑定代理，工作模式不走直连")
    return await launch_persistent_context_async(
        profile, headless=config.CLOAK_HEADLESS, proxy=proxy, locale="ja-JP", timezone="Asia/Tokyo",
        geoip=config.CLOAK_GEOIP, humanize=config.CLOAK_HUMANIZE,
        license_key=config.CLOAK_LICENSE_KEY or None)


async def generate_work_video(account: str, prompt: str, ratio: str = None, *,
                              on_conversation_id=None, on_poll=None,
                              reference_image_paths: list[str] | None = None) -> dict:
    """工作模式出一条 15 秒。发送前的失败可以换号；发送后超时不换号，避免重复出片。"""
    text = build_prompt(prompt, ratio)
    async with _work_slot(on_poll):
        profile = tempfile.mkdtemp(prefix="dola-work-")
        context = None
        try:
            emit_feedback(on_poll, progress="工作模式：准备浏览器")
            async with async_playwright() as p:
                cookies = await _copied_session(p, account)
            context = await _launch_cloak(account, profile)
            await context.add_cookies(cookies)
            page = context.pages[0] if context.pages else await context.new_page()
            session = _WorkSession(account, page, on_poll, ratio=ratio,
                                   attachments=len(reference_image_paths or []))

            async def stop_logout(route):
                request = route.request
                if request.resource_type in ("xhr", "fetch", "document"):
                    session.logouts.append(urlsplit(request.url).path)
                    await route.abort()
                else:
                    await route.continue_()
            await context.route(re.compile(r"logout", re.I), stop_logout)
            session.watch()

            await session.open_chat()
            await session.enter_work_mode()
            if reference_image_paths:
                emit_feedback(on_poll, progress="工作模式：上传参考图")
                await session.attach_references(reference_image_paths)
            await session.choose_skill()
            emit_feedback(on_poll, progress="工作模式：发送请求")
            conversation_id = await session.send(text)
            deadline = time.time() + config.WORK_15S_TIMEOUT
            if on_conversation_id:
                on_conversation_id(account, conversation_id, deadline)
            vm_str = await session.wait_for_video(conversation_id, deadline)
            return await _deliver(account, vm_str, conversation_id, on_poll)
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                shutil.rmtree(Path(profile), ignore_errors=True)
