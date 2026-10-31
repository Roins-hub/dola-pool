"""Require platform download provenance before publishing a new video result.

This verifies the platform's original-download source, not every video frame.
Never manufacture provenance by editing a signed URL's query parameters.
"""
import base64
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from video_outcome import PlatformVideoError


UNAVAILABLE_REASON = '已有成片，但未取得可确认的官方无水印版本；保留原会话及已下载文件供补收，未重新生成，也未释放已受理额度。'


class NoWatermarkAvailable(PlatformVideoError):
    """Post-generation delivery failure; the pool must not rotate or refund."""
    task_phase = 'no_watermark_unavailable'

    def __init__(self):
        RuntimeError.__init__(self, UNAVAILABLE_REASON)


def known_watermarked(url):
    try:
        query = parse_qs(urlsplit(str(url or '')).query)
        for key, values in query.items():
            key = key.lower()
            for value in values:
                value = value.lower()
                if key in ('watermark', 'with_watermark', 'has_watermark') and value in ('1', 'true', 'yes'):
                    return True
                if key == 'lr' and (value == 'cici_ai' or ('watermark' in value and value != 'unwatermarked')):
                    return True
    except ValueError:
        pass
    return False


def _explicit_original(url):
    return parse_qs(urlsplit(url).query).get('lr') == ['unwatermarked'] and not known_watermarked(url)


def dola_download(video_model, fallback_url):
    """Choose an untouched platform video_model URL; fallback stays unverified."""
    candidates = []
    try:
        vm = json.loads(video_model or '{}') if isinstance(video_model, str) else video_model
        videos = (vm or {}).get('video_list') or {}
        entries = videos.values() if isinstance(videos, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                raw = entry.get('main_url') or ''
                if not isinstance(raw, str):
                    continue
                url = raw if raw.startswith(('https://', 'http://')) else base64.b64decode(raw).decode('utf-8')
                parsed = urlsplit(url)
                # Dola's own video_model also returns signed HTTP originals.
                # Preserve their exact URL, restricting this compatibility to
                # observed Dola media hosts; the watermark checks still apply.
                official_http = (parsed.scheme == 'http'
                                 and re.fullmatch(r'v\d+-dola\.dola\.com', parsed.hostname or '')
                                 and parsed.username is None and parsed.password is None
                                 and parsed.port in (None, 80))
                if (parsed.scheme != 'https' and not official_http) or not parsed.hostname or known_watermarked(url):
                    continue
                try:
                    bitrate = int(entry.get('bitrate') or entry.get('real_bitrate') or 0)
                except (TypeError, ValueError):
                    bitrate = 0
                candidates.append((_explicit_original(url), bitrate, url))
            except (TypeError, ValueError, UnicodeError):
                continue
    except (TypeError, ValueError, AttributeError):
        pass
    if candidates:
        verified, _, url = max(candidates, key=lambda item: item[:2])
        return {'url': url, 'download_source': 'dola_video_model',
                'watermark': 'verified_download' if verified else 'unknown'}
    if fallback_url and not known_watermarked(fallback_url):
        return {'url': fallback_url, 'download_source': 'preview', 'watermark': 'unknown'}
    raise NoWatermarkAvailable()


def qualified_delivery(result):
    provenance = result.get('plugin_provenance') or {}
    plugin_original = (result.get('download_source') == 'dola_plugin_fallback'
        and provenance.get('original_response_url') is True
        and provenance.get('fallback_host') == 'vod-urls-mya.byteintlapi.com'
        and bool(provenance.get('clip_id')) and bool(provenance.get('conversation_id')))
    return ((result.get('download_source') in ('dola_video_model', 'dola_original_api', 'official_download') or plugin_original)
            and result.get('watermark') == 'verified_download'
            and not any(known_watermarked(result.get(key)) for key in ('video_url', 'source_url')))


def delivery_metadata(store, task_id, result):
    """Keep private, content-bound provenance so cached recovery cannot upgrade previews.

    Nothing is written into the web-served download directory. Existing files
    and historical tasks are retained; an unqualified result only loses its
    public result link. The manifest allows local recovery by task ID.
    """
    path = Path(result['local_path']).resolve()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    base = path.parent if store.db_path == ':memory:' else Path(store.db_path).resolve().parent
    manifest_dir = base / '.result_provenance'
    manifest = manifest_dir / (hashlib.sha256(str(task_id).encode()).hexdigest() + '.json')
    metadata = {key: result.get(key) for key in ('download_source', 'watermark', 'video_url', 'source_url', 'plugin_provenance',
                                                'vid', 'clip_id', 'creation_id', 'conversation_id')}
    cached = False
    # Missing provenance is allowed only for the exact file already verified
    # by this task; explicit unverified/preview evidence always wins.
    if not any(metadata.get(key) for key in ('download_source', 'watermark', 'video_url', 'source_url', 'plugin_provenance')):
        try:
            saved = json.loads(manifest.read_text(encoding='utf-8'))
            identities_match = not any(metadata.get(key) and (saved.get('metadata') or {}).get(key)
                and metadata[key] != saved['metadata'][key] for key in ('vid', 'clip_id', 'creation_id', 'conversation_id'))
            if saved.get('sha256') == sha256 and saved.get('local_path') == str(path) and identities_match:
                metadata = saved['metadata']
                cached = True
        except (OSError, ValueError, TypeError, KeyError):
            pass
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({'task_id': str(task_id), 'local_path': str(path),
        'sha256': sha256, 'metadata': metadata}, ensure_ascii=False), encoding='utf-8')
    return {**metadata, '_cached_provenance': cached,
            'verification_scope': 'platform_download_metadata' if qualified_delivery(metadata) else 'unknown'}
