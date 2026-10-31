"""HTTP contract probes using the existing isolated upload/server fixture."""
import unittest


class PlatformApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_dola_two_five_ten_keeps_model_duration_and_original_prompt(self):
        status,data=await self.f.request('/v1/videos/generations',
            {'platform':'dola','model':'seedance-2.5','duration':10,'ratio':'9:16','prompt':' 原文\n保持 '},'Bearer key1')
        self.assertEqual(status,200,data)
        row=self.f.store.get(data['id'])
        self.assertEqual((row['model'],row['duration'],row['prompt']),('seedance-2.5',10,' 原文\n保持 '))
        self.assertFalse(row.get('duration_profile'))
        self.assertEqual(self.f.runs[0][1:5],('seedance-2.5',' 原文\n保持 ','9:16',10))

    async def asyncSetUp(self):
        from test_member_reference_uploads import MemberUploadTests
        self.f = MemberUploadTests()
        await self.f.asyncSetUp()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    async def test_unknown_platform_is_rejected_before_task_creation(self):
        status, data = await self.f.request('/v1/videos/generations',
            {'platform': 'doubaoo', 'model': 'seedance-2.0', 'prompt': 'test'}, 'Bearer key1')
        self.assertEqual(status, 422, data)
        self.assertEqual(len(self.f.runs), 0)

    async def test_old_request_remains_dola(self):
        status, data = await self.f.request('/v1/videos/generations',
            {'model': 'seedance-2.0', 'prompt': 'test'}, 'Bearer key1')
        self.assertEqual(status, 200, data)
        self.assertEqual(self.f.store.get(data['id']).get('platform'), 'dola')

    async def test_explicit_dola_and_omitted_platform_replay_same_legacy_key(self):
        _submission_identity = self.f.ns['_submission_identity']
        VideoGenRequest = self.f.ns['VideoGenRequest']
        # The legacy Dola digest deliberately remains stable; platform is added for Doubao only.
        a = _submission_identity(VideoGenRequest(prompt='test', idempotency_key='test-platform-key'), 'same')
        b = _submission_identity(VideoGenRequest(platform='dola', prompt='test', idempotency_key='test-platform-key'), 'same')
        self.assertEqual(a, b)
