from pathlib import Path
import tempfile
import unittest

from providers.base import ProviderError
from providers.doubao.runtime import BrowserSession, write_bounded


class Chunks:
    def __init__(self, parts):self.parts=parts
    async def iter_chunked(self, size):
        for part in self.parts:yield part


class Response:
    def __init__(self, parts, headers=None):self.content=Chunks(parts);self.headers=headers or {}


class BoundedDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_oversize_is_rejected_without_a_partial_file(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'video.part'
            with self.assertRaises(ProviderError):
                await write_bounded(Response([b'1234',b'5678']),path,6)
            self.assertFalse(path.exists())

    async def test_declared_oversize_is_rejected_before_reading_and_valid_bytes_persist(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'video.part'
            with self.assertRaises(ProviderError):
                await write_bounded(Response([],{'Content-Length':'10000'}),path,6)
            self.assertFalse(path.exists())
            await write_bounded(Response([b'12',b'34']),path,6)
            self.assertEqual(path.read_bytes(),b'1234')

    async def test_integer_localhost_address_is_rejected_before_transport_creation(self):
        with tempfile.TemporaryDirectory() as root:
            session=BrowserSession(None,{'name':'fixture'})
            with self.assertRaises(ProviderError):
                await session.stream_to('https://2130706433/video.mp4',Path(root)/'out',1024)


if __name__=='__main__':unittest.main()
