"""Only explicitly observed, expiring Doubao capabilities may be advertised."""
import json
import math
import re
import time

from providers.base import ProviderError
from .accounts import atomic_json


ORDINARY_DIALOG_MODELS = {'dialog_seedance_2.0_fast': 'Seedance 2.0 Fast',
                        'dialog_seedance_2.5': 'Seedance 2.5'}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _evidence(model, key):
    return _text(model.get('evidence', {}).get(key))


def _validate_features(model):
    features = model.get('features', {})
    if not isinstance(features, dict) or set(features) - {'resolution', 'no_watermark'}:
        raise ValueError()
    for key, feature in features.items():
        if not isinstance(feature, dict) or type(feature.get('enabled')) is not bool:
            raise ValueError()
        if not feature['enabled']:
            continue
        if not _evidence(model, key+'_task_id'):
            raise ValueError()
        if key == 'resolution':
            values = feature.get('values')
            if (not isinstance(values, list) or not values
                    or any(v not in ('720p', '1080p') for v in values)
                    or len(set(values)) != len(values)):
                raise ValueError()


def _validate_references(model):
    refs, selectors = model.get('references', {}), model['selectors']
    bridge = model.get('native_media_bridge') is True
    dialog = model.get('entry_mode') == 'ordinary_dialog'
    if model.get('native_media_bridge') not in (None, False, True):
        raise ValueError()
    if bridge and not _evidence(model, 'native_store_ready_task_id'):
        raise ValueError()
    if not isinstance(refs, dict) or set(refs) - {'images', 'audio', 'video'}:
        raise ValueError()
    for kind, reference in refs.items():
        if not isinstance(reference, dict) or type(reference.get('enabled')) is not bool:
            raise ValueError()
        image_trial = dialog and kind == 'images'
        native_image_state = reference.get('native_state') == 'attachment_state_v1'
        if 'native_state' in reference and (not image_trial or not native_image_state):
            raise ValueError()
        allowed = {'enabled', 'max_count', 'max_bytes'}
        if image_trial:
            allowed |= {'validation_status', 'local_only', 'upload_observation_id', 'native_state',
                        'multi_upload_observation_id'}
        if dialog and set(reference) - allowed:
            raise ValueError()
        if not reference['enabled']:
            continue
        if image_trial:
            # The observed native multi-input is shared by ordinary-dialog
            # models; it is not a 2.5-only feature. Runtime still requires every
            # attachment to finish in order before any prompt can be sent.
            multi = reference.get('multi_upload_observation_id')
            if multi is not None and (not native_image_state or model.get('id') not in ORDINARY_DIALOG_MODELS
                    or not isinstance(multi,str) or not re.fullmatch(r'[a-fA-F0-9]{64}',multi)):
                raise ValueError()
            if (reference.get('validation_status') != 'experimental'
                    or reference.get('local_only') is not True
                    or type(reference.get('max_count')) is not int
                    or not 1 <= reference['max_count'] <= (9 if multi else 1)
                    or not isinstance(reference.get('upload_observation_id'), str)
                    or not re.fullmatch(r'[a-fA-F0-9]{64}', reference['upload_observation_id'])):
                raise ValueError()
        if dialog and kind not in ('images', 'video'):
            raise ValueError()  # Other chat material types need their own adapter.
        if not bridge and not dialog and kind != 'images':
            raise ValueError()  # Legacy automation still has no native media uploader.
        task = {'images': 'image', 'audio': 'audio', 'video': 'video'}[kind]+'_task_id'
        direct_input = (_text(selectors.get('upload')) or
                        (dialog and _text(selectors.get('upload_'+('image' if kind == 'images' else kind)))))
        chooser = (bridge or dialog) and _text(selectors.get('upload_trigger')) and _text(selectors.get('upload_option'))
        if ((not image_trial and not _evidence(model, task))
                or not (_text(selectors.get('upload_ready')) or bridge) or not (direct_input or chooser)):
            raise ValueError()
        if native_image_state and not _text(selectors.get('upload_image')):
            raise ValueError()
        if dialog and not native_image_state and any(not _text(selectors.get(key)) for key in ('upload_pending', 'upload_error')):
            raise ValueError()
        if type(reference.get('max_count')) is not int or not 1 <= reference['max_count'] <= 50:
            raise ValueError()
        limit = (15 if image_trial else 30 if kind == 'images' else 200)*1024*1024
        if type(reference.get('max_bytes')) is not int or not 0 < reference['max_bytes'] <= limit:
            raise ValueError()


