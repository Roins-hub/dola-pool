"""豆包（doubao.com）号的纯 API 出片。

协议与 dola 同一套（见 protocol/doubao_site.py），但对话流程不一样，所以单独一条路径，
dola 那边的 30 秒拼接 / 时长吸附 / 额度点数一概不碰：

1. 提交「生成视频：<提示词>，<N>s」+ chat_ability（模型/比例/时长）。豆包从文字里读时长。
2. 豆包先回「请先确认参数……确认后我再开始生成视频」→ 核对时长、模型都对，回「确认生成」。
3. 免费额度用完时再问一次「今日视频生成免费额度已用完，本次将消耗付费额度。是否继续生成？」
   → DOUBAO_ALLOW_PAID 开着就再回「确认生成」，关着就判这个号今天额度用完、换号。
4. 免费号请求会员模型（2.0 标准版 / 2.5）时回「当前无法使用 Seedance 2.5 模型……是否改用 Mini？」
   → **绝不确认**（会悄悄降级），抛 DoubaoModelLockedError，号池记下这个号不是会员、换号。
5. 出片后用官方接口 /creativity/resource/get_without_watermark 取无水印原片下载。

以上话术均为 2026-10-18 实测（会员号 + 免费号各一个）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import config
import dola_responder
import sites
from account_transport import redact_error
from generation_errors import UpstreamReplyError, VideoSkillUnavailableError
from upstream_reply import reply_failure
from pure_api_gen import (
    PureApiError, PureLoginExpired,
    UnsupportedDurationError, _emit_note_text, _emit_upstream_note, _load_state,
    _media_duration, _progress_heartbeat, _timeout_for_upstream_wait,
    _wait_seconds_from_texts, clip_is_short,
)

log = logging.getLogger("dola.doubao")

CONFIRM_REPLY = "确认生成"
PAID_SCENE = "gen_video_by_downgraded_model"
LOGIN_EXPIRED_CODES = ("710012001",)


class DoubaoQuotaError(PureApiError):
    """豆包号今天出不了片了：免费额度用完且（没开付费 / 付费额度也没有）。号池按「每日上限」处理。"""


class DoubaoModelLockedError(PureApiError):
    """这个号用不了请求的模型（免费号请求 2.0 标准版 / 2.5）。号池记下「非会员」，换号重派。"""


class DoubaoPlanUnavailableError(PureApiError):
    """上游明确拒绝当前套餐的创作权限；暂停该号，不当作登录失效或等待出片。"""


def _site():
    from protocol import doubao_site
    from pure_signer import install_site
    module = doubao_site.load()
    install_site(module, doubao_site.SITE, doubao_site.SIGNER_ENV)
    return module


def _session(dp, state_file, proxy: str):
    from protocol import doubao_site
    session, ctx, headers = dp.setup_session(region=doubao_site.REGION, pc_version=doubao_site.PC_VERSION,
                                             proxy=proxy or "", login_pure=True, login_state_file=str(state_file))
    stage = "身份初始化"
    try:
        data = dp.dola_api_post_json(session, ctx, "/alice/user/get_web_anon_id", {},
                                     label="doubao_session_identity", referer=f"{dp.BASE}/chat/")
        if str(data.get("code")) in LOGIN_EXPIRED_CODES:
            raise PureLoginExpired("豆包登录态已失效，尚未提交生成")
        dp.assert_ok(data, "doubao_session_identity")
        identity = data.get("data") if isinstance(data.get("data"), dict) else data
        dp.apply_anon_id_to_ctx(ctx, identity)
        if identity.get("device_id"):
            ctx["device_id"] = str(identity["device_id"])
        stage = "会话启动"
        launch = dp.dola_api_post_json(session, ctx, "/alice/user/launch", {},
                                       label="doubao_session_launch", referer=f"{dp.BASE}/chat/")
        if str(launch.get("code")) in LOGIN_EXPIRED_CODES:
            raise PureLoginExpired("豆包登录态已失效，尚未提交生成")
        dp.assert_ok(launch, "doubao_session_launch")
        session._doubao_launch = launch
        stage = "保存账号上下文"
        state_path = Path(state_file)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        saved = state.get("ctx") if isinstance(state.get("ctx"), dict) else {}
        state["ctx"] = saved
        saved.update({key: ctx[key] for key in ("fp", "web_id", "tea_uuid", "device_id") if ctx.get(key)})
        # Keep an interrupted write from truncating the account's imported login state.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state_path.parent,
                                         prefix=".cookie_state_", suffix=".tmp", delete=False) as saved_file:
            temp_path = Path(saved_file.name)
            try:
                temp_path.chmod(0o600)
                saved_file.write(json.dumps(state, ensure_ascii=False, indent=2))
            except BaseException:
                saved_file.close()
                temp_path.unlink(missing_ok=True)
                raise
        try:
            os.replace(temp_path, state_path)
        finally:
            temp_path.unlink(missing_ok=True)
        return session, ctx, headers
    except PureLoginExpired:
        session.close()
        raise
    except Exception as exc:
        session.close()
        raise PureApiError(f"豆包{stage}失败（{type(exc).__name__}），尚未提交生成") from None


# ---------------------------------------------------------------- 会话解析

def _messages(chain: Any) -> list[dict]:
    """/im/chain/single 回包 → 按会话序号排好的消息（只取用得到的字段）。"""
    found: dict[str, dict] = {}

    def walk(obj):
        if isinstance(obj, dict):
            if "index_in_conv" in obj and "message_id" in obj and isinstance(obj.get("content_block"), list):
                found[str(obj["message_id"])] = obj
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(chain)
    out = []
    for raw in found.values():
        texts: list[str] = []
        buttons: list[dict] = []
        vids: list[str] = []
        generating = False
        finished = True
        for block in raw.get("content_block") or []:
            if not isinstance(block, dict):
                continue
            if block.get("is_finish") is False:
                finished = False
            content = block.get("content") or {}
            text = (content.get("text_block") or {}).get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
            button = content.get("button_block")
            if isinstance(button, dict):
                buttons.append(button)
            creation = content.get("creation_block")
            if isinstance(creation, dict):
                generating = True
                for item in creation.get("creations") or []:
                    vid = ((item or {}).get("video") or {}).get("vid")
                    if isinstance(vid, str) and vid and vid not in vids:
                        vids.append(vid)
        try:
            index = int(raw.get("index_in_conv") or 0)
        except (TypeError, ValueError):
            index = 0
        ext = raw.get("ext") if isinstance(raw.get("ext"), dict) else {}
        out.append({
            "index": index,
            "bot": str(raw.get("user_type")) == "2",
            "text": "\n".join(texts),
            "buttons": buttons,
            "vids": vids,
            "finished": finished and str(ext.get("is_finish", "1")).lower() in {"1", "true"},
            # 哪个智能体回的：视频创作是 Agent-Creation（带 use_creation），普通对话是 seed_main
            "agent_intention": str(ext.get("agent_intention") or ""),
            "agent_name": str(ext.get("agent_name") or ""),
            "creation": bool(str(ext.get("use_creation") or "") == "1" or str(ext.get("has_video_gen") or "") == "1"
                             or str(ext.get("agent_name") or "") == "Agent-Creation"),
            "generating": generating or str(ext.get("has_video_gen") or "") == "1",
            "references": sum(len((block.get("content") or {}).get("attachment_block", {}).get("attachments") or [])
                              for block in raw.get("content_block") or [] if isinstance(block, dict)),
        })
    out.sort(key=lambda m: m["index"])
    return out


_RECOVER_RE = re.compile(r"预计\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*恢复")


def quota_recover_at(text: str, now: float | None = None) -> float | None:
    """豆包额度用完时给的恢复时刻（北京时间）→ 时间戳；没说就返回 None。

    2026-11-03 体验套餐原话：「近 7 天额度已用完，预计11月5日 01:59 恢复。升级到标准套餐畅享更多额度。」
    额度按 7 天算，不是每天零点重置，所以要锁到它说的时刻。
    """
    m = _RECOVER_RE.search(text or "")
    if not m:
        return None
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001 - 没有 tzdata 时用固定 +8
        tz = _dt.timezone(_dt.timedelta(hours=8))
    now = time.time() if now is None else now
    year = _dt.datetime.fromtimestamp(now, tz).year
    month, day, hour, minute = (int(x) for x in m.groups())
    try:
        at = _dt.datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()
        if at < now - 86400:               # 跨年：12 月底说「1月2日恢复」
            at = _dt.datetime(year + 1, month, day, hour, minute, tzinfo=tz).timestamp()
    except ValueError:
        return None
    return at


def _compact(text: str) -> str:
    return re.sub(r"[\s*]+", "", text or "")


def looks_like_model_locked(text: str) -> bool:
    c = _compact(text)
    return "无法使用" in c and ("模型" in c or "seedance" in c.lower())


def plain_chat_reply(message: dict) -> bool:
    """普通助手的最终文字没有调用视频技能，权限和等待话术均不作创作回执。"""
    return bool(message.get("finished") and message.get("text") and not message.get("creation")
                and not message.get("generating") and (message.get("agent_name") == "豆包"
                    or message.get("agent_intention") == "seed_main"))


def reference_failure(text: str) -> str:
    """A required image was rejected, unavailable, or explicitly dropped from creation."""
    value = _compact(text)
    # Questions and conditional advice are not a report that the supplied image failed.
    value = re.sub(r"(?:如果|若|假如|当(?!前)).{0,60}?(?:[，,。；;]|$)", "", value)
    value = re.sub(r"(?:不会|并未|没有|无需|不要|避免|防止).{0,8}(?:放弃|忽略|不使用|不参考|丢失|失效|无法读取)", "", value)
    patterns = (
        r"(?:参考图|参考图片|图片资源|图像资源|上传的图片|参考素材).{0,24}(?:无法读取|不可读取|无法访问|不可用|已失效|失效素材|不存在|未找到|丢失|加载失败)",
        r"(?:无法读取|无法访问|无法使用|无法识别|未能读取|未能访问).{0,16}(?:参考图|参考图片|上传的图片|图片资源|参考素材)",
        r"(?:不再引用|不使用|不参考|放弃|忽略).{0,12}(?:参考图|参考图片|上传的图片|失效素材)",
        r"(?:改为|改成|先按|直接按|仅按).{0,12}(?:纯文字|纯文本|文字设定|文字描述).{0,12}生成",
    )
    return text[-1200:] if any(re.search(pattern, value) for pattern in patterns) else ""


def wants_creation_confirmation(text: str) -> bool:
    value = _compact(text)
    return (not re.search(r"(?:无法|不能|没办法|不支持).{0,12}(?:生成|导出).{0,6}视频", value)
            and bool(re.search(r"(?:要不要|是否|需要我|可以.{0,8}直接).{0,24}生成.{0,8}视频(?!提示词|脚本|方案)", value)))


def creation_reference_error(messages: list[dict], has_images: bool) -> str:
    if not has_images:
        return ""
    return next((reference_failure(m.get("text") or "") for m in reversed(messages)
                 if m.get("bot") and m.get("finished") and reference_failure(m.get("text") or "")), "")


def video_ratio_error(video_model: str, ratio: str | None) -> str:
    if not ratio or not re.fullmatch(r"\d{1,2}:\d{1,2}", ratio):
        return ""
    try:
        import video_link
        meta = video_link.meta_from_model(video_model, None, None, ratio)
        width, height = meta.get("video_width"), meta.get("video_height")
        left, right = map(int, ratio.split(":"))
        if width and height and right and abs(width / height - left / right) > (left / right) * .02:
            return f"实际视频为 {width}×{height}，画幅与请求 {ratio} 不符，已保留成片，未补做"
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return ""


def looks_like_plan_unavailable(text: str) -> bool:
    c = _compact(text)
    return bool(re.search(r"(?:套餐|订阅|权益).{0,20}(?:不可|不能|不支持|无法).{0,12}(?:创作|生成视频|视频生成)", c))


def looks_like_paid_prompt(text: str, buttons: list[dict] | None = None) -> bool:
    c = _compact(text)
    if any(str((b or {}).get("scene")) == PAID_SCENE for b in buttons or []):
        return True
    return "付费额度" in c and ("是否继续" in c or "是否" in c) and "将消耗付费额度，并" not in c


def looks_like_accepted(text: str) -> bool:
    c = _compact(text)
    return any(k in c for k in ("视频生成已提交", "本次生成将消耗", "将消耗付费额度，并", "视频生成好后",
                                "我会主动发送给你", "本次使用Seedance"))


def looks_like_confirm_request(text: str) -> bool:
    c = _compact(text)
    return any(k in c for k in ("确认后我再开始", "请先确认", "参数确认", "请确认"))


QUOTA_EXHAUSTED_MARKERS = (
    "额度已用完", "额度不足", "次数已用完", "今日已达上限",
)


def looks_like_quota_exhausted(text: str) -> bool:
    c = _compact(text)
    if looks_like_paid_prompt(text) or looks_like_accepted(text):
        return False
    return any(k in c for k in QUOTA_EXHAUSTED_MARKERS)


_DURATION_RE = re.compile(r"(?:时长\s*[：:]?\s*|生成\s*(?:一(?:条|段))?\s*)(\d{1,2})\s*(?:秒|s\b)", re.I)
_MODEL_RE = re.compile(r"(?:模型[：:]\s*|(?:改用|使用|用)\s*)(Seedance\s*[\d.]+(?:\s*(?:Mini|Fast))?)", re.I)


def confirm_mismatch(text: str, *, duration: int, model_display: str, ratio: str | None = None) -> str:
    """确认文案里的参数和请求不一致时返回说明（空串 = 一致或读不出）。"""
    c = re.sub(r"\*+", "", text or "")
    m = _DURATION_RE.search(c)
    if m and int(m.group(1)) != int(duration):
        return f"豆包把时长改成了 {m.group(1)} 秒（请求 {duration} 秒）"
    m = _MODEL_RE.search(c)
    if m and model_display:
        said = re.sub(r"\s+", " ", m.group(1)).strip().lower()
        if said != model_display.lower():
            return f"豆包把模型改成了 {m.group(1).strip()}（请求 {model_display}）"
    ratios = re.findall(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)", c)
    if ratio and re.fullmatch(r"\d{1,2}:\d{1,2}", ratio):
        candidates = {f"{int(left)}:{int(right)}" for left, right in ratios}
        if candidates.intersection({"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}) - {ratio}:
            return f"豆包确认的画幅与请求 {ratio} 不符"
    return ""


# ---------------------------------------------------------------- 上游接口

def _keep_reply_text(dp, text: str):
    """视频模式会把文字改写成「生成视频：…，比例，时长」；确认只发原话（输入框原文也是原话）。"""
    def hook(payload):
        for message in payload.get("messages") or []:
            for block in message.get("content_block") or []:
                text_block = (block.get("content") or {}).get("text_block")
                if isinstance(text_block, dict):
                    text_block["text"] = text
        ability = payload.get("chat_ability")
        if isinstance(ability, dict) and ability.get("ability_param"):
            param = json.loads(ability["ability_param"])
            if isinstance(param.get("input_box_content"), dict):
                param["input_box_content"]["user_input_content"] = text
            ability["ability_param"] = dp.compact_json(param)
    return hook


def _reply(dp, session, ctx, headers, conversation_id: str, text: str = CONFIRM_REPLY, *,
           model: str = "", duration: int = 5, ratio: str | None = None):
    """在会话里回一句话。

    给了 model（视频任务的确认 / 付费确认）时按「视频生成」技能发：带和首条消息同样的 chat_ability。
    2026-11-03 实测：不带技能的「确认生成」被豆包普通对话（seed_main）接走，它嘴上说「已确认，开始生成」，
    视频创作智能体（Agent-Creation）却没收到，永远不出片。
    """
    try:
        chain = dp.fetch_single_chain(session, ctx, conversation_id)
    except Exception as exc:
        raise TimeoutError(f"豆包确认前读取原会话失败（{type(exc).__name__}），保留原会话核对，未重复提交") from None
    result = dp.submit_chat_completion(
        session, ctx, headers, images=[], audios=[], prompt=text, duration=duration if model else 5,
        model=model, ratio=ratio, timeout_sec=180, video_ability=bool(model), conversation_id=conversation_id,
        section_id=dp.find_latest_section_id(chain) or "",
        last_message_index=dp.find_latest_message_index(chain),
        payload_hook=_keep_reply_text(dp, text) if model else None)
    if getattr(result, "error", ""):
        raise TimeoutError("豆包确认回复未取得有效回执，保留原会话核对，未重复提交")
    return result


def nowatermark_url(dp, session, ctx, vid: str) -> tuple[str, float | None]:
    """官方无水印下载地址（网页「下载」按钮用的同一个接口）。"""
    data = dp.dola_api_post_json(session, ctx, "/creativity/resource/get_without_watermark",
                                 {"vid": [vid]}, label="get_without_watermark",
                                 referer=f"{dp.BASE}/chat/")
    info = (((data.get("data") or {}).get("download_video") or {}).get(vid) or {})
    url = str(info.get("download_url") or "")
    try:
        seconds = float(info.get("duration")) if info.get("duration") is not None else None
    except (TypeError, ValueError):
        seconds = None
    return url, seconds


def _download(dp, session, ctx, conversation_id: str, vid: str, chain: Any, out_path: Path) -> tuple[Path, str, bool]:
    """先取无水印原片；拿不到就退回会话里的带水印地址（并在结果里注明）。"""
    url, unwatermarked = "", True
    try:
        url, _ = nowatermark_url(dp, session, ctx, vid)
    except Exception as exc:  # noqa: BLE001
        log.warning("doubao 无水印地址获取失败 %s: %s", vid, redact_error(exc, limit=200))
    if not url:
        unwatermarked = False
        urls = dp.find_video_urls(chain)
        url = urls[0] if urls else ""
    if not url:
        raise PureApiError(f"豆包出片了（vid={vid}），但拿不到下载地址")
    saved = dp.download_video(url, str(out_path), referer=f"{dp.BASE}/chat/{conversation_id}", proxy="")
    return Path(saved), url, unwatermarked


def _post_plain(dp, session, ctx, endpoint: str, body: dict) -> dict:
    """网页对 /privacy/* 这类设置接口不加 a_bogus 签名，照抄（签名器也不认这个路径）。"""
    headers = {**dp.headers_base(f"{dp.BASE}/chat/"), **dp.ajax_headers(),
               "Accept": "application/json, text/plain, */*", "Content-Type": "application/json",
               "agw-js-conv": "str"}
    resp = dp.send_with_cookie_header(session, "POST", dp.build_url(endpoint, ctx), headers=headers,
                                      data=dp.compact_json(body), timeout=30)
    return dp.request_json(resp, endpoint)


