"""Doubao-only prompt and native material helpers.

These helpers never send a chat message or alter native upload capabilities.
Paths passed here must already be resolved and authorized by the API owner;
plain user-supplied path strings are deliberately not accepted.
"""
import asyncio
import mimetypes
from pathlib import Path
import time

from providers.base import ProviderError


RATIOS = frozenset(('1:1', '3:4', '4:3', '9:16', '16:9', '21:9'))
KINDS = ('image', 'video', 'audio')
REFERENCE_KEYS = {'image': 'images', 'video': 'video', 'audio': 'audio'}
SPEC_MARK = '【视频生成规格】'


def reject_removed_cast(request):
    if any(request.get(key) not in (None, '', [], '[]') for key in ('cast', 'cast_ids')):
        raise ProviderError('unsupported_cast', '角色素材库已移除，请使用独立参考素材上传。')


def normalize_video_spec(request):
    """Validate explicit preferences without remapping duration or credit cost."""
    if not isinstance(request, dict):
        raise ProviderError('invalid_video_spec', '豆包视频参数必须是一个对象。')
    reject_removed_cast(request)
    duration = request.get('duration')
    if type(duration) is not int or not 1 <= duration <= 300:
        raise ProviderError('unsupported_duration', '豆包视频时长必须为 1 至 300 秒的整数。')
    ratio = request.get('ratio') or request.get('size') or '16:9'
    if not isinstance(ratio, str) or ratio not in RATIOS:
        raise ProviderError('unsupported_ratio', '请选择豆包支持的视频比例。')
    resolution = request.get('resolution', 'auto')
    if resolution not in ('auto', '720p', '1080p'):
        raise ProviderError('unsupported_resolution', '分辨率仅支持原生默认、720p 或 1080p。')
    model, prompt = request.get('model'), request.get('prompt')
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ProviderError('unsupported_model', '请明确选择豆包模型。')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
        raise ProviderError('invalid_prompt', '提示词不能为空且最多 20000 字符。')
    lead = request.get('lead_prompt')
    if lead is not None and (not isinstance(lead, str) or len(lead) > 4000):
        raise ProviderError('invalid_prompt', '前置提示词最多 4000 字符。')
    return {'model': model, 'duration': duration, 'ratio': ratio, 'resolution': resolution}


def compose_video_prompt(request, model_label):
    """Build one native message, keeping the user's text byte-for-byte intact.

    The specification is a preference, not evidence of native model selection,
    resolution, duration, or platform billing. The caller must verify those.
    """
    spec = normalize_video_spec(request)
    if (not isinstance(model_label, str) or not model_label.strip() or len(model_label) > 200
            or any(ord(c) < 32 for c in model_label)):
        raise ProviderError('unsupported_model', '豆包模型显示名称无效。')
    sentence = f'{SPEC_MARK}请使用{model_label.strip()}生成一条{spec["duration"]}秒、'
    if spec['resolution'] != 'auto':
        sentence += spec['resolution'] + '、'
    sentence += spec['ratio'] + '的视频。'
    original = request['prompt']
    if original.endswith(sentence):
        return original
    parts = []
    if (request.get('lead_prompt') or '').strip():
        parts.append(request['lead_prompt'])
    parts.extend((original, sentence))
    return '\n'.join(parts)


def _reference_fields(reference):
    if not isinstance(reference, dict) or reference.get('kind') not in KINDS:
        raise ProviderError('unsupported_reference', '豆包仅支持图片、视频和音频参考素材。')
    path, size = reference.get('path'), reference.get('size_bytes')
    if not isinstance(path, Path) or type(size) is not int or size <= 0:
        raise ProviderError('unsupported_reference', '参考素材必须由服务端确认路径和有效大小。')
    name = reference.get('name', path.name)
    if (not isinstance(name, str) or not name or len(name) > 255
            or '/' in name or '\\' in name or any(ord(c) < 32 for c in name)):
        raise ProviderError('unsupported_reference', '参考素材名称无效。')
    return {'kind': reference['kind'], 'path': path, 'name': name, 'size_bytes': size}


def normalize_references(references, capability):
    """Validate already-owned materials against this verified capability only."""
    if not isinstance(references, list) or len(references) > 50:
        raise ProviderError('unsupported_reference', '参考素材必须为列表且总数最多 50 个。')
    specs = capability.get('references', {})
    result, counts = [], dict.fromkeys(KINDS, 0)
    for reference in references:
        value = _reference_fields(reference)
        kind = value['kind']
        cap = specs.get(REFERENCE_KEYS[kind], {})
        counts[kind] += 1
        if (cap.get('enabled') is not True or type(cap.get('max_count')) is not int
                or not 1 <= cap['max_count'] <= 50 or counts[kind] > cap['max_count']
                or type(cap.get('max_bytes')) is not int or cap['max_bytes'] <= 0
                or value['size_bytes'] > cap['max_bytes']):
            raise ProviderError('unsupported_reference', '参考素材类型尚未验证，或数量、大小超过该模型限制。')
        result.append(value)
    return result


def _accepts(accept, reference):
    tokens = [token.strip().lower() for token in (accept or '').split(',') if token.strip()]
    if not tokens:
        return True  # HTML's empty accept permits all; readiness still required.
    suffix = reference['path'].suffix.lower()
    mime = mimetypes.guess_type(reference['path'].name)[0] or ''
    return any(token in (suffix, mime) or token == reference['kind']+'/*' or token == '*/*' for token in tokens)


