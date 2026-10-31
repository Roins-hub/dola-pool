import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

try:
    import batch_references as snapshots
except ModuleNotFoundError:
    snapshots = None


BATCH = 'batch_' + 'a' * 32
TASK1 = 'video_' + '1' * 32
TASK2 = 'video_' + '2' * 32
IMAGE = '3' * 32 + '.png'


class BatchReferenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(snapshots, 'The persistent batch reference snapshot feature is missing')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.uploads = self.root / 'uploads'
        self.calls = []

    def references(self, task):
        return ['https://images.example/first.png?private=do-not-store', f'upload:{task}/{IMAGE}']

    async def downloader(self, references, task_id, *, account=None):
        self.calls.append((list(references), task_id, account))
        await asyncio.sleep(.01)
        folder = self.root / f'download-{len(self.calls)}'
        folder.mkdir()
        first, second = folder / 'remote.png', folder / 'uploaded.png'
        first.write_bytes(b'remote-content-version-' + str(len(self.calls)).encode())
        second.write_bytes(b'uploaded-content')
        return folder, [str(first), str(second)]

    async def prepare(self, task=TASK1, references=None, batch=BATCH, account='first-bound-isp'):
        return await snapshots.prepare_batch_references(batch, task,
            self.references(task) if references is None else references, account,
            upload_root=self.uploads, download_references=self.downloader)

    async def test_restart_and_different_child_account_reuse_identical_ordered_bytes(self):
        temporary, paths = await self.prepare()
        self.assertIsNone(temporary)
        original = [Path(path).read_bytes() for path in paths]
        spec = importlib.util.spec_from_file_location('batch_references_restart_fixture', snapshots.__file__)
        restarted = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(restarted)
        later_temporary, later_paths = await restarted.prepare_batch_references(BATCH, TASK2,
            self.references(TASK2), 'another-bound-isp', upload_root=self.uploads,
            download_references=self.downloader)
        self.assertIsNone(later_temporary)
        self.assertEqual(later_paths, paths)
        self.assertEqual([Path(path).read_bytes() for path in later_paths], original)
        self.assertEqual(original, [b'remote-content-version-1', b'uploaded-content'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1:], (TASK1, 'first-bound-isp'))
        folder = self.uploads / 'batch-snapshots' / BATCH
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual([item['sha256'] for item in manifest['files']],
                         [hashlib.sha256(value).hexdigest() for value in original])
        raw = (folder / 'manifest.json').read_text(encoding='utf-8')
        for private in ('images.example', 'do-not-store', 'first-bound-isp', TASK1, 'Cookie'):
            self.assertNotIn(private, raw)

    async def test_simultaneous_children_only_download_the_reference_set_once(self):
        results = await asyncio.gather(self.prepare(), self.prepare(TASK2))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.calls), 1)

    async def test_tampered_snapshot_is_rejected_without_fetching_changed_remote_content(self):
        _, paths = await self.prepare()
        Path(paths[0]).write_bytes(b'changed-on-disk')
        with self.assertRaises(snapshots.BatchReferenceSnapshotError):
            await self.prepare(TASK2)
        self.assertEqual(len(self.calls), 1)

    async def test_different_reference_order_cannot_reuse_the_existing_snapshot(self):
        await self.prepare()
        with self.assertRaises(snapshots.BatchReferenceSnapshotError):
            await self.prepare(TASK2, list(reversed(self.references(TASK2))))
        self.assertEqual(len(self.calls), 1)

    async def test_manifest_cannot_reference_files_outside_the_batch(self):
        await self.prepare()
        outside = self.uploads / 'outside.png'
        outside.write_bytes(b'leave-me-alone')
        path = self.uploads / 'batch-snapshots' / BATCH / 'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['files'][0]['file'] = '../../outside.png'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(snapshots.BatchReferenceSnapshotError):
            await self.prepare(TASK2)
        with self.assertRaises(snapshots.BatchReferenceSnapshotError):
            await snapshots.cleanup_batch_references(BATCH, self.uploads)
        self.assertEqual(outside.read_bytes(), b'leave-me-alone')

    async def test_invalid_batch_or_foreign_upload_reference_never_calls_the_downloader(self):
        for batch in ('../escape', 'batch_a', 'batch_' + 'g' * 32):
            with self.subTest(batch=batch), self.assertRaises(ValueError):
                await self.prepare(batch=batch)
        with self.assertRaises(ValueError):
            await self.prepare(TASK2, self.references(TASK1))
        self.assertEqual(self.calls, [])

    async def test_cleanup_only_removes_the_validated_batch_directory(self):
        await self.prepare()
        other = self.uploads / 'batch-snapshots' / ('batch_' + 'b' * 32)
        other.mkdir()
        sentinel = other / 'keep.txt'
        sentinel.write_text('keep', encoding='utf-8')
        await snapshots.cleanup_batch_references(BATCH, self.uploads)
        self.assertFalse((self.uploads / 'batch-snapshots' / BATCH).exists())
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')
        await snapshots.cleanup_batch_references(BATCH, self.uploads)

    async def test_missing_download_file_never_publishes_a_partial_manifest(self):
        async def incomplete(references, task, *, account=None):
            self.calls.append('incomplete')
            return None, [str(self.root / 'missing.png')]
        with self.assertRaises(snapshots.BatchReferenceSnapshotError):
            await snapshots.prepare_batch_references(BATCH, TASK1, self.references(TASK1), None,
                upload_root=self.uploads, download_references=incomplete)
        self.assertFalse((self.uploads / 'batch-snapshots' / BATCH).exists())
        _, paths = await self.prepare()
        self.assertEqual(len(paths), 2)

    async def test_empty_snapshot_skips_network_and_cannot_be_replaced_with_later_material(self):
        self.assertEqual(await self.prepare(references=[]), (None, []))
        self.assertEqual(await self.prepare(TASK2, references=[]), (None, []))
        with self.assertRaises(snapshots.BatchReferenceSnapshotError):
            await self.prepare(TASK2)
        self.assertEqual(self.calls, [])

    async def test_cleanup_refuses_unowned_extra_files_without_removing_any_recorded_file(self):
        _, paths = await self.prepare()
        folder = self.uploads / 'batch-snapshots' / BATCH
        extra = folder / 'not-created-by-snapshot.txt'
        extra.write_bytes(b'preserve')
        with self.assertRaises(snapshots.BatchReferenceSnapshotError):
            await snapshots.cleanup_batch_references(BATCH, self.uploads)
        self.assertTrue(all(Path(path).is_file() for path in paths))
        self.assertEqual(extra.read_bytes(), b'preserve')

    async def test_changed_staging_path_after_download_is_rejected_before_writing_snapshot_files(self):
        async def replacing_downloader(references, task, *, account=None):
            downloaded = await self.downloader(references, task, account=account)
            stage = next((self.uploads / 'batch-snapshots').glob(f'.{BATCH}.*.tmp'))
            stage.rmdir()
            stage.write_bytes(b'foreign-staging-path')
            return downloaded
        error = None
        try:
            await snapshots.prepare_batch_references(BATCH, TASK1, self.references(TASK1), None,
                upload_root=self.uploads, download_references=replacing_downloader)
        except Exception as caught:
            error = caught
        self.assertIsInstance(error, snapshots.BatchReferenceSnapshotError)
        self.assertFalse((self.uploads / 'batch-snapshots' / BATCH).exists())


if __name__ == '__main__':
    unittest.main()
