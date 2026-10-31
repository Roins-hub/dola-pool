import struct
import tempfile
import unittest
from pathlib import Path

from video_metadata import read_video_duration, InvalidVideoError


def box(kind, data):
    return struct.pack('>I4s', len(data) + 8, kind) + data


def mp4(seconds=30, version=0):
    if version:
        header = bytes([1, 0, 0, 0]) + struct.pack('>QQIQ', 0, 0, 1000, int(seconds * 1000))
    else:
        header = bytes(4) + struct.pack('>IIII', 0, 0, 1000, int(seconds * 1000))
    return (box(b'ftyp', b'isom' + bytes(4) + b'isom') +
            box(b'moov', box(b'mvhd', header + bytes(80))) + box(b'mdat', bytes(64)))


class MetadataTests(unittest.TestCase):
    def test_actual_duration_is_read_from_both_mp4_header_versions(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.mp4'
            for version in (0, 1):
                path.write_bytes(mp4(29.97, version))
                self.assertAlmostEqual(read_video_duration(path), 29.97, places=2)

    def test_html_truncation_and_empty_or_missing_media_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.mp4'
            for data in (b'<html>login</html>', mp4()[:-1], mp4(0), mp4()[:-72]):
                path.write_bytes(data)
                with self.assertRaises(InvalidVideoError):
                    read_video_duration(path)
