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

from account_transport import http_proxy
import config
import dola_responder
import failure_text
from browser import cloak_proxy_settings, cookie_value, launch_account_context
from browser_feedback import emit as emit_feedback
from generation_errors import (
    PreSubmissionParametersError, PreSubmissionUploadError, UploadRouteError, UpstreamReplyError, WorkQuotaError,
)
from upstream_reply import QUOTA_RE
from video_worker import RiskControlError, _download
from video_worker_ui import (
    DAILY_LIMIT_PATTERN, AccountLimitedError, CreditInsufficientError, LoginExpiredError,
    dismiss_popups, fetch_higher_bitrate_url, find_captcha_frame, is_logged_out,
    raise_if_region_restricted, solve_slider,
)
from work_reply import (
    WORK_DURATION, WORK_LIMIT_RE, ask_summary, asks_to_confirm, build_prompt, chain_messages, choose_options,
    generation_started, pending_ask, recent_conversations, reply_finished, reply_text, session_cookies, tools_ran,
    upload_wait_seconds, uploads_committed, video_models, work_limit_until,
)

SKILL_RE = re.compile(r"^(Creative video|创意视频|クリエイティブ動画|クリエイティブビデオ)$")
SKILL_CHIP_RE = re.compile(r"Creative video|创意视频|クリエイティブ動画|クリエイティブビデオ")
WORK_TAB_RE = re.compile(r"^(ワーク|工作|Work)$")
UPLOAD_ITEM_RE = re.compile(r"ファイルまたは画像|上传文件|Upload file", re.I)
COMPOSER = 'textarea, [contenteditable="true"]'
CONFIRM_OPTION_RE = re.compile(r"确认|確認|開始|开始")
POLL_SECONDS = 10
# 回复写完后等这么多轮还没动静，才算助手在等回答
QUIET_POLLS = 2
# 回答之后至少等这么多轮，再判断它有没有理会
ANSWER_GAP_POLLS = 3
# 回复没写完、也没在出片，却这么多轮一个字没动 → 它在等人回答（2026-11-09：确认单发出来后一直挂着不结束）
STALLED_POLLS = 9
# 同一道选择题最多答这么多次：先点选项，不行再打字
ASK_ROUNDS = 3
# 连续这么多轮读不到会话就在进度里说一声（代理不稳时页面内请求会失败，照样接着等）
CHAIN_FAIL_NOTE_POLLS = 3
ASK_SUBMIT_RE = re.compile(r"^(送信|提交|确定|確定|確認|确认|Submit|Send|OK)$", re.I)
ASK_INPUT_RE = re.compile(r"回答を入力|输入.{0,6}回答|輸入.{0,6}回答|your own answer", re.I)
# 工具跑完、回复写完后这么多轮还没有视频 → 这条会话不会再出片
SETTLED_POLLS = 6
# 排队等名额时隔这么久报一次进度：看门狗 10 分钟没进度就把任务判成卡死
SLOT_NOTE_SECONDS = 30
# 发出后等会话 id：带参考图时助手起步慢，代理差时更慢
CONVERSATION_WAIT_SECONDS = 120
# 关浏览器最多等这么久：卡住时不能一直占着名额
CLOSE_SECONDS = 30
LIMIT_RE = re.compile(r"(今日|本日|今天).{0,12}(额度|額度|次数|回数|上限).{0,12}(用完|用尽|已满|達し|到达|上限)|1日あたりの上限")
THROTTLE_NOTE = "710022002 工作模式请求被 dola 拒绝，页面随后要退出登录（已拦下）"

COMPOSER_TEXT_JS = r"""(selector) => {
  const box = document.querySelector(selector);
  return box ? (box.value || box.innerText || '') : '';
}"""

# 只看当前会话那一行：别的旧会话也可能挂着「一時停止中」
PAUSED_JS = r"""(conversationId) => {
  const row = document.querySelector(`a[href*="/chat/${conversationId}"]`);
  return !!row && /一時停止中|已暂停|Paused/.test(row.innerText || '');
}"""

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

