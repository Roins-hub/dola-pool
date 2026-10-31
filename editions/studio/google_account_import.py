"""Durable, serial Google login queue. Credentials stay protected by Windows DPAPI."""
import asyncio
import base64
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import uuid
import config

from ck_import import _validated_proxy


def parse_google_accounts(raw):
    if not isinstance(raw, str) or len(raw) > 262144:
        raise ValueError('账号内容过大，请分批导入。')
    rows = []
    email_pattern = r'[^\s@]+@[^\s@]+\.[^\s@]+'
    for number, line in enumerate(raw.lstrip('\ufeff').splitlines(), 1):
        if not line.strip():
            continue
        fields = line.strip().split('----')
        if len(fields) not in (2, 3) or not re.fullmatch(email_pattern, fields[0].strip()) or not fields[1]:
            raise ValueError(f'第 {number} 行格式错误，应为 邮箱----密码----备用邮箱（或 TOTP 密钥）。')
        third = fields[2].strip() if len(fields) == 3 else ''
        recovery, secret = '', ''
        if third:
            if re.fullmatch(email_pattern, third):
                recovery = third
            else:
                secret = third.replace(' ', '').upper()
                try:
                    if len(base64.b32decode(secret + '=' * (-len(secret) % 8))) < 10:
                        raise ValueError()
                except (ValueError, base64.binascii.Error):
                    raise ValueError(f'第 {number} 行第三段既不是备用邮箱，也不是有效的 TOTP 密钥。') from None
        rows.append(dict(email=fields[0].strip(), password=fields[1], recovery_email=recovery, totp=secret))
    if not rows or len(rows) > 500:
        raise ValueError('每批请填写 1–500 个账号，一行一个。')
    return rows


def _dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise ValueError('Google 密码导入目前需要 Windows 本地加密存储。')
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError('本机账号凭据无法加密或解密，请使用原 Windows 用户重试。')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def read_credentials(path):
    return json.loads(_dpapi(Path(path).read_bytes(), decrypt=True))


def import_google_accounts(pool, raw, category='disposable', proxy='', *, proxy_pool=()):
    rows = parse_google_accounts(raw)
    if category not in ('disposable', 'long_term'):
        raise ValueError('账号分类无效')
    proxy = proxy.strip()
    _validated_proxy(proxy)
    root = pool.accounts_dir.resolve()
    existing = {a['email'].strip().lower(): a['name'] for a in pool.list_accounts() if a['email']}
    candidates = list(proxy_pool)
    candidates += [p.read_text(encoding='utf-8-sig').strip() for p in root.glob('*/proxy.txt')
                   if not p.parent.name.startswith('.')]
    routes = {}
    for candidate in candidates:
        try:
            normalized = _validated_proxy(candidate)
            if normalized:
                routes.setdefault(normalized, candidate.strip())
        except ValueError:
            continue
    encrypted = [_dpapi(json.dumps(row).encode()) for row in rows]
    results = []
    root.mkdir(parents=True, exist_ok=True)
    for row, blob in zip(rows, encrypted):
        key = row['email'].lower()
        if key in existing:
            results.append({'name': existing[key], 'existing': True})
            continue
        name = 'google_' + uuid.uuid4().hex[:20]
        staging = root / ('.' + name)
        staging.mkdir()
        try:
            (staging / 'google-login.dpapi').write_bytes(blob)
            route = proxy or (list(routes.values())[len(results) % len(routes)] if routes else '')
            if route:
                (staging / 'proxy.txt').write_text(route + '\n', encoding='utf-8')
            # Metadata is blocked before the profile becomes visible to the scheduler.
            pool.set_onboarding(name, 'queued')
            pool.set_email(name, row['email'])
            with pool._conn:
                pool._conn.execute('UPDATE accounts_meta SET category=? WHERE name=?', (category, name))
            staging.rename(root / name)
        finally:
            if staging.exists() and staging.resolve().parent == root:
                shutil.rmtree(staging)
        existing[key] = name
        results.append({'name': name, 'state': 'queued'})
    return {'imported': sum(not r.get('existing') for r in results),
            'skipped': sum(bool(r.get('existing')) for r in results), 'results': results}


class GoogleImportQueue:
    def __init__(self, pool, *, login=None, verify=None):
        from add_account import add_account_flow
        from account_login import check_account_login
        self.pool = pool
        self.login = login or add_account_flow
        self.verify = verify or check_account_login
        self.task = None

    def recover(self):
        for account in self.pool.list_accounts():
            if account['onboarding_state'] == 'running':
                self.pool.set_onboarding(account['name'], 'queued')

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    def retry(self, name):
        account = next((a for a in self.pool.list_accounts() if a['name'] == name), None)
        if not account or account['onboarding_state'] != 'attention':
            raise ValueError('只有待处理的 Google 登录账号可以重试。')
        if not (self.pool.accounts_dir / name / 'google-login.dpapi').exists():
            raise ValueError('没有保存的登录凭据，请重新导入。')
        self.pool.set_onboarding(name, 'queued')
        self.start()

    async def run(self):
        from add_account import GoogleLoginAttention
        while True:
            account = next((a for a in self.pool.list_accounts() if a['onboarding_state'] == 'queued'), None)
            if not account:
                return
            name = account['name']
            credential_file = self.pool.accounts_dir / name / 'google-login.dpapi'
            lock = self.pool._locks.setdefault(name, asyncio.Lock())
            route = self.pool._submission_proxy(account)
            slot = self.pool._proxy_slots.setdefault(route, asyncio.Semaphore(1))
            try:
                async with lock, slot, self.pool.semaphore:
                    # Deletion is disallowed while the account lock is held.
                    self.pool.set_onboarding(name, 'running')
                    credentials = read_credentials(credential_file)
                    await asyncio.wait_for(self.login(name, credentials['email'], credentials['password'],
                        credentials['totp'], recovery_email=credentials['recovery_email'],
                        login_proxy=None if config.ISP_ONLY else config.PROXY or None,
                        birth_date=credentials.get('birth_date', '')), timeout=300)
                    if await self.verify(name) is not True:
                        raise GoogleLoginAttention('Dola 登录状态尚未确认，请检查代理或完成额外验证后重试。')
                    self.pool.set_login_status(name, True)
                    self.pool.set_onboarding(name, 'ready')
                    credential_file.unlink(missing_ok=True)
            except asyncio.CancelledError:
                self.pool.set_onboarding(name, 'queued')
                raise
            except GoogleLoginAttention as error:
                self.pool.set_onboarding(name, 'attention', str(error))
            except Exception as error:
                # Browser/network exceptions may include passwords or signed URLs.
                self.pool.set_onboarding(name, 'attention',
                    f'登录未完成（{type(error).__name__}），请检查代理、账号资料或额外验证后重试。')
            finally:
                self.pool._available.set()
