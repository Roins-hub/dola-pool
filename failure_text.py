"""把上游（dola）的拒绝原文整理成可读的失败说明，用于任务报错字段与账号备注。

线上实测的上游话术（2026-09-22/23）：

- 肖像保护：
  「出于肖像保护考虑，未认证人脸暂不支持用 Dreamina Seedance 2.5 生成视频。你可以尝试换其它参考图或文生视频。」
- 内容审核：
  「内容审核不合规」「涉嫌违规」「请更换参考图后再试」等
- 版权/侵权：
  「涉嫌侵权」「未授权使用他人形象/作品」
- 时长协商（不算审核，但属于"上游要你确认"）：
  「单条视频目前支持 4–15 秒…请确认生成方式」

历史问题：这些原话要么被截断（120/300/500 字），要么被提示词回显和重复片段淹没，
运营在面板上看不到"到底是什么原因被拒"。本模块负责：

1. `split_fragments()`：按 ` | ` 拆开（协议层用 ` | ` 拼接多次失败原因）；
2. `clean()`：丢掉提示词回显等噪音片段、去重、压平换行与多余空白；
3. `label_for()` / `summarize()`：识别分类（肖像保护 / 内容审核 / 版权侵权 / 参考图…）
   并生成「【上游·肖像保护】<原文>」这样的可读文本；
4. `account_note_line()`：写进账号「备注」的一行（带时间与任务号）。
"""
from __future__ import annotations

import re
import time

# 有序：先匹配更具体的（肖像保护、版权），再落到泛化的内容审核
LABEL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("肖像保护", ("肖像保护", "未认证人脸", "人脸认证", "肖像权")),
    ("版权/侵权", ("侵权", "版权", "盗用", "未授权使用", "知识产权")),
    ("内容审核", ("内容审核", "审核不", "不合规", "违规", "违反社区", "内容规范",
                  "敏感", "涉黄", "暴力", "政治", "擦边")),
    ("参考图问题", ("更换参考图", "换张参考图", "参考图不", "上传的图片")),
    # 上游说法带 markdown（**4–15 秒**）和不同破折号（4–15 / 4-15 / 4到15），
    # 所以关键词按「去空格 + 破折号归一」后再匹配（见 _variants）。
    ("时长协商", ("单条视频", "仅支持 4-15", "支持 4-15", "4-15", "4到15", "拆成两段", "拆成2段",
                  "拆成 2 条", "确认后我直接生成")),
    ("限流", ("当前服务访问频繁", "710022002", "请稍后重试", "出了点问题")),
)

# 这些片段是提示词回显/回执噪音，不该占满报错
_ECHO_PREFIXES = ("生成视频：", "生成视频:", "本次使用", "我将按", "我可以按")
_ECHO_MARKERS = ("动态分镜脚本", "分镜脚本：", "角色参考：", "场景参考：")
_MAX_FRAGMENT_CHARS = 300

# 上游文案里的破折号有 - – — － 等多种，markdown 还会夹 ** 和空格。
_DASH_RE = re.compile(r"[\u2010-\u2015\u2212\uff0d]")


def _variants(text: str) -> tuple[str, ...]:
    """返回三种形态用于关键词匹配：原文 / 破折号归一 / 再去掉 markdown 与空格。"""
    raw = str(text or "")
    dashed = _DASH_RE.sub("-", raw)
    return raw, dashed, dashed.replace("*", "").replace(" ", "")


def _hit(text: str, keywords: tuple[str, ...]) -> bool:
    variants = _variants(text)
    for key in keywords:
        if key in variants[0] or key in variants[1]:
            return True
        if key.replace(" ", "") in variants[2]:
            return True
    return False


def split_fragments(text: str) -> list[str]:
    """按 ` | ` 拆分（协议层拼接格式），并清掉空白片段。"""
    if not text:
        return []
    parts = [p.strip() for p in str(text).split("|")]
    return [p for p in parts if p]


def _is_noise(fragment: str) -> bool:
    if len(fragment) > _MAX_FRAGMENT_CHARS:
        return True
    if fragment.startswith(_ECHO_PREFIXES):
        return True
    return any(marker in fragment for marker in _ECHO_MARKERS)


