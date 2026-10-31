"""Public task evidence. Never persist request bodies, credentials or media URLs here."""
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

from video_capabilities import to_wire_model

PHASES = frozenset(('checking_verification', 'waiting_verification', 'verification_continue', 'verification_incomplete',
                   'queued', 'preparing', 'trace_ready', 'sending', 'submitted', 'awaiting_receipt',
                   'accepted', 'polling', 'media_ready', 'downloading', 'completed', 'not_accepted',
                   'request_mismatch', 'download_failed', 'duration_mismatch', 'failed',
                   'platform_copyright', 'platform_audio_copyright', 'platform_rejected',
                   'platform_rejected_no_charge', 'platform_review_rejected', 'query_timeout',
                   'account_retry', 'submission_unknown', 'waiting_lane', 'download_metadata',
                   'ratio_mismatch', 'resolution_mismatch', 'video_metadata_missing', 'text_only_ack',
                   'no_watermark_unavailable', 'watermark_setup_failed', 'output_validation_failed',
                   'expert_weekly_limit', 'partial_results', 'batch_paused', 'waiting_quota', 'subscription_required',
                   'platform_daily_limit'))


class SubmissionUnconfirmedError(RuntimeError):
    task_phase = 'not_accepted'


class RequestMismatchError(RuntimeError):
    task_phase = 'request_mismatch'


def safe_text(value):
    text = str(value or '')
    text = re.sub(r'(?im)\b(?:cookie|authorization|proxy-authorization)\s*[:=][^\r\n]*', '[凭据已隐藏]', text)
    text = re.sub(r'(?:https?|socks5h?)://[^\s<>"\']+', '[链接已隐藏]', text, flags=re.I)
    text = re.sub(r'(?i)\b(?:sessionid(?:_ss)?|uid_tt(?:_ss)?|msToken|token|password|a_bogus)\s*[=:]\s*[^\s;,]+',
                  '[凭据已隐藏]', text)
    return text[:6000]


def normalize_model(model):
    try:
        return to_wire_model(model)
    except ValueError:
        # Keep unknown wire values distinct so an unexpected model remains a mismatch.
        return str(model or '').lower().replace('-', '_')


def matches_submitted_prompt(text, prompt, ratio=None):
    if not isinstance(text, str) or not isinstance(prompt, str):
        return False
    if text == prompt:
        return True
    # Compare a copy: only the observed label, ratio and removed trailing ASCII spaces.
    variants = (prompt, prompt.rstrip(' '))
    if text in variants:
        return True
    label = next((prefix for prefix in ('Generiertes Video: ', 'Generated video: ')
                  if text.startswith(prefix)), None)
    if label is None:
        return False
    native_text = text[len(label):]
    if native_text in variants:
        return True
    ratios = ('21:9', '16:9', '4:3', '1:1', '3:4', '9:16')
    if ratio is not None:
        ratios = (ratio,) if ratio in ratios else ()
    return any(native_text == variant + ', ' + aspect for variant in variants for aspect in ratios)


