"""Exercise upload initialization without a browser, CDP, or an upstream request."""
from pathlib import Path
import tempfile
import unittest

from integrations import dola_ref_upload as upload


class GuardPage:
    def __init__(self, guard):
        self.frames = [self]
        self.guard = guard
        self.guard_calls = 0
        self.upload_started = False

    async def evaluate(self, script, arg=None, *, isolated_context=True):
        if script == upload.FIND_INPUT:
            return True
        if script == '() => window.__dolaReferenceBatch || null':
            return None
        if '__dolaReferenceBatch={signature,ready:false}' in script:
            return None
        if script == upload.IMAGE_ORDER_SOURCE:
            self.guard_calls += 1
            if isinstance(self.guard, Exception):
                raise self.guard
            return self.guard
        raise AssertionError('Unexpected evaluation after a failed order guard')

    def locator(self, selector):
        self.upload_started = True
        raise AssertionError('A failed guard must not proceed to upload setup')


class ReferenceOrderDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def upload_failure(self, guard):
        page = GuardPage(guard)
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'reference.png'
            file.write_bytes(b'local image fixture')
            ok, message = await upload.upload_reference_media(page, [str(file)], timeout_s=1)
        self.assertFalse(ok)
        self.assertEqual(page.guard_calls, 1)
        self.assertFalse(page.upload_started)
        self.assertIn('未上传或发送提示词', message)
        return message

    async def test_known_guard_reason_has_safe_actionable_chinese_diagnosis(self):
        for code, explanation in (
            ('context', '未找到当前视频输入上下文'),
            ('manifest', '参考图身份清单无效'),
            ('runtime', '页面原生运行环境未就绪'),
            ('store', '参考图原生附件状态未就绪'),
            ('existing_attachments', '输入框已有附件'),
            ('send_guard', '签名前参考图发送校验入口未就绪'),
        ):
            with self.subTest(code=code):
                message = await self.upload_failure({'ok': False, 'reason': code,
                    'error': 'https://proxy-user:proxy-password@private.example/?token=secret'})
                self.assertIn(explanation, message)
                for secret in ('private.example', 'proxy-user', 'proxy-password', 'secret'):
                    self.assertNotIn(secret, message)

    async def test_unknown_or_malformed_guard_response_is_safe_and_never_uploads(self):
        for guard in (None, False, [], 'unexpected',
                      {'ok': False, 'reason': ['store']},
                      {'ok': False, 'reason': 'https://private.example/?token=secret'},
                      {'ok': 'true'}, {'ok': 1}):
            with self.subTest(guard=guard):
                message = await self.upload_failure(guard)
                self.assertIn('未取得有效校验状态', message)
                self.assertNotIn('private.example', message)
                self.assertNotIn('secret', message)

    async def test_guard_evaluation_error_never_exposes_exception_details(self):
        page = GuardPage(RuntimeError('https://proxy-user:proxy-password@private.example/?token=secret'))
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'reference.png'
            file.write_bytes(b'local image fixture')
            ok, message = await upload.upload_reference_media(page, [str(file)], timeout_s=1)
        self.assertFalse(ok)
        self.assertFalse(page.upload_started)
        self.assertIn('未发送提示词', message)
        for secret in ('private.example', 'proxy-user', 'proxy-password', 'secret'):
            self.assertNotIn(secret, message)


if __name__ == '__main__':
    unittest.main()