async def attach_native(page, references, selectors, *, timeout=180, native_state=None):
    """Upload once through actual file inputs and wait for fresh ready items.

    ``upload_ready`` must identify completed native attachment items, never the
    original file input. ``upload_error`` and ``upload_pending`` are optional
    observed native selectors. Inputs' accept/multiple attributes are read only.
    The caller remains responsible for task ownership and capability validation.
    """
    values = [_reference_fields(reference) for reference in references]
    result = {'ok': True, 'counts': {kind: 0 for kind in KINDS}, 'files': []}
    if not values:
        return result
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ProviderError('reference_upload_timeout', '参考素材上传等待时间无效。')
    use_native = isinstance(native_state, dict) and native_state.get('supported') is True
    ready_selector = selectors.get('upload_ready')
    if not use_native and (not isinstance(ready_selector, str) or not ready_selector.strip()):
        raise ProviderError('unsupported_reference', '豆包原生附件完成状态尚未确认。')
    deadline = time.monotonic() + timeout
    chooser_mode = (not selectors.get('upload') and not any(selectors.get('upload_'+value['kind']) for value in values)
                    and selectors.get('upload_trigger') and selectors.get('upload_option'))
    grouped = {}
    if not chooser_mode:
        for value in values:
            selector = selectors.get('upload_'+value['kind']) or selectors.get('upload')
            if not isinstance(selector, str) or not selector.strip():
                raise ProviderError('unsupported_reference', '豆包原生参考素材上传入口未就绪。')
            grouped.setdefault(selector, []).append(value)
    # Preflight every group before touching any input, so unsupported audio or
    # video never leaves a half-attached request containing images alone.
    controls = []
    for selector, group in grouped.items():
        control = page.locator(selector)
        if await control.count() != 1:
            raise ProviderError('unsupported_reference', '豆包原生上传入口未唯一确认。')
        props = await control.evaluate('(n)=>({tag:n.tagName,type:n.type,accept:n.accept,multiple:n.multiple,disabled:n.disabled})')
        if use_native and (not all(_accepts(props['accept'], value) for value in group) or (len(group)>1 and not props['multiple'])):
            from .native_bridge import authorize_native_upload
            if await authorize_native_upload(page, control):
                props = await control.evaluate('(n)=>({tag:n.tagName,type:n.type,accept:n.accept,multiple:n.multiple,disabled:n.disabled})')
        if (props['tag'] != 'INPUT' or props['type'] != 'file' or props['disabled']
                or (len(group) > 1 and not props['multiple'])
                or not all(_accepts(props['accept'], value) for value in group)):
            raise ProviderError('unsupported_reference', '豆包原生上传入口不接受所选参考素材，未修改其支持类型。')
        controls.append((control, group, False))
    if chooser_mode:
        try:
            await page.locator(selectors['upload_trigger']).click(timeout=timeout*1000)
            remaining = max(1, (deadline-time.monotonic())*1000)
            async with page.expect_file_chooser(timeout=remaining) as event:
                await page.locator(selectors['upload_option']).click(timeout=remaining)
            chooser = await event.value
            props = await chooser.element.evaluate('(n)=>({tag:n.tagName,type:n.type,accept:n.accept,multiple:n.multiple,disabled:n.disabled})')
        except Exception:
            raise ProviderError('unsupported_reference', '豆包原生文件选择入口未就绪。') from None
        if use_native and (not all(_accepts(props['accept'], value) for value in values) or (len(values)>1 and not props['multiple'])):
            from .native_bridge import authorize_native_upload
            if await authorize_native_upload(page, chooser.element):
                props = await chooser.element.evaluate('(n)=>({tag:n.tagName,type:n.type,accept:n.accept,multiple:n.multiple,disabled:n.disabled})')
        if (props['tag'] != 'INPUT' or props['type'] != 'file' or props['disabled']
                or (len(values) > 1 and not props['multiple'])
                or not all(_accepts(props['accept'], value) for value in values)):
            raise ProviderError('unsupported_reference', '豆包原生文件选择入口不接受所选参考素材，未修改其支持类型。')
        controls.append((chooser, values, True))
    ready = page.locator(ready_selector) if not use_native else None
    errors = page.locator(selectors['upload_error']) if selectors.get('upload_error') else None
    pending = page.locator(selectors['upload_pending']) if selectors.get('upload_pending') else None
    for control, group, is_chooser in controls:
        baseline = await ready.count() if ready else 0
        previous_errors = await errors.all_inner_texts() if errors else []
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError('reference_upload_timeout', '豆包参考素材上传超时，未提交提示词。', 503)
        try:
            method = control.set_files if is_chooser else control.set_input_files
            await method([str(value['path']) for value in group], timeout=remaining*1000)
        except Exception:
            raise ProviderError('reference_upload_failed', '豆包原生参考素材上传未完成，未提交提示词。', 503) from None
        if use_native:
            continue  # All groups are checked together against the native store.
        while True:
            if errors:
                current_errors = await errors.all_inner_texts()
                if len(current_errors) > len(previous_errors) or any(text not in previous_errors for text in current_errors):
                    raise ProviderError('reference_upload_failed', '豆包原生页面报告参考素材上传失败，未提交提示词。', 503)
            if await ready.count() >= baseline+len(group) and (not pending or not await pending.count()):
                break
            if time.monotonic() >= deadline:
                raise ProviderError('reference_upload_timeout', '豆包参考素材未确认上传完成，未提交提示词。', 503)
            await asyncio.sleep(min(.1, max(0, deadline-time.monotonic())))
    if use_native:
        from .native_bridge import wait_native_uploads
        await wait_native_uploads(page, [{'kind': value['kind'], 'name': value['name']} for value in values],
                                  native_state.get('before_keys', []), timeout=max(.001, deadline-time.monotonic()))
    for value in values:
        result['counts'][value['kind']] += 1
        result['files'].append(value['name'])
    return result
