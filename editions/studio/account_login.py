"""Read-only login verification. Network and UI failures are not logout evidence."""
import asyncio
import json
from pathlib import Path
import time

import requests

import config
import proxy_usage


ACCOUNTS_ROOT = Path('accounts')
ACCOUNT_INFO_URL = (
    'https://www.dola.com/passport/account/info/v2/?aid=495671'
    '&account_sdk_source=web&sdk_version=2.2.11-doubao.0&device_platform=web'
)
_SESSION_NAMES = {'sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard'}
_IDENTITY_NAMES = ('user_id', 'user_id_str', 'uid')


def classify_login_response(status, body, login_flag=''):
    """True = authenticated identity; False = explicit auth denial; None = unknown."""
    if status not in (200, 401) or not isinstance(body, dict):
        return None
    data = body.get('data')
    if not isinstance(data, dict):
        return None
    identity = any(str(data.get(key) or '').strip() not in ('', '0') for key in _IDENTITY_NAMES)
    message = str(body.get('message') or '').lower()
    if login_flag == '0':
        # Contradictory evidence must not overwrite a known login state.
        return False if message == 'error' and not identity else None
    # Observed on this exact passport endpoint with an anonymous control GET.
    # A code alone is insufficient; require its explicit session-expired reason.
    expired = (message == 'error' and data.get('error_code') == 13
               and str(data.get('description') or '').strip().lower()
               == 'session expired, please sign in again')
    if expired:
        return None if identity else False
    blocked = str(data.get('is_blocked') or '').lower() in ('1', 'true')
    if status == 200 and message == 'success' and identity and not blocked:
        return True
    # No guesses based on error codes, rate limits, an empty body or generic errors.
    return None


def load_login_cookies(root):
    """Use the newest saved browser/polling state; never read or expose credentials in logs."""
    candidates = [root / 'cookies.json'] + [
        path for path in (root / 'polling').glob('*.json') if path.stem.isdigit()
    ]
    readable = []
    for path in candidates:
        try:
            readable.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(readable, reverse=True):
        try:
            state = json.loads(path.read_text(encoding='utf-8-sig'))
            cookies = state if isinstance(state, list) else state.get('cookies', state.get('Cookies'))
            if isinstance(cookies, list) and cookies:
                return cookies
        except (ValueError, OSError, AttributeError):
            continue
    return []


def read_account_login(account):
    """One GET through the account's configured ISP. No chat, retry, redirect or browser."""
    if not account or account.startswith('.') or '/' in account or '\\' in account:
        return None
    try:
        with requests.Session() as session:
            session.trust_env = False
            for cookie in load_login_cookies(ACCOUNTS_ROOT / account):
                if not isinstance(cookie, dict):
                    continue
                domain = str(cookie.get('domain') or '.dola.com').lower().lstrip('.')
                if domain not in ('dola.com', 'www.dola.com'):
                    continue
                name, value = cookie.get('name'), cookie.get('value')
                if not isinstance(name, str) or not isinstance(value, str) or not value:
                    continue
                expires = cookie.get('expires', -1)
                if isinstance(expires, (int, float)) and 0 <= expires <= time.time():
                    continue
                session.cookies.set(name, value, domain=cookie.get('domain') or '.dola.com',
                                    path=cookie.get('path') or '/', secure=bool(cookie.get('secure')))
            prepared = session.prepare_request(requests.Request('GET', ACCOUNT_INFO_URL, headers={
                'Accept': 'application/json, text/plain, */*',
                'Referer': 'https://www.dola.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            }))
            # Check the scoped header, not just a cookie's presence in a file.
            names = {part.partition('=')[0].strip() for part in prepared.headers.get('Cookie', '').split(';')}
            if not names.intersection(_SESSION_NAMES):
                return None
            proxy = config.proxy_for(account)
            proxy_url = config._proxy_to_url(proxy) if proxy else None
            if proxy_url:
                session.proxies.update(http=proxy_url, https=proxy_url)
            proxy_usage.reserve_for_account(account, proxy=proxy)
            # Explicit proxies also avoid system proxy changes or accidental direct fallback.
            with session.send(prepared, proxies=session.proxies, timeout=(10, 15), allow_redirects=False) as response:
                try:
                    body = response.json()
                except ValueError:
                    return None
                return classify_login_response(response.status_code, body, response.headers.get('x-tt-agw-login', ''))
    except Exception as exc:
        # Raw request exceptions can contain proxy passwords or Cookie values.
        print(f'[{account}] 只读登录检测无法判定（{type(exc).__name__}），保留原登录状态', flush=True)
        return None


async def check_account_login(account):
    return await asyncio.to_thread(read_account_login, account)