def _validate_combinations(model):
    combinations = model.get('combinations')
    if not isinstance(combinations, list) or not combinations:
        raise ValueError()
    resolutions = model.get('features', {}).get('resolution', {})
    seen = set()
    for combination in combinations:
        allowed = {'duration', 'ratio', 'resolution'}
        if model.get('entry_mode') == 'ordinary_dialog':
            allowed.add('evidence')
        if (not isinstance(combination, dict) or set(combination)-allowed
                or type(combination.get('duration')) is not int
                or combination['duration'] not in model['durations']
                or combination.get('ratio') not in model['ratios']):
            raise ValueError()
        resolution = combination.get('resolution', 'auto')
        if resolution not in ('auto', '720p', '1080p'):
            raise ValueError()
        if resolution != 'auto' and (resolutions.get('enabled') is not True or resolution not in resolutions.get('values', [])):
            raise ValueError()
        key = (combination['duration'], combination['ratio'], resolution)
        if key in seen:
            raise ValueError()
        seen.add(key)


def _validate_dialog(model):
    """A chat receipt alone cannot prove model, duration or aspect ratio."""
    canonical = ORDINARY_DIALOG_MODELS.get(model['id'])
    if not canonical or model.get('prompt_model_label', canonical) != canonical:
        raise ValueError()
    if model.get('native_media_bridge') not in (None, False):
        raise ValueError()
    if any(key in model['selectors'] for key in ('video_entry', 'model', 'duration', 'ratio')):
        raise ValueError()
    if any(item.get('enabled') is True for item in model.get('features', {}).values()):
        raise ValueError()
    _validate_combinations(model)
    if model.get('validation_status') == 'experimental':
        # Native menu observations allow this trial only; they are not delivered-video proof.
        allowed_selectors = {'composer', 'send', 'assistant', 'new_chat'}
        image_reference = model.get('references', {}).get('images', {})
        if image_reference.get('enabled') is True:
            allowed_selectors |= {'upload_image', 'upload_ready'}
            if image_reference.get('native_state') != 'attachment_state_v1':
                allowed_selectors |= {'upload', 'upload_trigger', 'upload_option', 'upload_pending', 'upload_error'}
        if (model['id'] != 'dialog_seedance_2.5' or model['durations'] != [30]
                or model['ratios'] != ['16:9'] or model.get('account_category') != '标准账号'
                or '试验' not in model['label']
                or set(model['selectors']) - allowed_selectors
                or any(item.get('enabled') is True for kind, item in model.get('references', {}).items()
                       if kind != 'images')
                or model['combinations'] != [{'duration': 30, 'ratio': '16:9'}]
                or not _evidence(model, 'native_options_observation_id')):
            raise ValueError()
        return
    combinations = model['combinations']
    if ({item['duration'] for item in combinations} != set(model['durations'])
            or {item['ratio'] for item in combinations} != set(model['ratios'])
            or len(model['durations']) != len(set(model['durations']))
            or len(model['ratios']) != len(set(model['ratios']))):
        raise ValueError()
    for item in combinations:
        evidence = item.get('evidence')
        if not isinstance(evidence, dict) or any(not _text(evidence.get(key))
                for key in ('task_id', 'conversation_id', 'artifact_sha256')):
            raise ValueError()
        if evidence.get('receipt_model') != canonical or not re.fullmatch(r'[a-fA-F0-9]{64}', evidence['artifact_sha256']):
            raise ValueError()
        actual = evidence.get('actual_duration')
        if (type(actual) not in (int, float) or not math.isfinite(actual)
                or abs(actual - item['duration']) > .5):
            raise ValueError()
        width, height = evidence.get('width'), evidence.get('height')
        if any(type(v) is not int or not 0 < v <= 65535 for v in (width, height)):
            raise ValueError()
        match = re.fullmatch(r'([1-9][0-9]*):([1-9][0-9]*)', item['ratio'])
        if match is None:
            raise ValueError()
        expected = int(match.group(1)) / int(match.group(2))
        if abs(width / height - expected) / expected > .03:
            raise ValueError()
    if model['evidence']['text_task_id'] not in {item['evidence']['task_id'] for item in combinations}:
        raise ValueError()
    if (model.get('references', {}).get('video', {}).get('enabled') is True
            and model['evidence']['video_task_id'] in {item['evidence']['task_id'] for item in combinations}):
        raise ValueError()  # A text-only artifact is not reference-video proof.


