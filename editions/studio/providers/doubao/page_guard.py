"""Dismiss only the observed desktop promotion; leave all other dialogs alone."""
import re
import logging

from providers.base import ProviderError


PAGE_READY_TIMEOUT = 30
PROMOTION_HEADINGS = {
    '下載電腦版使用完整功能', '下载电脑版使用完整功能',
    '下载豆包电脑版解锁本地任务能力',
}
LOGIN_LABEL = re.compile(r'^(?:登录|登入|Log in|Sign in)$', re.I)
PROMOTION_HEADING_PATTERN = re.compile(
    r'^\s*(?:' + '|'.join(r'\s*'.join(map(re.escape, text))
                         for text in sorted(PROMOTION_HEADINGS)) + r')\s*$')


async def install_download_promotion_guard(page):
    """Handle a late official promotion inside the original native action.

    A one-time preflight cannot cover overlays appearing while click() waits.
    The actionability handler closes only the observed promotion and lets that
    same click continue. It never retries a send, presses Escape or removes DOM.
    """
    if getattr(page, '_doubao_download_promotion_guard', False):
        return
    known = page.get_by_role('dialog').filter(visible=True).filter(
        has=page.locator('h2').filter(has_text=PROMOTION_HEADING_PATTERN)).filter(
        has=page.locator('[data-testid="desktop_download_guide_popup_btn"]').filter(visible=True))

    async def dismiss():
        if getattr(page, '_doubao_dismissing_promotion', False):
            return
        await clear_download_promotion(page, reject_other_dialogs=False)

    # clear_download_promotion verifies that each native close really hides the
    # dialog; re-querying also handles more than one known promotion safely.
    await page.add_locator_handler(known.first, dismiss, no_wait_after=True)
    page._doubao_download_promotion_guard = True


async def login_entry_visible(page):
    entries = page.get_by_role('button', name=LOGIN_LABEL).filter(visible=True)
    return bool(await entries.count())


async def clear_download_promotion(page, *, reject_other_dialogs=True):
    """Use the native labelled close button, never JS removal or broad Escape."""
    previous = getattr(page, '_doubao_dismissing_promotion', False)
    page._doubao_dismissing_promotion = True
    try:
        await _clear_download_promotions(page, reject_other_dialogs=reject_other_dialogs)
    finally:
        page._doubao_dismissing_promotion = previous


async def _clear_download_promotions(page, *, reject_other_dialogs):
    dialogs = page.get_by_role('dialog').filter(visible=True)
    index = 0
    dismissed = 0
    while index < await dialogs.count():
        dialog = dialogs.nth(index)
        headings = await dialog.locator('h2').all_inner_texts()
        known = any(re.sub(r'\s+', '', text) in PROMOTION_HEADINGS for text in headings)
        download = dialog.locator('[data-testid="desktop_download_guide_popup_btn"]').filter(visible=True)
        if not known or await download.count() != 1:
            if reject_other_dialogs:
                raise ProviderError('page_blocked', '豆包页面有未识别或需要人工处理的弹窗；提示词未发送。', 409)
            index += 1
            continue
        close = dialog.get_by_role('button', name=re.compile(r'^(?:关闭|關閉|Close)$', re.I)).filter(visible=True)
        if await close.count() != 1 or not await close.is_enabled():
            raise ProviderError('page_blocked', '豆包电脑版推广弹窗的关闭入口不唯一或不可用；提示词未发送。', 409)
        # Bind this observed dialog for the hidden check; the nth visible-dialog
        # locator can refer to another dialog immediately after this one closes.
        target = await dialog.element_handle()
        if target is None:
            continue
        try:
            await close.click(timeout=3000)
            await target.wait_for_element_state('hidden', timeout=3000)
        finally:
            await target.dispose()
        dismissed += 1
        logging.getLogger(__name__).info('Doubao desktop promotion closed with native close button')
        if dismissed >= 3:
            if await dialogs.count():
                raise ProviderError('page_blocked', '豆包推广弹窗反复出现，已停止操作等待检查。', 409)
            break
