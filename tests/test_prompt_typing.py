"""Line breaks must never reach the composer as Enter key presses (that sends half a prompt)."""
import ast
import asyncio
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(*names):
    tree = ast.parse((ROOT / "video_worker_ui.py").read_text(encoding="utf-8"))
    body = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    scope = {"re": re}
    exec(compile(ast.Module(body=body, type_ignores=[]), "worker-test", "exec"), scope)
    return scope


def test_ai_note_and_prompt_are_typed_as_one_line():
    import prompt_context
    scope = _load("single_line_prompt", "type_prompt")
    joined = prompt_context.submission_prompt("小猫在草地上奔跑", "素材来源说明：参考图由 AI 生成。")
    typed = []

    class Keyboard:
        async def type(self, text, delay=0): typed.append((text, delay))

    class Page:
        keyboard = Keyboard()

    asyncio.run(scope["type_prompt"](Page(), joined))
    assert typed == [("素材来源说明：参考图由 AI 生成。 小猫在草地上奔跑", 40)]


def test_user_line_breaks_and_crlf_collapse_to_single_spaces():
    single = _load("single_line_prompt")["single_line_prompt"]
    assert single("镜头一：日出\r\n\r\n  镜头二：飞鸟\n镜头三") == "镜头一：日出 镜头二：飞鸟 镜头三"
    assert single("  无换行  ") == "无换行"
    assert single(None) == ""
    assert "\n" not in single("a\n\n\nb") and "\r" not in single("a\rb")
