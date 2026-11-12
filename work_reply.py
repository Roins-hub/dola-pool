"""工作模式 15 秒的纯逻辑：是否走这条路、提示词、会话内容解析。不碰浏览器，便于单测。"""
import importlib.util
import json
import re

import config
import sites

WORK_DURATION = 15
MAX_CONCURRENCY = 5
CONFIRM_RE = re.compile(r"确认|確認")
QUOTA_NOTE = "工作模式免费额度已用：每号 7 天 1 条"
WORK_QUOTA_SECONDS = 7 * 86400
# 2026-11-07：「7日間の無料利用分の上限に到達しました。2026年11月14日 10:57（GMT+08:00）に再開するか…」
WORK_LIMIT_RE = re.compile(r"7\s*日間.{0,16}上限|無料利用分.{0,8}上限|上限に到達|7\s*天.{0,16}(上限|用完|额度)|免费(使用|额度).{0,10}(上限|用完)", re.S)
RESUME_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2}):(\d{2})"
                       r"(?:\s*[（(]\s*GMT\s*([+-])(\d{1,2}):?(\d{2})?\s*[)）])?")


def unavailable_reason() -> str:
    """这个环境开不了时说明原因；能开时返回空串。"""
    if importlib.util.find_spec("cloakbrowser") is None:
        return ("当前环境没有安装 CloakBrowser，开不了。Docker 镜像暂不支持；"
                "服务器脚本部署可安装 deploy-cloak-requirements.txt 后重启服务。")
    return ""


def settings_view() -> dict:
    reason = unavailable_reason()
    return {"enabled": config.WORK_15S_ENABLED, "available": not reason, "reason": reason,
            "concurrency": config.WORK_15S_CONCURRENCY, "max_concurrency": MAX_CONCURRENCY,
            "v20_durations": list(config.V20_DURATIONS)}


def apply_saved(saved: str | None, concurrency: str | None = None) -> dict:
    """启动时恢复：面板存过就听面板，没存过沿用环境变量；环境不支持时一律关闭。"""
    wanted = saved == "1" if saved in ("0", "1") else config.WORK_15S_ENABLED
    config.set_work_15s(wanted and not unavailable_reason())
    if concurrency and concurrency.isdigit():
        config.WORK_15S_CONCURRENCY = min(MAX_CONCURRENCY, max(1, int(concurrency)))
    return settings_view()


def uses_work_mode(model: str, duration, execution_mode: str = "auto") -> bool:
    """这一单是否走工作模式：开关打开、Dola 的 2.0、15 秒。"""
    if execution_mode == "work":
        return not work_request_error(model, duration)
    if not config.WORK_15S_ENABLED:
        return False
    try:
        if int(duration or 0) != WORK_DURATION:
            return False
    except (TypeError, ValueError):
        return False
    text = (model or "").lower().replace("_", "-")
    return ("2.0" in text or "2-0" in text) and "doubao" not in text


def is_work_request(model: str, duration, execution_mode: str = "auto") -> bool:
    """已提交任务的类型不随面板开关改变，供恢复和记账使用。"""
    if execution_mode == "work":
        return not work_request_error(model, duration)
    try:
        seconds = int(duration or 0)
    except (TypeError, ValueError):
        return False
    text = (model or "").lower().replace("_", "-")
    return seconds in (15, 30) and ("2.0" in text or "2-0" in text) and "doubao" not in text


def uses_work_pair(model: str, duration, execution_mode: str = "auto") -> bool:
    """这一单是否是两段工作模式拼成的 30 秒：开关打开、Dola 的 2.0、30 秒。"""
    if execution_mode == "work":
        return False
    try:
        if int(duration or 0) != 2 * WORK_DURATION:
            return False
    except (TypeError, ValueError):
        return False
    return uses_work_mode(model, WORK_DURATION)


def work_request_error(model: str, duration) -> str:
    """显式工作模式的受理范围；原生时长是否可用由上游决定，不自动拼接。"""
    try:
        seconds = int(duration or 0)
    except (TypeError, ValueError):
        return "工作模式时长无效"
    spec = sites.doubao_spec(model)
    if spec:
        return "" if sites.DOUBAO_MIN_DURATION <= seconds <= spec[1] else f"{model} 支持 4~{spec[1]} 秒"
    text = (model or "").lower().replace("_", "-")
    if ("2.0" in text or "2-0" in text) and "doubao" not in text and seconds in (5, 10, 15, 30):
        return ""
    return "Dola 工作模式仅支持 Seedance 2.0 的 5/10/15/30 秒请求；30 秒为原生尝试，上游拒绝时结束"