def watermark_off(dp, session, ctx) -> bool | None:
    """这个号的「AI 生成水印管理 → 图片、视频」是不是「无水印」（读不到返回 None）。

    /creativity/user_config/get 的 config_map["1"].watermark_option.is_on：true = 无水印（2026-10-18 实测：
    关掉后出片干净；没有这一项 / false = 右下角「豆包AI生成」）。"""
    data = dp.dola_api_post_json(session, ctx, "/creativity/user_config/get", {}, label="user_config_get",
                                 referer=f"{dp.BASE}/chat/")
    config_map = (data.get("data") or {}).get("config_map")
    if not isinstance(config_map, dict):
        return None
    option = ((config_map.get("1") or {}).get("watermark_option") or {})
    return bool(option.get("is_on"))


def set_no_watermark(dp, session, ctx) -> None:
    """和网页里点「无水印」发的两条请求一样（3-test.har）：隐私设置 object=150 → 1，创作设置 is_on → true。"""
    _post_plain(dp, session, ctx, "/privacy/watermark_config/set",
                {"configs": [{"object": 150, "value": 1, "version": 0}]})
    dp.dola_api_post_json(session, ctx, "/creativity/user_config/set",
                          {"config_type": 1, "config_value": {"watermark_option": {"is_on": True}}},
                          label="user_config_set", referer=f"{dp.BASE}/chat/")