class Capabilities:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(value, dict) and isinstance(value.get('models'), list):
                for model in value['models']:
                    if not isinstance(model, dict):
                        continue
                    # Old role-library evidence must not disable unrelated verified parameters.
                    if isinstance(model.get('features'), dict):
                        model['features'].pop('cast', None)
                    if isinstance(model.get('evidence'), dict):
                        model['evidence'].pop('cast_task_id', None)
            self.validate(value)
            return value
        except (OSError, ValueError, TypeError, KeyError):
            return {'version': 'unverified', 'models': []}

    @staticmethod
    def validate(value):
        try:
            if not isinstance(value, dict) or not value.get('version') or not isinstance(value.get('models'), list):
                raise ValueError()
            if not 0 < value['verified_at'] <= time.time()+60 or value['expires_at'] <= time.time():
                raise ValueError()
            if value['expires_at'] - value['verified_at'] > 7*86400:
                raise ValueError()
            seen = set()
            for model in value['models']:
                identifier = model['id']
                if not isinstance(identifier, str) or not identifier or identifier in seen:
                    raise ValueError()
                seen.add(identifier)
                status = model.get('validation_status', 'verified')
                if status not in ('verified', 'experimental') or not _text(model.get('label')):
                    raise ValueError()
                if status == 'verified' and not _evidence(model, 'text_task_id'):
                    raise ValueError()
                if not isinstance(model.get('durations'), list) or not model['durations'] or any(type(v) is not int or not 1 <= v <= 300 for v in model['durations']):
                    raise ValueError()
                if not isinstance(model.get('ratios'), list) or not model['ratios'] or any(not isinstance(v, str) or not v for v in model['ratios']):
                    raise ValueError()
                selectors = model['selectors']
                mode = model.get('entry_mode', 'native_controls')
                if mode not in ('native_controls', 'ordinary_dialog') or 'skill' in model or not isinstance(selectors, dict):
                    raise ValueError()
                if status == 'experimental' and mode != 'ordinary_dialog':
                    raise ValueError()
                keys = (('composer', 'send', 'assistant', 'new_chat') if mode == 'ordinary_dialog' else
                        ('video_entry', 'composer', 'send', 'assistant', 'model', 'duration', 'ratio'))
                for key in keys:
                    if not isinstance(selectors.get(key), str) or not selectors[key].strip():
                        raise ValueError()
                if not model.get('receipt_patterns') or not model.get('rejection_patterns'):
                    raise ValueError()
                for values in (model['receipt_patterns'], model['rejection_patterns']):
                    if not isinstance(values, list) or any(not isinstance(v, str) or not v or len(v)>300 for v in values):
                        raise ValueError()
                _validate_references(model)
                _validate_features(model)
                if mode == 'ordinary_dialog':
                    _validate_dialog(model)
                elif model.get('native_media_bridge') is True and 'combinations' in model:
                    _validate_combinations(model)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError('invalid_capabilities', '能力清单缺少真实验证记录、对应入口定位或已过期；未开放生成。') from None

    def save(self, value):
        self.validate(value)
        atomic_json(self.path, value)
        return self.load()

    def public_models(self):
        result = []
        for model in self.load()['models']:
            public = {key: value for key, value in model.items() if key in
                      ('id', 'label', 'enabled', 'durations', 'ratios', 'verified_at', 'entry_mode',
                       'native_media_bridge', 'validation_status', 'account_category')}
            if 'references' in model:
                public['references'] = {kind: {key: value for key, value in reference.items()
                                              if key in ('enabled', 'max_count', 'max_bytes',
                                                         'validation_status', 'local_only')}
                                        for kind, reference in model['references'].items()}
            if 'features' in model:
                public['features'] = {kind: {key: value for key, value in feature.items()
                                            if key == 'enabled' or (key == 'values' and kind == 'resolution')}
                                      for kind, feature in model['features'].items()}
            if 'combinations' in model:
                public['combinations'] = [{key: value for key, value in item.items()
                                           if key in ('duration', 'ratio', 'resolution')}
                                          for item in model['combinations']]
            result.append(public)
        return result