RECENT_JS = r"""async ({query}) => {
  try {
  const r = await fetch('/im/chain/recent_conv?' + query, {method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json; encoding=utf-8', 'agw-js-conv': 'str', 'Accept': '*/*'},
    body: JSON.stringify({cmd: 3200, uplink_body: {pull_recent_conv_chain_uplink_body: {conv_version: 0, limit: 12,
      message_count_per_conv: 0, option: {not_need_message: true, need_complete_conversation: true, need_coco_bot: true,
      need_pc_pin_chain: true, pc_pin_query_type: 0, exclude_archive: true, only_archive: false}, api_version: 1, direction: 3}},
      sequence_id: crypto.randomUUID(), channel: 2, version: '1'})});
  return {status: r.status, text: await r.text()};
  } catch (e) { return {status: 0, text: String(e)}; }
}"""

CHAIN_JS = r"""async ({query, conversationId}) => {
  try {
  const r = await fetch('/im/chain/single?' + query, {method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json; encoding=utf-8', 'agw-js-conv': 'str', 'Accept': '*/*'},
    body: JSON.stringify({cmd: 3100, uplink_body: {pull_singe_chain_uplink_body: {
      conversation_id: conversationId, anchor_index: Number.MAX_SAFE_INTEGER, conversation_type: 3,
      direction: 1, limit: 20, ext: {}, filter: {index_list: []}}},
      sequence_id: crypto.randomUUID(), channel: 2, version: '1'})});
  return {status: r.status, text: await r.text()};
  } catch (e) { return {status: 0, text: String(e)}; }
}"""

# 同一时间只开这么多 CloakBrowser：有头运行，一单要占约 6 分钟
_slots: asyncio.Semaphore | None = None
_slots_size = 0


def _work_slots() -> asyncio.Semaphore:
    global _slots, _slots_size
    size = max(1, config.WORK_15S_CONCURRENCY)
    if _slots is None or _slots_size != size:
        # 面板改了并发：新任务用新的名额，已经在跑的单子各自归还旧名额
        _slots, _slots_size = asyncio.Semaphore(size), size
    return _slots


def browser_error(exc: BaseException) -> bool:
    return type(exc).__module__.split(".")[0] in ("playwright", "patchright")


def short_error(exc: BaseException) -> str:
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return re.sub(r"https?://\S+", "<URL>", first)[:120]


def translate_browser_error(exc: BaseException, account: str, went_out: bool) -> Exception:
    """浏览器原始报错看不懂，也分不清能不能重试：以「请求发没发出去」为界。"""
    if went_out:
        return TimeoutError(f"{account} 工作模式发送后浏览器出错（{short_error(exc)}），可能已在生成，不换号重提")
    return PreSubmissionParametersError(f"{account} 工作模式发送前浏览器出错：{short_error(exc)}")


def failure_from_reply(account: str, reply: str, *, ran_tools: bool) -> Exception | None:
    """助手回复里明说做不了时对应的错误；额度类换号，其余按原因分类。认不出返回 None。"""
    if WORK_LIMIT_RE.search(reply):
        # 工作模式自己的 7 天额度：只锁这个号的 15 秒，不动它的每日额度
        return WorkQuotaError(f"{account} 工作模式免费额度已用完：{reply[-160:]}", until=work_limit_until(reply))
    if DAILY_LIMIT_PATTERN.search(reply) or LIMIT_RE.search(reply):
        return AccountLimitedError(f"{account} 工作模式：{reply[-200:]}")
    if QUOTA_RE.search(reply):
        return CreditInsufficientError(f"{account} 工作模式：{reply[-200:]}")
    label = failure_text.label_for(reply)
    if not label:
        return None
    # 肖像保护按号而异，且没有片子在生成：标成未开始，调度器才会换号重提
    return UpstreamReplyError(label, reply, generation_started=ran_tools and label != "肖像保护")


