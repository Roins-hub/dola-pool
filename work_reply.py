"""工作模式 15 秒的纯逻辑：是否走这条路、提示词、会话内容解析。不碰浏览器，便于单测。"""
import importlib.util
import json
import re

import config

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


def uses_work_mode(model: str, duration) -> bool:
    """这一单是否走工作模式：开关打开、Dola 的 2.0、15 秒。"""
    if not config.WORK_15S_ENABLED:
        return False
    try:
        if int(duration or 0) != WORK_DURATION:
            return False
    except (TypeError, ValueError):
        return False
    text = (model or "").lower().replace("_", "-")
    return ("2.0" in text or "2-0" in text) and "doubao" not in text


def build_prompt(prompt: str, ratio: str | None) -> str:
    """助手只看文字，时长和比例都要写进去；换行会让输入框提前发送。"""
    text = re.sub(r"\s*[\r\n]+\s*", " ", prompt or "").strip().rstrip("，,。. ")
    if ratio:
        text += f"，画面比例 {ratio}"
    if not re.search(r"(?<!\d)15\s*(?:s\b|秒)", text, re.I):
        text += "，15s"
    return text


def session_cookies(cookies: list[dict]) -> list[dict]:
    keep = []
    for cookie in cookies:
        host = str(cookie.get("domain") or "").lower().lstrip(".")
        if host == "dola.com" or host.endswith(".dola.com"):
            keep.append(cookie)
    return keep


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
