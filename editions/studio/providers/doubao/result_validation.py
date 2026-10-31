"""Compare delivered video metadata with Doubao generation preferences."""
import math
import re

from providers.base import ProviderError
from .downloads import read_video_dimensions


def _dimensions(value):
    if not isinstance(value, dict):
        return None
    width, height = value.get('width'), value.get('height')
    if any(isinstance(number, bool) or not isinstance(number, (int, float))
           or not math.isfinite(number) or not 0 < number <= 65535
           for number in (width, height)):
        return None
    return width, height


def validate_delivered(request, media):
    """Fail without removing the file if output does not satisfy the request.

    A verified_download marker proves official download-URL evidence only;
    it does not claim computer vision inspection of visible watermarks.
    """
    dimensions = _dimensions(media.get('dimensions'))
    if dimensions is None:
        path = media.get('output_path') or media.get('local_path')
        if path:
            try:
                dimensions = _dimensions(read_video_dimensions(path))
            except (OSError, ValueError, TypeError):
                pass
    if dimensions is None:
        raise ProviderError('video_metadata_missing', '成片已保留，但未读取到有效视频尺寸，无法确认请求规格。', 503)
    width, height = dimensions
    ratio = request.get('ratio') or 'default'
    if ratio not in ('default', 'auto'):
        match = re.fullmatch(r'(\d+):(\d+)', str(ratio))
        if not match or not all(int(part) > 0 for part in match.groups()):
            raise ProviderError('ratio_mismatch', '成片已保留，但请求比例无效，无法确认比例。', 503)
        expected = int(match.group(1)) / int(match.group(2))
        if abs(width / height - expected) / expected > .03:
            raise ProviderError('ratio_mismatch', f'成片实际尺寸 {width:g}×{height:g}，与请求比例 {ratio} 不符；文件已保留，未重新生成。', 503)
    resolution = request.get('resolution', 'auto')
    required = {'720p': 720, '1080p': 1080}.get(resolution)
    if required and min(width, height) < required:
        raise ProviderError('resolution_mismatch', f'成片实际尺寸 {width:g}×{height:g}，未达到请求的 {resolution}；文件已保留，未重新生成。', 503)
    if request.get('require_no_watermark') and media.get('watermark') != 'verified_download':
        raise ProviderError('no_watermark_unavailable', '成片已保留，但未确认官方无水印下载来源，未标记为验证通过。', 503)
