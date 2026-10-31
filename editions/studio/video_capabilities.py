"""Model and duration validation shared by video submission entry points."""

SUPPORTED_DURATIONS = (5, 10, 15, 30)
SUPPORTED_DURATIONS_BY_MODEL = {
    'seedance-1.0': [5, 10, 15],
    'seedance-2.0': [5, 10, 15, 30],
    'seedance-2.5': [5, 10, 15, 30],
}
FAST15_PROFILE = 'fast15-via30'
EXPERT15_PROFILE = 'pro-fast15'
PLUGIN_EXPERT30_PROFILE = 'plugin-expert30'
PLUGIN_EXPERT30_MODELS = {
    'seedance-2.5': 'seedance_v2.5',
    'seedance-2.0': 'seedance_v2.0',
    'seedance-2.0-std': 'seedance_v2.0_std',
    'seedance-2.0-mini': 'seedance_v2.0_mini',
}
PLUGIN_EXPERT30_RATIOS = ('1:1', '3:4', '4:3', '9:16', '16:9', '21:9')
PLUGIN_EXPERT30_RESOLUTIONS = ('480p', '720p', '1080p')
PLUGIN_EXPERT_DURATIONS = (5, 10, 15, 30)


def normalize_model(model: str) -> str:
    key = model.strip().lower().replace('-', '_') if isinstance(model, str) else ''
    if key == 'ic_mini':
        return 'seedance-1.0'
    for version, short in (('1.0', '10'), ('2.0', '20'), ('2.5', '25')):
        if key in (f'seedance_{version}', f'seedance_v{version}',
                   f'seedance_{short}', f'seedance_v{short}'):
            return f'seedance-{version}'
    raise ValueError('不支持的模型，当前支持 Seedance 1.0（seedance-1.0）、Fast 2.0（seedance-2.0）和 Seedance 2.5（seedance-2.5）')


def to_wire_model(model: str) -> str:
    return {'seedance-1.0': 'ic_mini', 'seedance-2.0': 'seedance_v2.0',
            'seedance-2.5': 'seedance_v2.5'}[normalize_model(model)]


def supported_durations(model: str) -> tuple[int, ...]:
    return tuple(SUPPORTED_DURATIONS_BY_MODEL[normalize_model(model)])


def validate_video_parameters(model: str, duration: int | str | None) -> tuple[str, int]:
    canonical = normalize_model(model)
    if canonical not in SUPPORTED_DURATIONS_BY_MODEL:
        raise ValueError('此模型仅支持插件专家入口')
    if duration is None:
        duration = 10
    elif isinstance(duration, str):
        try:
            duration = int(duration.strip())
        except ValueError:
            raise ValueError('视频时长必须是整数秒') from None
    if type(duration) is not int:
        raise ValueError('视频时长必须是整数秒')
    allowed = SUPPORTED_DURATIONS_BY_MODEL[canonical]
    if duration not in allowed:
        label = {'seedance-1.0': 'Seedance 1.0', 'seedance-2.0': 'Fast 2.0',
                 'seedance-2.5': 'Seedance 2.5'}[canonical]
        raise ValueError(f'{label} 仅支持 {"、".join(map(str, allowed))} 秒视频')
    return canonical, duration


def submission_duration(model: str, duration: int | str | None,
                        duration_profile: str | None = None) -> int:
    """Resolve explicit profiles without changing ordinary model/duration pairs."""
    if duration_profile == PLUGIN_EXPERT30_PROFILE:
        return validate_plugin_expert30_parameters(model, duration)['duration']
    canonical, output_duration = validate_video_parameters(model, duration)
    if duration_profile is None:
        return output_duration
    if duration_profile == EXPERT15_PROFILE and canonical == 'seedance-2.0' and output_duration == 15:
        return 15
    if (duration_profile != FAST15_PROFILE or canonical != 'seedance-2.0'
            or output_duration != 15):
        raise ValueError('时长链路仅支持 Fast 2.0 / 15秒：fast15-via30 或 pro-fast15')
    return 30


def validate_plugin_expert30_parameters(model: str, duration=30, ratio=None,
                                        resolution=None) -> dict:
    key = model.strip().lower().replace('-', '_') if isinstance(model, str) else ''
    canonical = next((name for name, wire in PLUGIN_EXPERT30_MODELS.items()
                      if key in (name.replace('-', '_'), wire)), None)
    if canonical is None:
        canonical = normalize_model(model)
    if canonical not in PLUGIN_EXPERT30_MODELS:
        raise ValueError('插件专家仅支持2.5、Fast 2.0、2.0标准版及2.0 Mini')
    if duration is None:
        duration = 30
    if isinstance(duration, str):
        try:
            duration = int(duration.strip())
        except ValueError:
            raise ValueError('插件专家申请时长必须为5、10、15或30秒') from None
    if type(duration) is not int or duration not in PLUGIN_EXPERT_DURATIONS:
        raise ValueError('插件专家申请时长必须为5、10、15或30秒')
    ratio = '16:9' if ratio in (None, '', 'default') else ratio
    resolution = '720p' if resolution in (None, '', 'auto') else resolution
    if ratio not in PLUGIN_EXPERT30_RATIOS:
        raise ValueError('插件专家不支持此比例')
    if resolution not in PLUGIN_EXPERT30_RESOLUTIONS:
        raise ValueError('插件专家仅支持480p、720p、1080p')
    return dict(model=canonical, duration=duration, ratio=ratio, resolution=resolution,
                wire_model=PLUGIN_EXPERT30_MODELS[canonical])


def plugin_expert_result_shape(durations, requested_duration=30, two_part=False):
    """Accept only a complete native result matching this request's duration rule."""
    import math
    if (type(requested_duration) is not int or requested_duration not in PLUGIN_EXPERT_DURATIONS
            or type(two_part) is not bool or (two_part and requested_duration != 30)):
        return False
    try:
        values = [float(value) for value in durations]
    except (ValueError, TypeError, OverflowError):
        return False
    if not values or any(not math.isfinite(value) or value <= 0 for value in values):
        return False
    if requested_duration == 30 and len(values) == 2:
        return all(abs(value - 15) <= 1 for value in values)
    return not two_part and len(values) == 1 and abs(values[0] - requested_duration) <= 1
