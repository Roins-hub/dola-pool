"""Select visible video settings and verify them before submitting a prompt."""
import asyncio
import re

from generation_errors import PreSubmissionParametersError


PARAMS_TRIGGER = 'button[data-input-engine-actionbar-render-entry-key="video-generation-params-panel"]'
DURATION_LABEL = re.compile(r"^\s*(\d+)\s*(?:s|sec(?:onds?)?|秒)\s*$", re.I)


def duration_labels(text):
    """Read visible seconds, not the slider's zero-based aria-valuenow index."""
    return [int(match.group(1)) for line in str(text).splitlines()
            if (match := DURATION_LABEL.fullmatch(line))]


def summary_duration(text):
    match = DURATION_LABEL.fullmatch(str(text).strip().split("·")[-1])
    return int(match.group(1)) if match else None


async def _select_slider(page, slider, duration):
    section = slider.locator('xpath=ancestor::section[1]')
    labels = duration_labels(await section.inner_text())
    if not labels:
        raise PreSubmissionParametersError("无法读取网页时长滑条的秒数，尚未提交生成")
    if len(labels) >= 3:
        low, high = labels[-2:]
        if not low <= duration <= high:
            raise PreSubmissionParametersError(
                f"当前网页时长范围为 {low}～{high} 秒，不能选择 {duration} 秒，尚未提交生成")
    if labels[0] == duration:
        return
    # Real keyboard input keeps React state in sync. Do not set DOM attributes.
    await slider.press("Home", timeout=3000)
    await page.wait_for_timeout(120)
    visited = set()
    for _ in range(61):
        labels = duration_labels(await section.inner_text())
        current = labels[0] if labels else None
        if current == duration:
            return
        if current is None or current > duration or current in visited:
            break
        visited.add(current)
        await slider.press("ArrowRight", timeout=3000)
        await page.wait_for_timeout(120)
    raise PreSubmissionParametersError(f"网页时长滑条未能选中 {duration} 秒，尚未提交生成")


async def _select_modern(page, trigger, duration, ratio):
    if await trigger.get_attribute("aria-expanded") != "true":
        await trigger.click(timeout=4000)
    menu = page.locator('[role="menu"][data-creation-params-panel-id]:visible')
    await menu.wait_for(state="visible", timeout=4000)
    if await menu.count() != 1:
        raise PreSubmissionParametersError("无法唯一识别视频参数菜单，尚未提交生成")
    trigger_id = await trigger.get_attribute("data-creation-params-panel-id")
    if await menu.get_attribute("data-creation-params-panel-id") != trigger_id:
        raise PreSubmissionParametersError("视频参数菜单与当前输入框不匹配，尚未提交生成")
    try:
        if ratio:
            option = menu.get_by_role("button", name=ratio, exact=True)
            if await option.count() != 1:
                raise PreSubmissionParametersError(f"网页没有可选比例 {ratio}，尚未提交生成")
            await option.click(timeout=3000)
            if await trigger.get_attribute("aria-expanded") != "true":
                await trigger.click(timeout=3000)
                await menu.wait_for(state="visible", timeout=3000)
        if duration is not None:
            slider = menu.get_by_role("slider")
            if await slider.count() != 1:
                raise PreSubmissionParametersError("无法唯一识别时长滑条，尚未提交生成")
            await _select_slider(page, slider, duration)
    finally:
        if await trigger.get_attribute("aria-expanded") == "true":
            await page.keyboard.press("Escape")
    # A click is not success until the closed actionbar summary confirms it.
    for _ in range(10):
        text = await trigger.inner_text()
        ratio_ok = not ratio or text.split("·")[0].strip().replace(" ", "") == ratio
        if (duration is None or summary_duration(text) == duration) and ratio_ok:
            return
        await page.wait_for_timeout(100)
    raise PreSubmissionParametersError(
        f"网页参数回读不符（要求 {duration} 秒 / {ratio or '原比例'}），尚未提交生成")


async def _select_legacy(page, duration, ratio):
    if ratio:
        await page.get_by_text("比率", exact=True).first.click(timeout=3000)
        await page.get_by_text(ratio, exact=True).first.click(timeout=3000)
    if duration is None:
        return
    trigger = page.get_by_text(DURATION_LABEL).filter(visible=True)
    if await trigger.count() != 1:
        raise PreSubmissionParametersError("没有找到可识别的时长控件，尚未提交生成")
    if summary_duration(await trigger.inner_text()) != duration:
        await trigger.click(timeout=3000)
        target = page.get_by_text(re.compile(rf"^\s*{duration}\s*(?:s|秒)\s*$", re.I)).filter(visible=True)
        await target.first.click(timeout=3000)
    current = page.get_by_text(DURATION_LABEL).filter(visible=True)
    if await current.count() != 1 or summary_duration(await current.inner_text()) != duration:
        raise PreSubmissionParametersError(f"旧版页面未确认 {duration} 秒，尚未提交生成")


async def select_video_parameters(page, duration, ratio=None):
    """Support the combined panel and retain the legacy duration selectors."""
    if duration is None and not ratio:
        return
    try:
        async with asyncio.timeout(25):
            trigger = page.locator(PARAMS_TRIGGER + ':visible')
            # Entering video mode renders the actionbar asynchronously. Do not
            # mistake that short render gap for a legacy page without this control.
            if not await trigger.count():
                try:
                    await trigger.wait_for(state="visible", timeout=4000)
                except Exception:
                    pass
            if await trigger.count():
                if await trigger.count() != 1:
                    raise PreSubmissionParametersError("存在多个视频参数入口，尚未提交生成")
                await _select_modern(page, trigger, duration, ratio)
            else:
                await _select_legacy(page, duration, ratio)
    except PreSubmissionParametersError:
        raise
    except Exception:
        # Browser exception strings can contain signed URLs and account details.
        raise PreSubmissionParametersError(
            f"设置视频参数失败：未确认 {duration} 秒 / {ratio or '原比例'}，尚未提交生成") from None
