"""Independent Doubao identities, reservations and account rotation.

Cookie bytes live only inside this provider's private profile tree. Database
identity indexes contain hashes, including accounts in the recycle bin.
"""
import hashlib
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid

from ck_import import _validated_proxy
import config
from config import _parse_proxy, require_isp_proxy
from providers.base import ProviderError
from .environment import create_environment, environment_summary, validate_environment


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_bytes(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_bytes(value)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def normalize_official_user_id(value):
    """Keep opaque official strings and exactly representable numeric IDs."""
    if type(value) is int and 0 < value <= 9007199254740991:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) or value == '0':
        raise ProviderError('invalid_official_identity', '尚未确认官方账号身份，未覆盖已保存的登录会话。', 409)
    return value


def valid_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[\w\-][\w.\-]{0,99}', name) or name in ('.', '..'):
        raise ProviderError('invalid_account', '账号名仅支持文字、数字、下划线、点和短横线，最长 100 字符。')
    return name


def parse_proxy(raw):
    try:
        if raw == 'direct':
            return 'direct', {'server': 'direct'}
        for local in config.doubao_local_proxy_options().values():
            if raw == local['server']:
                return raw, {'server': raw}
        normalized = _validated_proxy(raw)
        parsed = require_isp_proxy(_parse_proxy(normalized))
        # This provider currently uses an authenticated HTTP bridge; unsupported
        # transports must be rejected rather than silently reinterpreted.
        if urlsplit(parsed['server']).scheme != 'http':
            raise ValueError()
        return normalized, parsed
    except (ValueError, TypeError, AttributeError):
        raise ProviderError('proxy_unavailable', '豆包需要明确选择本机直连、公网 HTTP ISP 或已配置的专用代理；连接方式缺失或格式不受支持。', 503) from None


