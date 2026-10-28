"""Account-bound HTTP transport without implicit direct fallback."""
import re
import config
import proxy_store
import socks_relay


def redact_error(error):
    text = str(error)
    text = re.sub(r'https?://[^\s<>\"\x27]+|socks5h?://[^\s<>\"\x27]+', '[URL]', text)
    return re.sub(r'(?i)(authorization|token|password|cookie|signature)([=: ]+)[^ ,;]+',
                  r'\1\2[REDACTED]', text)[:1500]


def http_proxy(account):
    """aiohttp needs an HTTP relay for authenticated SOCKS; never use direct on failure."""
    info = proxy_store.get_account_proxy(account) if account else None
    if config.ISOLATE_SHARED_EGRESS and not info:
        raise RuntimeError('严格隔离模式下账号没有启用的绑定代理，禁止直连')
    url = proxy_store.account_proxy_url(account) if account else config.PROXY
    if info and info.get('mode') == 'extract' and not (proxy_store.get_session(account) or {}).get('node_id'):
        raise RuntimeError('账号代理没有可用节点，禁止直连回退')
    if config.ISOLATE_SHARED_EGRESS and not url:
        raise RuntimeError('账号代理不可用，禁止直连')
    if url and url.startswith(('socks5://', 'socks5h://')):
        return socks_relay.local_http_proxy_for(url)
    return url or None


def diagnose_upload_route(account, host):
    """Credential-free HTTPS GET; HTTP errors still prove connectivity, not upload success."""
    if host != 'imagex-ap-southeast-1.bytevcloudapi.com':
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
