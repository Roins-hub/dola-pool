"""Enable each Dola account's native original-download preference before sending.

Only the observed creation user-config endpoints are used. An enabled preference
does not prove a particular video is watermark-free; publication checks remain.
"""
import asyncio
import json
from pathlib import Path
import re
import time
import config

ACCOUNTS_DIR = Path(config.DOLA_ACCOUNTS_DIR)
GET = '/creativity/user_config/get'
SET = '/creativity/user_config/set'
PARAMS = {'aid': '495671', 'real_aid': '495671', 'device_platform': 'web',
          'samantha_web': '1', 'web_platform': 'browser', 'use-olympus-account': '1'}


class WatermarkPreferenceError(RuntimeError):
    task_phase = 'watermark_setup_failed'


def _failure(error):
    """Classify transport failures without persisting upstream text or URLs."""
    text = str(error).lower()
    if 'watermark_login_expired' in text:
        return 'login_expired', False
    status = re.search(r'watermark_http_(\d{3})\b', text)
    if status:
        code = int(status[1])
        return f'http_{code}', code in (408, 500, 502, 503, 504)
    if type(error).__name__ == 'TargetClosedError' or 'target page, context or browser has been closed' in text:
        return 'browser_closed', False
    if (isinstance(error, TimeoutError) or type(error).__name__ in ('TimeoutError', 'ReadTimeout', 'ConnectTimeout', 'ServerTimeoutError')
            or 'timed out' in text or 'timeouterror' in text or 'aborterror' in text):
        return 'timeout', True
    if (isinstance(error, (OSError, ConnectionError))
            or type(error).__name__ in ('ClientConnectionError', 'ServerDisconnectedError', 'ClientPayloadError')
            or any(value in text for value in ('failed to fetch', 'networkerror', 'net::err_', 'load failed'))):
        return 'network_error', True
    return 'unexpected_error', False


def _request_error(error, stage, attempts):
    code, _ = _failure(error)
    operation = {'read': '读取', 'write': '保存', 'verify': '回读'}[stage]
    reason = {'timeout': '超时', 'network_error': '连接中断', 'browser_closed': '时浏览器已关闭',
              'login_expired': '时发现账号未登录', 'unexpected_error': '异常'}.get(code)
    if reason is None:
        reason = f'失败（HTTP {code.removeprefix("http_")}）'
    result = WatermarkPreferenceError(
        f'无水印设置{operation}{reason}（已尝试 {attempts} 次），提示词未发送；请检查该账号登录或绑定线路。')
    result.failure_code, result.stage, result.attempts = code, stage, attempts
    return result


async def _read_option(post, result, stage):
    result['stage'] = stage
    for attempt in range(1, 4):
        result['attempts'] = attempt
        try:
            response = await asyncio.wait_for(post(GET, {}), timeout=25)
        except WatermarkPreferenceError:
            raise
        except Exception as error:
            if not _failure(error)[1] or attempt == 3:
                raise _request_error(error, stage, attempt) from None
            await asyncio.sleep(0.5 * attempt)
        else:
            # Business/permission errors and malformed config must never be
            # treated as transient transport failures or as an enabled flag.
            return _option(response)


def _path(account):
    if not account or Path(account).name != account or account.startswith('.'):
        raise ValueError('invalid account')
    root = ACCOUNTS_DIR / account
    if not root.is_dir():
        raise ValueError('account directory missing')
    return root / 'watermark-status.json'


def _option(response):
    if not isinstance(response, dict) or response.get('code') not in (0, '0'):
        raise WatermarkPreferenceError('平台未确认无水印设置，提示词未发送。')
    data = response.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('config_map'), dict):
        raise WatermarkPreferenceError('平台未返回有效水印配置，提示词未发送。')
    option = (data['config_map'].get('1') or {}).get('watermark_option') or {}
    if not isinstance(option, dict):
        raise WatermarkPreferenceError('平台水印配置格式异常，提示词未发送。')
    return option


