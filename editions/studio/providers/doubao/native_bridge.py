"""DOM-channel control for the isolated Doubao legacy video-17 adapter."""
import asyncio
import json
import math
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit

from providers.base import ProviderError


SOURCE = Path(__file__).with_name('native_media.js').read_text(encoding='utf-8')
ATTR = 'data-doubao-native-media-status'


def _preference(value):
    if value not in ('auto', '720p', '1080p'):
        raise ProviderError('unsupported_resolution', '豆包分辨率仅支持原生默认、720p 或 1080p。')
    return value


def _expected(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 50:
        raise ProviderError('unsupported_reference', '豆包参考素材数量无效。')
    result = []
    for value in values:
        if (not isinstance(value, dict) or value.get('kind') not in ('image', 'video', 'audio')
                or not isinstance(value.get('name'), str) or not value['name'] or len(value['name']) > 255
                or any(ord(char) < 32 or char in '/\\' for char in value['name'])):
            raise ProviderError('unsupported_reference', '豆包参考素材类型或名称无效。')
        result.append({'kind': value['kind'], 'name': value['name']})
    return result


async def install_native_media(context, resolution='auto'):
    """Install before navigation; automatic preference leaves native defaults."""
    await context.add_init_script('globalThis.__DOUBAO_NATIVE_MEDIA_PREFERENCE__=' + json.dumps(_preference(resolution)) + ';\n' + SOURCE)


async def _command(page, options):
    try:
        parsed = urlsplit(page.url)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or not (host == 'doubao.com' or host.endswith('.doubao.com')):
            return {'supported': False, 'ready': False, 'reason_code': 'unsupported_origin'}
        result = await page.evaluate('''options=>{
          document.documentElement.setAttribute('data-doubao-native-media-command',JSON.stringify(options));
          document.dispatchEvent(new Event('doubao-native-media-command'));
          const raw=document.documentElement.getAttribute('data-doubao-native-media-status');
          return raw?JSON.parse(raw):{supported:false,ready:false,reason_code:'adapter_not_installed'};
        }''', options)
        return result if isinstance(result, dict) else {'supported': False, 'ready': False, 'reason_code': 'invalid_native_status'}
    except Exception:
        return {'supported': False, 'ready': False, 'reason_code': 'native_status_unavailable'}


async def prepare_native_media(page, *, resolution='auto', expected=None, before_keys=None, chat_id=None):
    options = {'op': 'prepare', 'resolution': _preference(resolution), 'expected': _expected(expected)}
    if before_keys is not None:
        if not isinstance(before_keys, list) or any(not isinstance(key, str) or len(key) > 256 for key in before_keys):
            raise ProviderError('unsupported_reference', '豆包原生附件基线无效。')
        options['before_keys'] = before_keys
    if chat_id is not None:
        if not isinstance(chat_id, str) or not 0 < len(chat_id) <= 200:
            raise ProviderError('unsupported_reference', '豆包原生会话标识无效。')
        options['chat_id'] = chat_id
    return await _command(page, options)


async def native_media_status(page):
    return await _command(page, {'op': 'status'})


async def authorize_native_upload(page, control):
    """Permit one input only after its native React-17/config binding is proven."""
    token = uuid.uuid4().hex
    attribute = 'data-doubao-native-upload-probe'
    previous = await control.get_attribute(attribute)
    await control.evaluate('(node,token)=>node.setAttribute("data-doubao-native-upload-probe",token)', token)
    try:
        state = await _command(page, {'op': 'authorize_upload', 'selector': f'[{attribute}="{token}"]'})
        return state.get('upload_input_authorized') is True
    finally:
        await control.evaluate('''(node,value)=>{if(value===null)node.removeAttribute('data-doubao-native-upload-probe');else node.setAttribute('data-doubao-native-upload-probe',value)}''', previous)


async def assert_native_ready(page):
    state = await native_media_status(page)
    if not state.get('supported'):
        raise ProviderError('native_media_unsupported', '豆包旧版视频素材接口尚未识别，提示词未发送。', 503)
    if not state.get('ready'):
        raise ProviderError('native_media_not_ready', '豆包原生素材、顺序或分辨率尚未确认，提示词未发送。', 409)
    return state


async def wait_native_uploads(page, expected, before_keys, *, timeout=180):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ProviderError('reference_upload_timeout', '豆包参考素材等待时间无效。')
    _expected(expected)
    state = await native_media_status(page)
    # Preserve the configured resolution and original upload baseline.
    state = await prepare_native_media(page, resolution=state.get('resolution', 'auto'), expected=expected, before_keys=before_keys)
    deadline = time.monotonic() + timeout
    while True:
        if not state.get('supported'):
            raise ProviderError('native_media_unsupported', '豆包原生素材接口发生变化，未提交提示词。', 503)
        if state.get('errors'):
            raise ProviderError('reference_upload_failed', '豆包素材上传失败、重复或顺序不符，未提交提示词。', 503)
        if state.get('ready'):
            return state
        if time.monotonic() >= deadline:
            raise ProviderError('reference_upload_timeout', '豆包原生附件尚未完成上传，未提交提示词。', 503)
        await asyncio.sleep(min(.3, max(0, deadline-time.monotonic())))
        state = await native_media_status(page)
