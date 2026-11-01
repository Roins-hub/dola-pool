"""dola 追问时的兜底答话：不再逐句识别话术，而是按任务参数回一句「重申需求、直接开工」。

两层：
1. 通用回复（不依赖外部服务）：dola 带「追问」标记或说完话迟迟不开工时，发一句固定的话，
   重申比例 / 时长 / 参考图，并说明这是一条新的生成请求（dola 有跨会话记忆，会说「已经生成过了」）。
   每个任务最多 GENERIC_LIMIT 次。
2. AI 代答（可选）：通用回复用完 dola 仍在问，就把 dola 原话和任务参数交给一个兼容 OpenAI 的模型，
   让它写一句回复。回复必须只确认 / 重申参数 / 选与参数一致的选项；出现与参数不一致的比例或秒数就丢弃。

两层都不改客户要的内容；答不了就结束任务（此时尚未开始生成，不扣点）。
"""
from __future__ import annotations

import json
import re

GENERIC_LIMIT = 2
AI_TIMEOUT = 25
MAX_REPLY_CHARS = 200

# AI 代答设置：server 启动和面板保存时写进来（见 server.py 的 /api/admin/settings/responder）
DEFAULTS = {"enabled": False, "base_url": "", "api_key": "", "model": ""}
SETTINGS: dict = dict(DEFAULTS)

_RATIO = re.compile(r'(?<![\d.])(\d{1,2})\s*[:：比]\s*(\d{1,2})(?![\d.])')
_SECONDS = re.compile(r'(\d{1,3})\s*(?:秒|seconds?\b|secs?\b|s\b)', re.I)
_KNOWN_RATIOS = {'16:9', '9:16', '1:1', '4:3', '3:4', '21:9', '9:21', '3:2', '2:3'}


def _valid_ratio(ratio) -> str | None:
    r = str(ratio or '').replace('：', ':').strip()
    return r if r in _KNOWN_RATIOS else None


def _spec(ratio, duration, attachments) -> str:
    parts = []
    if _valid_ratio(ratio):
        parts.append(f"画面比例 {_valid_ratio(ratio)}")
    if duration:
        parts.append(f"时长 {int(duration)} 秒")
    if attachments:
        parts.append(f"使用本次上传的 {int(attachments)} 张参考图")
    return "，".join(parts)


# dola 跨会话记忆认出同号做过同一提示词时的说法（2026-11-01：「この動画はすでに生成されています」）。
# 网页版的记忆开关回成功却存不住（acc20 / acc148 / 客户自己的号刷新后都是オン），只能在回复里说明。
_ALREADY = re.compile(r'(すでに|既に|もう)\s*(生成|作成)|(生成|作成)済み|已经?(生成|做)(过|好)|生成过|'
                      r'already\s+(been\s+)?(generated|created|made)', re.I)


def says_already_generated(text: str) -> bool:
    return bool(_ALREADY.search(text or ''))


def generic_reply(ratio=None, duration=None, attachments=0, round_no=0, dola_text=None) -> str:
    """第 round_no 次通用回复（0 起）。第二次措辞更直接，避免 dola 把两次当成同一句。"""
    spec = _spec(ratio, duration, attachments)
    if says_already_generated(dola_text):
        text = ("这是一条新的生成请求，和之前生成过的视频无关，请不要沿用之前的结果，现在重新生成一条新的视频：按上面的提示词"
                if round_no == 0 else
                "请再生成一次：之前那条不算，这次需要一条全新的视频，直接开始生成，不要再提问：按上面的提示词")
    elif round_no == 0:
        text = "请直接生成一条新的视频，不需要再确认或询问：按我上面的提示词"
    else:
        text = "这是一条新的生成请求（与之前的任何视频无关），请现在就开始生成，不要再提问：按上面的提示词"
    return f"{text}，{spec}。" if spec else f"{text}。"


def enabled() -> bool:
    s = SETTINGS
    return bool(s.get("enabled") and s.get("base_url") and s.get("api_key") and s.get("model"))


