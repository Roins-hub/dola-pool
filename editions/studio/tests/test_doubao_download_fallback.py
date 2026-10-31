from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from providers.base import ProviderError
from providers.doubao.runtime import BrowserSession,HOME
from test_video_metadata import mp4


class DownloadFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_always_requires_official_source_without_changing_request_preferences(self):
        self.session.request = {'require_no_watermark': False}
        async def stream(url, path, limit, **kwargs):
            Path(path).write_bytes(mp4(6))
        self.session.stream_to = stream
        from providers.doubao import downloads
        await self.session.download('https://cdn.example/preview.mp4', 'fixture')
        self.assertTrue(downloads.resolve_download.await_args.kwargs['require_no_watermark'])
        self.assertFalse(self.session.request['require_no_watermark'])

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name)/'downloads'
        self.session=BrowserSession(SimpleNamespace(downloads=self.output),{'name':'fixture'})
        self.session.page=SimpleNamespace(url=HOME+'original')
        self.session.request={'require_no_watermark':True}
        self.first={'url':'https://cdn.example/main.mp4','source':'official_download','watermark':'verified_download'}
        self.backup={**self.first,'url':'https://cdn.example/backup.mp4'}
        self.resolver=patch('providers.doubao.downloads.resolve_download',AsyncMock(return_value={**self.first,'alternatives':[self.backup]}))
        self.resolver.start();self.addCleanup(self.resolver.stop)
        self.calls=[]

    async def test_failed_main_uses_its_safe_backup_on_the_same_session(self):
        async def stream(url,path,limit,**kwargs):
            self.calls.append((url,kwargs))
            if url==self.first['url']:raise ProviderError('download_failed','fixture 403')
            Path(path).write_bytes(mp4(6))
        self.session.stream_to=stream
        result=await self.session.download('https://cdn.example/preview.mp4','video_'+'a'*32)
        self.assertEqual([call[0] for call in self.calls],[self.first['url'],self.backup['url']])
        self.assertTrue(all(call[1]=={'referer':HOME} for call in self.calls))
        self.assertEqual(result['actual_duration'],6)
        self.assertEqual(result['watermark'],'verified_download')
        self.assertTrue(Path(result['output_path']).is_file())

    async def test_html_main_is_never_completed_and_backup_must_pass_mp4_validation(self):
        async def stream(url,path,limit,**kwargs):
            self.calls.append(url);Path(path).write_bytes(b'<html>CDN error</html>' if len(self.calls)==1 else mp4(6))
        self.session.stream_to=stream
        result=await self.session.download('https://cdn.example/preview.mp4','video_'+'b'*32)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(Path(result['output_path']).read_bytes(),mp4(6))

    async def test_all_download_failures_leave_no_partial_file_or_false_success(self):
        async def stream(url,path,limit,**kwargs):Path(path).write_bytes(b'<html>error</html>')
        self.session.stream_to=stream
        with self.assertRaises(ProviderError):await self.session.download('https://cdn.example/preview.mp4','video_'+'c'*32)
        self.assertEqual(list(self.output.iterdir()),[])


if __name__=='__main__':unittest.main()
