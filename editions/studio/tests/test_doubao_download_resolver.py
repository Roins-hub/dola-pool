"""Offline official-download fixtures; no account or external network is used."""
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from patchright.async_api import async_playwright

from providers.base import ProviderError
from providers.doubao.downloads import resolve_download, read_video_dimensions
from video_metadata import InvalidVideoError


PREVIEW = 'https://preview.example.com/movie.mp4?token=old'
NODE = '7000000000000000002'
OTHER = '7000000000000000003'
MESSAGE = '7000000000000000004'
DOWNLOAD = 'https://videoweb-download.doubao.com/movie.mp4?download=true'
BACKUP = 'https://videoweb.doubao.com/backup.mp4?download=true'


class OfficialDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(Path(self.temp.name) / 'browser'), headless=True)
        self.calls = []
        self.replies = {}
        self.message_reply = {'code': 0, 'data': {}}
        self.html = '<body></body>'
        await self.context.route('**/*', self.route)
        self.page = await self.context.new_page()
        await self.page.goto('https://www.doubao.com/chat/fixture-conversation')
        # DNS is the one external boundary; the implementation must still reject
        # literal private/credentialed/non-HTTPS candidates before this boundary.
        self.validation = patch('providers.doubao.downloads._validate_url_async',
                                AsyncMock(side_effect=lambda url: url))
        self.validation.start()

    async def asyncTearDown(self):
        self.validation.stop()
        await self.context.close()
        await self.playwright.stop()
        self.temp.cleanup()

    async def route(self, route):
        request = route.request
        if request.is_navigation_request():
            await route.fulfill(status=200, content_type='text/html', body=self.html)
        elif '/samantha/aispace/' in request.url:
            body = json.loads(request.post_data)
            self.calls.append({'url': request.url, 'body': body, 'headers': request.headers})
            if request.url.endswith('/message_node_info'):
                reply = self.message_reply
            else:
                node = body['requests'][0]['node_id']
                reply = self.replies.get(node, {'code': 1, 'msg': 'unavailable'})
            await route.fulfill(status=200, content_type='application/json', body=json.dumps(reply))
        else:
            await route.abort()

    def reply(self, node=NODE, main=DOWNLOAD, backup=BACKUP):
        self.replies[node] = {'code': 0, 'data': {'download_infos': [
            {'node_id': node, 'main_url': main, 'backup_url': backup}]}}

    async def media(self, *, node=NODE, message=MESSAGE, src=PREVIEW):
        await self.page.evaluate('''({node,message,src}) => {
          const card=document.createElement('div');card.dataset.messageId=message;
          const video=document.createElement('video');video.style.cssText='width:160px;height:90px';video.src=src;
          video['__reactProps$fixture']={video:{node_id:node}};
          card.append(video);document.body.append(card);
        }''', {'node': node, 'message': message, 'src': src})

    async def test_official_request_keeps_19_digit_ids_and_selects_verified_download(self):
        self.reply()
        result = await resolve_download(self.page, PREVIEW, node_id=NODE, require_no_watermark=True)
        self.assertEqual(result['url'], DOWNLOAD)
        self.assertEqual(result['node_id'], NODE)
        self.assertEqual(result['source'], 'official_download')
        self.assertEqual(result['watermark'], 'verified_download')
        self.assertEqual(result['alternatives'][0]['url'], BACKUP)
        self.assertEqual(self.calls[0]['body'], {'requests': [{'node_id': NODE}]})
        self.assertEqual(self.calls[0]['headers']['agw-js-conv'], 'str')
        self.assertEqual(self.calls[0]['headers']['origin'], 'https://www.doubao.com')

    async def test_matching_visible_video_scopes_fiber_and_ignores_another_message(self):
        await self.media(node=OTHER, message='7000000000000000005', src='https://preview.example.com/other.mp4')
        await self.media(src='https://preview.example.com/movie.mp4?token=new')
        self.reply()
        result = await resolve_download(self.page, PREVIEW, require_no_watermark=True)
        self.assertEqual(result['node_id'], NODE)
        self.assertEqual([item['body'] for item in self.calls], [{'requests': [{'node_id': NODE}]}])

    async def test_message_lookup_is_limited_to_explicit_message_and_keeps_string_precision(self):
        self.message_reply = {'code': 0, 'data': {'records': [{'node_id_str': NODE}]}}
        self.reply()
        result = await resolve_download(self.page, PREVIEW, message_id=MESSAGE, require_no_watermark=True)
        self.assertEqual(result['node_id'], NODE)
        self.assertEqual(self.calls[0]['body'], {'message_ids_str': [MESSAGE]})
        self.assertEqual(len(self.calls), 2)

    async def test_missing_evidence_never_queries_unrelated_recent_messages(self):
        await self.media(node=OTHER, src='https://preview.example.com/other.mp4')
        result = await resolve_download(self.page, PREVIEW)
        self.assertEqual(result['url'], PREVIEW)
        self.assertEqual(result['watermark'], 'unknown')
        self.assertEqual(self.calls, [])
        with self.assertRaises(ProviderError) as error:
            await resolve_download(self.page, PREVIEW, require_no_watermark=True)
        self.assertEqual(error.exception.code, 'no_watermark_unavailable')

    async def test_unconfirmed_main_url_uses_verified_backup_instead(self):
        self.reply(main='https://preview.example.com/preview.mp4')
        result = await resolve_download(self.page, PREVIEW, node_id=NODE, require_no_watermark=True)
        self.assertEqual(result['url'], BACKUP)

    async def test_credentials_http_and_private_candidates_cannot_be_selected(self):
        for unsafe in ('http://videoweb.doubao.com/v?download=true',
                       'https://127.0.0.1/v?download=true&mime_type=video_mp4',
                       'https://2130706433/v?download=true&mime_type=video_mp4',
                       'https://user:secret@videoweb.doubao.com/v?download=true',
                       'https://videoweb.doubao.com.evil.example/v?download=true'):
            self.reply(main=unsafe, backup=BACKUP)
            result = await resolve_download(self.page, PREVIEW, node_id=NODE, require_no_watermark=True)
            self.assertEqual(result['url'], BACKUP)

    async def test_dns_private_address_is_rejected_and_does_not_prevent_safe_backup(self):
        self.reply()
        async def check(url):
            if url == DOWNLOAD:
                raise ValueError('private address')
            return url
        with patch('providers.doubao.downloads._validate_url_async', side_effect=check):
            result = await resolve_download(self.page, PREVIEW, node_id=NODE, require_no_watermark=True)
        self.assertEqual(result['url'], BACKUP)

    async def test_gateway_errors_and_unverified_urls_do_not_become_watermark_proof(self):
        self.reply(main='https://preview.example.com/movie.mp4', backup='')
        with self.assertRaises(ProviderError) as error:
            await resolve_download(self.page, PREVIEW, node_id=NODE, require_no_watermark=True)
        self.assertEqual(error.exception.code, 'no_watermark_unavailable')
        self.replies[NODE] = {'code': 1, 'data': {'download_infos': [{'main_url': DOWNLOAD}]}}
        result = await resolve_download(self.page, PREVIEW, node_id=NODE)
        self.assertEqual(result['url'], PREVIEW)

    async def test_another_origin_is_rejected_and_rounded_node_is_not_used_for_download(self):
        await self.page.goto('https://example.com/chat/fixture')
        with self.assertRaises(ProviderError):
            await resolve_download(self.page, PREVIEW, node_id=NODE, require_no_watermark=True)
        self.assertEqual(self.calls, [])
        await self.page.goto('https://www.doubao.com/chat/fixture')
        await self.media(node=7000000000000000005)
        with self.assertRaises(ProviderError):
            await resolve_download(self.page, PREVIEW, require_no_watermark=True)
        self.assertEqual([item['body'] for item in self.calls], [{'message_ids_str': [MESSAGE]}])

    async def test_media_without_message_card_cannot_harvest_thread_fiber_ids(self):
        await self.page.evaluate('''({url,node})=>{
          document.body['__reactProps$thread']={thread:{node_id:node}};
          const video=document.createElement('video');video.style.cssText='width:160px;height:90px';
          video.src=url;document.body.append(video);
        }''', {'url': PREVIEW, 'node': OTHER})
        result = await resolve_download(self.page, PREVIEW)
        self.assertEqual(result['source'], 'preview')
        self.assertEqual(self.calls, [])

    async def test_request_timeout_can_preserve_preview_but_never_claim_official_download(self):
        class StalledPage:
            url = 'https://www.doubao.com/chat/fixture'
            async def evaluate(self, *_):
                raise TimeoutError('page closed or stalled')
        result = await resolve_download(StalledPage(), PREVIEW)
        self.assertEqual(result['source'], 'preview')
        with self.assertRaises(ProviderError):
            await resolve_download(StalledPage(), PREVIEW, require_no_watermark=True)


