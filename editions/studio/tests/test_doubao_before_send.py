"""Only a pre-click failure can safely release a fenced video submission."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from providers.base import ProviderError
from providers.doubao.runtime import BrowserSession, BeforeSendError


class BeforeSendTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        session=BrowserSession(None,{})
        session.capability={'entry_mode':'ordinary_dialog','selectors':{'send':'#send'}}
        # This test isolates pre-click error classification; real DOM guards are
        # covered by test_doubao_page_guard. The page has no dialogs or identity.
        empty=SimpleNamespace(count=AsyncMock(return_value=0))
        empty.filter=lambda **kwargs:empty
        session.page=SimpleNamespace(get_by_role=lambda *args,**kwargs:empty,
                                     is_closed=lambda:False,url='https://www.doubao.com/chat/',
                                     evaluate=AsyncMock(return_value=None))
        return session

    async def test_failed_send_locator_is_explicitly_before_send_and_preserves_public_error(self):
        session=self.session()
        session._one=AsyncMock(side_effect=ProviderError('native_state_changed','Native control missing',409))
        with self.assertRaises(ProviderError) as caught:await session.submit()
        self.assertIsInstance(caught.exception,BeforeSendError)
        self.assertEqual(caught.exception.code,'native_state_changed')
        self.assertEqual(caught.exception.status_code,409)
        self.assertEqual(str(caught.exception),'Native control missing')

    async def test_native_preflight_failure_preserves_code_and_does_not_click(self):
        session=self.session();session.capability['native_media_bridge']=True
        button=SimpleNamespace(click=AsyncMock(),is_enabled=AsyncMock(return_value=True))
        session._one=AsyncMock(return_value=button)
        with patch('providers.doubao.native_bridge.assert_native_ready',AsyncMock(side_effect=ProviderError('native_media_not_ready','Not ready',503))):
            with self.assertRaises(ProviderError) as caught:await session.submit()
        self.assertIsInstance(caught.exception,BeforeSendError)
        self.assertEqual(caught.exception.code,'native_media_not_ready')
        self.assertEqual(caught.exception.status_code,503)
        self.assertEqual(button.click.await_count,0)

    async def test_unexpected_preflight_error_is_safe_public_before_send_failure(self):
        session=self.session();session._one=AsyncMock(side_effect=RuntimeError('private request diagnostics'))
        try:await session.submit()
        except Exception as error:
            self.assertIsInstance(error,BeforeSendError)
            self.assertEqual(error.code,'native_state_changed')
            self.assertNotIn('private',str(error))
        else:self.fail('Preflight must fail before the click')

    async def test_exception_after_click_starts_is_not_declared_before_send(self):
        for mode in ('ordinary_dialog','native_controls'):
            session=self.session();session.capability['entry_mode']=mode
            button=SimpleNamespace(click=AsyncMock(side_effect=TimeoutError('native click dispatch timeout')),
                                   is_enabled=AsyncMock(return_value=True))
            session._one=AsyncMock(return_value=button)
            with self.assertRaises(TimeoutError) as caught:await session.submit()
            self.assertNotIsInstance(caught.exception,BeforeSendError)
            self.assertEqual(button.click.await_count,1)


if __name__=='__main__':unittest.main()
