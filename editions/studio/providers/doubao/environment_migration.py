"""Back up and upgrade only active account environments, keeping auth and quota intact."""
from collections import Counter
import json
from pathlib import Path

from providers.base import ProviderError
from .accounts import AccountStore, _atomic_bytes, atomic_json
from .environment import create_environment, validate_environment


def migrate_environments(root, backup_root):
    root, backup_root = Path(root).resolve(), Path(backup_root).resolve()
    if backup_root.is_relative_to(root):
        raise ProviderError('invalid_environment_backup', '环境备份必须放在豆包运行资料目录之外，未迁移任何档案。', 409)
    if not (root / 'pool_usage.db').is_file():
        raise ProviderError('account_not_found', '豆包账号数据库不存在，未创建或迁移账号。', 409)
    store = AccountStore(root)
    with store.lock, store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT 1 FROM attempts WHERE state IN ('reserved','unknown','accepted') LIMIT 1").fetchone():
            raise ProviderError('account_busy', '豆包有在途或待核查任务，未迁移环境。', 409)
        rows = conn.execute('SELECT * FROM accounts WHERE trash_token IS NULL ORDER BY created_at,id').fetchall()
        staged, regions = [], Counter()
        for row in rows:
            path = store.folder(row) / 'environment.json'
            try:
                before = path.read_bytes()
                previous = json.loads(before.decode('utf-8'))
                binding = store.bound_proxy(row['name'])
                if previous.get('schema') == 2:
                    environment = validate_environment(previous, row['id'], binding)
                elif previous.get('schema') == 1:
                    environment = create_environment(row['id'], binding, previous=previous)
                    validate_environment(environment, row['id'], binding)
                    staged.append((row['id'], path, before, environment))
                else:
                    raise ValueError()
                regions[environment['route_region']] += 1
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                raise ProviderError('invalid_account_environment', '有豆包环境档案损坏或线路不匹配，整批迁移尚未写入。', 409) from None
        # Every account has been validated before the first production file changes.
        for identifier, _, before, _ in staged:
            backup = backup_root / identifier / 'environment.json'
            if backup.exists() and backup.read_bytes() != before:
                raise ProviderError('environment_backup_conflict', '该备份位置已有不同版本，请使用新的备份目录。', 409)
            backup.parent.mkdir(parents=True, exist_ok=True)
            _atomic_bytes(backup, before)
        changed = []
        try:
            for _, path, before, environment in staged:
                changed.append((path, before))
                atomic_json(path, environment)
        except BaseException:
            for path, before in reversed(changed):
                _atomic_bytes(path, before)
            raise ProviderError('environment_migration_failed', '环境迁移写入失败，原档案已恢复，备份已保留。', 503) from None
        return {'active_accounts': len(rows), 'migrated': len(staged), 'regions': dict(regions)}
