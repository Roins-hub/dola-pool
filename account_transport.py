"""Account-bound HTTP transport without implicit direct fallback."""
import re
import config
import proxy_store
import socks_relay


def redact_error(error, *, limit=1500):
    text = str(error)
    text = re.sub(r'https?://[^\s<>\"\x27]+|socks5h?://[^\s<>\"\x27]+', '[URL]', text)
    return re.sub(r'(?i)(authorization|token|password|cookie|signature)([=: ]+)[^ ,;]+',
                  r'\1\2[REDACTED]', text)[:limit]


def account_proxy_url(account):
    """Resolve the account route once and enforce isolation for every HTTP client."""
    info = proxy_store.get_account_proxy(account) if account else None
    if config.ISOLATE_SHARED_EGRESS and not info:
        raise RuntimeError('严格隔离模式下账号没有启用的绑定代理，禁止直连')
    url = proxy_store.account_proxy_url(account) if account else config.PROXY
    if info and info.get('mode') == 'extract' and not (proxy_store.get_session(account) or {}).get('node_id'):
        raise RuntimeError('账号代理没有可用节点，禁止直连回退')
    if config.ISOLATE_SHARED_EGRESS and (not url or str(url).lower() in ('direct', 'none', 'off', 'no')):
        raise RuntimeError('账号代理不可用，禁止直连')
    return url or None


def requests_proxy(account):
    """requests resolves SOCKS destinations at the bound proxy, including uploads."""
    url = account_proxy_url(account)
    return 'socks5h://' + url[len('socks5://'):] if url and url.startswith('socks5://') else url


def http_proxy(account):
    """aiohttp needs an HTTP relay for authenticated SOCKS; never use direct on failure."""
    url = account_proxy_url(account)
    if url and url.startswith(('socks5://', 'socks5h://')):
        return socks_relay.local_http_proxy_for(url)
    return url or None


EGRESS_CHECK_URL = 'https://www.dola.com/favicon.ico'


def egress_alive(account, attempts=2, *, site='dola'):
    """Can the account's own proxy reach its site? Same relay as the browser, no direct fallback.

    A sticky residential IP can go offline and stay assigned until the session rotates; this
    catches it in a few seconds instead of a 30-second browser launch failing. Two tries so one
    dropped connection on a flaky link is not read as a dead exit.
    """
    import requests
    proxy = http_proxy(account)
    if not proxy:
        return {'ok': True, 'detail': '未配代理'}
    last = ''
    for _ in range(max(1, attempts)):
        try:
            with requests.Session() as session:
                session.trust_env = False
                target = 'https://www.doubao.com/favicon.ico' if site == 'doubao' else EGRESS_CHECK_URL
                response = session.get(target, proxies={'http': proxy, 'https': proxy},
                                       timeout=(8, 10), allow_redirects=False, stream=True)
                with response:
                    # The local relay answers 502 when the upstream proxy cannot connect.
                    if response.status_code in (407, 502, 503, 504):
                        last = f'代理返回 HTTP {response.status_code}'
                        continue
                    return {'ok': True, 'detail': f'HTTP {response.status_code}'}
        except Exception as exc:
            last = type(exc).__name__
    return {'ok': False, 'detail': last or '连接失败'}


def diagnose_upload_route(account, host):
    """Credential-free HTTPS GET; HTTP errors still prove connectivity, not upload success."""
    host = str(host or '').lower()
    if not any(host == domain or host.endswith('.' + domain)
               for domain in ('bytevcloudapi.com', 'vodupload.com')):
        return {'reachable': None, 'detail': '未知上传域名，未主动探测'}
    import requests
    try:
        proxy = http_proxy(account)
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(f'https://{host}/', proxies={'https': proxy} if proxy else {},
                                   timeout=(5, 8), allow_redirects=False, stream=True)
            with response:
                # A proxy 407/502/503 is not proof that ImageX answered.
                reachable = response.status_code not in (407, 502, 503, 504)
                return {'reachable': reachable, 'http_status': response.status_code,
                        'detail': f'上传域名连通性诊断 HTTP {response.status_code}（未上传图片）'}
    except Exception as exc:
        return {'reachable': False, 'detail': f'上传域名连接失败：{type(exc).__name__}'}
