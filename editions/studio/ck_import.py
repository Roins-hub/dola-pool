"""Import a raw Dola Cookie header into a new account directory."""
import json
from pathlib import Path
import random
import re
import shutil
from urllib.parse import quote, urlsplit
import uuid


def _validated_proxy(proxy):
    proxy = proxy.strip()
    if not proxy:
        return ''
    try:
        if len(proxy) > 4096 or re.search(r'[\x00-\x20\x7f]', proxy):
            raise ValueError()
        normalized = proxy
        if '://' not in normalized:
            parts = normalized.split(':', 3)
            normalized = (f'http://{quote(parts[2], safe="")}:{quote(parts[3], safe="")}@{parts[0]}:{parts[1]}'
                          if len(parts) == 4 else 'http://' + normalized)
        parsed = urlsplit(normalized)
        if (parsed.scheme not in ('http', 'https', 'socks5') or not parsed.hostname or not parsed.port
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or (parsed.scheme == 'socks5' and parsed.username)):
            raise ValueError()
    except ValueError:
        raise ValueError('代理格式不正确，请使用 HTTP 代理地址或 host:port:user:pass。') from None
    return normalized


def _proxy_endpoint(proxy):
    parsed = urlsplit(_validated_proxy(proxy))
    return parsed.scheme.lower(), parsed.hostname, parsed.port


def _inherited_browser_options(root, proxy):
    """Reuse only agreed route settings, never another account's browser state."""
    endpoint = _proxy_endpoint(proxy)
    inherited = {}
    fields = ('isp_transport', 'locale', 'accept_language', 'timezone_id', 'site_language')
    for path in sorted(root.glob('*/browser.json')):
        if path.parent.name.startswith('.'):
            continue
        try:
            other = (path.parent / 'proxy.txt').read_text(encoding='utf-8-sig').strip()
            if not other or _proxy_endpoint(other) != endpoint:
                continue
        except (OSError, ValueError):
            # No proven endpoint match: it is not a template for this route.
            continue
        try:
            saved = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(saved, dict):
                raise ValueError()
            selected = {key: saved[key] for key in fields if key in saved}
            for key, value in selected.items():
                if key == 'isp_transport':
                    if type(value) is not bool:
                        raise ValueError()
                elif (not isinstance(value, str) or re.search(r'[\x00-\x1f\x7f]', value)
                      or (key != 'site_language' and not value.strip())):
                    raise ValueError()
        except (OSError, ValueError, TypeError):
            raise ValueError('同线路账号浏览器配置无效，请修复后再导入。') from None
        for key, value in selected.items():
            if key in inherited and inherited[key] != value:
                raise ValueError('同线路账号浏览器配置冲突，请统一线路配置后再导入。')
            inherited[key] = value
    return inherited


def _cookie_identities(cookies):
    identities = set()
    for cookie in cookies:
        key, value = cookie['name'], cookie['value']
        if not value:
            continue
        if key in ('sessionid', 'sessionid_ss'):
            identities.add(('session', value))
        elif key in ('uid_tt', 'uid_tt_ss'):
            identities.add(('user', value))
    return identities


def _cookie_sets(raw):
    """Normalize browser JSON, Netscape export and one-header-per-line TXT locally."""
    if len(raw) > 131072:
        raise ValueError('CK 内容过大，每个文件最多 128 KB。')
    raw = raw.strip().lstrip('\ufeff').strip()
    if not raw:
        raise ValueError('CK 文件为空。')
    if raw.startswith(('[', '{')):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = data['cookies']
            if not isinstance(data, list) or not data:
                raise ValueError()
            if all(isinstance(c, dict) and 'name' in c and 'value' in c for c in data):
                for cookie in data:
                    domain = str(cookie.get('domain') or '').lstrip('.').lower()
                    if domain and domain != 'dola.com' and not domain.endswith('.dola.com'):
                        raise ValueError()
                return [[{'name': str(c['name']), 'value': str(c['value'])} for c in data]]
            sets = []
            for account in data:
                if isinstance(account, dict) and isinstance(account.get('cookies'), list):
                    sets.extend(_cookie_sets(json.dumps(account['cookies'])))
                elif isinstance(account, str):
                    sets.extend(_cookie_sets(account))
                else:
                    raise ValueError()
            return sets
        except (ValueError, KeyError, TypeError, RecursionError):
            raise ValueError('Cookie JSON 格式不正确，或包含其他网站的 Cookie。') from None
    if '\t' in raw:
        cookies = []
        for line in raw.splitlines():
            if line.startswith('#HttpOnly_'):
                line = line[len('#HttpOnly_'):]
            elif line.startswith('#') or not line.strip():
                continue
            parts = line.split('\t')
            if len(parts) != 7:
                raise ValueError('Netscape Cookie 文件格式不正确。')
            domain = parts[0].lstrip('.').lower()
            if domain != 'dola.com' and not domain.endswith('.dola.com'):
                continue
            cookies.append({'name': parts[5], 'value': parts[6]})
        return [cookies]
    lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    lines = [line.split('----', 1)[1].strip()
             if '----' in line and 'sessionid' in line.split('----', 1)[1] else line
             for line in lines]
    # A complete login header on each line represents separate accounts. Otherwise
    # newlines are wrapping within one header, not separate accounts.
    headers = lines if len(lines) > 1 and all(re.search(r'(?:^|[;:\s])sessionid(?:_ss)?=', line) for line in lines) else ['; '.join(line.rstrip(';') for line in lines)]
    sets = []
    for header in headers:
        if header.lower().startswith('cookie:'):
            header = header[7:].strip()
        cookies = []
        for part in header.split(';'):
            part = part.strip()
            if not part:
                continue
            key, equals, value = part.partition('=')
            if not equals:
                raise ValueError('CK 格式不正确，请使用完整 Cookie 字符串、浏览器 JSON 或 Netscape 文件。')
            cookies.append({'name': key.strip(), 'value': value.strip()})
        sets.append(cookies)
    return sets


