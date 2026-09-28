"""失败文案整理：上游拒稿原文要完整、可读地落到任务报错与账号备注。

线上真实样本（tasks.db，2026-09-23 抓取）：
    连续 3 次生成均失败（依次尝试账号: acc1、acc10、acc100）: acc100 出片未成功
    status=failed: 出于肖像保护考虑，未认证人脸暂不支持用 Dreamina Seedance 2.5 生成视频。
    你可以尝试换其它参考图或文生视频。 | <同一句重复 2 遍> | 生成视频：出场角色 …<提示词回显>
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import failure_text as ft


PORTRAIT = (
    "出于肖像保护考虑，未认证人脸暂不支持用 Dreamina Seedance 2.5 生成视频。"
    "你可以尝试换其它参考图或文生视频。"
)
PROMPT_ECHO = "生成视频：出场角色\n萧烬：清俊冷峻，黑发半束，玄色战甲染血。" + "三" * 400
REAL_PORTRAIT_ERROR = (
    "连续 3 次生成均失败（依次尝试账号: acc1、acc10、acc100）: acc100 出片未成功 "
    f"status=failed: {PORTRAIT} | {PORTRAIT} | {PORTRAIT} | {PROMPT_ECHO}"
)


def test_label_detection():
    assert ft.label_for(PORTRAIT) == "肖像保护"
    assert ft.label_for("你的输入内容审核不合规，请修改后重试") == "内容审核"
    assert ft.label_for("该参考图涉嫌侵权，未授权使用他人形象") == "版权/侵权"
    assert ft.label_for("acc9 提交失败: 710022002: 当前服务访问频繁，请稍后重试") == "限流"
    assert ft.label_for("acc9 出片未成功 status=rate_limited: 出了点问题，请稍后重试。") == "限流"
    # 上游时长话术带 markdown 与 en-dash，关键词要能命中（线上 88 条漏标的原因）
    duration = "acc9 出片未成功 status=duration_split: 抱歉，视频生成目前支持 **4–15 秒**，不支持 30 秒完整长视频。"
    assert ft.label_for(duration) == "时长协商"
    assert ft.label_for("acc9 出片未成功 status=duration_split: 视频生成目前支持4到15秒的时长。我可以按你提供的分镜内容，压缩生成一个15秒竖版视频。") == "时长协商"


def test_clean_drops_prompt_echo_and_duplicates():
    cleaned = ft.clean(REAL_PORTRAIT_ERROR)
    assert "三三三" not in cleaned          # 提示词回显被丢弃
    # 原报错里同一句话拼了 3 遍，去重后只剩：带技术前缀的 1 段 + 裸原话 1 段
    assert len(ft.split_fragments(cleaned)) == 2


def test_summarize_puts_upstream_reason_first():
    out = ft.summarize(REAL_PORTRAIT_ERROR)
    first, _, rest = out.partition("\n")
    assert first == f"【上游·肖像保护】{PORTRAIT}"
    assert "acc1、acc10、acc100" in rest     # 换号信息保留在「技术细节」里
    assert "status=failed" in rest
    assert len(out) < 400                    # 原报错 500+ 字被压成可读的两行


def test_summarize_without_label_keeps_original_text():
    raw = "acc9 提交失败: 上游未返回 conversation_id"
    assert ft.summarize(raw) == raw


def test_summarize_limits_label_but_keeps_detail():
    """限流这类短话术也标上分类，且不写账号备注（needs_account_note 仍为 False）。"""
    raw = "连续 3 次生成均失败（依次尝试账号: acc107、acc108）: acc109 提交失败: 710022002: 当前服务访问频繁，请稍后重试"
    out = ft.summarize(raw)
    # 上游错误码 710022002 保留（面板上一眼能对上号），只切掉「账号 + 提交失败」这类技术前缀
    assert out.splitlines()[0] == "【上游·限流】710022002: 当前服务访问频繁，请稍后重试"
    assert "acc107、acc108" in out
    assert ft.needs_account_note(raw) is False


def test_summarize_limit_respected():
    assert len(ft.summarize(REAL_PORTRAIT_ERROR, limit=80)) == 80


def test_account_note_line():
    when = time.mktime(time.strptime("2026-09-23 01:05", "%Y-%m-%d %H:%M"))
    line = ft.account_note_line("肖像保护", REAL_PORTRAIT_ERROR, "video_abc123", when)
    assert line.startswith("[2026-09-23 01:05]")
    assert "肖像保护：" in line
    assert PORTRAIT in line
    assert line.endswith("（任务 video_abc123）")
    # 技术前缀（账号名 / status=）不该出现在备注里
    assert "status=failed" not in line


def test_needs_account_note_only_for_audit_rejections():
    assert ft.needs_account_note(REAL_PORTRAIT_ERROR) is True
    assert ft.needs_account_note("涉嫌侵权，未授权使用他人形象") is True
    assert ft.needs_account_note("710022002: 当前服务访问频繁") is False
    assert ft.needs_account_note("单条视频目前支持 4-15 秒，请确认生成方式") is False


def test_tech_prefix_trim_keeps_leading_words():
    """「出于肖像保护考虑」的"出于"不能被当成技术前缀切掉。"""
    assert ft.upstream_reason(REAL_PORTRAIT_ERROR, "肖像保护") == PORTRAIT
    assert ft.upstream_reason(
        "acc7 出片未成功 status=failed: 你的输入内容审核不合规，请修改后重试。",
        "内容审核",
    ) == "你的输入内容审核不合规，请修改后重试。"
    # 上游原话自带冒号时不该被切碎
    assert ft.upstream_reason("提示：此图片涉及肖像保护，请更换参考图", "肖像保护") == \
        "提示：此图片涉及肖像保护，请更换参考图"