@asynccontextmanager
async def _work_slot(on_poll):
    slots = _work_slots()
    note = f"工作模式：排队中（同时最多 {max(1, config.WORK_15S_CONCURRENCY)} 单）"
    if slots.locked():
        emit_feedback(on_poll, progress=note)
    while True:
        try:
            await asyncio.wait_for(slots.acquire(), SLOT_NOTE_SECONDS)
            break
        except TimeoutError:
            emit_feedback(on_poll, progress=note)
    try:
        yield
    finally:
        slots.release()


class _WorkSession:
    """一次工作模式出片：打开页面、发送、确认、等视频。"""

    def __init__(self, account, page, on_poll, *, ratio=None, attachments=0, prompt=""):
        self.account, self.page, self.on_poll = account, page, on_poll
        self.ratio, self.attachments = ratio, attachments
        self.confirmed = False
        self.nudges = 0
        self.ai_tried = False
        self.ai_note = ""
        self.tried: list[str] = []
        self.logouts: list[str] = []
        self.sent: list[str] = []
        self.page_query = ""
        self.upload_actions: list[str] = []
        self.send_error = ""
        self.url_before = ""
        self.conversation_id = ""
        self.ask_rounds = 0
        self.prompt = prompt
        self.choice_answers = {}
        self.uploading = False
        self.upload_error = None

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

        def on_failed(request):
            parts = urlsplit(request.url)
            if parts.path.endswith("/chat/completion"):
                self.send_error = str(request.failure or "连接失败")[:120]
            host = (parts.hostname or "").lower()
            if self.uploading and any(host == domain or host.endswith("." + domain)
                                      for domain in ("bytevcloudapi.com", "vodupload.com")):
                self.upload_error = UploadRouteError(host, short_error(request.failure or "连接失败"),
                                                     stage="工作模式参考图")
        self.page.on("requestfailed", on_failed)

    def rejected(self) -> bool:
        return bool(self.logouts)

    def raise_if_rejected(self):
        if self.rejected():
            raise RiskControlError(THROTTLE_NOTE)

    async def raise_if_blocked(self):
        """找不到按钮、菜单时先看是不是掉线或地区受限，别报成页面元素的问题。"""
        self.raise_if_rejected()
        raise_if_region_restricted(self.page, self.account)
        if await is_logged_out(self.page):
            raise LoginExpiredError(f"{self.account} 登录态失效，工作模式未发送")

    async def solve_captcha(self):
        frame = find_captcha_frame(self.page)
        if not frame:
            return
        emit_feedback(self.on_poll, progress="工作模式：出现滑块验证，正在处理")
        for attempt in range(1, 4):
            if await solve_slider(self.page, frame, attempt, self.account):
                await self.page.wait_for_timeout(3000)
                return
            await self.page.wait_for_timeout(2000)
            frame = find_captcha_frame(self.page)
            if not frame:
                return
        raise RiskControlError(f"{self.account} 工作模式滑块 3 次未通过")

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
            await self.raise_if_blocked()
            raise PreSubmissionParametersError(f"{self.account} 页面上没有工作模式入口")
        await tab.first.click(timeout=6000)
        await self.page.wait_for_timeout(8000)
        await self.raise_if_blocked()

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
        self.uploading, self.upload_error = True, None
        try:
            picked = await self._pick_files(paths)
            self.raise_if_upload_failed()
            if not picked:
                await self._paste_files(paths)
            total = sum(Path(path).stat().st_size for path in paths if Path(path).exists())
            wait = upload_wait_seconds(total)
            for _ in range(wait):
                self.raise_if_upload_failed()
                if uploads_committed(self.upload_actions) >= len(paths):
                    break
                await page.wait_for_timeout(1000)
            else:
                raise PreSubmissionUploadError(
                    f"{self.account} 工作模式参考图 {wait} 秒内没有传完（{total / 1048576:.1f} MB）")
            await page.wait_for_timeout(2500)
            self.raise_if_upload_failed()
        finally:
            self.uploading = False

    def raise_if_upload_failed(self):
        if self.upload_error:
            print(f"[{self.account}] 工作模式上传失败：{self.upload_error}", flush=True)
            raise self.upload_error

    async def choose_skill(self):
        page = self.page
        skill = page.get_by_text(SKILL_RE)
        for _ in range(3):
            await page.locator(COMPOSER).first.click(timeout=8000)
            await page.keyboard.type("/", delay=120)
            await page.wait_for_timeout(3000)
            # 它排在菜单下方的分组里，列表没加载完就找不到；输入名字让菜单筛选出来
            await page.keyboard.type("Creative", delay=80)
            for _ in range(10):
                await page.wait_for_timeout(1000)
                if await skill.count():
                    await skill.first.click(timeout=5000)
                    await page.wait_for_timeout(1500)
                    return
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(2500)
            await self.raise_if_blocked()
        raise PreSubmissionParametersError(f"{self.account} 技能列表里没有 Creative video")

    async def composer_text(self) -> str:
        return await self.page.evaluate(COMPOSER_TEXT_JS, COMPOSER)

    async def fill_prompt(self, text: str) -> None:
        """切模式、选技能后输入框可能重绘，文字写进去要读回来确认。"""
        page, marker = self.page, text[:12]
        for _ in range(3):
            if marker in await self.composer_text():
                return
            await page.locator(COMPOSER).first.click(timeout=8000)
            await page.keyboard.insert_text(text)
            await page.wait_for_timeout(1500)
            if marker in await self.composer_text():
                return
            await page.wait_for_timeout(2000)
        raise PreSubmissionParametersError(f"{self.account} 工作模式提示词没有写进输入框")

    def went_out(self) -> bool:
        """看到请求，或页面跳到了新会话（local_ 开头是还没拿到真实 id）。"""
        url = self.page.url
        return bool(self.sent) or (url != self.url_before and bool(re.search(r"/chat/(local_|\d)", url)))

    async def wait_sent(self, seconds: int) -> bool:
        for _ in range(seconds):
            if self.went_out():
                return True
            await self.page.wait_for_timeout(1000)
        return self.went_out()

    async def press_send(self) -> bool:
        """回车，没发出去再点发送按钮。按钮灰着说明输入框空了，点也没用，交给上层重来。"""
        page = self.page
        self.url_before = page.url
        await page.keyboard.press("Enter")
        if await self.wait_sent(8):
            return True
        button = page.locator('[data-testid="chat_input_send_button"]')
        if await button.count() and await button.first.is_enabled():
            await button.first.click(timeout=4000)
            return await self.wait_sent(10)
        return False

    def raise_if_send_failed(self):
        if self.send_error:
            # 请求没到 Dola（多半是代理断了）：没有会话，换号不会重复出片
            raise PreSubmissionParametersError(f"{self.account} 工作模式请求没连上：{self.send_error}")

    async def restore_composer(self) -> None:
        """页面重绘把技能和文字清掉时重新选一遍。已经在工作模式（看不到入口、输入框还在）就只重选技能。"""
        self.raise_if_send_failed()
        if SKILL_CHIP_RE.search(await self.composer_text()):
            return
        if await self.page.get_by_text(WORK_TAB_RE).count():
            await self.enter_work_mode()
        elif not await self.page.locator(COMPOSER).count():
            await self.raise_if_blocked()
            raise PreSubmissionParametersError(f"{self.account} 工作模式输入框不见了")
        await self.choose_skill()

    async def send(self, text: str) -> str:
        page = self.page
        for _ in range(3):
            await self.restore_composer()
            await self.fill_prompt(text)
            if await self.press_send():
                break
            self.raise_if_send_failed()
            await self.raise_if_blocked()
            await page.wait_for_timeout(3000)
        else:
            left = (await self.composer_text()).strip()[:40]
            raise PreSubmissionParametersError(f"{self.account} 工作模式请求没有发出去（输入框内容：{left or '空'}）")
        sent_at = time.time()
        for _ in range(CONVERSATION_WAIT_SECONDS):
            self.raise_if_rejected()
            await self.solve_captcha()
            match = re.search(r"/chat/(\d+)", page.url)
            if match and page.url != self.url_before:
                return match.group(1)
            self.raise_if_send_failed()
            await page.wait_for_timeout(1000)
        self.raise_if_rejected()
        known, conversation = await self.find_conversation(sent_at)
        if conversation:
            return conversation
        if known:
            raise PreSubmissionParametersError(f"{self.account} 工作模式请求 {CONVERSATION_WAIT_SECONDS} 秒没有回应，账号上也没有新会话")
        raise TimeoutError(f"{self.account} 工作模式 {CONVERSATION_WAIT_SECONDS} 秒没有拿到会话，可能已在生成，不换号重提")

    async def find_conversation(self, since: float) -> tuple[bool, str | None]:
        """查账号最近的会话：(查到了列表, 发送之后新建的会话 id)。查不到列表就说不准。"""
        try:
            result = await self.page.evaluate(RECENT_JS, {"query": await self.chain_query()})
            if result["status"] != 200:
                return False, None
            conversations = recent_conversations(json.loads(result["text"]))
        except Exception:  # noqa: BLE001 - 查询失败时按「说不准」处理，绝不当成没发出去
            return False, None
        fresh = [cid for cid, updated in conversations if updated >= since - 5]
        return True, fresh[0] if fresh else None

    async def chain_query(self) -> str:
        cookies = await self.page.context.cookies("https://www.dola.com")
        source = self.page_query or (self.sent[0] if self.sent else "")
        if not source:
            raise PreSubmissionParametersError(f"{self.account} 页面签名参数尚未就绪")
        query = dict(parse_qsl(urlsplit(source).query, keep_blank_values=True))
        query.pop("a_bogus", None)
        query["msToken"] = cookie_value(cookies, "msToken") or query.get("msToken", "")
        return urlencode(query)

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
            reply, why = await dola_responder.ai_reply_detail(question, self.ratio, WORK_DURATION,
                                                              self.attachments)
            if not reply:
                self.ai_note = why
                emit_feedback(self.on_poll, progress=f"工作模式：AI 代答没有回复（{why[:80]}）")
            return reply, "AI 代答"
        return None, ""

    async def type_reply(self, reply: str):
        """打字回复。选择题卡片有自己的输入框（「または、自分の回答を入力してください」）时填那里。"""
        page = self.page
        box = page.get_by_placeholder(ASK_INPUT_RE)
        target = box.last if await box.count() else page.locator(COMPOSER).first
        await target.click(timeout=8000)
        await page.keyboard.insert_text(reply)
        await page.wait_for_timeout(1000)
        await page.keyboard.press("Enter")

    async def choice_labels(self, questions: list[dict]) -> list[str]:
        key = json.dumps(questions, ensure_ascii=False, sort_keys=True)
        if key in self.choice_answers:
            return self.choice_answers[key]
        labels = choose_options(questions)
        if dola_responder.enabled():
            emit_feedback(self.on_poll, progress="工作模式：正在请 AI 选择方案")
            selected = []
            for question in questions:
                options = question.get("options") or []
                if not options:
                    continue
                text = (f"原始视频要求：{self.prompt[:4000]}\n选择题：{question.get('title', '')}\n"
                        + "\n".join(f"- {option}" for option in options)
                        + "\n请选最符合原始要求的一个选项，只回复该选项的完整原文，不加解释。")
                try:
                    reply, _why = await dola_responder.ai_reply_detail(
                        text, self.ratio, WORK_DURATION, self.attachments)
                except Exception as exc:
                    print(f"[{self.account}] 工作模式 AI 选项请求失败：{short_error(exc)}", flush=True)
                    reply = None
                matched = next((option for option in options if reply and option.strip() == reply.strip()), None)
                selected.append(matched or labels[len(selected)])
            labels = selected
        self.choice_answers[key] = labels
        return labels

    async def answer_ask(self, questions: list[dict]) -> str:
        """回答选择题，返回给人看的说明。第一次点选项，还挂着就改成打字（题目允许自己输入）。"""
        page, labels = self.page, await self.choice_labels(questions)
        self.ask_rounds += 1
        clicked = 0
        if self.ask_rounds == 1:
            for label in labels:
                try:
                    option = page.get_by_text(label, exact=True)
                    if await option.count():
                        await option.last.click(timeout=4000)
                        clicked += 1
                        await page.wait_for_timeout(1200)
                except Exception as exc:  # noqa: BLE001 - 点不到就改打字，不能让整单因此失败
                    print(f"[{self.account}] 工作模式选择题点选失败：{short_error(exc)}", flush=True)
            if clicked:
                try:
                    submit = page.get_by_role("button", name=ASK_SUBMIT_RE)
                    if await submit.count() and await submit.last.is_enabled():
                        await submit.last.click(timeout=3000)
                except Exception:  # noqa: BLE001 - 有的题点了选项就算提交
                    pass
        picked = "、".join(labels) or "推荐方案"
        if clicked < max(1, len(labels)):
            await self.type_reply(f"确认，按以下选择直接生成：{picked}" if labels else "确认，按推荐方案直接生成")
            how = "已打字回答选择题"
        else:
            how = "已选"
        await page.wait_for_timeout(6000)
        print(f"[{self.account}] 工作模式选择题（{ask_summary(questions)}）{how}：{picked}", flush=True)
        return f"{how}：{picked}"

    async def answer(self, reply: str):
        page = self.page
        await self.type_reply(reply)
        await page.wait_for_timeout(6000)
        if await self.paused():
            # 选择题没收下打字回复：点带「确认」的选项
            option = page.get_by_text(CONFIRM_OPTION_RE)
            if await option.count():
                await option.last.click(timeout=4000)

    async def question(self) -> str:
        return await self.page.evaluate(QUESTION_JS, COMPOSER)

    async def paused(self) -> bool:
        return bool(await self.page.evaluate(PAUSED_JS, self.conversation_id))

    async def read_chain(self, query: str, conversation_id: str):
        """读会话内容；读不到返回 None。代理抖一下、页面重绘都会让这次读取失败，不能因此丢掉整单
        （2026-11-09：acc23 两次都是等到快出片时 TypeError: Failed to fetch，任务直接判失败）。"""
        try:
            result = await self.page.evaluate(CHAIN_JS, {"query": query, "conversationId": conversation_id})
        except Exception as exc:  # noqa: BLE001
            if not browser_error(exc) or self.page.is_closed():
                raise
            print(f"[{self.account}] 工作模式读会话失败，下一轮再读：{short_error(exc)}", flush=True)
            return None
        if result["status"] != 200:
            return None
        try:
            return json.loads(result["text"])
        except ValueError:
            return None

    async def wait_for_video(self, conversation_id: str, deadline: float) -> str:
        self.conversation_id = conversation_id
        query_text = await self.chain_query()
        stable, last, since_answer, unread = 0, "", ANSWER_GAP_POLLS, 0
        answered = set()
        while time.time() < deadline:
            await self.page.wait_for_timeout(POLL_SECONDS * 1000)
            self.raise_if_rejected()
            await self.solve_captcha()
            if await is_logged_out(self.page):
                raise LoginExpiredError(f"{self.account} 工作模式生成中登录态失效")
            chain = await self.read_chain(query_text, conversation_id)
            if chain is None:
                unread += 1
                emit_feedback(self.on_poll, progress="工作模式：读取会话失败（网络不稳），继续等待"
                              if unread >= CHAIN_FAIL_NOTE_POLLS else "工作模式：助手处理中")
                continue
            unread = 0
            emit_feedback(self.on_poll, progress="工作模式：助手处理中")
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
                    raise (failure_from_reply(self.account, reply, ran_tools=True)
                           or UpstreamReplyError("生成失败", reply, generation_started=True))
                continue
            if finished and stable >= QUIET_POLLS and not asks_to_confirm(reply_text(latest)):
                failure = failure_from_reply(self.account, reply_text(latest), ran_tools=False)
                if failure:
                    raise failure
            making = any(generation_started(m) for m in bots)
            # 选择题：以会话内容为准（页面侧栏不一定标「一時停止中」）。已经在出片就不插话。
            ask = pending_ask(latest) if latest and not finished and not making else None
            if ask and since_answer >= ANSWER_GAP_POLLS and self.ask_rounds < ASK_ROUNDS:
                emit_feedback(self.on_poll, progress=f"工作模式：助手让选方案（{ask_summary(ask)[:60]}）")
                how = await self.answer_ask(ask)
                self.tried.append("选择题")
                emit_feedback(self.on_poll, progress=f"工作模式：{how[:80]}")
                stable, since_answer = 0, 0
                continue
            question = ""
            paused = await self.paused()
            if ask:
                pass                # 刚答过，或答了几次还挂着：等它自己往下走
            elif paused:
                question = await self.question()
            elif finished and stable >= QUIET_POLLS and (
                    latest.get("index_in_conv") not in answered or stable >= SETTLED_POLLS):
                question = reply_text(latest)
                answered.add(latest.get("index_in_conv"))
            elif (latest and not finished and not making and stable >= STALLED_POLLS
                  and reply_text(latest).strip()):
                # 回复没标完成，却一分多钟一个字没动，也没在出片：它在等人确认
                question = reply_text(latest)
            if question and since_answer >= ANSWER_GAP_POLLS:
                reply, how = await self.next_answer(question)
                if not reply and paused:
                    continue        # 选择题没人答也会自己往下走，不算失败
                if not reply:
                    raise UpstreamReplyError(
                        "上游在等待回复",
                        f"工作模式助手在追问，已尝试 {len(self.tried)} 次回复仍未开始生成"
                        + (f"（AI 代答：{self.ai_note[:80]}）" if self.ai_note else "") + f"：{question[-800:]}",
                        generation_started=False)
                self.tried.append(how)
                emit_feedback(self.on_poll, progress=f"工作模式：已{how}：{reply[:60]}")
                await self.answer(reply)
                stable, since_answer = 0, 0
        raise TimeoutError(f"{self.account} 工作模式等待超时，可能仍在生成，不换号重提")