def import_ck_batch(accounts_dir, name, raw, proxy='', *, proxy_pool=()):
    sets = _cookie_sets(raw)
    if len(sets) > 100:
        raise ValueError('每个文件最多导入 100 个账号。')
    results = []
    for index, cookies in enumerate(sets):
        account_name = name if len(sets) == 1 else f'{name[:58]}_{index + 1}'
        try:
            result = import_ck(accounts_dir, account_name, json.dumps(cookies), proxy, proxy_pool=proxy_pool)
            results.append(result)
        except (ValueError, FileExistsError, OSError) as error:
            results.append({'name': account_name, 'error': str(error) if isinstance(error, (ValueError, FileExistsError)) else 'CK 保存失败，请检查目录权限。'})
    return {'results': results, 'imported': sum(not r.get('existing') and not r.get('error') for r in results),
            'skipped': sum(bool(r.get('existing')) for r in results), 'failed': sum(bool(r.get('error')) for r in results)}


def import_ck(accounts_dir, name, raw, proxy='', *, proxy_pool=()):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', name, re.I):
        raise ValueError('名称只能使用 1–64 位字母、数字、下划线或短横线，不能使用系统保留名称。')
    sets = _cookie_sets(raw)
    if len(sets) != 1:
        raise ValueError('文件包含多个账号，请使用一键导入 CK。')
    cookies = sets[0]
    for cookie in cookies:
        if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", cookie['name']) or
                re.search(r'[\x00-\x20\x7f;]', cookie['value'])):
            raise ValueError('CK 格式不正确，请使用提取工具导出的 TXT 文件或完整 Cookie 字符串。')
    if not any(c['name'] in ('sessionid', 'sessionid_ss') and c['value'] for c in cookies):
        raise ValueError('CK 中没有登录凭据 sessionid，请在 Dola 登录后重新提取完整 CK。')
    proxy = proxy.strip()
    _validated_proxy(proxy)
    root = Path(accounts_dir).resolve()
    target = root / name
    identities = _cookie_identities(cookies)
    for saved_path in sorted(root.glob('*/cookies.json')):
        if saved_path.parent.name.startswith('.'):
            continue
        try:
            saved = json.loads(saved_path.read_text(encoding='utf-8-sig'))
            if identities & _cookie_identities(saved):
                return {'name': saved_path.parent.name, 'cookie_count': len(saved),
                        'status': 'unverified', 'existing': True}
        except (OSError, ValueError, TypeError, KeyError):
            continue
    if target.exists():
        try:
            saved = json.loads((target / 'cookies.json').read_text(encoding='utf-8-sig'))
            # Browser cookies may include domain/expiry metadata. Compare the
            # actual header pairs, preserving all existing files and routing.
            if sorted((c['name'], c['value']) for c in saved) == sorted((c['name'], c['value']) for c in cookies):
                return {'name': name, 'cookie_count': len(cookies), 'status': 'unverified', 'existing': True}
        except (OSError, ValueError, TypeError, KeyError):
            pass
        raise FileExistsError('名称已存在，请换一个名称；已有 CK 不会被覆盖。')
    if not proxy:
        candidates = list(proxy_pool)
        for file in root.glob('*/proxy.txt'):
            if not file.parent.name.startswith('.'):
                candidates.append(file.read_text(encoding='utf-8-sig').strip())
        unique = {}
        for candidate in candidates:
            try:
                normalized = _validated_proxy(candidate)
                if normalized:
                    unique.setdefault(normalized, candidate.strip())
            except ValueError:
                continue
        if unique:
            proxy = random.choice(list(unique.values()))
    browser_options = _inherited_browser_options(root, proxy) if proxy else {}
    root.mkdir(parents=True, exist_ok=True)
    staging = root / ('.ck-import-' + uuid.uuid4().hex)
    staging.mkdir()
    try:
        (staging / 'cookies.json').write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding='utf-8')
        if proxy:
            (staging / 'proxy.txt').write_text(proxy + '\n', encoding='utf-8')
        if browser_options:
            (staging / 'browser.json').write_text(
                json.dumps(browser_options, ensure_ascii=False, indent=2), encoding='utf-8')
        # Windows directory rename fails if the destination exists. Dot directories
        # stay invisible to BrowserPool until the complete import is committed.
        staging.rename(target)
    finally:
        if staging.exists() and staging.resolve().parent == root:
            shutil.rmtree(staging)
    return {'name': name, 'cookie_count': len(cookies), 'status': 'unverified'}
