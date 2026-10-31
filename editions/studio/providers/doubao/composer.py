"""A single input contract for the ordinary chat's two native editor layouts.

This only operates observed DOM controls. It does not alter requests, browser
identity, model specifications or page application state.
"""
import asyncio
import logging
import re
import time

from providers.base import ProviderError
from .page_guard import PAGE_READY_TIMEOUT, clear_download_promotion


def _rendered_text(value):
    value = value.replace('\r\n', '\n').replace('\r', '\n').replace('\xa0', ' ')
    return re.sub(r'\n+', '\n', value).strip()


class DialogComposer:
    def __init__(self, page, selector):
        self.page = page
        # Filtering remains part of the locator: no cached index or arbitrary
        # first match can turn into a different editor after a native re-render.
        self.locator = page.locator(selector).filter(visible=True).and_(page.locator(
            ':not(:disabled):not([readonly]):not([aria-disabled="true"]):not([aria-readonly="true"])'))

    async def resolve(self, *, stage='对话输入框', timeout=PAGE_READY_TIMEOUT):
        started = time.monotonic()
        deadline = started + timeout
        while True:
            await clear_download_promotion(self.page)
            count = await self.locator.count()
            if count == 1 and await self.locator.is_editable():
                logging.getLogger(__name__).info('Doubao composer ready: %s (%.2fs)', stage, time.monotonic()-started)
                return self.locator
            # During a native new-chat transition both layouts may coexist.
            # Wait for one usable input; never pick one of two usable inputs.
            if time.monotonic() >= deadline:
                reason = '存在多个可用输入框' if count > 1 else '未就绪或等待超时'
                logging.getLogger(__name__).warning('Doubao composer stopped: %s, usable=%d', stage, count)
                raise ProviderError('native_state_changed', f'豆包{stage}{reason}；提示词未发送。')
            await asyncio.sleep(min(.1, max(0, deadline-time.monotonic())))

    async def verify_text(self, expected, *, stage='发送前输入框'):
        composer = await self.resolve(stage=stage)
        actual = await composer.evaluate('(n)=>n.value??n.innerText')
        if not expected.strip() or _rendered_text(actual) != _rendered_text(expected):
            raise ProviderError('native_state_changed', '豆包输入框中的提示词不完整或已改变；提示词未发送。')
        return composer

    async def fill(self, text):
        composer = await self.resolve(stage='提示词输入框')
        await composer.fill(text, timeout=8000)
        return await self.verify_text(text, stage='写入后输入框')