async def _deliver(account: str, vm_str: str, conversation_id: str, on_poll) -> dict:
    url = None
    for attempt in range(3):
        url = await fetch_higher_bitrate_url(vm_str, account)
        if url:
            break
        await asyncio.sleep(3 * (attempt + 1))
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


async def _launch_cloak(account: str, profile: str, extension_paths: list[str] | None = None):
    try:
        from cloakbrowser import launch_persistent_context_async
    except ImportError as exc:
        raise PreSubmissionParametersError("工作模式需要 CloakBrowser，当前环境没有安装") from exc
    # 带密码的 socks5 浏览器连不上，要走号池的本地中转（和 patchright 路径一样）
    proxy = http_proxy(account)
    if not proxy:
        raise PreSubmissionParametersError(f"{account} 没有绑定代理，工作模式不走直连")
    return await launch_persistent_context_async(
        profile, headless=config.CLOAK_HEADLESS, proxy=cloak_proxy_settings(proxy),
        locale="ja-JP", timezone="Asia/Tokyo",
        geoip=config.CLOAK_GEOIP, humanize=config.CLOAK_HUMANIZE,
        extension_paths=extension_paths or None,
        license_key=config.CLOAK_LICENSE_KEY or None)


@asynccontextmanager
async def _work_browser(account: str, on_poll=None, *, ratio=None, attachments=0, prompt=""):
    """生成和只读取回共用浏览器名额、独立临时目录、绑定代理与退出登录拦截。"""
    async with _work_slot(on_poll):
        profile = tempfile.mkdtemp(prefix="dola-work-")
        context = session = None
        try:
            emit_feedback(on_poll, progress="工作模式：准备浏览器")
            async with async_playwright() as p:
                cookies = await _copied_session(p, account)
            context = await _launch_cloak(account, profile)
            await context.add_cookies(cookies)
            page = context.pages[0] if context.pages else await context.new_page()
            session = _WorkSession(account, page, on_poll, ratio=ratio,
                                   attachments=attachments, prompt=prompt)

            async def stop_logout(route):
                request = route.request
                if request.resource_type in ("xhr", "fetch", "document"):
                    session.logouts.append(urlsplit(request.url).path)
                    await route.abort()
                else:
                    await route.continue_()
            await context.route(re.compile(r"logout", re.I), stop_logout)
            session.watch()

            emit_feedback(on_poll, progress="工作模式：打开页面")
            await session.open_chat()
            yield session
        except Exception as exc:
            if not browser_error(exc):
                raise
            raise translate_browser_error(exc, account, bool(session and session.went_out())) from exc
        finally:
            try:
                if context is not None:
                    await asyncio.wait_for(context.close(), CLOSE_SECONDS)
            except Exception as exc:  # noqa: BLE001 - 关不掉也要让出名额
                print(f"[{account}] 工作模式关闭浏览器失败：{short_error(exc)}", flush=True)
            finally:
                shutil.rmtree(Path(profile), ignore_errors=True)


