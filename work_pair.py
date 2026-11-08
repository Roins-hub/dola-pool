"""2.0 的 30 秒：两段 15 秒（工作模式）拼起来。

第二段用第一段最后一帧做参考图，并让助手「接着这一刻继续」，2026-11-07 实测接缝几乎看不出来。
一个号 7 天只能出 1 条 15 秒，所以两段必然落在两个号上。这里只放和号池无关的部分：
提示词、取最后一帧、拼接。
"""
import json
import subprocess
from pathlib import Path

PAIR_DURATION = 30
CONTINUE_PREFIX = "接着参考图这一刻继续："
CONTINUE_SUFFIX = "，保持同样的人物、场景、光线、色调和镜头运动，动作自然衔接"


def continue_prompt(prompt: str) -> str:
    return f"{CONTINUE_PREFIX}{(prompt or '').strip()}{CONTINUE_SUFFIX}"


def _run(args: list[str], what: str) -> None:
    result = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"{what}失败：{(result.stderr or '').strip()[-200:]}")


def probe(path: str | Path) -> dict:
    """宽、高、有没有声音。ffprobe 读不出来时按 1280×720、有声音处理。"""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                              "-of", "json", str(path)], capture_output=True, text=True, timeout=60).stdout
        streams = json.loads(out or "{}").get("streams") or []
    except (OSError, ValueError, subprocess.SubprocessError):
        streams = []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {"width": int(video.get("width") or 1280), "height": int(video.get("height") or 720),
            "audio": any(s.get("codec_type") == "audio" for s in streams) or not streams}


def last_frame(video: str | Path, out: str | Path) -> Path:
    out = Path(out)
    _run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.15", "-i", str(video), "-update", "1",
          "-frames:v", "1", str(out)], "取第一段最后一帧")
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("取第一段最后一帧失败：没有生成图片")
    return out


def concat_args(first: str, second: str, out: str, width: int, height: int, audio: bool) -> list[str]:
    """两段统一到第一段的尺寸、24fps 再拼；第二段比例不同时补黑边，不拉伸。"""
    fit = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
           f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24")
    if audio:
        graph = f"[0:v]{fit}[a];[1:v]{fit}[b];[a][0:a][b][1:a]concat=n=2:v=1:a=1[v][au]"
        maps = ["-map", "[v]", "-map", "[au]", "-c:a", "aac", "-b:a", "128k"]
    else:
        graph = f"[0:v]{fit}[a];[1:v]{fit}[b];[a][b]concat=n=2:v=1:a=0[v]"
        maps = ["-map", "[v]"]
    return ["ffmpeg", "-v", "error", "-y", "-i", first, "-i", second, "-filter_complex", graph, *maps,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", out]


def concat(first: str | Path, second: str | Path, out: str | Path) -> Path:
    a, b = probe(first), probe(second)
    _run(concat_args(str(first), str(second), str(out), a["width"], a["height"], a["audio"] and b["audio"]),
         "拼接两段 15 秒")
    out = Path(out)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("拼接两段 15 秒失败：没有生成文件")
    return out
