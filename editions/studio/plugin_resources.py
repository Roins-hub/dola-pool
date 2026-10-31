"""Use the fixed plugin parser, with all network IO restricted to the original ISP."""
import asyncio
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import aiohttp
from video_delivery import known_watermarked, NoWatermarkAvailable

ROOT = Path(__file__).resolve().parent
EXTENSION_DIR = ROOT / 'extensions/dola30-plugin1660'


async def run_resolver(payload):
    node = os.environ.get('DOLA_PLUGIN_NODE') or shutil.which('node')
    if not node and Path(r'C:\Program Files\nodejs\node.exe').is_file():
        node = r'C:\Program Files\nodejs\node.exe'
    if not node:
        raise RuntimeError('专家无水印解析需要Node.js，请配置DOLA_PLUGIN_NODE')
    process = await asyncio.create_subprocess_exec(node, str(ROOT / 'plugin-resolver.cjs'),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), 35)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode or len(stdout) > 8 * 1024 * 1024:
        raise RuntimeError('原插件资源解析未完成，保留原会话等待补收')
    return json.loads(stdout)


def _allowed_url(url, kind='media'):
    try:
        parsed = urlsplit(url)
        port = 443 if parsed.scheme == 'https' else 80
        if parsed.scheme not in ('https', 'http') or parsed.username or parsed.password or parsed.port not in (None, port):
            return False
        if kind == 'fallback':
            return parsed.scheme == 'https' and parsed.hostname == 'vod-urls-mya.byteintlapi.com'
        return bool(re.fullmatch(r'v\d+-dola\.dola\.com', parsed.hostname or ''))
    except (ValueError, TypeError):
        return False


def validated_resource(resource, source, *, official_identity_verified=False):
    url = resource.get('url') if isinstance(resource, dict) else None
    official = source == 'dola_original_api' and official_identity_verified is True
    if not _allowed_url(url) or known_watermarked(url):
        return None
    if parse_qs(urlsplit(url).query).get('lr') != ['unwatermarked'] and not official:
        return None
    return {'url': url, 'download_source': source, 'watermark': 'verified_download',
            'width': resource.get('width'), 'height': resource.get('height'),
            'official_identity_verified': official}


def _reserve(session):
    if session.proxy_url and session.own_isp:
        import config, proxy_usage
        proxy_usage.usage.reserve(config._parse_proxy(session.proxy_url), 1)


@asynccontextmanager
async def _network_lease(session):
    lane = getattr(session, 'network_lane', None)
    if lane:
        async with lane:
            yield
    else:
        yield


async def _fallback_responses(session, requests, vid):
    responses = []
    # External query hosts never receive the authenticated Dola session or Cookie.
    async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=15)) as client:
        for request in requests:
            url = request['request_url']
            if not _allowed_url(url, 'fallback'):
                continue
            query_vid = (parse_qs(urlsplit(url).query).get('video_id') or parse_qs(urlsplit(url).query).get('vid') or [''])[0]
            if vid and query_vid and query_vid != vid:
                continue
            _reserve(session)
            try:
                async with _network_lease(session), client.get(url, proxy=session.proxy_url, allow_redirects=False,
                        headers={'Accept': 'application/json,text/plain,*/*', 'User-Agent': session.state.get('user_agent', '')}) as response:
                    raw = await response.content.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024:
                        continue
                    payload = json.loads(raw)
                    response_vid = str((payload.get('data') or {}).get('video_id') or payload.get('video_id') or '')
                    if vid and response_vid and response_vid != vid:
                        continue
                    responses.append({**request, 'status': response.status, 'payload': payload})
            except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
                continue
    return responses


async def _download_unlimited(session, url, *, preview=False):
    """Download only the exact returned media URL, without redirects or credentials."""
    import config
    from video_metadata import read_video_duration
    if not _allowed_url(url):
        raise NoWatermarkAvailable()
    destination = Path(config.DOWNLOAD_DIR) / ('expert30_' + __import__('uuid').uuid4().hex + '.mp4')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.mp4.part')
    _reserve(session)
    try:
        async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=180)) as client:
            async with client.get(url, proxy=session.proxy_url, allow_redirects=False,
                    headers={'User-Agent': session.state.get('user_agent', ''),
                             'Referer': f'https://www.dola.com/chat/{session.conversation_id}'}) as response:
                if response.status != 200:
                    raise NoWatermarkAvailable()
                count = 0
                with temporary.open('wb') as stream:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        count += len(chunk)
                        if count > 512 * 1024 * 1024:
                            raise ValueError('视频文件超过下载上限')
                        stream.write(chunk)
                if response.content_length is not None and response.content_length != count:
                    raise ValueError('视频文件未下载完整')
        read_video_duration(temporary)
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