def ensure_no_watermark(dp, session, ctx, account: str = "") -> bool | None:
    """读水印设置；没关且 DOUBAO_AUTO_NO_WATERMARK 开着就替号关掉。返回最终是不是「无水印」（读不到 None）。"""
    try:
        off = watermark_off(dp, session, ctx)
    except Exception as exc:  # noqa: BLE001 - 读设置失败不影响出片
        log.info("doubao 水印设置读取失败 %s: %s", account, redact_error(exc, limit=160))
        return None
    if off or off is None or not config.DOUBAO_AUTO_NO_WATERMARK:
        return off
    try:
        set_no_watermark(dp, session, ctx)
        off = watermark_off(dp, session, ctx)
        log.info("doubao %s 已关闭 AI 生成明水印 → %s", account, "成功" if off else "未生效")
    except Exception as exc:  # noqa: BLE001
        log.warning("doubao %s 关闭水印失败: %s", account, redact_error(exc, limit=160))
    return off


def _check_login(dp, session, ctx) -> tuple[bool, str]:
    """用额度查询接口判登录（不发消息、不花额度）。返回 (已登录, 原因)。"""
    resp = dp.dola_api_post_response(session, ctx, "/alice/commerce/sale/subscription/quota/summary/",
                                     {"product_line": "membership"}, label="quota_summary",
                                     referer=f"{dp.BASE}/chat/")
    data = dp.request_json(resp, "quota_summary")
    code = str(data.get("code"))
    if code == "0":
        return True, "logged_in"
    msg = str(data.get("msg") or data.get("message") or "")
    if code in LOGIN_EXPIRED_CODES or "登录" in msg:
        return False, f"被登出（{code} {msg}）"
    raise PureApiError(f"豆包额度查询失败 {code} {msg}"[:200])