def _prompt_diagnostics(texts, prompt, ratio):
    """Return scalar metadata only; normalization here never affects audit decisions."""
    if not isinstance(prompt, str):
        return {}
    def digest(text):
        return hashlib.sha256(text.encode('utf-8', errors='surrogatepass')).hexdigest()
    def line_endings(text):
        return text.replace('\r\n', '\n').replace('\r', '\n')
    def rendered(text):
        return re.sub(r'\n+', '\n', line_endings(text).replace('\xa0', ' ')).strip()
    result = {'prompt_expected_chars': len(prompt), 'prompt_expected_sha256': digest(prompt),
              'prompt_text_block_count': len(texts)}
    if len(texts) != 1:
        result['prompt_diagnostic_scope'] = 'multiple_text_blocks' if texts else 'missing_text_blocks'
        return result  # Do not concatenate blocks or select an arbitrary one.
    observed = compared = texts[0]
    scope = 'single_text_block'
    label = next((prefix for prefix in ('Generiertes Video: ', 'Generated video: ')
                  if observed.startswith(prefix)), None)
    if label is not None and observed != prompt:
        compared, scope = observed[len(label):], 'known_native_unwrapped'
        ratios = ('21:9', '16:9', '4:3', '1:1', '3:4', '9:16')
        allowed = ratios if ratio is None else ((ratio,) if ratio in ratios else ())
        suffix = next((', ' + aspect for aspect in allowed if compared.endswith(', ' + aspect)), None)
        # An aspect ratio can be literal user text; do not remove it when the body
        # already matches the expected prompt under the diagnostic rendering rule.
        if suffix is not None and rendered(compared) != rendered(prompt):
            compared = compared[:-len(suffix)]
    first_diff = next((i for i, (actual, expected) in enumerate(zip(compared, prompt))
                       if actual != expected), None)
    if first_diff is None and compared != prompt:
        first_diff = min(len(compared), len(prompt))
    result.update(prompt_observed_chars=len(observed), prompt_observed_sha256=digest(observed),
                  prompt_compared_chars=len(compared), prompt_compared_sha256=digest(compared),
                  prompt_first_diff_index=first_diff, prompt_diagnostic_scope=scope,
                  prompt_line_endings_equal=line_endings(compared) == line_endings(prompt),
                  prompt_render_equal=bool(prompt.strip()) and rendered(compared) == rendered(prompt))
    return result


def _unknown_audit(reason):
    return {'model': None, 'duration': None, 'ratio': None, 'model_matches': None,
            'duration_matches': None, 'ratio_matches': None, 'prompt_matches': None,
            'prompt_exact': None, 'matches': False, 'audit_status': 'unknown', 'reason': reason}


def submission_audit(observed):
    return observed[-1] if observed else _unknown_audit('未捕获提交请求，无法核对')


def audit_submission(raw, model, duration, ratio, prompt):
    """Audit a submitted request; unknown fields are not proof of platform rejection."""
    evidence = _unknown_audit('提交请求无法解析，无法核对')
    texts = None
    try:
        body = json.loads(raw)
        param = body['chat_ability']['ability_param']
        if isinstance(param, str):
            param = json.loads(param)
        if not isinstance(param, dict):
            return evidence
        actual_model, actual_duration = param.get('model'), param.get('duration')
        actual_ratio = param.get('ratio') or param.get('aspect_ratio')
        if isinstance(actual_model, str) and actual_model:
            evidence['model'] = normalize_model(actual_model)
            evidence['model_matches'] = evidence['model'] == normalize_model(model)
        if isinstance(actual_duration, (int, float, str)) and not isinstance(actual_duration, bool):
            try:
                if math.isfinite(float(actual_duration)):
                    evidence['duration'] = actual_duration
                    evidence['duration_matches'] = float(actual_duration) == float(duration)
            except (ValueError, TypeError, OverflowError):
                pass
        if isinstance(actual_ratio, str) and actual_ratio:
            evidence['ratio'] = actual_ratio
            evidence['ratio_matches'] = actual_ratio == ratio if ratio else True
        elif not ratio:
            evidence['ratio_matches'] = True  # No explicit requested ratio to verify.
        texts = []
        def walk(node, depth=0):
            if depth > 20:
                return
            if isinstance(node, dict):
                block = node.get('text_block')
                if isinstance(block, dict) and isinstance(block.get('text'), str):
                    texts.append(block['text'])
                for key, value in node.items():
                    if key != 'chat_ability':
                        walk(value, depth + 1)
            elif isinstance(node, list):
                for value in node:
                    walk(value, depth + 1)
        walk(body.get('messages', body.get('message', {})))
        if texts:
            evidence['prompt_exact'] = any(text == prompt for text in texts)
            evidence['prompt_matches'] = any(matches_submitted_prompt(text, prompt, evidence['ratio']) for text in texts)
        fields = (('model_matches', '模型'), ('duration_matches', '时长'),
                  ('ratio_matches', '画幅'), ('prompt_matches', '完整提示词'))
        mismatched = [label for key, label in fields if evidence[key] is False]
        unknown = [label for key, label in fields if evidence[key] is None]
        if mismatched:
            evidence.update(audit_status='mismatch', reason='、'.join(mismatched) + '与预期不符')
        elif unknown:
            evidence['reason'] = '未读取到可核对的' + '、'.join(unknown)
        else:
            evidence.update(matches=True,
                            audit_status='passed' if evidence['prompt_exact'] else 'format_equivalent',
                            reason='参数与完整提示词核对通过' if evidence['prompt_exact'] else
                                   '提示词仅存在已确认的平台标签、画幅后缀或末尾普通空格差异')
    except (KeyError, TypeError, ValueError):
        pass
    if texts is not None and not evidence['matches']:
        evidence.update(_prompt_diagnostics(texts, prompt, evidence['ratio']))
    # Do not allow arbitrary values from an unexpected response schema into evidence.
    for key in ('model', 'ratio'):
        value = evidence[key]
        evidence[key] = safe_text(value)[:80] if isinstance(value, str) else None
    if not isinstance(evidence['duration'], (int, float, str)):
        evidence['duration'] = None
    elif isinstance(evidence['duration'], str):
        evidence['duration'] = safe_text(evidence['duration'])[:20]
    return evidence


