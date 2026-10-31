import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from test_video_metadata import mp4

from test_video_pipeline import pool_module
import video_worker_ui as worker


class BackgroundRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_german_duration_warning_does_not_override_two_credit_receipt(self):
        async def fetch():
            return {'texts': ['Die maximale unterstützte Videolänge beträgt 15 Sekunden.',
                             'Das Video wird mit Dreamina Seedance 2.5 generiert. Es werden 2 Credits verbraucht.']}
        async def download(url): self.fail('No video returned yet')
        with patch.object(worker.config, 'POLL_INTERVAL', .01):
            with self.assertRaises(TimeoutError):
                await worker._wait_for_video('fixture', '123', time.time() + .02, fetch, download)

    async def test_duration_warning_followed_by_generation_receipt_still_collects_the_result(self):
        attempts = 0
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'video.mp4'
            path.write_bytes(mp4())
            async def fetch():
                nonlocal attempts
                attempts += 1
                return {'videos': [] if attempts == 1 else ['https://cdn.test/video.mp4'], 'texts': [
                    '视频生成当前支持的时长范围是4到15秒，不能直接生成30秒。我将按最接近的支持时长15秒生成。',
                    '本次使用 Dreamina Seedance 2.5 生成，将消耗 2 个视频生成额度，预计等待 55 分钟。'
                ]}
            async def download(url): return path
            with patch.object(worker.config, 'POLL_INTERVAL', .01):
                result = await worker._wait_for_video('fixture', '123', time.time() + 1, fetch, download)
            self.assertEqual(attempts, 2)
            self.assertEqual(Path(result['local_path']).read_bytes(), mp4())

    async def test_duration_proposal_is_not_platform_failure_and_keeps_querying(self):
        for text in (
            'Video generation currently supports durations from 4 to 15 seconds. I can generate it at the nearest supported duration of 15 seconds.',
            'Die maximale unterstützte Videolänge beträgt 15 Sekunden. Ich kann das Video daher mit 15 Sekunden in 9:16 erstellen;',
        ):
            with self.subTest(text=text):
                attempts = 0
                async def fetch():
                    nonlocal attempts
                    attempts += 1
                    return {'videos': [], 'texts': [text]}
                async def download(url):
                    self.fail('refused generation downloaded')
                with patch.object(worker.config, 'POLL_INTERVAL', .01):
                    with self.assertRaises(worker.SubmissionUnconfirmedError):
                        await worker._wait_for_video('fixture', '123', time.time() + .05, fetch, download)
                self.assertGreater(attempts, 1)

    async def test_explicit_generation_failure_stops_at_first_poll(self):
        attempts = 0
        async def fetch():
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                self.fail('continued polling after Dola reported generation failure')
            return {'videos': [], 'texts': [
                'Ich kann das Video nicht generieren und für diese Generierung wurden keine Credits verwendet.',
                'Das Video wird mithilfe des Seedance 2.0 Modells generiert. Es ist in 5 Minuten fertig.',
            ]}
        async def download(url): self.fail('failed generation downloaded')
        with patch.object(worker.config, 'POLL_INTERVAL', .01):
            with self.assertRaisesRegex(RuntimeError, 'Dola.*无法生成视频'):
                await worker._wait_for_video('fixture', '123', time.time() + 1, fetch, download)
        self.assertEqual(attempts, 1)

    async def test_finished_video_is_recovered_despite_failure_text_in_history(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'video.mp4'
            path.write_bytes(mp4())
            async def fetch():
                return {'videos': ['https://cdn.test/video.mp4'], 'texts': [
                    'Ich kann das Video nicht generieren und für diese Generierung wurden keine Credits verwendet.'
                ]}
            async def download(url): return path
            result = await worker._wait_for_video('fixture', '123', time.time() + 1, fetch, download)
            self.assertEqual(Path(result['local_path']).read_bytes(), mp4())

    async def test_generation_estimate_keeps_polling_until_video_arrives(self):
        attempts = 0
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'video.mp4'
            path.write_bytes(mp4())
            async def fetch():
                nonlocal attempts
                attempts += 1
                return {'videos': [] if attempts == 1 else ['https://cdn.test/video.mp4'], 'texts': [
                    'Das Video wird mit dem Dreamina Seedance 2.5-Modell generiert. '
                    'Es wird in 2 Stunden fertig sein. Ich sende es dir, wenn es fertig ist.'
                ]}
            async def download(url): return path
            with patch.object(worker.config, 'POLL_INTERVAL', .01):
                result = await worker._wait_for_video('fixture', '123', time.time() + 1, fetch, download)
            self.assertEqual(attempts, 2)
            self.assertEqual(Path(result['local_path']).read_bytes(), mp4())

    async def test_transient_poll_error_retries_without_resubmission(self):
        attempts = 0
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'video.mp4'
            path.write_bytes(mp4())
            async def fetch():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise worker.PollingResponseError('fixture temporary error')
                return {'texts': [], 'videos': ['https://cdn.test/video.mp4']}
            async def download(url): return path
            with patch.object(worker.config, 'POLL_INTERVAL', .01):
                result = await worker._wait_for_video('fixture', '123', time.time() + 1, fetch, download)
            self.assertEqual(Path(result['local_path']).read_bytes(), mp4())
            self.assertEqual(attempts, 2)

    async def test_expired_recovery_does_not_extend_deadline_or_send_requests(self):
        async def fetch(): self.fail('expired task sent a poll')
        async def download(url): self.fail('expired task downloaded')
        with self.assertRaises(worker.VideoQueryTimeout):
            await worker._wait_for_video('fixture', '123', time.time() - 1, fetch, download)

    async def test_recovery_fetches_and_downloads_without_opening_a_browser(self):
        class Session:
            proxy_url = 'http://original-line:80'
            own_isp = True
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def poll(self):
                return {'ok': True, 'videos': ['https://cdn.test/video.mp4'], 'videoModels': [], 'texts': []}

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'video.mp4'
            path.write_bytes(mp4())
            async def download(url, account, **kwargs):
                self.assertEqual(kwargs.get('proxy_url'), 'http://original-line:80')
                return path
            with patch.object(worker, 'async_playwright', side_effect=AssertionError('recovery reopened browser')), \
                 patch.object(worker, 'HttpVideoSession', Session, create=True), \
                 patch.object(worker, '_download', download):
                result = await worker.resume_video('fixture', '12345', 10)
            self.assertEqual(result['conversation_id'], '12345')
            self.assertEqual(Path(result['local_path']).read_bytes(), mp4())


class HttpTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import video_polling
        self.transport = video_polling
        self.root = tempfile.TemporaryDirectory()
        self.calls = []
        self.response_headers = {}
        self.response_status = 200
        self.data = {'status_code': 0, 'downlink_body': {'pull_singe_chain_downlink_body': {
            'messages': [{'content': json.dumps([{'block_type': 2074, 'content': {
                'creation_block': {'creations': [{'type': 2, 'video': {
                    'download_url': 'https://cdn.test/finished.mp4', 'video_model': ''}}]}}}])}]}}}
        async def serve(request):
            self.calls.append((request.headers.get('Cookie'), await request.json()))
            return web.json_response(self.data, status=self.response_status, headers=self.response_headers)
        app = web.Application()
        app.router.add_post('/im/chain/single', serve)
        async def video(request):
            return web.Response(body=mp4(), content_type='video/mp4')
        app.router.add_get('/video.mp4', video)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f'http://127.0.0.1:{port}'
        self.patches = [patch.object(video_polling, 'ACCOUNTS_DIR', Path(self.root.name)),
                        patch.object(video_polling, 'API_BASE', f'http://127.0.0.1:{port}')]
        for p in self.patches: p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()
        await self.runner.cleanup()
        self.root.cleanup()

    def save_snapshot(self):
        with patch.object(self.transport.config, 'proxy_url_for', return_value=None), \
             patch.object(self.transport.config, 'account_proxy_raw', return_value=('', 'none')):
            self.transport.save_poll_state('fixture', '12345',
                                           [{'name': 'sessionid', 'value': 'fixture-session'}], 'fixture-agent')

    async def test_restart_uses_saved_identity_and_original_proxy_and_parses_finished_video(self):
        self.save_snapshot()
        with patch.object(self.transport.config, 'proxy_url_for', return_value='http://unreachable.invalid:1'):
            async with self.transport.HttpVideoSession('fixture', '12345') as session:
                result = await session.poll()
        self.assertEqual(result['videos'], ['https://cdn.test/finished.mp4'])
        cookie, body = self.calls[0]
        self.assertEqual(cookie, 'sessionid=fixture-session')
        self.assertEqual(body['uplink_body']['pull_singe_chain_uplink_body']['conversation_id'], '12345')
        self.assertEqual(body['cmd'], 3100)

    async def test_http_200_with_api_error_is_not_treated_as_pending_video(self):
        self.save_snapshot()
        self.data = {'status_code': 1001, 'status_desc': 'fixture failure'}
        async with self.transport.HttpVideoSession('fixture', '12345') as session:
            with self.assertRaisesRegex(self.transport.PollingResponseError, '1001'):
                await session.poll()

    async def test_explicit_login_loss_is_not_retried_as_pending_video(self):
        self.save_snapshot()
        self.response_headers = {'X-Tt-Agw-Login': '0'}
        async with self.transport.HttpVideoSession('fixture', '12345') as session:
            with self.assertRaises(worker.LoginExpiredError):
                await session.poll()

    async def test_unauthorized_response_is_login_loss_but_proxy_error_is_not(self):
        self.save_snapshot()
        async with self.transport.HttpVideoSession('fixture', '12345') as session:
            self.response_status = 401
            with self.assertRaises(worker.LoginExpiredError):
                await session.poll()
            self.response_status = 502
            with self.assertRaises(self.transport.PollingResponseError):
                await session.poll()

    async def test_malformed_response_is_not_treated_as_pending_video(self):
        self.save_snapshot()
        self.data = {'status_code': 0, 'downlink_body': []}
        async with self.transport.HttpVideoSession('fixture', '12345') as session:
            with self.assertRaises(self.transport.PollingResponseError):
                await session.poll()

    async def test_receipt_after_long_reply_is_not_truncated(self):
        self.save_snapshot()
        text = '时长说明。' * 40 + '本次使用 Seedance 2.5 生成，将消耗2个视频生成额度，预计等待55分钟。'
        self.data['downlink_body']['pull_singe_chain_downlink_body']['messages'] = [
            {'content': [{'content': {'text_block': {'text': text}}}]}]
        async with self.transport.HttpVideoSession('fixture', '12345') as session:
            result = await session.poll()
        self.assertEqual(result['texts'], [text])

    async def test_download_honors_saved_direct_route_after_current_proxy_changes(self):
        with patch.object(worker.config, 'DOWNLOAD_DIR', self.root.name), \
             patch.object(worker.config, 'proxy_url_for', return_value='http://unreachable.invalid:1'):
            path = await worker._download(self.base_url + '/video.mp4', 'fixture',
                                          proxy_url=None, own_isp=False)
        self.assertEqual(path.read_bytes(), mp4())


if __name__ == '__main__':
    unittest.main()