# ---------------------------------------------------------------- 对外入口

def probe_login(state_file, proxy: str = "") -> tuple[bool, str]:
    """导入 / 面板「验证」用：只读校验网页身份与额度，不提交生成。"""
    dp = _site()
    session = None
    try:
        _load_state(Path(state_file))
        session, ctx, _ = _session(dp, state_file, proxy)
        ok, reason = _check_login(dp, session, ctx)
        if ok:
            import doubao_account
            kind = doubao_account.from_file(state_file)
            try:
                if proxy:
                    data = getattr(session, "_doubao_launch", None) or dp.dola_api_post_json(
                        session, ctx, "/alice/user/launch", {}, label="account_modes")
                    kind = doubao_account.launch_type(data, kind)
                doubao_account.remember(state_file, kind)
            except Exception:  # 入口识别失败保留 Cookie 分类，不改变登录验证结果。
                pass
        return ok, reason
    except Exception as exc:  # noqa: BLE001
        return False, redact_error(exc, limit=300).replace("\n", " ")
    finally:
        if session is not None:
            session.close()


def hello_probe(state_file, proxy: str = "", **_ignored) -> dict[str, Any]:
    """派单前探测（与 dola 的「你好」探测同构）。豆包号只查登录态，不发消息：
    发「你好」会在号里多出一条对话，也没必要 —— 豆包登出时额度接口直接报 710012001。"""
    started = time.time()
    dp = _site()
    session = None
    status, login_ok, reason = "error", False, ""
    no_watermark = None
    try:
        _load_state(Path(state_file))
        session, ctx, _ = _session(dp, state_file, proxy)
        login_ok, reason = _check_login(dp, session, ctx)
        status = "ok" if login_ok else "logged_out"
        if login_ok:
            no_watermark = ensure_no_watermark(dp, session, ctx)
    except Exception as exc:  # noqa: BLE001
        reason = redact_error(exc, limit=200).replace("\n", " ") or exc.__class__.__name__
    finally:
        if session is not None:
            session.close()
    return {"ok": status == "ok", "status": status, "login_ok": login_ok, "replied": login_ok,
            "reason": "豆包登录有效" if status == "ok" else reason, "reply": "",
            "no_watermark": no_watermark, "elapsed": round(time.time() - started, 2)}


