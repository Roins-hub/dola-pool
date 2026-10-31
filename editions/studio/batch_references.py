"""Immutable, ordered local references shared by all children of one batch.

The first selected account downloads through the project's existing downloader.
The manifest contains digests and local filenames, never source URLs or cookies.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
import weakref


_BATCH_ID = re.compile(r'batch_[a-f0-9]{32}')
_TASK_ID = re.compile(r'video_[a-f0-9]{32}')
_HASH = re.compile(r'[a-f0-9]{64}')
_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.mp3', '.wav', '.m4a',
             '.aac', '.flac', '.ogg', '.mp4', '.mov', '.webm'}
_UPLOAD = re.compile(r'[a-f0-9]{32}\.(?:jpg|png|webp|mp3|wav|m4a|aac|flac|ogg|mp4|mov|webm)')
_LOCKS = weakref.WeakValueDictionary()


class BatchReferenceSnapshotError(ValueError):
    task_phase = 'request_mismatch'


def _locations(batch_id, upload_root):
    if not isinstance(batch_id, str) or not _BATCH_ID.fullmatch(batch_id):
        raise BatchReferenceSnapshotError('批次参考素材标识无效')
    root = Path(upload_root).resolve()
    parent = root / 'batch-snapshots'
    target = parent / batch_id
    if parent.resolve() != parent or target.resolve() != target:
        raise BatchReferenceSnapshotError('批次参考素材路径校验失败')
    return parent, target


def _lock(target):
    key = str(target)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _input_digest(references, task_id):
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise BatchReferenceSnapshotError('任务参考素材标识无效')
    if not isinstance(references, (list, tuple)):
        raise BatchReferenceSnapshotError('批次参考素材列表无效')
    ordered = []
    for value in references:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise BatchReferenceSnapshotError('批次参考素材标识无效')
        if value.startswith('upload:'):
            prefix = f'upload:{task_id}/'
            if not value.startswith(prefix) or not _UPLOAD.fullmatch(value[len(prefix):]):
                raise BatchReferenceSnapshotError('参考素材不属于当前子任务')
            identity = 'upload:' + value[len(prefix):]
        elif value.startswith(('http://', 'https://')):
            identity = value
        else:
            raise BatchReferenceSnapshotError('批次参考素材来源无效')
        # Match the existing downloader's stable first-occurrence deduplication.
        if identity not in ordered:
            ordered.append(identity)
    encoded = json.dumps(ordered, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest(), len(ordered)


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(target, batch_id, expected_digest=None, expected_count=None, *, verify_files=True):
    manifest_path = target / 'manifest.json'
    try:
        if (not target.is_dir() or target.resolve() != target
                or manifest_path.resolve().parent != target or not manifest_path.is_file()
                or manifest_path.stat().st_size > 256 * 1024):
            raise ValueError()
        value = json.loads(manifest_path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or value.get('version') != 1
                or value.get('batch_id') != batch_id
                or not isinstance(value.get('references_sha256'), str)
                or not _HASH.fullmatch(value['references_sha256'])
                or not isinstance(value.get('files'), list)):
            raise ValueError()
        if expected_digest is not None and value['references_sha256'] != expected_digest:
            raise ValueError()
        if expected_count is not None and len(value['files']) != expected_count:
            raise ValueError()
        paths = []
        for index, item in enumerate(value['files'], 1):
            if not isinstance(item, dict) or item.get('index') != index:
                raise ValueError()
            filename = item.get('file')
            suffix = Path(filename).suffix if isinstance(filename, str) else ''
            if (suffix not in _SUFFIXES or filename != f'{index:04d}{suffix}'
                    or not isinstance(item.get('sha256'), str) or not _HASH.fullmatch(item['sha256'])
                    or type(item.get('size')) is not int or item['size'] <= 0):
                raise ValueError()
            path = target / filename
            if path.resolve().parent != target or path.is_symlink():
                raise ValueError()
            if verify_files and (not path.is_file() or path.stat().st_size != item['size']
                                 or _file_digest(path) != item['sha256']):
                raise ValueError()
            paths.append(str(path))
        return value, paths
    except (OSError, ValueError, TypeError, KeyError):
        raise BatchReferenceSnapshotError('批次参考素材快照校验失败，未重新下载或发送') from None


def _remove_stage(stage, parent, batch_id, created):
    if (stage.parent != parent or stage.resolve() != stage
            or not re.fullmatch(r'\.' + re.escape(batch_id) + r'\.[a-f0-9]{32}\.tmp', stage.name)):
        return
    for filename in created:
        path = stage / filename
        try:
            if path.parent == stage and not path.is_dir():
                path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        stage.rmdir()
    except OSError:
        pass


def _cleanup_download(temporary, paths, task_id):
    """Only clean the known temporary directory contract owned by media.py."""
    if temporary is None:
        return
    folder = Path(temporary).absolute()
    if (folder.resolve() != folder or folder.parent != Path(tempfile.gettempdir()).resolve()
            or not folder.name.startswith(f'dola_ref_{task_id}_')):
        return
    for raw in paths:
        path = Path(raw).absolute()
        try:
            if (path.parent == folder and path.resolve().parent == folder and path.is_file()
                    and re.fullmatch(r'image_\d+\.[a-z0-9]+', path.name)):
                path.unlink()
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass


async def prepare_batch_references(batch_id, task_id, references, account, *,
                                   upload_root, download_references):
    parent, target = _locations(batch_id, upload_root)
    identity, count = _input_digest(references, task_id)
    async with _lock(target):
        _locations(batch_id, upload_root)
        if target.exists():
            return None, _read_manifest(target, batch_id, identity, count)[1]
        parent.mkdir(parents=True, exist_ok=True)
        _locations(batch_id, upload_root)
        stage = parent / f'.{batch_id}.{uuid.uuid4().hex}.tmp'
        stage.mkdir()
        created, temporary, sources = [], None, []
        try:
            if references:
                temporary, sources = await download_references(references, task_id, account=account)
            _locations(batch_id, upload_root)
            if stage.resolve() != stage or not stage.is_dir():
                raise BatchReferenceSnapshotError('批次参考素材暂存路径校验失败')
            if not isinstance(sources, (list, tuple)) or len(sources) != count:
                raise BatchReferenceSnapshotError('批次参考素材下载数量与申请不符')
            files = []
            for index, raw in enumerate(sources, 1):
                source = Path(raw).absolute()
                if (not source.is_file() or source.is_symlink() or source.resolve() != source
                        or source.suffix.lower() not in _SUFFIXES or source.stat().st_size <= 0):
                    raise BatchReferenceSnapshotError('批次参考素材下载文件校验失败')
                filename = f'{index:04d}{source.suffix.lower()}'
                destination = stage / filename
                created.append(filename)
                with source.open('rb') as incoming, destination.open('xb') as outgoing:
                    for chunk in iter(lambda: incoming.read(256 * 1024), b''):
                        outgoing.write(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                files.append(dict(index=index, file=filename, sha256=_file_digest(destination),
                                  size=destination.stat().st_size))
            manifest = dict(version=1, batch_id=batch_id, references_sha256=identity, files=files)
            created.append('manifest.json')
            with (stage / 'manifest.json').open('x', encoding='utf-8') as stream:
                json.dump(manifest, stream, ensure_ascii=False, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            _locations(batch_id, upload_root)
            if target.exists():
                return None, _read_manifest(target, batch_id, identity, count)[1]
            stage.rename(target)
            return None, _read_manifest(target, batch_id, identity, count)[1]
        finally:
            if stage.exists():
                _remove_stage(stage, parent, batch_id, created)
            _cleanup_download(temporary, sources if isinstance(sources, (list, tuple)) else [], task_id)


async def cleanup_batch_references(batch_id, upload_root):
    """Caller must first establish that every child is terminal and inactive."""
    parent, target = _locations(batch_id, upload_root)
    async with _lock(target):
        _locations(batch_id, upload_root)
        if not target.exists():
            return
        manifest, paths = _read_manifest(target, batch_id, verify_files=False)
        expected = {item['file'] for item in manifest['files']} | {'manifest.json'}
        if {item.name for item in target.iterdir()} != expected:
            raise BatchReferenceSnapshotError('批次参考素材目录包含未知文件，未清理')
        for raw in paths:
            path = Path(raw)
            if path.parent != target or path.resolve().parent != target or path.is_dir():
                raise BatchReferenceSnapshotError('批次参考素材清理路径校验失败')
        for raw in paths:
            Path(raw).unlink(missing_ok=True)
        (target / 'manifest.json').unlink()
        target.rmdir()
