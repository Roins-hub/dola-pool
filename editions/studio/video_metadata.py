"""Read MP4 movie duration and reject partial/non-MP4 downloads without ffmpeg."""
import math
import struct
from pathlib import Path


class InvalidVideoError(ValueError):
    pass


def read_video_duration(path):
    path = Path(path)
    size = path.stat().st_size
    duration = None
    kinds = set()
    media = False
    with path.open('rb') as stream:
        def boxes(start, end, depth=0):
            nonlocal duration, media
            if depth > 4:
                raise InvalidVideoError('MP4 容器层级异常')
            offset = start
            while offset < end:
                if end - offset < 8:
                    raise InvalidVideoError('MP4 文件不完整')
                stream.seek(offset)
                length, kind = struct.unpack('>I4s', stream.read(8))
                header = 8
                if length == 1:
                    if end - offset < 16:
                        raise InvalidVideoError('MP4 文件头不完整')
                    length = struct.unpack('>Q', stream.read(8))[0]
                    header = 16
                elif length == 0:
                    length = end - offset
                if length < header or offset + length > end:
                    raise InvalidVideoError('MP4 文件被截断')
                if depth == 0:
                    kinds.add(kind)
                    if kind == b'mdat' and length > header:
                        media = True
                if kind == b'moov' and depth == 0:
                    boxes(offset + header, offset + length, depth + 1)
                elif kind == b'mvhd' and depth == 1:
                    stream.seek(offset + header)
                    data = stream.read(min(length - header, 32))
                    if len(data) < 20 or data[0] not in (0, 1):
                        raise InvalidVideoError('MP4 时长头无效')
                    if data[0] == 1:
                        if len(data) < 32:
                            raise InvalidVideoError('MP4 时长头不完整')
                        timescale, ticks = struct.unpack('>IQ', data[20:32])
                    else:
                        timescale, ticks = struct.unpack('>II', data[12:20])
                    duration = ticks / timescale if timescale else None
                offset += length
        boxes(0, size)
    if not {b'ftyp', b'moov', b'mdat'}.issubset(kinds) or not media:
        raise InvalidVideoError('下载内容不是完整 MP4 视频')
    if duration is None or not math.isfinite(duration) or not 0 < duration < 86400:
        raise InvalidVideoError('MP4 没有有效的视频时长')
    return round(duration, 3)