def _stage(account, on_poll, text):
    print(f"[{account}] {text}", flush=True)
    emit_feedback(on_poll, progress=text)


async def generate_work_video(account: str, prompt: str, ratio: str = None, *,
                              on_conversation_id=None, on_poll=None,
                              reference_image_paths: list[str] | None = None) -> dict:
    """工作模式出一条 15 秒；已发送超时由任务恢复器接着读原会话。"""
    text = build_prompt(prompt, ratio)
    async with _work_browser(account, on_poll, ratio=ratio, prompt=prompt,
                             attachments=len(reference_image_paths or [])) as session:
        _stage(account, on_poll, "工作模式：切换到工作模式")
        await session.enter_work_mode()
        if reference_image_paths:
            _stage(account, on_poll, "工作模式：上传参考图")
            await session.attach_references(reference_image_paths)
        _stage(account, on_poll, "工作模式：选择技能")
        await session.choose_skill()
        _stage(account, on_poll, "工作模式：发送请求")
        conversation_id = await session.send(text)
        deadline = time.time() + config.WORK_15S_TIMEOUT
        if on_conversation_id:
            on_conversation_id(account, conversation_id, deadline)
        _stage(account, on_poll, "工作模式：请求已提交，等待原会话出片")
        vm_str = await session.wait_for_video(conversation_id, deadline)
        _stage(account, on_poll, "工作模式：已读到视频结果")
        return await _deliver(account, vm_str, conversation_id, on_poll)


async def fetch_work_conversation_video(account: str, conversation_id: str, *, on_poll=None) -> dict:
    """只读已有会话，不发送提示词、不代答、不创建新会话。"""
    async with _work_browser(account, on_poll) as session:
        _stage(account, on_poll, "工作模式：只读检查超时原会话")
        session.conversation_id = conversation_id
        chain = await session.read_chain(await session.chain_query(), conversation_id)
        pending = {"status": "pending", "local_path": "", "final": False}
        if chain is None:
            return pending
        found = video_models(chain)
        if found:
            return {**await _deliver(account, found[0], conversation_id, on_poll),
                    "status": "succeeded", "final": False}
        bots = [m for m in chain_messages(chain) if str(m.get("user_type")) == "2"]
        latest = bots[-1] if bots else None
        if latest and reply_finished(latest):
            failure = failure_from_reply(account, reply_text(latest),
                                         ran_tools=any(generation_started(m) for m in bots))
            if failure:
                return {"status": "failed", "local_path": "", "final": True,
                        "detail": str(failure), "work_limit_until": getattr(failure, "until", None),
                        "work_quota_blocked": isinstance(failure, WorkQuotaError)}
        return pending
