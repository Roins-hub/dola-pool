import asyncio
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

import media
import reference_uploads as uploads


def wav_bytes():
    stream = BytesIO()
    with wave.open(stream, 'wb') as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(8000)
        sound.writeframes(b'\0\0' * 800)
    return stream.getvalue()


class PrivateMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_is_private_owned_and_restored_with_task_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = uploads.save_media(wav_bytes(), '旁白.WAV', tmp, 'user:1', 4096)
            self.assertEqual(item['kind'], 'audio')
            self.assertEqual(item['name'], '旁白.WAV')
            task = 'video_' + 'a' * 32
            with self.assertRaises(ValueError):
                uploads.attach_images([], tmp, 'user:2', task, media_ids=[item['id']])
            refs = uploads.attach_images([], tmp, 'user:1', task, media_ids=[item['id']])
            (Path(tmp) / 'incoming' / item['id']).unlink()
            with patch.object(media.config, 'REFERENCE_UPLOAD_DIR', tmp):
                root, files = await media.download_reference_images(refs, task)
            try:
                self.assertEqual(Path(files[0]).read_bytes(), wav_bytes())
                self.assertEqual(Path(files[0]).suffix, '.wav')
            finally:
                for file in files: Path(file).unlink()
                root.rmdir()
            uploads.cleanup_task_images(tmp, task)
            self.assertFalse((Path(tmp) / 'tasks' / task).exists())

    async def test_bad_extension_fake_content_size_and_traversal_never_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            for data, name, limit in [(wav_bytes(), 'x.txt', 4096),
                                      (b'not audio', 'x.mp3', 4096),
                                      (wav_bytes(), 'x.wav', 10),
                                      (b'', 'x.wav', 4096)]:
                with self.subTest(name=name, limit=limit), self.assertRaises(ValueError):
                    uploads.save_media(data, name, tmp, 'user:1', limit)
            with self.assertRaises(ValueError):
                uploads.attach_images([], tmp, 'user:1', 'video_'+'b'*32,
                                      media_ids=['../secret.wav'])
            self.assertFalse(list(Path(tmp).rglob('*.wav')))

    async def test_media_count_and_image_channel_cannot_be_bypassed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ids = [uploads.save_media(wav_bytes(), 'voice.wav', tmp, 'user:1', 4096)['id']
                   for _ in range(2)]
            task = 'video_'+'c'*32
            with self.assertRaises(ValueError):
                uploads.attach_images(ids, tmp, 'user:1', task)
            with self.assertRaises(ValueError):
                uploads.attach_images([], tmp, 'user:1', task, media_ids=ids, media_max_count=1)
            self.assertFalse((Path(tmp)/'tasks'/task).exists())


class NativeUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_slot_invalid_or_missing_file_cannot_report_success(self):
        from integrations.dola_ref_upload import upload_reference_media
        class Page:
            frames = []
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp)/'x.wav'
            file.write_bytes(wav_bytes())
            for files in [[], [str(file.with_suffix('.txt'))], [str(file)]]:
                ok, message = await upload_reference_media(Page(), files, timeout_s=.05)
                self.assertFalse(ok, message)

    async def test_upload_gate_does_not_fill_or_send_when_native_upload_fails(self):
        import ast
        tree = ast.parse(Path('video_worker_ui.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == 'attach_reference_images')
        events = []
        async def upload(page, paths, **kwargs):
            events.append('upload')
            return False, '素材上传被平台拒绝'
        ns = {'upload_reference_media':upload, 'BrowserSetupError':RuntimeError}
        exec(compile(ast.Module(body=[node],type_ignores=[]), '<upload-gate>', 'exec'), ns)
        with self.assertRaisesRegex(RuntimeError, '素材'):
            await ns['attach_reference_images'](object(), ['voice.wav'])
        self.assertEqual(events, ['upload'])