async def _download(session, url, *, preview=False):
    from video_worker_ui import _download_slots
    async with _download_slots, _network_lease(session):
        return await _download_unlimited(session, url, preview=preview)


async def resolve_clip(session, clip, resolution='720p'):
    """One clip in, one identity-bound original out. Failed slots are never renumbered."""
    from video_metadata import read_video_duration
    vid = clip.get('vid') or ''
    payload = {'conversation_id': session.conversation_id, 'resolution': resolution,
               'video_models': clip.get('video_models') or [], 'fallback_apis': clip.get('fallback_apis') or []}
    prepared = await run_resolver(payload)
    candidates = [validated_resource(r, 'dola_video_model') for r in prepared.get('model_resources', [])]
    responses = await _fallback_responses(session, prepared['fallback_requests'], vid)
    if responses:
        decoded = await run_resolver({**payload, 'fallback_responses': responses})
        candidates += [validated_resource(r, 'dola_plugin_fallback') for r in decoded.get('fallback_results', [])]
    # Original VID query is independent of the model/fallback paths.
    preview = None
    if not any(candidates) and not vid and clip.get('preview_urls'):
        preview = await _download(session, clip['preview_urls'][0], preview=True)
        with preview.open('rb') as stream:
            match = re.search(rb'vid:(v[0-9a-zA-Z]{20,50})', stream.read(1024 * 1024))
        if match:
            vid = match.group(1).decode()
    if not any(candidates) and vid:
        from dola_watermark import PARAMS
        _reserve(session)
        async with _network_lease(session), session.session.post('https://www.dola.com/creativity/resource/get_without_watermark',
                params=PARAMS, json={'vid': [vid]}, proxy=session.proxy_url, allow_redirects=False) as response:
            raw = await response.content.read(4 * 1024 * 1024 + 1)
            official = json.loads(raw) if response.status == 200 and len(raw) <= 4 * 1024 * 1024 else {}
        data = official.get('data') or {}
        if official.get('code') in (0, '0') and data.get('without_watermark') is True:
            item = (data.get('download_video') or {}).get(vid)
            if isinstance(item, dict):
                if item.get('video_model'):
                    decoded = await run_resolver({'conversation_id': session.conversation_id,
                        'video_models': [item['video_model']], 'fallback_apis': []})
                    candidates += [validated_resource(r, 'dola_original_api', official_identity_verified=True) for r in decoded.get('model_resources', [])]
                candidates.append(validated_resource({'url': item.get('download_url') or item.get('url')}, 'dola_original_api', official_identity_verified=True))
            elif isinstance(item, str):
                candidates.append(validated_resource({'url': item}, 'dola_original_api', official_identity_verified=True))
    try:
        for candidate in candidates:
            if not candidate:
                continue
            try:
                local = await _download(session, candidate['url'])
                with local.open('rb') as stream:
                    embedded = re.search(rb'vid:(v[0-9a-zA-Z]{20,50})', stream.read(1024 * 1024))
                if vid and embedded and embedded.group(1).decode() != vid:
                    local.unlink(missing_ok=True)
                    continue
                if not vid and embedded:
                    vid = embedded.group(1).decode()
                return {'clip_id': clip['clip_id'], 'index': clip['index'], 'vid': vid,
                    'creation_id': clip.get('creation_id', ''), 'local_path': str(local),
                    'video_url': candidate['url'], 'download_source': candidate['download_source'],
                    'watermark': 'verified_download', 'actual_duration': read_video_duration(local),
                    'plugin_provenance': {'conversation_id': session.conversation_id, 'clip_id': clip['clip_id'],
                                          'vid': vid, 'creation_id': clip.get('creation_id', ''),
                                          'official_without_watermark': candidate.get('official_identity_verified', False),
                                          'original_response_url': True, 'fallback_host': 'vod-urls-mya.byteintlapi.com'},
                    'account': session.account, 'conversation_id': session.conversation_id}
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError, NoWatermarkAvailable):
                continue
    finally:
        # A preview is diagnostic only and is never returned as a completed result.
        if preview:
            preview.unlink(missing_ok=True)
    raise NoWatermarkAvailable()
