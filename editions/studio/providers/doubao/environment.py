"""Stable account browser settings, using native Chromium hardware identity.

Route matching describes the configured route, not a geolocation measurement or
an assertion that separate profiles have separate physical devices.
"""
import copy
import hashlib
import math
import re
import time
from urllib.parse import urlsplit

import config
from providers.base import ProviderError


_REGIONS = {
    'CN': ('zh-CN', 'Asia/Shanghai', '本机网络'),
    'HK': ('zh-HK', 'Asia/Hong_Kong', '香港'),
    'TW': ('zh-TW', 'Asia/Taipei', '台湾'),
}
_COUNTRIES = {'CN': 'CN', '中国': 'CN', '中国大陆': 'CN', '本机网络': 'CN',
              'HK': 'HK', '香港': 'HK', 'TW': 'TW', '台湾': 'TW'}
_WINDOWS = ((1280, 720), (1366, 768), (1440, 900), (1536, 864))
_FIELDS = {'schema', 'environment_id', 'created_at', 'required_proxy_server',
           'route_region', 'locale', 'timezone_id', 'viewport', 'screen',
           'device_scale_factor', 'is_mobile', 'has_touch', 'color_scheme',
           'hardware_mode', 'status'}


def _invalid():
    return ProviderError('environment_unavailable',
                         '豆包账号浏览器环境与身份或绑定线路不匹配，已停止，未切换其他环境或网络。', 503)