def _flatten(text: str) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s{2,}", " ", text).strip()


def clean(text: str, *, limit: int = 1500) -> str:
    """去掉噪音片段与重复，压平空白，按 limit 截断。"""
    fragments = []
    for fragment in split_fragments(text):
        flat = _flatten(fragment)
        if not flat or _is_noise(flat) or flat in fragments:
            continue
        fragments.append(flat)
    joined = " | ".join(fragments) if fragments else _flatten(str(text or ""))
    return joined[:limit]


def label_for(text: str) -> str:
    """识别失败分类；识别不到返回空串。"""
    if not text:
        return ""
    for label, keywords in LABEL_RULES:
        if _hit(text, keywords):
            return label
    return ""


def upstream_reason(text: str, label: str = "") -> str:
    """取出最像"上游原话"的那一段（命中分类关键词的第一段）。"""
    fragments = [f for f in split_fragments(text) if not _is_noise(_flatten(f))]
    if label:
        for label_name, keywords in LABEL_RULES:
            if label_name != label:
                continue
            for fragment in fragments:
                if _hit(fragment, keywords):
                    return _trim_tech_prefix(_flatten(fragment), keywords)
    return _flatten(fragments[0]) if fragments else _flatten(str(text or ""))


# 技术前缀长这样：「连续 3 次生成均失败（依次尝试账号: …）: acc100 出片未成功 status=failed: 」
_TECH_HEAD_RE = re.compile(r"(status=\w+|出片未成功|失败|尝试账号|acc\d+)")
_DELIM_RE = re.compile(r"[:：]\s*")


def _trim_tech_prefix(fragment: str, keywords: tuple[str, ...]) -> str:
    """切掉「acc100 出片未成功 status=failed: 」这类技术前缀，只留上游原话。

    在关键词前的最后一个冒号处切，且只在冒号前确实是技术前缀（含 status=/失败/账号名）时切，
    这样「出于肖像保护考虑…」的"出于"不会被切掉，「提示：此图片涉及肖像保护」也不会被切碎。
    """
    positions = [fragment.find(key) for key in keywords]
    positions = [pos for pos in positions if pos > 0]
    if not positions:
        return fragment
    head = fragment[:min(positions)]
    matches = list(_DELIM_RE.finditer(head))
    if not matches:
        return fragment
    last = matches[-1]
    if not _TECH_HEAD_RE.search(head[:last.start()]):
        return fragment
    return fragment[last.end():].strip() or fragment


def _strip_all(text: str, part: str) -> str:
    """从 text 里去掉所有 part（去重后的报错常把同一句原话带多份）。"""
    if not part or part not in text:
        return text
    text = text.replace(part, " ")
    text = re.sub(r"(?:\s*\|\s*)+", " | ", text)
    return text.strip(" |｜、，,：: \t")


def summarize(text: str, *, limit: int = 1500) -> str:
    """生成写进任务报错字段的文本。

    命中审核类分类时输出两行：第一行是「【上游·分类】<上游原话>」（面板一眼能看懂），
    第二行是「技术细节：<清理后的完整报错>」（换号顺序、status 等，便于排查）。
    """
    raw = str(text or "")
    cleaned = clean(raw, limit=limit)
    label = label_for(raw)
    if not label:
        return cleaned
    if cleaned.startswith(f"【上游·{label}】"):
        return cleaned
    reason = upstream_reason(raw, label)
    head = f"【上游·{label}】{reason}"
    rest = _strip_all(cleaned, reason)
    if rest and rest != reason:
        head = f"{head}\n技术细节：{rest}"
    return head[:limit]


def account_note_line(label: str, text: str, task_id: str = "",
                      when: float | None = None) -> str:
    """写进账号「备注」的一行：时间 + 分类 + 上游原话（+ 任务号）。"""
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(when or time.time()))
    reason = upstream_reason(text, label)[:200]
    tail = f"（任务 {task_id}）" if task_id else ""
    return f"[{stamp}] {label}：{reason}{tail}"


def needs_account_note(text: str) -> bool:
    """只有审核类失败才需要写账号备注（限流/时长协商这些不写，别污染人工备注）。"""
    return label_for(text) in ("肖像保护", "版权/侵权", "内容审核")