def box(kind, payload):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def fixture_mp4(width, height, *, version=0, rotated=False, audio_first=False):
    # ISO BMFF tkhd v0/v1 includes the 36-byte transformation matrix then
    # 16.16 width/height. A real audio track with 0x0 dimensions precedes video.
    prefix = bytes([version, 0, 0, 7]) + bytes(48 if version else 36)
    matrix = ((0, 65536, 0, -65536, 0, 0, 0, 0, 1073741824) if rotated else
              (65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824))
    header = prefix + struct.pack('>9i', *matrix) + struct.pack('>II', width << 16, height << 16)
    video = box(b'trak', box(b'tkhd', header) + box(b'mdia', box(b'hdlr', bytes(8) + b'vide' + bytes(12))))
    audio = box(b'trak', box(b'tkhd', bytes(84)) + box(b'mdia', box(b'hdlr', bytes(8) + b'soun' + bytes(12))))
    return box(b'ftyp', b'isom' + bytes(4) + b'isom') + box(b'moov', (audio if audio_first else b'') + video) + box(b'mdat', bytes(100))


class VideoDimensionTests(unittest.TestCase):
    def test_reads_video_track_in_both_versions_ignoring_audio_and_rotation_is_applied(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.mp4'
            for version in (0, 1):
                path.write_bytes(fixture_mp4(1920, 1080, version=version, audio_first=True))
                self.assertEqual(read_video_dimensions(path), {'width': 1920, 'height': 1080})
            path.write_bytes(fixture_mp4(1920, 1080, rotated=True))
            self.assertEqual(read_video_dimensions(path), {'width': 1080, 'height': 1920})

    def test_html_truncation_and_invalid_track_size_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.mp4'
            for data in (b'<html>not a video</html>', fixture_mp4(1920, 1080)[:-1],
                         fixture_mp4(0, 1080), fixture_mp4(1920, 0)):
                path.write_bytes(data)
                with self.assertRaises(InvalidVideoError):
                    read_video_dimensions(path)

    def test_no_track_header_returns_unknown_instead_of_inventing_resolution(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.mp4'
            path.write_bytes(box(b'ftyp', b'isom' + bytes(4)) + box(b'moov', b'') + box(b'mdat', b'0'))
            self.assertIsNone(read_video_dimensions(path))


if __name__ == '__main__':
    unittest.main()