def public_settings() -> dict:
    s = dict(SETTINGS)
    key = s.pop("api_key", "") or ""
    s["api_key_set"] = bool(key)
    s["api_key_hint"] = ("••••" + key[-4:]) if len(key) >= 8 else ("已保存" if key else "")
    s["ready"] = enabled()
    return s


def apply_settings(saved: dict | None) -> dict:
    SETTINGS.clear()
    SETTINGS.update(DEFAULTS)
    for k in DEFAULTS:
        if saved and k in saved and saved[k] is not None:
            SETTINGS[k] = saved[k]
    SETTINGS["enabled"] = bool(SETTINGS.get("enabled"))
    for k in ("base_url", "api_key", "model"):
        SETTINGS[k] = str(SETTINGS.get(k) or "").strip()
    SETTINGS["base_url"] = SETTINGS["base_url"].rstrip("/")
    return SETTINGS


SYSTEM_PROMPT = (
    "你在替一个视频生成服务和 Dola（字节的 AI 助手）沟通。服务已经把生成视频的请求发给了 Dola，"
    "但 Dola 没有开始生成，而是回复了一段话（可能是日文）。你的任务：写一句简短的回复，让 Dola 立即按原始要求生成视频。\n"
    "规则：\n"
    "1. 只能确认开始、重申下面给出的参数、或者从 Dola 列出的选项里选与参数一致的那个（写出选项字母和内容）。\n"
    "2. 绝不能改变画面比例、时长、参考图或视频内容，也不要提出新的要求。\n"
    "3. 如果 Dola 说已经生成过了，就说明这是一条新的请求、请重新生成一条。\n"
    "4. 如果 Dola 是在拒绝（内容违规、肖像保护、额度不足、无法生成），不要回复，只输出 GIVE_UP。\n"
    "5. 用中文，不超过 80 个字，只输出回复正文，不要解释、不要加引号。"
)


def _user_message(dola_text, ratio, duration, attachments) -> str:
    spec = _spec(ratio, duration, attachments) or "（请求没有指定比例和参考图）"
    return f"Dola 的回复：\n{str(dola_text)[:1500]}\n\n本次请求的参数：{spec}"


def check_reply(reply: str, ratio=None, duration=None) -> str | None:
    """模型写的回复必须安全：非空、不放弃、不太长、出现的比例和秒数都与参数一致。"""
    text = str(reply or "").strip().strip('"“”「」')
    if not text or "GIVE_UP" in text.upper() or len(text) > MAX_REPLY_CHARS:
        return None
    want_ratio = _valid_ratio(ratio)
    for a, b in _RATIO.findall(text):
        r = f"{int(a)}:{int(b)}"
        if r in _KNOWN_RATIOS and r != want_ratio:
            return None
    for n in _SECONDS.findall(text):
        if duration and int(n) != int(duration):
            return None
    return text


async def ai_reply(dola_text, ratio=None, duration=None, attachments=0, *, session=None) -> str | None:
    """调用兼容 OpenAI 的 /v1/chat/completions 写一句回复；未配置、出错或不安全时返回 None。"""
    if not enabled():
        return None
    import aiohttp
    base = SETTINGS["base_url"]
    url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
    body = {"model": SETTINGS["model"], "temperature": 0.2, "max_tokens": 200,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": _user_message(dola_text, ratio, duration, attachments)}]}
    headers = {"Authorization": f"Bearer {SETTINGS['api_key']}", "Content-Type": "application/json"}
    own = session is None
    sess = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AI_TIMEOUT))
    try:
        async with sess.post(url, data=json.dumps(body), headers=headers) as resp:
            if resp.status != 200:
                print(f"[responder] AI 代答接口 HTTP {resp.status}", flush=True)
                return None
            data = await resp.json(content_type=None)
        text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        return check_reply(text, ratio, duration)
    except Exception as exc:  # noqa: BLE001
        print(f"[responder] AI 代答失败: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        return None
    finally:
        if own:
            await sess.close()
