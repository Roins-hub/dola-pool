"""Exercise the saved task contract without importing or starting the live service."""
import ast
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest

from store import TaskStore
from video_capabilities import submission_duration


class ExpertRuntimeOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_media_paths_are_only_injected_after_account_reference_preparation(self):
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        names = {'_run_task', '_plugin_task_options'}
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names]
        store = TaskStore(':memory:')
        self.addCleanup(store._conn.close)
        task_id = 'video_'+'a'*32
        store.create(task_id, 'seedance-2.5', 'exact', '16:9', 15,
                     duration_profile='plugin-expert30', generation_options='{"resolution":"720p"}')
        with tempfile.TemporaryDirectory() as folder:
            image, video = Path(folder)/'image.png', Path(folder)/'clip.mp4'
            image.write_bytes(b'image')
            video.write_bytes(b'video')
            seen = {}
            async def prepare(refs, current_id, *, account):
                self.assertEqual((current_id, account), (task_id, 'fixture-account'))
                return None, [str(image), str(video)]
            async def generate(*args, **kwargs):
                seen['before'] = dict(kwargs['generation_options'])
                seen['paths'] = await kwargs['prepare_references']('fixture-account')
                seen['after'] = dict(kwargs['generation_options'])
                return {'fixture':'finished'}
            async def noop(*args, **kwargs):
                return None
            errors = []
            ns = {'store':store, 'config':SimpleNamespace(ISP_ONLY=True, PUBLIC_BASE='http://fixture',
                    REFERENCE_UPLOAD_DIR=folder), 'asyncio':asyncio, 'json':json, 'time':time,
                  'shutil':shutil, 'Path':Path, 'submission_duration':submission_duration,
                  'key_limiter':SimpleNamespace(acquire=noop,release=noop),
                  'pool':SimpleNamespace(generate_video=generate), 'download_reference_images':prepare,
                  'complete_task':lambda *args:None, 'fail_task':lambda *args:errors.append(args),
                  'cleanup_task_images':lambda *args:None}
            for name in ('AllAccountsLimitedError','AllAccountsQuotaBlockedError',
                         'AllAccountsProxyBlockedError','ExplicitNotAcceptedError'):
                ns[name] = type(name, (Exception,), {})
            exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),
                         'real-expert-task-runtime', 'exec'), ns)
            await ns['_run_task'](task_id, 'seedance-2.5', 'exact', '16:9', 15, [], {})
            self.assertEqual(errors, [])
            self.assertNotIn('reference_media_paths', seen['before'])
            self.assertEqual(seen['paths'], [str(image), str(video)])
            self.assertEqual(seen['after'].get('reference_media_paths'), [str(video.resolve())])
            self.assertEqual(seen['after'].get('requested_duration'), 15)
            self.assertNotIn('reference_media_paths', json.loads(store.get(task_id)['generation_options']))
