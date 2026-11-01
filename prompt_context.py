"""Optional caller-declared reference provenance, separate from the user's prompt."""

AI_REFERENCE_NOTE = (
    "素材来源说明：参考图中的人物完全由 AI 生成，是虚构角色，不对应任何现实人物。"
)


def reference_supplement(source: str, has_images: bool) -> str:
    if source not in ("unspecified", "ai_generated"):
        raise ValueError("reference_source 仅支持 unspecified 或 ai_generated")
    if source == "ai_generated":
        if not has_images:
            raise ValueError("reference_source=ai_generated 需要同时提供参考图")
        return AI_REFERENCE_NOTE
    return ""


def submission_prompt(prompt: str, supplement: str = "") -> str:
    return f"{supplement}\n\n{prompt}" if supplement else prompt


_RATIO_SHAPE = {"16:9": "横幅", "21:9": "横幅", "4:3": "横幅", "3:2": "横幅",
                "9:16": "竖幅", "9:21": "竖幅", "3:4": "竖幅", "2:3": "竖幅", "1:1": "方形"}


def ratio_note(prompt: str, ratio) -> str:
    """提示词里写的比例和请求参数不一致时，追加一句「以参数为准」——客户端参数说了算。

    2026-11-01：参数 16:9、提示词「9:16竖幅」，dola 有时按提示词出竖幅、有时反问用哪个。
    只在两者确实冲突时追加；提示词没写比例或与参数一致时不动。
    """
    from upstream_reply import prompt_ratio, _RATIO_IN_TEXT, _KNOWN_RATIOS, _PORTRAIT_WORDS, _LANDSCAPE_WORDS
    want = str(ratio or "").replace("：", ":").strip()
    if want not in _RATIO_SHAPE:
        return ""
    text = str(prompt or "")
    mentioned = {f"{a}:{b}" for a, b in _RATIO_IN_TEXT.findall(text) if f"{a}:{b}" in _KNOWN_RATIOS}
    shape = _RATIO_SHAPE[want]
    conflict = bool(mentioned - {want}) or (shape != "竖幅" and _PORTRAIT_WORDS.search(text)) \
        or (shape != "横幅" and _LANDSCAPE_WORDS.search(text))
    if not conflict and prompt_ratio(text) in (None, want):
        return ""
    return f"【画面比例】以 {want}（{shape}）为准，忽略上文中的其他比例描述。"


def dispatch_prompt(prompt: str, supplement: str = "", ratio=None) -> str:
    """真正发给 dola 的文字：素材说明在前，比例说明在后，客户原文不改。"""
    text = submission_prompt(prompt, supplement)
    note = ratio_note(prompt, ratio)
    return f"{text}\n\n{note}" if note else text