def parse_cookies(raw):
    """Import native cookie attributes without changing their JS visibility.

    Header cookies have no metadata: session credentials default HttpOnly;
    other cookies default readable by the official site's JavaScript. Existing
    persisted cookies are not migrated or guessed by this parser.
    """
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 131072:
        raise ProviderError('invalid_cookie', 'Cookie 不能为空且每个账号最多 128 KB。')
    raw = raw.strip().lstrip('\ufeff')
    cookies = []
    try:
        if raw.startswith(('[', '{')):
            source = json.loads(raw)
            if isinstance(source, dict):
                source = source['cookies']
            if not isinstance(source, list):
                raise ValueError()
        elif '\t' in raw:
            source = []
            for line in raw.splitlines():
                http_only = False
                if line.startswith('#HttpOnly_'):
                    http_only = True
                    line = line[len('#HttpOnly_'):]
                elif line.startswith('#') or not line.strip():
                    continue
                fields = line.split('\t')
                if (len(fields) != 7 or fields[1] not in ('TRUE', 'FALSE')
                        or fields[3] not in ('TRUE', 'FALSE')
                        or not re.fullmatch(r'[0-9]+', fields[4]) or fields[0].startswith('..')):
                    raise ValueError()
                domain = fields[0]
                if fields[1] == 'TRUE' and not domain.startswith('.'):
                    domain = '.' + domain
                elif fields[1] == 'FALSE' and domain.startswith('.'):
                    domain = domain[1:]
                cookie = {'domain': domain, 'path': fields[2], 'name': fields[5], 'value': fields[6],
                          'secure': fields[3] == 'TRUE', 'httpOnly': http_only}
                if int(fields[4]):  # Netscape zero denotes a session cookie.
                    cookie['expires'] = int(fields[4])
                source.append(cookie)
        else:
            # The administrator has explicitly selected Doubao. Header cookies
            # have no original domain and remain unverified until a login check.
            source = []
            for item in raw.removeprefix('Cookie:').strip().split(';'):
                if not item.strip():
                    continue
                key, value = item.strip().split('=', 1)
                source.append({'name': key, 'value': value, 'domain': '.doubao.com'})
        for item in source:
            domain = item.get('domain') or '.doubao.com'
            if not isinstance(domain, str):
                raise ValueError()
            domain = domain.lower()
            host = domain[1:] if domain.startswith('.') else domain
            if (len(host) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*', host)
                    or any(len(label) > 63 for label in host.split('.'))
                    or (host != 'doubao.com' and not host.endswith('.doubao.com'))):
                raise ValueError()
            name, value = item['name'], item['value']
            if not isinstance(name, str) or not re.fullmatch(r'[!#$%&\x27*+.^_`|~\w-]+', name):
                raise ValueError()
            if not isinstance(value, str) or re.search(r'[\x00-\x20\x7f;]', value):
                raise ValueError()
            path = item.get('path', '/')
            secure = item.get('secure', True)
            http_only = item.get('httpOnly', name in ('sessionid', 'sessionid_ss', 'sid_guard'))
            if (not isinstance(path, str) or not path.startswith('/') or re.search(r'[\x00-\x1f\x7f;]', path)
                    or type(secure) is not bool or type(http_only) is not bool):
                raise ValueError()
            cookie = {'name': name, 'value': value, 'domain': domain,
                      'path': path, 'secure': secure, 'httpOnly': http_only}
            same_site = item.get('sameSite', 'Lax')
            same_sites = {'Strict': 'Strict', 'Lax': 'Lax', 'None': 'None',
                          'strict': 'Strict', 'lax': 'Lax', 'none': 'None',
                          'no_restriction': 'None', 'unspecified': None}
            if not isinstance(same_site, str) or same_site not in same_sites:
                raise ValueError()
            if same_sites[same_site] is not None:
                cookie['sameSite'] = same_sites[same_site]
            for key in ('expires', 'expirationDate'):
                if key in item:
                    expires = item[key]
                    if (type(expires) not in (int, float)
                            or not (expires == -1 or 0 <= expires <= 253402300799)
                            or not math.isfinite(expires)):
                        raise ValueError()
                    if 'expires' in cookie and cookie['expires'] != expires:
                        raise ValueError()
                    cookie['expires'] = expires
            cookies.append(cookie)
        if not any(c['name'] in ('sessionid', 'sessionid_ss', 'sid_guard') and c['value'] for c in cookies):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ProviderError('invalid_cookie', '豆包 Cookie 格式无效、缺少会话凭证或包含其他平台的域。') from None
    identities = []
    for item in cookies:
        if item['name'] in ('uid_tt', 'uid_tt_ss', 'sessionid', 'sessionid_ss', 'sid_guard'):
            kind = 'user' if item['name'].startswith('uid_tt') else 'session'
            if item['value']:
                identities.append(hashlib.sha256((kind + ':' + item['value']).encode()).hexdigest())
    return cookies, sorted(set(identities))


class AccountStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'pool_usage.db'
        self.lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS accounts(
                    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                    category TEXT NOT NULL DEFAULT 'default', enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'unverified', identities TEXT NOT NULL,
                    send_count INTEGER NOT NULL DEFAULT 0, last_sent REAL,
                    quota_limit INTEGER, quota_used INTEGER NOT NULL DEFAULT 0,
                    platform_credits REAL, credits_observed_at REAL,
                    diagnostics TEXT NOT NULL DEFAULT '{}', last_checked_at REAL,
                    trash_token TEXT UNIQUE, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(
                    task_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'reserved',
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _row(self, name, conn=None, include_trash=False):
        if conn is None:
            with self.connect() as db:
                return self._row(name, db, include_trash)
        row = conn.execute('SELECT * FROM accounts WHERE (name=? OR id=?)', (name, name)).fetchone()
        if row is None or (row['trash_token'] and not include_trash):
            raise ProviderError('account_not_found', '豆包账号不存在。', 404)
        return row

    def folder(self, row):
        parent = 'trash' if row['trash_token'] else 'accounts'
        return self.root / parent / row['id']

    def public(self, row, conn):
        count = conn.execute("SELECT COUNT(*) FROM attempts WHERE account_id=? AND state IN ('reserved','unknown','accepted','charge_unknown')", (row['id'],)).fetchone()[0]
        inflight = conn.execute("SELECT COUNT(*) FROM attempts WHERE account_id=? AND state IN ('reserved','unknown','accepted')", (row['id'],)).fetchone()[0]
        checks = json.loads(row['diagnostics'])
        label = '未配置'
        region = '未验证'
        try:
            proxy = json.loads((self.folder(row) / 'proxy.json').read_text(encoding='utf-8'))
            if proxy == {'server': 'direct'}:
                label = '本机直连（豆包专用）'
                region = '本机网络'
            else:
                endpoint = urlsplit(proxy['server'])
                if not endpoint.hostname or not endpoint.port:
                    raise ValueError()
                label = f'{endpoint.hostname}:{endpoint.port}'
                for local in config.doubao_local_proxy_options().values():
                    if proxy == {'server': local['server']}:
                        label, region = local['label'], local['country']
                        break
        except (OSError, ValueError, KeyError, TypeError):
            label = '配置损坏'
        environment = {'status': 'invalid', 'region': '未匹配', 'locale': None,
                       'timezone_id': None, 'viewport': None, 'hardware_mode': 'native', 'isolated': True}
        try:
            binding = self._read_binding(row)
            saved = json.loads((self.folder(row) / 'environment.json').read_text(encoding='utf-8'))
            environment = environment_summary(saved, row['id'], binding)
        except (OSError, ValueError, TypeError, KeyError, ProviderError):
            pass
        return {'platform': 'doubao', 'account_id': row['id'], 'name': row['name'],
                'category': row['category'], 'enabled': bool(row['enabled']), 'status': row['status'],
                'send_count': row['send_count'], 'last_sent': row['last_sent'],
                'quota_limit': row['quota_limit'], 'quota_used': row['quota_used'], 'quota_reserved': count, 'inflight_tasks':inflight,
                'quota_remaining': None if row['quota_limit'] is None else max(0, row['quota_limit'] - row['quota_used'] - count),
                'platform_credits': row['platform_credits'], 'credits_observed_at': row['credits_observed_at'],
                'proxy_label': label, 'route_region': checks.get('route_region', region),
                'browser_environment': environment,
                'route_verified': bool(checks.get('route_verified')),
                'login_verified': bool(checks.get('login_verified')), 'diagnostics': checks,
                'last_checked_at': row['last_checked_at'], 'token': row['trash_token']}

    def list_accounts(self, trash=False):
        with self.connect() as conn:
            rows = conn.execute('SELECT * FROM accounts WHERE trash_token IS ' + ('NOT NULL' if trash else 'NULL') + ' ORDER BY created_at,id').fetchall()
            return [self.public(row, conn) for row in rows]

    def import_ck(self, name, raw, proxy, category='default', *, environment=None):
        name = valid_name(name)
        cookies, identities = parse_cookies(raw)
        normalized, binding = parse_proxy(proxy)
        if not isinstance(category, str) or not category.strip() or len(category) > 80:
            raise ProviderError('invalid_category', '分类名称无效。')
        identifier = uuid.uuid4().hex
        if environment is not None:
            if (not isinstance(environment, dict)
                    or not isinstance(environment.get('environment_id'), str)
                    or not re.fullmatch(r'[a-f0-9]{32}', environment['environment_id'])):
                raise ProviderError('invalid_account', '登录浏览器环境的账号标识无效。')
            identifier = environment['environment_id']
            environment = validate_environment(environment, identifier, binding)
        else:
            environment = create_environment(identifier, binding)
        folder = self.root / 'accounts' / identifier
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM accounts WHERE id=?', (identifier,)).fetchone():
                raise ProviderError('duplicate_account', '该登录浏览器已绑定账号，未覆盖原有账号资料。', 409)
            for row in conn.execute('SELECT name,identities FROM accounts'):
                if row['name'] == name or set(identities) & set(json.loads(row['identities'])):
                    raise ProviderError('duplicate_account', '该豆包账号已存在于号池或回收站，未重复导入。', 409)
            created_folder = False
            try:
                folder.mkdir(parents=True)
                created_folder = True
                atomic_json(folder / 'cookies.json', cookies)
                atomic_json(folder / 'proxy.json', binding)
                atomic_json(folder / 'environment.json', environment)
                limit = self.quota_policy()['limit']
                conn.execute('INSERT INTO accounts(id,name,category,identities,created_at,quota_limit) VALUES(?,?,?,?,?,?)',
                             (identifier, name, category.strip(), json.dumps(identities), time.time(), limit))
                conn.commit()
            except BaseException:
                if created_folder and folder.exists():
                    shutil.rmtree(folder)
                raise
            return self.public(self._row(identifier, conn), conn)

    def _official_identity(self, row):
        path = self.folder(row) / 'official-identity.json'
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError()
            value['user_id'] = normalize_official_user_id(value['user_id'])
            return value
        except (OSError, ValueError, KeyError, TypeError):
            raise ProviderError('invalid_official_identity', '已保存的官方身份记录不可用，未覆盖账号或登录会话。', 409) from None

    def official_user_id(self, name):
        """Private identity lookup; the public account view never includes it."""
        identity = self._official_identity(self._row(name))
        return identity['user_id'] if identity else None

    @staticmethod
    def _mark_unavailable(conn, row, reason, duplicate_of=None):
        checks = json.loads(row['diagnostics'])
        checks.update(blocked_reason=reason, blocked_at=time.time())
        if reason == 'login_required':
            checks.update(login_verified=False, login_state='signed_out')
        if duplicate_of is not None:
            checks['duplicate_of'] = duplicate_of
        conn.execute('UPDATE accounts SET enabled=0,status=?,diagnostics=?,last_checked_at=? WHERE id=?',
                     (reason, json.dumps(checks, ensure_ascii=False), time.time(), row['id']))

    def mark_unavailable(self, name, reason, duplicate_of=None):
        if reason not in ('login_required', 'duplicate', 'identity_mismatch'):
            raise ValueError('unsupported account block reason')
        with self.lock, self.connect() as conn:
            row = self._row(name, conn)
            self._mark_unavailable(conn, row, reason, duplicate_of)

    def observe_identity(self, name, official_user_id):
        """Bind observed native identity; quarantine legacy duplicates before send.

        No cookies, proxy or environment files are replaced by this check.
        """
        official_user_id = normalize_official_user_id(official_user_id)
        blocked = None
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._row(name, conn)
            own = self._official_identity(row)
            if own and own['user_id'] != official_user_id:
                self._mark_unavailable(conn, row, 'identity_mismatch')
                blocked = ProviderError('account_identity_mismatch', '豆包当前官方身份与此账号记录不符，已暂停；未发送提示词。', 409)
            else:
                digest = hashlib.sha256(('official:' + official_user_id).encode()).hexdigest()
                for other in conn.execute('SELECT * FROM accounts WHERE id<>?', (row['id'],)):
                    checks = json.loads(other['diagnostics'])
                    # An explicitly quarantined legacy alias does not block its
                    # preserved canonical owner, but never becomes a new owner.
                    if not other['enabled'] and checks.get('blocked_reason') == 'duplicate' and checks.get('duplicate_of') == row['name']:
                        continue
                    identity = self._official_identity(other)
                    if (identity and identity['user_id'] == official_user_id) or digest in json.loads(other['identities']):
                        self._mark_unavailable(conn, row, 'duplicate', other['name'])
                        blocked = ProviderError('duplicate_account', '同一豆包官方账号已存在于其他记录，当前重复记录已暂停；未发送提示词。', 409)
                        break
                if blocked is None and own is None:
                    path = self.folder(row) / 'official-identity.json'
                    try:
                        atomic_json(path, {'user_id': official_user_id, 'source': 'official_router_account_info', 'observed_at': time.time()})
                        identities = sorted(set(json.loads(row['identities'])) | {digest})
                        conn.execute('UPDATE accounts SET identities=? WHERE id=?', (json.dumps(identities), row['id']))
                        conn.commit()
                    except BaseException:
                        path.unlink(missing_ok=True)
                        raise
        if blocked is not None:
            raise blocked

    def refresh_session(self, name, raw, official_user_id):
        """Refresh an explicitly selected account, preserving all scheduling data.

        An absent stable ID may be bound by the explicit restore operation. Cookie
        user markers may rotate; they are not the official user identity. Automatic
        runtime sync calls this only after an already-bound identity matches.
        """
        official_user_id = normalize_official_user_id(official_user_id)
        cookies, incoming = parse_cookies(raw)
        official_hash = hashlib.sha256(('official:' + official_user_id).encode()).hexdigest()
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._row(name, conn)
            identity = self._official_identity(row)
            if identity and identity['user_id'] != official_user_id:
                raise ProviderError('account_identity_mismatch', '扫码身份与原账号不一致，未覆盖原账号的登录会话。', 409)
            for other in conn.execute('SELECT * FROM accounts WHERE id<>?', (row['id'],)):
                checks = json.loads(other['diagnostics'])
                if not other['enabled'] and checks.get('blocked_reason') == 'duplicate' and checks.get('duplicate_of') == row['name']:
                    continue
                other_identity = self._official_identity(other)
                if ((other_identity and other_identity['user_id'] == official_user_id)
                        or (set(incoming) | {official_hash}) & set(json.loads(other['identities']))):
                    raise ProviderError('duplicate_account', '该官方账号已属于其他账号记录或回收站，未混入当前账号。', 409)
            folder = self.folder(row)
            cookie_path, identity_path = folder / 'cookies.json', folder / 'official-identity.json'
            previous = {path: path.read_bytes() if path.exists() else None for path in (cookie_path, identity_path)}
            try:
                atomic_json(cookie_path, cookies)
                atomic_json(identity_path, {**(identity or {}), 'user_id': official_user_id,
                    'source': 'official_router_account_info', 'observed_at': time.time()})
                identities = sorted(set(json.loads(row['identities'])) | set(incoming) | {official_hash})
                conn.execute('UPDATE accounts SET identities=? WHERE id=?', (json.dumps(identities), row['id']))
                conn.commit()
            except BaseException:
                for path, value in previous.items():
                    if value is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_bytes(path, value)
                raise
            return self.public(self._row(row['id'], conn), conn)

    def _read_binding(self, row):
        try:
            binding = json.loads((self.folder(row) / 'proxy.json').read_text(encoding='utf-8'))
            direct = binding == {'server': 'direct'}
            local = any(binding == {'server': option['server']}
                        for option in config.doubao_local_proxy_options().values())
            if not direct and not local:
                require_isp_proxy(binding)
            if not direct and urlsplit(binding['server']).scheme != 'http':
                raise ValueError()
            return binding
        except (ValueError, TypeError, OSError, KeyError):
            raise ProviderError('proxy_unavailable', '豆包账号绑定线路或环境配置损坏，已停止，未回退其他网络。', 503) from None

    def _read_environment(self, row, binding):
        try:
            env = json.loads((self.folder(row) / 'environment.json').read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            raise ProviderError('environment_unavailable', '豆包账号浏览器环境档案损坏，未启动浏览器。', 503) from None
        if isinstance(env, dict) and type(env.get('schema')) is int and env['schema'] == 1:
            return create_environment(row['id'], binding, previous=env), True
        return validate_environment(env, row['id'], binding), False

    def bound_proxy(self, name):
        with self.lock:
            row = self._row(name)
            binding = self._read_binding(row)
            self._read_environment(row, binding)  # Validate only; never reset a saved environment.
            return binding

    def browser_environment(self, name):
        """Migrate a valid legacy profile once; never repair route/identity mismatches."""
        with self.lock:
            row = self._row(name)
            binding = self._read_binding(row)
            env, legacy = self._read_environment(row, binding)
            if legacy:
                atomic_json(self.folder(row) / 'environment.json', env)
            return env

    def _busy(self, row, conn):
        if conn.execute("SELECT 1 FROM attempts WHERE account_id=? AND state IN ('reserved','unknown','accepted')", (row['id'],)).fetchone():
            raise ProviderError('account_busy', '该豆包账号仍有在途或待核查任务，请先完成核查。', 409)

    def patch(self, name, **changes):
        allowed = {'enabled', 'category', 'proxy', 'quota_limit'}
        if not changes or set(changes) - allowed:
            raise ProviderError('invalid_account', '包含不支持的账号修改字段。')
        for key, value in changes.items():
            if key == 'enabled' and type(value) is not bool:
                raise ProviderError('invalid_account', '启停参数必须是布尔值。')
            if key == 'quota_limit' and value is not None and (type(value) is not int or not 0 <= value <= 100000):
                raise ProviderError('invalid_quota', '本地次数应为空或 0～100000 的整数。')
            if key == 'category' and (not isinstance(value, str) or not value.strip() or len(value) > 80):
                raise ProviderError('invalid_category', '分类名称无效。')
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._row(name, conn)
            previous = {}
            binding = env = None
            if 'proxy' in changes:
                self._busy(row, conn)
                _, binding = parse_proxy(changes.pop('proxy'))
                folder = self.folder(row)
                old_binding = self._read_binding(row)
                old_env, _ = self._read_environment(row, old_binding)
                env = create_environment(row['id'], binding, previous=old_env)
                previous = {path: path.read_bytes() for path in (folder / 'proxy.json', folder / 'environment.json')}
            try:
                if binding is not None:
                    atomic_json(folder / 'proxy.json', binding)
                    atomic_json(folder / 'environment.json', env)
                    conn.execute("UPDATE accounts SET diagnostics='{}',status='unverified',last_checked_at=NULL WHERE id=?", (row['id'],))
                for key, value in changes.items():
                    if key == 'enabled':
                        value = int(value)
                    conn.execute(f'UPDATE accounts SET {key}=? WHERE id=?', (value, row['id']))
                conn.commit()
            except BaseException:
                for path, value in previous.items():
                    _atomic_bytes(path, value)
                raise
            return self.public(self._row(name, conn), conn)

    def delete(self, name):
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._row(name, conn)
            self._busy(row, conn)
            token = uuid.uuid4().hex
            target = self.root / 'trash' / row['id']
            target.parent.mkdir(parents=True, exist_ok=True)
            self.folder(row).replace(target)
            conn.execute('UPDATE accounts SET trash_token=?,enabled=0 WHERE id=?', (token, row['id']))
            return {'platform': 'doubao', 'name': row['name'], 'token': token}

    def restore(self, token):
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM accounts WHERE trash_token=?', (token,)).fetchone()
            if row is None:
                raise ProviderError('account_not_found', '回收记录不存在。', 404)
            target = self.root / 'accounts' / row['id']
            if target.exists():
                raise ProviderError('restore_conflict', '目标账号目录已存在，请先处理重名冲突。', 409)
            self.folder(row).replace(target)
            conn.execute("UPDATE accounts SET trash_token=NULL,status='unverified',diagnostics='{}' WHERE id=?", (row['id'],))
            conn.commit()
            return self.public(self._row(row['id'], conn), conn)

    def reserve(self, task_id, name):
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._row(name, conn)
            attempt = conn.execute('SELECT * FROM attempts WHERE task_id=?', (task_id,)).fetchone()
            if attempt:
                if attempt['account_id'] != row['id']:
                    raise ProviderError('account_conflict', '任务已绑定另一个豆包账号。', 409)
                return dict(attempt)
            self._busy(row, conn)
            public = self.public(row, conn)
            if not row['enabled'] or public['quota_remaining'] == 0:
                raise ProviderError('account_unavailable', '豆包账号已停用或达到本地次数上限。', 503)
            conn.execute('INSERT INTO attempts(task_id,account_id,created_at) VALUES(?,?,?)', (task_id, row['id'], time.time()))
            return {'task_id': task_id, 'account_id': row['id'], 'sent': 0, 'state': 'reserved'}

    def mark_sent(self, task_id):
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM attempts WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise ProviderError('task_not_found', '任务未预留账号。', 409)
            if not row['sent']:
                conn.execute('UPDATE attempts SET sent=1 WHERE task_id=?', (task_id,))
                conn.execute('UPDATE accounts SET send_count=send_count+1,last_sent=? WHERE id=?', (time.time(), row['account_id']))

    def reject_before_send(self, task_id, previous_last_sent=None):
        """Undo only this reserved attempt after a proved failure before click.

        The provider sets the durable send fence before calling submit. A
        pre-click check can still reject without ever contacting the platform;
        only that explicit error permits restoring rotation and reservation.
        Once accepted, unknown or settled, an attempt is never reversed here.
        """
        if previous_last_sent is not None and (type(previous_last_sent) not in (int, float)
                or not math.isfinite(previous_last_sent) or previous_last_sent < 0):
            raise ProviderError('unsafe_before_send_rollback', '发送前的轮询记录无效，未修改账号计数。', 409)
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM attempts WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise ProviderError('task_not_found', '任务未预留账号。', 409)
            if row['state'] == 'rejected' and not row['sent']:
                return
            if row['state'] != 'reserved':
                raise ProviderError('unsafe_before_send_rollback', '任务已经发送或结果待核查，禁止撤销额度和轮询计数。', 409)
            if row['sent']:
                changed = conn.execute('UPDATE accounts SET send_count=send_count-1,last_sent=? WHERE id=? AND send_count>0',
                                       (previous_last_sent, row['account_id']))
                if changed.rowcount != 1:
                    raise ProviderError('unsafe_before_send_rollback', '账号发送计数不一致，未撤销记录。', 409)
            conn.execute("UPDATE attempts SET sent=0,state='rejected' WHERE task_id=?", (task_id,))

    def settle(self, task_id, outcome):
        if outcome not in ('rejected', 'unknown', 'accepted', 'completed', 'charge_unknown'):
            raise ValueError('unsupported settlement')
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM attempts WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                return
            if row['state'] in ('rejected', 'completed'):
                return
            if outcome == 'completed':
                conn.execute('UPDATE accounts SET quota_used=quota_used+1 WHERE id=?', (row['account_id'],))
            conn.execute('UPDATE attempts SET state=? WHERE task_id=?', (outcome, task_id))

    def save_diagnostics(self, name, diagnostics):
        with self.connect() as conn:
            row = self._row(name, conn)
            conn.execute('UPDATE accounts SET diagnostics=?,last_checked_at=?,status=? WHERE id=?',
                         (json.dumps(diagnostics, ensure_ascii=False), time.time(),
                          'ready' if diagnostics.get('login_verified') and diagnostics.get('route_verified') else 'unverified', row['id']))
            if diagnostics.get('login_state') == 'signed_out':
                self._mark_unavailable(conn, self._row(name, conn), 'login_required')
            conn.commit()
            return self.public(self._row(name, conn), conn)

    def quota_policy(self, limit=...):
        with self.connect() as conn:
            if limit is not ...:
                if limit is not None and (type(limit) is not int or not 0 <= limit <= 100000):
                    raise ProviderError('invalid_quota', '豆包本地次数应为空或 0～100000 的整数。')
                conn.execute("INSERT INTO settings(key,value) VALUES('quota_limit',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(limit),))
                conn.execute('UPDATE accounts SET quota_limit=?', (limit,))
            row = conn.execute("SELECT value FROM settings WHERE key='quota_limit'").fetchone()
            return {'mode': 'independent', 'limit': json.loads(row['value']) if row else None,
                    'reset': 'manual', 'note': '独立本地次数；平台积分需实测，不套用 Dola 策略。'}