async def ensure_preference(account, post):
    """Retry reads on this account; issue at most one SET and confirm it live."""
    path = _path(account)
    result = {'enabled': False, 'changed': False, 'checked_at': time.time()}
    try:
        async with asyncio.timeout(80):
            option = await _read_option(post, result, 'read')
            if option.get('is_on') is not True:
                subscription = option.get('subscribe_config') or {}
                if subscription.get('need_upgrade') is True:
                    raise WatermarkPreferenceError('平台要求升级才能开启无水印，提示词未发送。')
                result.update(stage='write', attempts=1)
                try:
                    ack = await asyncio.wait_for(post(SET, {
                        'config_type': 1, 'config_value': {'watermark_option': {'is_on': True}}
                    }), timeout=25)
                except WatermarkPreferenceError:
                    raise
                except Exception as error:
                    if not _failure(error)[1]:
                        raise _request_error(error, 'write', 1) from None
                    # The server may have saved it before the reply was lost.
                    # Reconcile with GET instead of blindly repeating SET.
                else:
                    if not isinstance(ack, dict) or ack.get('code') not in (0, '0'):
                        raise WatermarkPreferenceError('平台未保存无水印设置，提示词未发送。')
                if (await _read_option(post, result, 'verify')).get('is_on') is not True:
                    raise WatermarkPreferenceError('无水印设置回读未生效，提示词未发送。')
                result['changed'] = True
        result['enabled'] = True
        return result
    except WatermarkPreferenceError as error:
        result['reason'] = str(error)
        error.account = account
        if getattr(error, 'failure_code', None):
            result['failure_code'] = error.failure_code
        raise
    except Exception as error:
        failure = _request_error(error, result.get('stage', 'read'), result.get('attempts', 1))
        failure.account = account
        result.update(reason=str(failure), failure_code=failure.failure_code)
        raise failure from None
    finally:
        # Status is advisory only. Never skip a live check based on this file.
        try:
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            temporary.replace(path)
        except OSError:
            pass


async def ensure_page_preference(page, account):
    """Uses the existing browser context's login and already-bound ISP."""
    async def post(path, body):
        return await page.evaluate("""async ({path, body, params}) => {
            if (!['/creativity/user_config/get', '/creativity/user_config/set'].includes(path))
                throw new Error('Unsupported settings endpoint');
            const url = new URL(path, 'https://www.dola.com');
            url.search = new URLSearchParams(params).toString();
            const response = await fetch(url, {method: 'POST', credentials: 'include',
                redirect: 'error', signal: AbortSignal.timeout(20000),
                headers: {'Content-Type': 'application/json', 'agw-js-conv': 'str'},
                body: JSON.stringify(body)});
            if (response.headers.get('x-tt-agw-login') === '0')
                throw new Error('watermark_login_expired');
            if (!response.ok) throw new Error('watermark_http_' + response.status);
            return await response.json();
        }""", {'path': path, 'body': body, 'params': PARAMS})
    return await ensure_preference(account, post)


def ensure_protocol_preference(account, session, context, protocol):
    """Runs inside the existing HTTP preparation thread using its bound session."""
    async def post(path, body):
        with protocol.dola_api_post_response(session, context, path, body) as response:
            if protocol.agw_login_flag(response) == '0':
                raise RuntimeError('watermark_login_expired')
            if response.status_code != 200:
                raise RuntimeError(f'watermark_http_{response.status_code}')
            return protocol.request_json(response, 'watermark_config')
    return asyncio.run(ensure_preference(account, post))


async def ensure_saved_preference(account):
    """Account maintenance only: no browser and no generation request."""
    import config
    import proxy_usage
    from video_polling import HttpVideoSession
    async with HttpVideoSession(account, '0') as session:
        async def post(path, body):
            if session.proxy_url and session.own_isp:
                proxy_usage.usage.reserve(config._parse_proxy(session.proxy_url), 1)
            async with session.session.post('https://www.dola.com' + path, params=PARAMS,
                    json=body, proxy=session.proxy_url, allow_redirects=False) as response:
                if response.headers.get('x-tt-agw-login') == '0':
                    raise RuntimeError('watermark_login_expired')
                if response.status != 200:
                    raise RuntimeError(f'watermark_http_{response.status}')
                return await response.json(content_type=None)
        return await ensure_preference(account, post)