def observe_submission(page, model, duration, ratio, prompt, on_event=None):
    observed = []
    def capture(request):
        url = urlsplit(request.url)
        if (request.method != 'POST' or url.hostname not in ('www.dola.com', 'dola.com') or
                url.path.rstrip('/') != '/chat/completion'):
            return
        evidence = audit_submission(request.post_data or '', model, duration, ratio, prompt)
        observed.append(evidence)
        if on_event:
            on_event('submitted', evidence)
    page.on('request', capture)
    return observed


def public_details(details):
    result = {}
    if isinstance(details, dict):
        for key in ('reply', 'reason', 'model', 'duration', 'ratio', 'prompt_matches', 'matches',
                    'model_matches', 'duration_matches', 'ratio_matches', 'prompt_exact', 'audit_status',
                    'actual_duration', 'video_count', 'account', 'conversation_id'):
            value = details.get(key)
            if isinstance(value, str):
                result[key] = safe_text(value)
            elif isinstance(value, (bool, int, float)):
                result[key] = value
        # Diagnostics have their own strict scalar whitelist; never echo a text,
        # fragment, request body/header, arbitrary string or nested object.
        for key in ('prompt_expected_chars', 'prompt_observed_chars', 'prompt_compared_chars',
                    'prompt_first_diff_index', 'prompt_text_block_count'):
            value = details.get(key)
            if type(value) is int and 0 <= value < 2 ** 31:
                result[key] = value
        for key in ('prompt_expected_sha256', 'prompt_observed_sha256', 'prompt_compared_sha256'):
            value = details.get(key)
            if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value):
                result[key] = value
        for key in ('prompt_line_endings_equal', 'prompt_render_equal'):
            value = details.get(key)
            if type(value) is bool:
                result[key] = value
        scope = details.get('prompt_diagnostic_scope')
        if isinstance(scope, str) and scope in ('single_text_block', 'known_native_unwrapped',
                                              'multiple_text_blocks', 'missing_text_blocks'):
            result['prompt_diagnostic_scope'] = scope
        dimensions = details.get('dimensions')
        if (isinstance(dimensions, dict)
                and all(type(dimensions.get(key)) is int and 0 < dimensions[key] <= 65535
                        for key in ('width', 'height'))):
            result['dimensions'] = {key: dimensions[key] for key in ('width', 'height')}
        source = details.get('download_source')
        if isinstance(source, str) and source in ('official_download', 'official_preview', 'preview', 'dola_video_model', 'dola_original_api'):
            result['download_source'] = source
        watermark = details.get('watermark')
        if isinstance(watermark, str) and watermark in ('verified_download', 'unknown'):
            result['watermark'] = watermark
        if details.get('verification_scope') in ('platform_download_metadata', 'unknown'):
            result['verification_scope'] = details['verification_scope']
    return result
