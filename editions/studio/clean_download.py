"""Task-bound access to verified Dola official original video files only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from video_delivery import qualified_delivery


MAX_SOURCE_BYTES = 250 * 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class CleanDownloadFiles:
    """Resolve only files recorded for one task by the existing provenance log."""

    def __init__(self, db_path: str, download_dir: str | Path):
        self.download_dir = Path(download_dir).resolve()
        self.base_dir = self.download_dir.parent if db_path == ':memory:' else Path(db_path).resolve().parent
        self.provenance_dir = self.base_dir / '.result_provenance'

    @staticmethod
    def _key(task_id: str) -> str:
        return hashlib.sha256(task_id.encode('utf-8')).hexdigest()

    def source(self, task_id: str) -> dict | None:
        manifest_path = self.provenance_dir / (self._key(task_id) + '.json')
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            if manifest.get('task_id') != task_id:
                return None
            path = Path(manifest['local_path']).resolve(strict=True)
            if not path.is_relative_to(self.download_dir) or path.suffix.lower() != '.mp4':
                return None
            if not path.is_file() or not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
                return None
            digest = file_sha256(path)
            if digest != manifest.get('sha256'):
                return None
            return {'path': path, 'sha256': digest, 'metadata': manifest.get('metadata') or {}}
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def status(self, task_id: str) -> dict:
        source = self.source(task_id)
        official = bool(source and qualified_delivery(source['metadata']))
        return {
            'official': {
                'available': official,
                'verified': official,
                'reason': '' if official else '平台尚未提供可验证的无水印原片',
            },
        }

    def download_path(self, task_id: str, kind: str) -> Path | None:
        source = self.source(task_id)
        if kind == 'official' and source and qualified_delivery(source['metadata']):
            return source['path']
        return None
