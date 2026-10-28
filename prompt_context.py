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