def generate_doubao_video(
    *,
    account: str,
    prompt: str,
    ratio: str | None,
    duration: int,
    model: str,
    cookie_state_path: str | Path,
    proxy: str = "",
    output_dir: str | Path = "",
    timeout: int | None = None,
    poll_interval: int | None = None,
    on_conversation_id: Callable | None = None,
    on_poll: Callable | None = None,
    on_balance: Callable | None = None,      # 豆包不报剩余条数，保留参数只为与 dola 路径同签名
    reference_image_paths: list | None = None,
) -> dict[str, Any]:
    spec = sites.doubao_spec(model)
    if spec is None:
        raise PureApiError(f"不是豆包模型: {model}")
    upstream_model, max_seconds, _member_only, display = spec
    duration = int(duration or 5)
    if not (sites.DOUBAO_MIN_DURATION <= duration <= max_seconds):
        raise UnsupportedDurationError(
            f"{model} 支持 {sites.DOUBAO_MIN_DURATION}~{max_seconds} 秒（收到 {duration} 秒）")
    timeout = int(timeout or config.PURE_API_TIMEOUT)
    poll = max(3, int(poll_interval or config.DOUBAO_POLL_INTERVAL))
    state_file = Path(cookie_state_path)
    _load_state(state_file)

    dp = _site()
    session, ctx, headers = _session(dp, state_file, proxy)
    try:
        from protocol import doubao_site
        log.info("doubao prepare account=%s model=%s duration=%s ratio=%s references=%s client=%s proxy_bound=%s",
                 account, upstream_model, duration, ratio, len(reference_image_paths or []),
                 doubao_site.PC_VERSION, bool(proxy))
        # 水印是按号的设置，出片时生效：提交前确认这个号已关（没关就替它关）
        no_watermark = ensure_no_watermark(dp, session, ctx, account)
        images: list[dict[str, Any]] = []
        for path in (dp.normalize_image_paths(reference_image_paths) if reference_image_paths else []):
            try:
                images.append(dp.upload_image(session, ctx, headers, path))
            except Exception as exc:  # noqa: BLE001
                raise PureApiError(f"参考图上传失败（{path.name}）: {redact_error(exc, limit=200)}") from None
        log.info("doubao references account=%s requested=%s uploaded=%s", account,
                 len(reference_image_paths or []), len(images))
        _emit_note_text(on_poll, f"豆包：准备提交视频创作，参考图 {len(images)} 张")
        submit = dp.submit_chat_completion(
            session, ctx, headers, images=images, audios=[], prompt=prompt, duration=duration,
            model=upstream_model, ratio=ratio, timeout_sec=180, video_ability=True)
        conversation_id = submit.conversation_id
        if not conversation_id:
            if submit.error:
                msg = redact_error(f"{submit.error_code or 'upstream'}: {submit.error}", limit=300)
                if any(k in msg.lower() for k in ("login", "登录", "未登录", "anonymous", "710012001")):
                    raise PureLoginExpired(f"{account} 豆包会话失效: {msg[:200]}")
                raise PureApiError(f"{account} 提交失败: {msg[:300]}")
            raise TimeoutError(f"{account} 提交未返回会话标识，结果待核对，未自动重发")

        submitted_at = time.time()
        deadline = submitted_at + timeout
        if on_conversation_id:
            try:
                on_conversation_id(account, conversation_id, deadline)
            except Exception:  # noqa: BLE001
                pass
        _emit_note_text(on_poll, "豆包：会话已建立，等待视频创作受理")
        if submit.error:
            raise TimeoutError(f"{account} 豆包会话已建立但回执异常，保留原会话核对，未重复提交")

        handled: set[int] = set()
        confirms = 0
        paid = False
        accepted = False
        replied_any = False
        seen_texts: list[str] = []
        chain: Any = None
        vid = ""
        reference_error = ""
        plain_seen: set[int] = set()
        creation_confirmation = False
        responder = dola_responder.DialogueResponder(
            prompt=prompt, ratio=ratio, duration=duration, attachments=len(images), model=display, site="豆包")
        with _progress_heartbeat(on_poll):
            while time.time() < deadline:
                try:
                    chain = dp.fetch_single_chain(session, ctx, conversation_id)
                except dp.DolaNoDataError as exc:
                    raise PureApiError(f"{account} 豆包会话已失效: {redact_error(exc, limit=200)}") from None
                except Exception as exc:  # noqa: BLE001
                    log.info("doubao 轮询异常 %s（继续等）: %s", account, redact_error(exc, limit=160))
                    time.sleep(poll)
                    continue
                msgs = _messages(chain)
                was_accepted = accepted
                accepted = accepted or any(m["bot"] and (m["generating"] or
                    not plain_chat_reply(m) and looks_like_accepted(m["text"])) for m in msgs)
                if accepted and not was_accepted:
                    log.info("doubao creation account=%s accepted=true references=%s", account, len(images))
                    _emit_note_text(on_poll, "豆包：视频创作已受理，等待生成")
                previous_reference_error = reference_error
                reference_error = reference_error or creation_reference_error(msgs, bool(images))
                if reference_error and not previous_reference_error:
                    _emit_upstream_note(on_poll, dp, [reference_error])
                for m in msgs:
                    if m["bot"] and m["vids"]:
                        vid = m["vids"][0]
                if vid:
                    break
                if reference_error and not accepted:
                    raise UpstreamReplyError("参考图不可用", reference_error,
                                             generation_started=False, allow_account_retry=False)
                if reference_error:
                    _emit_note_text(on_poll, "豆包：参考图未被使用，只读取回已受理任务，不再确认或补做")
                    time.sleep(poll)
                    continue
                latest = next((m for m in reversed(msgs) if m["bot"]), None)
                if latest and latest["index"] <= max((m["index"] for m in msgs if not m["bot"]), default=-1):
                    latest = None
                for m in ([latest] if latest else []):
                    if not m["bot"] or m["index"] in handled or not m["finished"] or not m["text"]:
                        continue
                    handled.add(m["index"])
                    replied_any = True
                    text = m["text"]
                    seen_texts.append(text)
                    _emit_upstream_note(on_poll, dp, [text])
                    if not accepted and plain_chat_reply(m):
                        label = reply_failure(text)
                        if label in ("内容拒绝", "肖像保护", "内容审核", "版权侵权"):
                            raise UpstreamReplyError(label, text[:1000],
                                                     generation_started=False, allow_account_retry=False)
                        if label == "需要补充信息":
                            raise UpstreamReplyError(label, text[:1000],
                                                     generation_started=False, allow_account_retry=False)
                        if confirms or creation_confirmation:
                            raise TimeoutError(
                                f"{account} 确认回复被普通对话接走，保留原会话核对，未重复提交：{text[:1000]}")
                        if any(row["creation"] or row["generating"] for row in msgs if row["bot"]):
                            _emit_note_text(on_poll, "豆包：已有创作记录，继续查询原会话")
                            continue
                        if looks_like_paid_prompt(text, m["buttons"]):
                            raise UpstreamReplyError("创作权限未确认", text[:1000],
                                                     generation_started=False, allow_account_retry=False)
                        if (wants_creation_confirmation(text) and not creation_confirmation
                                and not any(row["creation"] or row["generating"] for row in msgs if row["bot"])):
                            mismatch = confirm_mismatch(text, duration=duration, model_display=display, ratio=ratio)
                            if mismatch:
                                raise UpstreamReplyError("参数不符", mismatch, generation_started=False,
                                                         allow_account_retry=False)
                            _reply(dp, session, ctx, headers, conversation_id,
                                   model=upstream_model, duration=duration, ratio=ratio)
                            creation_confirmation = True
                            _emit_note_text(on_poll, "豆包：已确认视频创作，等待创作回执")
                            continue
                        # A completed answer offering only text has no active video task. Give
                        # a handoff one extra read, then temporarily pause this account.
                        if m["index"] not in plain_seen:
                            plain_seen.add(m["index"])
                            handled.discard(m["index"])
                            continue
                        raise VideoSkillUnavailableError(
                            f"{account} 豆包由普通对话助手回复，未进入视频创作。上游原文：{text[:1000]}")
                    if looks_like_plan_unavailable(text):
                        raise DoubaoPlanUnavailableError(f"{account} 豆包套餐不支持创作：{text[:300]}")
                    if looks_like_model_locked(text):
                        raise DoubaoModelLockedError(f"{account} 用不了 {display}（豆包：{text[:160]}）")
                    if reply_failure(text) == "内容拒绝":
                        raise UpstreamReplyError("内容拒绝", text[:1000], generation_started=accepted)
                    if looks_like_paid_prompt(text, m["buttons"]):
                        if not config.DOUBAO_ALLOW_PAID:
                            raise DoubaoQuotaError(
                                f"{account} 豆包今日免费额度已用完（未开启 DOUBAO_ALLOW_PAID，不走付费额度）")
                        if paid:
                            continue
                        _reply(dp, session, ctx, headers, conversation_id,
                               model=upstream_model, duration=duration, ratio=ratio)
                        paid = True
                        _emit_note_text(on_poll, "豆包：免费额度已用完，已确认使用付费额度")
                        continue
                    if (confirms or creation_confirmation) and plain_chat_reply(m):
                        raise TimeoutError(
                            f"{account} 确认回复被普通对话接走，保留原会话核对，未重复提交：{text[:1000]}")
                    if looks_like_accepted(text):
                        accepted = True
                        wait = _wait_seconds_from_texts(dp, [text])
                        deadline = max(deadline, submitted_at + _timeout_for_upstream_wait(timeout, wait))
                        continue
                    if looks_like_quota_exhausted(text):
                        raise DoubaoQuotaError(f"{account} 豆包额度不足：{text[:200]}")
                    if accepted and dp.looks_like_generation_failure(text):
                        raise UpstreamReplyError("上游生成失败", text[:1000], generation_started=True)
                    if looks_like_confirm_request(text) and not accepted:
                        mismatch = confirm_mismatch(text, duration=duration, model_display=display, ratio=ratio)
                        if mismatch:
                            if "模型" in mismatch:
                                raise DoubaoModelLockedError(f"{account} {mismatch}，未确认（不扣额度）")
                            raise UnsupportedDurationError(f"{mismatch}，未确认（不扣额度）")
                        if confirms >= 2:
                            raise TimeoutError(f"{account} 豆包反复要求确认，保留原会话核对：{text[:200]}")
                        _reply(dp, session, ctx, headers, conversation_id,
                               model=upstream_model, duration=duration, ratio=ratio)
                        confirms += 1
                        continue
                    if not accepted and dola_responder.enabled():
                        _emit_note_text(on_poll, "豆包：正在请 AI 判断上游回复")
                        decision = asyncio.run(responder.decide(text))
                        if decision:
                            _emit_note_text(on_poll, f"豆包 AI 判断：{dola_responder.DECISION_ACTIONS[decision['action']]}，{decision['reason']}")
                            reply = dola_responder.decision_reply(decision, text)
                            if reply:
                                _reply(dp, session, ctx, headers, conversation_id, text=reply,
                                       model=upstream_model, duration=duration, ratio=ratio)
                                _emit_note_text(on_poll, f"豆包：已 AI 代答：{reply}")
                        else:
                            raise UpstreamReplyError("上游在等待回复",
                                f"豆包未开始生成，AI 判断失败（{responder.last_error}）：{text[:800]}", generation_started=False)
                if not replied_any and time.time() - submitted_at > config.NO_ACK_SECONDS:
                    raise TimeoutError(
                        f"豆包 {config.NO_ACK_SECONDS} 秒内没有回应，保留原会话核对，未自动重发")
                time.sleep(poll if accepted else min(poll, 5))
        if not vid:
            waited = int((time.time() - submitted_at) // 60)
            raise TimeoutError(
                f"{account} 豆包等待出片超时：已等 {waited} 分钟仍未出片；豆包端可能仍在生成，"
                f"为避免重复扣额度不自动重提。豆包回执：{' | '.join(seen_texts)[-300:]}")

        import video_link
        from work_reply import video_models
        models = video_models(chain)
        video_model = next((raw for raw in models
                            if str((json.loads(raw).get("vid") or json.loads(raw).get("video_id") or "")) == str(vid)), "")
        validation_error = "; ".join(filter(None, (
            f"参考图未用于生成：{reference_error}" if reference_error else "",
            video_ratio_error(video_model, ratio))))
        if video_link.enabled():
            for vm_str in models:
                vm = json.loads(vm_str)
                if str(vm.get("vid") or vm.get("video_id") or "") != str(vid):
                    continue
                linked = asyncio.run(video_link.from_model(vm_str, account, conversation_id))
                if linked:
                    if clip_is_short(linked["video_duration"], duration):
                        validation_error = "; ".join(filter(None, (validation_error,
                            f"豆包实际生成 {linked['video_duration']:.1f} 秒（请求 {duration} 秒），已保留成片，未补做")))
                    _emit_note_text(on_poll, "豆包：视频已生成，高码率直链交付")
                    return {**linked, "duration": duration, "model": upstream_model, "pure_api": True,
                            "validation_error": validation_error,
                            "site": sites.DOUBAO, "unwatermarked": bool(no_watermark),
                            "no_watermark_setting": no_watermark, "doubao_paid": paid,
                            "credits_used": None, "credits_left": None}
                break
        out_dir = Path(output_dir or config.DOWNLOAD_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{account}_{uuid.uuid4().hex[:12]}.mp4"
        try:
            saved, url, unwatermarked = _download(dp, session, ctx, conversation_id, vid, chain, out_path)
        except Exception as exc:  # noqa: BLE001
            raise TimeoutError(f"{account} 视频已生成但交付失败，保留原会话取回，不重新生成：{redact_error(exc, limit=200)}") from None
        actual = _media_duration(saved)
        if clip_is_short(actual, duration):
            validation_error = "; ".join(filter(None, (validation_error,
                f"豆包实际生成 {actual:.1f} 秒（请求 {duration} 秒），已保留成片，未补做")))
        return {
            "local_path": str(saved),
            "account": account,
            "conversation_id": conversation_id,
            "download_url": url,
            "duration": duration,
            "model": upstream_model,
            "pure_api": True,
            "site": sites.DOUBAO,
            # 下载接口对没关水印的号也会给「无水印」地址，但画面里照样有「豆包AI生成」：以号的设置为准
            "unwatermarked": bool(unwatermarked and no_watermark),
            "no_watermark_setting": no_watermark,
            "doubao_paid": paid,
            "validation_error": validation_error,
            "credits_used": None,
            "credits_left": None,
        }
    finally:
        session.close()


def fetch_conversation_video(cookie_state_path, conversation_id: str, *, proxy: str = "",
                             output_dir: str | Path | None = None, reference_count: int = 0,
                             duration: int | None = None, ratio: str | None = None) -> dict:
    """超时任务取回：只查已有会话，出片了就下载；**不提交任何新请求、不回确认**。"""
    dp = _site()
    _load_state(Path(cookie_state_path))
    session, ctx, _ = _session(dp, cookie_state_path, proxy)
    try:
        chain = dp.fetch_single_chain(session, ctx, conversation_id)
        msgs = _messages(chain)
        reference_error = creation_reference_error(msgs, bool(reference_count or
                                                   any(m.get("references") for m in msgs if not m["bot"])))
        vid = next((m["vids"][0] for m in msgs if m["bot"] and m["vids"]), "")
        texts = [m["text"] for m in msgs if m["bot"] and m["text"]]
        detail = " | ".join(texts)[-300:]
        if vid:
            out_dir = Path(output_dir or config.DOWNLOAD_DIR)
            out_dir.mkdir(parents=True, exist_ok=True)
            saved, _url, _ = _download(dp, session, ctx, conversation_id, vid, chain,
                                       out_dir / f"recovered_{uuid.uuid4().hex[:12]}.mp4")
            from work_reply import video_models
            vm = next((raw for raw in video_models(chain)
                       if str(json.loads(raw).get("vid") or json.loads(raw).get("video_id") or "") == vid), "")
            actual = _media_duration(saved) if duration else None
            errors = [f"参考图未用于生成：{reference_error}" if reference_error else "",
                      video_ratio_error(vm, ratio)]
            if duration and clip_is_short(actual, duration):
                errors.append(f"豆包实际生成 {actual:.1f} 秒（请求 {duration} 秒），已保留成片，未补做")
            return {"status": "succeeded", "local_path": str(saved), "final": False,
                    "validation_error": "; ".join(filter(None, errors)),
                    "detail": detail, "conversation_id": conversation_id}
        latest = next((m for m in reversed(msgs) if m["bot"]), {})
        last = latest.get("text", "")
        plan_unavailable = bool(latest.get("finished") and looks_like_plan_unavailable(last))
        plain_chat = plain_chat_reply(latest) and not any(m['bot'] and (m['generating'] or
            not plain_chat_reply(m) and looks_like_accepted(m['text'])) for m in msgs)
        if plain_chat:
            plan_unavailable = False
        # 停在确认 / 付费追问 / 模型锁 / 额度 / 拒稿上：没人回话就永远不会出片（也没扣额度），不用再盯
        final = bool(reference_error) and not any(m["generating"] for m in msgs if m["bot"])
        final = final or bool(last) and (plain_chat or plan_unavailable or looks_like_model_locked(last) or looks_like_quota_exhausted(last)
                                or looks_like_paid_prompt(last) or dp.looks_like_content_refusal(last)
                                or (looks_like_confirm_request(last) and not looks_like_accepted(last)))
        return {"status": "failed" if final else "pending", "local_path": "", "final": final,
                "detail": detail, "conversation_id": conversation_id,
                "plan_unavailable": plan_unavailable}
    finally:
        session.close()
