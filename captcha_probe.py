"""滑块验证码探针：走 dola 真实 UI 发送消息，触发并抓取滑块验证码的结构。

背景：页面内裸 fetch 提交被 shark 风控 710022004 拦截；前端收到该错误后会弹自己的滑块 UI。
本脚本用 headful 打开，UI 输入 + 发送，等待验证码弹出，保存截图/DOM/frame 信息供分析。

用法：python captcha_probe.py <账号名>
产物：captcha_probe.png（截图）、captcha_probe.html（DOM）、控制台 frame 列表
"""
import asyncio
import sys
from pathlib import Path

from patchright.async_api import async_playwright

from browser import launch_account_context

PROMPT = "海边日落，浪花拍打沙滩"


async def dump_state(page, context, tag):
    """保存截图 + DOM + frame 列表"""
    png = Path(f"captcha_probe_{tag}.png")
    html = Path(f"captcha_probe_{tag}.html")
    await page.screenshot(path=str(png), full_page=False)
    try:
        await html.write_text(await page.content(), encoding="utf-8")
    except Exception as e:
        print(f"  保存 html 失败: {e}")
    print(f"[{tag}] url={page.url}")
    print(f"[{tag}] 截图 -> {png}")
    frames = page.frames
    print(f"[{tag}] frames({len(frames)}):")
    for f in frames:
        print(f"    - name={f.name!r} url={f.url[:150]}")


async def main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"

    async with async_playwright() as p:
        context = await launch_account_context(p, account, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await dump_state(page, context, "01_loaded")

        # 找输入框：textarea / contenteditable
        box = await page.query_selector("textarea")
        input_kind = "textarea"
        if not box:
            box = await page.query_selector('[contenteditable="true"]')
            input_kind = "contenteditable"
        if not box:
            print("✗ 未找到输入框（登录态可能失效）")
            await context.close()
            return
        print(f"输入框: {input_kind}")

        await box.click()
        await page.wait_for_timeout(500)
        await page.keyboard.type(PROMPT, delay=120)  # 拟人打字
        await page.wait_for_timeout(800)
        await dump_state(page, context, "02_typed")

        print("按回车发送...")
        await page.keyboard.press("Enter")

        # 等验证码弹出（最多 30s，每秒检查一次）
        for i in range(30):
            await page.wait_for_timeout(1000)
            html = await page.content()
            low = html.lower()
            hit = [kw for kw in ("captcha", "verify", "secsdk", "shark", "滑块", "拖动", "slider", "puzzle") if kw in low]
            nframes = len(page.frames)
            if hit or nframes > 1:
                print(f"t+{i+1}s 疑似验证码出现: keywords={hit} frames={nframes}")
                await page.wait_for_timeout(2000)  # 等渲染完整
                await dump_state(page, context, "03_captcha")
                # 进一步：在每个 frame 里找 img/canvas
                for f in page.frames:
                    try:
                        info = await f.evaluate("""() => {
                            const imgs = [...document.images].map(im => ({src: (im.src||'').slice(0,200), w: im.naturalWidth, h: im.naturalHeight}));
                            const canvases = [...document.querySelectorAll('canvas')].map(c => ({w: c.width, h: c.height}));
                            return {imgs, canvases, bodyLen: document.body ? document.body.innerHTML.length : 0};
                        }""")
                        if info["imgs"] or info["canvases"]:
                            print(f"  frame {f.url[:80]}: {info}")
                    except Exception:
                        pass
                break
        else:
            print("30s 内未检测到验证码，dump 最终状态")
            await dump_state(page, context, "03_final")

        print("\n保持浏览器 60s 供肉眼观察（可手动操作）...")
        await page.wait_for_timeout(60000)
        await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        import traceback
        traceback.print_exc()