def work_count_error(model, duration, execution_mode, video_count) -> str:
    if video_count not in (1, 2):
        return "video_count 只支持 1 / 2"
    if video_count == 2 and (execution_mode != "work" or sites.is_doubao_model(model) or int(duration or 0) != 15):
        return "单任务两条视频仅支持显式 Dola 工作模式、每条 15 秒"
    return ""


def model_label(model: str) -> str:
    spec = sites.doubao_spec(model)
    if spec:
        return spec[3]
    return "Seedance 2.0 Fast"


LONGER_RE = re.compile(r"(?<![\d.:])(\d{2,3})\s*(?:s\b|秒|sec)", re.I)
# 提示词自己写了更长的时长（比如 30 秒 9 个分镜的脚本）时，助手会停下来让人选方案（2026-11-09 实测，
# 没人答要等一小时）；先把话说死，它就只走「确认」这一步。
SINGLE_CLIP_NOTE = ("，只生成一条 15 秒的连续视频：提示词里其它的总时长、分段和批次只作内容参考，"
                    "按推荐方案直接生成，不用让我选方案")


def asks_for_longer(text: str) -> bool:
    """提示词里写了比 15 秒更长的时长。"""
    return any(int(m.group(1)) > WORK_DURATION for m in LONGER_RE.finditer(text or ""))


def build_prompt(prompt: str, ratio: str | None, duration: int = WORK_DURATION, model: str = "", video_count: int = 1) -> str:
    """助手只看文字，时长和比例都要写进去；换行会让输入框提前发送。"""
    text = re.sub(r"\s*[\r\n]+\s*", " ", prompt or "").strip().rstrip("，,。. ")
    if ratio:
        text += f"，画面比例 {ratio}"
    if duration == WORK_DURATION and video_count == 1 and asks_for_longer(text):
        text += SINGLE_CLIP_NOTE
    elif not re.search(rf"(?<!\d){duration}\s*(?:s\b|秒)", text, re.I):
        text += f"，{duration}s"
    if model:
        count = "一条原生" if video_count == 1 else "两条独立视频文件，每条原生"
        entitlement = "当前账号已有的免费或会员生成权益" if sites.is_doubao_model(model) and config.DOUBAO_ALLOW_PAID else "当前免费额度"
        text += (f"。在这一次工作任务中，仅使用 {model_label(model)} 生成{count} {duration} 秒视频，仅使用{entitlement}；"
                 "若模型、时长或上述权益不可用，请说明原因并停止，不改模型、不缩短时长、不拆段、不购买、不充值、不升级套餐。")
    return text


def session_cookies(cookies: list[dict], site: str = sites.DOLA) -> list[dict]:
    keep = []
    for cookie in cookies:
        host = str(cookie.get("domain") or "").lower().lstrip(".")
        domain = "doubao.com" if site == sites.DOUBAO else "dola.com"
        if host == domain or host.endswith("." + domain):
            keep.append(cookie)
    return keep


def video_skill_labels(data, site: str = sites.DOLA) -> list[str]:
    """用稳定技能名读取当前显示名称，界面改名后仍定位同一视频技能。"""
    labels = []
    def walk(node):
        if isinstance(node, dict):
            stable = "doubao-creative-video" if site == sites.DOUBAO else "dola-creative-video"
            if node.get("name") == stable or node.get("skill_name") == stable:
                label = node.get("display_name")
                if isinstance(label, str) and label.strip():
                    labels.append(label.strip())
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(data)
    return list(dict.fromkeys(labels))


def chain_messages(chain) -> list[dict]:
    found = {}

    def walk(node):
        if isinstance(node, dict):
            if ("index_in_conv" in node and "message_id" in node
                    and isinstance(node.get("content_block"), list)):
                found[str(node["message_id"])] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(chain)
    return sorted(found.values(), key=lambda m: int(m.get("index_in_conv") or 0))


def video_models(chain) -> list[str]:
    out = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "video_model" and isinstance(value, str) and value.startswith("{"):
                    out.append(value)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and "video_model" in node and node[:1] in "{[":
            try:
                walk(json.loads(node))
            except ValueError:
                pass
    walk(chain)
    return out


def reply_text(message: dict) -> str:
    parts = []
    for block in message.get("content_block") or []:
        text = ((block.get("content") or {}).get("text_block") or {}).get("text")
        if text:
            parts.append(text)
    return "\n".join(parts)


def reply_finished(message: dict) -> bool:
    """助手这条回复写完了。生成途中文字和工具卡片也在这条里，只看消息自己的完成标记。"""
    return str((message.get("ext") or {}).get("is_finish")) == "1" and bool(reply_text(message))


