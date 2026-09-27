"""浏览器并发压力测试。

以“多个账号/视频任务同时拉起浏览器”的方式压测：使用临时 profile 同时启动
N 个 headed Chromium（与 add_account / browser_pool 相同的启动参数），保持数秒，
统计操作系统里真实出现的 Chromium 浏览器主进程数、总 RSS 和系统剩余内存。

不会访问 Google/dola、不登录、不消耗任何号池额度；用于估算当前服务器
可稳定并发的账号登录 / 视频任务浏览器数量。

仅在 Linux + Xvfb 环境有意义（服务已由 xvfb-run 启动）。
"""
import asyncio
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from patchright.async_api import async_playwright

from browser import LAUNCH_ARGS, proxy_kwargs_for

# 默认每级保持浏览器打开的时间（秒）
HOLD_SECONDS = 5.0
# 等待所有浏览器拉起的超时
READY_TIMEOUT = 15.0
# 压测中发现剩余内存低于该值时，这一级算“危险/不推荐继续”
MIN_AVAILABLE_MB = 350
# 开始下一级前剩余内存低于该值时，不再继续加码
PRE_MIN_AVAILABLE_MB = 600
# 采样间隔
SAMPLE_INTERVAL = 0.5


def mem_available_mb() -> int | None:
    """当前系统可用内存（/proc/meminfo MemAvailable，MiB）。"""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        return None
    return None


def _ps_rows() -> list[tuple[int, int, str]]:
    """返回 (pid, rss_kb, args) 列表。"""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,rss=,args="],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return []
    rows: list[tuple[int, int, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return rows


async def _sample(marker: str, seconds: float) -> dict:
    """浏览器保持打开期间持续采样，返回峰值统计。"""
    peak_browsers = 0
    peak_all_procs = 0
    peak_rss_mb = 0
    min_available_mb = mem_available_mb() or 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        rows = [r for r in _ps_rows() if marker in r[2]]
        # Chromium 每个浏览器实例只有主进程命令行不带 --type=
        main_count = sum(1 for r in rows if "--type=" not in r[2])
        all_count = len(rows)
        rss_mb = sum(r[1] for r in rows) // 1024
        avail = mem_available_mb()
        peak_browsers = max(peak_browsers, main_count)
        peak_all_procs = max(peak_all_procs, all_count)
        peak_rss_mb = max(peak_rss_mb, rss_mb)
        if avail is not None:
            min_available_mb = min(min_available_mb, avail)
        await asyncio.sleep(SAMPLE_INTERVAL)
    return {
        "peak_browser_processes": peak_browsers,
        "peak_chromium_processes": peak_all_procs,
        "peak_rss_mb": peak_rss_mb,
        "min_available_mb": min_available_mb,
    }


async def _open_one(p, marker: str, idx: int, launched: dict,
                    errors: list, release: asyncio.Event,
                    hold_seconds: float):
    """按账号登录相同的参数拉起一个 headed Chromium，保持到 release。"""
    prof = Path(tempfile.mkdtemp(prefix=f"{marker}-{idx}-"))
    ctx = None
    try:
        kwargs = {
            "headless": False,
            "args": list(LAUNCH_ARGS),
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
        }
        proxy_kwargs = proxy_kwargs_for(None)
        if proxy_kwargs:
            kwargs["proxy"] = proxy_kwargs
        ctx = await p.chromium.launch_persistent_context(str(prof), **kwargs)
        launched[idx] = True
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("about:blank", timeout=30000)
        try:
            await asyncio.wait_for(release.wait(), timeout=max(hold_seconds + 10, 20))
        finally:
            await ctx.close()
            ctx = None
    except asyncio.CancelledError:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
        raise
    except Exception as exc:
        errors.append(f"#{idx} 启动失败: {exc}")
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
        shutil.rmtree(prof, ignore_errors=True)


async def run_level(n: int, hold_seconds: float = HOLD_SECONDS,
                    marker: str | None = None) -> dict:
    """同时拉起 n 个浏览器并采样，返回该级结果。"""
    marker = marker or ("dola-stress-" + uuid.uuid4().hex[:10])
    release = asyncio.Event()
    launched: dict[int, bool] = {}
    errors: list[str] = []
    async with async_playwright() as p:
        tasks = [
            asyncio.create_task(
                _open_one(p, marker, i, launched, errors, release, hold_seconds)
            )
            for i in range(n)
        ]
        deadline = time.monotonic() + READY_TIMEOUT
        while len(launched) < n and time.monotonic() < deadline:
            await asyncio.sleep(0.3)
        result = await _sample(marker, max(hold_seconds, 3.0))
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    result.update({"requested": n, "launched": len(launched), "errors": errors})
    ok = (
        not errors
        and len(launched) == n
        and result.get("peak_browser_processes", 0) >= n
        and (result.get("min_available_mb") is None
             or result["min_available_mb"] > 200)
    )
    result["ok"] = ok
    return result


async def run_stress(max_concurrency: int = 8,
                     hold_seconds: float = HOLD_SECONDS) -> dict:
    """逐级 1→N 压测，返回推荐并发数与各级结果。"""
    results: list[dict] = []
    logs: list[str] = []
    recommended = 0
    estimated_mb: int | None = None
    for n in range(1, int(max_concurrency) + 1):
        avail = mem_available_mb()
        if avail is not None and avail < PRE_MIN_AVAILABLE_MB:
            logs.append(f"剩余内存 {avail}MB 低于安全线 {PRE_MIN_AVAILABLE_MB}MB，停止继续加压")
            break
        if estimated_mb is not None and avail is not None:
            # 按上一级实测峰值 RSS 预判：每个浏览器约 estimated_mb*0.7 增量，
            # 再留 800MB 系统余量，避免直接加压到 OOM。
            need_mb = int(estimated_mb * 0.7 * n) + 800
            if avail < need_mb:
                logs.append(
                    f"{n} 个并发：预计需要约 {need_mb}MB，当前可用 {avail}MB，"
                    f"不再加压（单浏览器实测约 {estimated_mb}MB）"
                )
                break
        res = await run_level(n, hold_seconds)
        results.append(res)
        min_avail = res["min_available_mb"]
        if res["ok"] and (min_avail is None or min_avail >= MIN_AVAILABLE_MB):
            recommended = n
            logs.append(
                f"{n} 个并发：OK（真实浏览器主进程峰值 "
                f"{res['peak_browser_processes']}，Chromium 总进程 "
                f"{res['peak_chromium_processes']}，峰值 RSS "
                f"{res['peak_rss_mb']}MB，最低可用内存 {min_avail}MB）"
            )
            if n == 1:
                estimated_mb = max(res["peak_rss_mb"], 256)
        else:
            reason = ("、".join(res["errors"][:3])
                      if res["errors"]
                      else f"最低可用内存 {min_avail}MB 过低"
                      if min_avail is not None and min_avail < MIN_AVAILABLE_MB
                      else f"实际浏览器主进程峰值 {res['peak_browser_processes']} 未达 {n}")
            logs.append(f"{n} 个并发：未达标（{reason}）")
            if not res["ok"] or (min_avail is not None and min_avail < MIN_AVAILABLE_MB):
                break
    return {"recommended": recommended, "results": results, "logs": logs}
