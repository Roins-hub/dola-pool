"""Original image byte limits match across inline and streamed inputs."""
import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

import config
import media


@pytest.mark.parametrize('size', [16 * 1024 * 1024, 20 * 1024 * 1024, 20 * 1024 * 1024 + 1])
@pytest.mark.parametrize('transport', ['base64', 'content-length', 'chunked'])
def test_reference_size_boundary(size, transport, monkeypatch):
    limit = 20 * 1024 * 1024
    monkeypatch.setattr(config, 'REFERENCE_IMAGE_MAX_BYTES', limit)
    output = BytesIO()
    Image.new('RGB', (48, 48)).save(output, 'PNG')
    # Valid PNG plus trailing bytes isolates byte-size validation from pixel limits.
    data = output.getvalue().ljust(size, b'\0')
    class Content:
        async def iter_chunked(self, chunk_size):
            for index in range(0, len(data), chunk_size):
                yield data[index:index + chunk_size]
    def read():
        if transport == 'base64':
            return media._decode_data_image('data:image/png;base64,' + base64.b64encode(data).decode())
        response = SimpleNamespace(content=Content(), headers=(
            {'Content-Length': str(size)} if transport == 'content-length' else {}))
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(media._read_response_image(response))
        finally:
            loop.close()
    if size > limit:
        with pytest.raises(ValueError, match=r'20 MiB.*20,971,520'):
            read()
    else:
        actual, suffix = read()
        assert actual == data and suffix == '.png'