def tools_ran(message: dict) -> bool:
    """助手已经调过工具（比如 text_to_video），说明不是在等人回答。"""
    return any((block.get("content") or {}).get("generic_tool_block")
               for block in message.get("content_block") or [])


def asks_to_confirm(text: str) -> bool:
    return bool(CONFIRM_RE.search(text or ""))


VIDEO_TOOL_RE = re.compile(r"video", re.I)
ASK_PREFER_RE = re.compile(r"推荐|推薦|おすすめ|recommend|确认|確認|开始|開始", re.I)


def generation_started(message: dict) -> bool:
    """助手已经调了出片工具（text_to_video / image_to_video）：片子在做了，不能再插话。"""
    for block in message.get("content_block") or []:
        tool = (block.get("content") or {}).get("generic_tool_block") or {}
        if VIDEO_TOOL_RE.search(str(tool.get("tool_name") or "")):
            return True
    return False


def pending_ask(message: dict) -> list[dict] | None:
    """助手挂着一道还没人答的选择题时，返回 [{"title": 题目, "options": [选项文字…]}]，否则 None。

    选择题在 interaction_ask_block 里（2026-11-09 实测）：页面侧栏不一定显示「一時停止中」，
    只看页面会漏掉，助手就干等到一小时后过期。答过的题 answer 里有选项；过期或答完后，
    助手会在这块后面接着写顶层内容。
    """
    blocks = message.get("content_block") or []
    for index in range(len(blocks) - 1, -1, -1):
        ask = (blocks[index].get("content") or {}).get("interaction_ask_block")
        if not ask:
            continue
        moved_on = any(not later.get("parent_id") and any((later.get("content") or {}).values())
                       for later in blocks[index + 1:])
        if moved_on:
            return None
        questions = []
        for question in ask.get("questions") or []:
            answer = question.get("answer") or {}
            if answer.get("selected_option_ids") or answer.get("text"):
                continue
            options = [str(o.get("label")) for o in question.get("options") or [] if o.get("label")]
            questions.append({"title": str(question.get("title") or ""), "options": options})
        return questions or None
    return None


def choose_options(questions: list[dict]) -> list[str]:
    """每道题选一个：优先带「推荐 / 确认」的选项，没有就选第一个。没有选项的题跳过。"""
    chosen = []
    for question in questions:
        options = question.get("options") or []
        if options:
            chosen.append(next((o for o in options if ASK_PREFER_RE.search(o)), options[0]))
    return chosen


def ask_summary(questions: list[dict]) -> str:
    return "；".join(q.get("title") or "" for q in questions)[:200]


def work_limit_until(text: str) -> float | None:
    """「7 天上限」提示里的恢复时间（epoch 秒）。没写时区按北京时间；读不到返回 None。"""
    import datetime
    m = RESUME_RE.search(text or "")
    if not m:
        return None
    year, month, day, hour, minute = (int(x) for x in m.groups()[:5])
    offset = 8 * 60
    if m.group(6):
        offset = int(m.group(7)) * 60 + int(m.group(8) or 0)
        offset = offset if m.group(6) == "+" else -offset
    try:
        tz = datetime.timezone(datetime.timedelta(minutes=offset))
        return datetime.datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()
    except ValueError:
        return None


def recent_conversations(data: dict) -> list[tuple[str, int]]:
    """recent_conv 的返回 → [(会话 id, 最后更新的秒级时间)]。conv_version 是微秒时间戳。"""
    out = []
    body = (data.get("downlink_body") or {}).get("pull_recent_conv_chain_downlink_body") or {}
    for cell in body.get("cells") or []:
        conversation = cell.get("conversation") or {}
        version = str(conversation.get("conv_version") or "")
        if conversation.get("conversation_id") and version[:10].isdigit():
            out.append((str(conversation["conversation_id"]), int(version[:10])))
    return out


def uploads_committed(actions: list[str]) -> int:
    return sum(1 for action in actions if action == "CommitImageUpload")


UPLOAD_WAIT_MIN = 60
UPLOAD_WAIT_MAX = 180
UPLOAD_SECONDS_PER_MB = 30


def upload_wait_seconds(total_bytes: int) -> int:
    """等参考图传完的秒数：每 MB 30 秒，最少 60、最多 180。

    上传域名已直连，正常几秒就传完；这是给直连也慢或被关掉直连时的兜底。
    """
    want = -(-max(0, int(total_bytes)) * UPLOAD_SECONDS_PER_MB // (1 << 20))
    return max(UPLOAD_WAIT_MIN, min(UPLOAD_WAIT_MAX, want))