def _binding_server(binding):
    if not isinstance(binding, dict) or not isinstance(binding.get('server'), str):
        raise _invalid()
    if binding == {'server': 'direct'}:
        return 'direct'
    if set(binding) - {'server', 'username', 'password'}:
        raise _invalid()
    if any(not isinstance(value, str) or not value for value in binding.values()):
        raise _invalid()
    try:
        parsed = urlsplit(binding['server'])
        if (parsed.scheme != 'http' or not parsed.hostname or not parsed.port
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError()
    except (TypeError, ValueError):
        raise _invalid() from None
    return binding['server']


def _region(binding):
    server = _binding_server(binding)
    if server == 'direct':
        return 'CN'
    # Match the configured exact endpoint. A port, account name or URL label is
    # not sufficient evidence of a country's network route.
    for option in config.doubao_local_proxy_options().values():
        if binding == {'server': option['server']}:
            return _COUNTRIES.get(option.get('country'), 'unknown')
    return 'unknown'


def _account_id(account_id):
    if not isinstance(account_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', account_id):
        raise _invalid()


def _device(account_id):
    _account_id(account_id)
    width, height = _WINDOWS[int(hashlib.sha256(account_id.encode()).hexdigest(), 16) % len(_WINDOWS)]
    return {'viewport': {'width': width, 'height': height},
            'screen': {'width': 1920, 'height': 1080}, 'device_scale_factor': 1,
            'is_mobile': False, 'has_touch': False, 'color_scheme': 'light',
            'hardware_mode': 'native'}


def _language(env):
    locale, timezone = env.get('locale'), env.get('timezone_id')
    # Chromium validates actual time zone support when opening the context;
    # lexical checks avoid adding an OS-specific time-zone database dependency.
    if (not isinstance(locale, str) or not re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', locale)
            or not isinstance(timezone, str) or len(timezone) > 100
            or not re.fullmatch(r'(?:UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)+)', timezone)):
        raise _invalid()


def _core(env, account_id):
    _account_id(account_id)
    if (not isinstance(env, dict) or set(env) != _FIELDS
            or type(env.get('schema')) is not int or env['schema'] != 2
            or env.get('environment_id') != account_id
            or type(env.get('created_at')) not in (int, float)
            or not math.isfinite(env['created_at']) or env['created_at'] <= 0):
        raise _invalid()
    for key, expected in _device(account_id).items():
        actual = env.get(key)
        if isinstance(expected, dict):
            if (not isinstance(actual, dict) or set(actual) != {'width', 'height'}
                    or any(type(actual[axis]) is not int for axis in ('width', 'height'))):
                raise _invalid()
        elif type(actual) is not type(expected):
            raise _invalid()
        if actual != expected:
            raise _invalid()
    _language(env)
    if (not isinstance(env.get('required_proxy_server'), str)
            or not isinstance(env.get('route_region'), str)
            or env.get('route_region') not in {*_REGIONS, 'unknown'}
            or env.get('status') not in ('matched', 'unmatched')):
        raise _invalid()


def create_environment(account_id, binding, previous=None):
    """Create or rebind one account's stable window and route settings.

    Schema 1 may be migrated only for the same exact saved route. Schema 2 may
    be rebound after its owner validates the old binding; identity/device fields
    remain stable. Neither operation changes browser cookies or hardware IDs.
    """
    _account_id(account_id)
    server, region = _binding_server(binding), _region(binding)
    created_at = time.time()
    locale, timezone = 'zh-CN', 'Asia/Shanghai'
    if previous is not None:
        if not isinstance(previous, dict):
            raise _invalid()
        if previous.get('schema') == 2:
            _core(previous, account_id)
        elif type(previous.get('schema')) is int and previous['schema'] == 1:
            if (previous.get('required_proxy_server') != server
                    or type(previous.get('created_at')) not in (int, float)
                    or not math.isfinite(previous['created_at']) or previous['created_at'] <= 0):
                raise _invalid()
            _language(previous)
        else:
            raise _invalid()
        created_at, locale, timezone = previous['created_at'], previous['locale'], previous['timezone_id']
    if region in _REGIONS:
        locale, timezone, _ = _REGIONS[region]
    env = {'schema': 2, 'environment_id': account_id, 'created_at': created_at,
           'required_proxy_server': server, 'route_region': region,
           'locale': locale, 'timezone_id': timezone, **_device(account_id),
           'status': 'matched' if region in _REGIONS else 'unmatched'}
    return validate_environment(env, account_id, binding)


def validate_environment(env, account_id, binding):
    """Validate without migrating, writing files, or silently repairing state."""
    _core(env, account_id)
    region = _region(binding)
    if env['required_proxy_server'] != _binding_server(binding) or env['route_region'] != region:
        raise _invalid()
    if region in _REGIONS:
        locale, timezone, _ = _REGIONS[region]
        if (env['locale'], env['timezone_id'], env['status']) != (locale, timezone, 'matched'):
            raise _invalid()
    elif env['status'] != 'unmatched':
        raise _invalid()
    return copy.deepcopy(env)


def browser_options(env):
    """Only Playwright-supported desktop settings; no UA/graphics spoofing."""
    _core(env, env.get('environment_id') if isinstance(env, dict) else None)
    return {key: copy.deepcopy(env[key]) for key in ('locale', 'timezone_id', 'viewport', 'screen',
            'device_scale_factor', 'is_mobile', 'has_touch', 'color_scheme')}


def environment_summary(env, account_id, binding):
    """Read-only public summary, including legacy state without migration."""
    if not isinstance(env, dict):
        raise _invalid()
    if type(env.get('schema')) is int and env['schema'] == 1:
        # Validate legacy inputs before labeling them as pending migration.
        create_environment(account_id, binding, previous=env)
        return {'status': 'legacy', 'region': _REGIONS.get(_region(binding), ('', '', '未匹配'))[2],
                'locale': env['locale'], 'timezone_id': env['timezone_id'],
                'viewport': None, 'hardware_mode': 'native', 'isolated': True}
    env = validate_environment(env, account_id, binding)
    return {'status': env['status'], 'region': _REGIONS.get(env['route_region'], ('', '', '未匹配'))[2],
            'locale': env['locale'], 'timezone_id': env['timezone_id'],
            'viewport': env['viewport'], 'hardware_mode': 'native', 'isolated': True}
