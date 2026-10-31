"""Doubao prompt/material helpers; all browser fixtures stay offline."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao.materials import (
    attach_native, compose_video_prompt, normalize_references, normalize_video_spec,
)


def request(**changes):
    return {'prompt': '  原创猫走进花园\n不要背景音乐。  ', 'model': 'native-model',
            'duration': 15, 'ratio': '9:16', **changes}


def capability(**changes):
    refs = {key: {'enabled': True, 'max_count': 3, 'max_bytes': 1024}
            for key in ('images', 'video', 'audio')}
    return {'references': refs, **changes}


class VideoSpecTests(unittest.TestCase):
    def test_duration_is_exact_and_auto_does_not_imply_1080p_or_credits(self):
        spec = normalize_video_spec(request(duration=30))
        self.assertEqual(spec, {'model': 'native-model', 'duration': 30,
                                'ratio': '9:16', 'resolution': 'auto'})
        prompt = compose_video_prompt(request(duration=30), 'Seedance 2.5')
        self.assertIn('30秒', prompt)
        self.assertNotIn('15秒', prompt)
        self.assertNotIn('1080p', prompt)
        self.assertNotIn('额度', prompt)

    def test_nullable_optional_lead_is_absent(self):
        prompt = compose_video_prompt(request(lead_prompt=None), 'Seedance 2.5')
        self.assertTrue(prompt.startswith(request()['prompt']))

    def test_preserves_original_prompt_and_composes_lead_and_spec_once(self):
        data = request(lead_prompt='请让人物保持一致。', resolution='1080p')
        prompt = compose_video_prompt(data, 'Seedance 2.5')
        self.assertIn(data['prompt'], prompt)
        self.assertLess(prompt.index(data['lead_prompt']), prompt.index(data['prompt']))
        self.assertEqual(prompt.count('【视频生成规格】'), 1)
        self.assertIn('1080p', prompt)
        self.assertEqual(compose_video_prompt({**data, 'prompt': prompt}, 'Seedance 2.5'), prompt)

    def test_rejects_bad_spec_instead_of_clamping_or_defaulting(self):
        for changes in ({'duration': True}, {'duration': '15'}, {'duration': 0},
                        {'duration': 301}, {'ratio': '2:3'}, {'resolution': '4k'},
                        {'model': ''}, {'prompt': '  '}, {'lead_prompt': ['x']}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                normalize_video_spec(request(**changes))
        with self.assertRaises(ProviderError):
            compose_video_prompt(request(), 'model\npretend receipt')

    def test_removed_cast_inputs_are_rejected_before_prompt_composition(self):
        for key in ('cast', 'cast_ids'):
            for value in ([{'name': '猫', 'image_indices': [1]}], ['old-role-id'], '["old-role-id"]'):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ProviderError) as error:
                        compose_video_prompt(request(**{key: value}), 'Seedance 2.5')
                    self.assertEqual(error.exception.code, 'unsupported_cast')

    def test_empty_legacy_cast_fields_do_not_block_ordinary_parameters(self):
        spec = normalize_video_spec(request(cast=[], cast_ids='[]'))
        self.assertEqual(spec['duration'], 15)


class ReferenceValidationTests(unittest.TestCase):
    def ref(self, kind='image', **changes):
        return {'kind': kind, 'path': Path('owned') / 'cat.png', 'name': 'cat.png',
                'size_bytes': 20, **changes}

    def test_caps_bound_each_kind_and_return_safe_metadata(self):
        refs = normalize_references([self.ref(), self.ref('audio', name='voice.mp3')], capability())
        self.assertEqual([r['kind'] for r in refs], ['image', 'audio'])
        self.assertEqual(refs[0]['path'], Path('owned/cat.png'))
        self.assertEqual(refs[0]['name'], 'cat.png')

    def test_disabled_kind_and_count_bytes_are_rejected_before_upload(self):
        caps = capability()
        caps['references']['audio']['enabled'] = False
        cases = [([self.ref('audio')], caps),
                 ([self.ref()] * 4, capability()),
                 ([self.ref(size_bytes=1025)], capability()),
                 ([self.ref(size_bytes=True)], capability()),
                 ([self.ref('file')], capability()),
                 ([self.ref(path='C:/not-an-owned-path.png')], capability()),
                 ([self.ref(name='C:/secret/cat.png')], capability())]
        for refs, cap in cases:
            with self.subTest(refs=refs), self.assertRaises(ProviderError):
                normalize_references(refs, cap)


HTML = '''<!doctype html><html><body>
<input id="image" type="file" accept="image/*" multiple>
<input id="audio" type="file" accept=".mp3,.wav" multiple>
<input id="video" type="file" accept="video/mp4" multiple>
<div id="ready"><span class="ready">old.png</span></div><div id="error"></div><div id="pending"></div>
<script>
document.body.dataset.sends='0';
for (const input of document.querySelectorAll('input')) input.onchange = e => {
  document.querySelector('#pending').innerHTML='<span class="pending">uploading</span>';
  setTimeout(()=>{
    for (const file of e.target.files) {
      const node=document.createElement('span');
      node.className=file.name.includes('bad')?'error':'ready';
      node.textContent=file.name.includes('bad')?'native upload rejected':file.name;
      document.querySelector(file.name.includes('bad')?'#error':'#ready').append(node);
    }
    document.querySelector('#pending').innerHTML='';
  }, 35);
};
</script></body></html>'''


class NativeAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        await self.page.set_content(HTML)
        self.selectors = {'upload_image': '#image', 'upload_audio': '#audio',
                          'upload_video': '#video', 'upload_ready': '.ready',
                          'upload_error': '.error', 'upload_pending': '.pending'}

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.temp.cleanup()

    def ref(self, name, kind):
        path = Path(self.temp.name) / name
        path.write_bytes(b'fixture')
        return {'kind': kind, 'path': path, 'name': name, 'size_bytes': 7}

    async def test_waits_for_new_native_ready_items_for_all_three_kinds(self):
        refs = [self.ref('cat.png', 'image'), self.ref('voice.mp3', 'audio'), self.ref('motion.mp4', 'video')]
        result = await attach_native(self.page, refs, self.selectors, timeout=3)
        self.assertEqual(result, {'ok': True, 'counts': {'image': 1, 'video': 1, 'audio': 1},
                                  'files': ['cat.png', 'voice.mp3', 'motion.mp4']})
        self.assertEqual(await self.page.locator('.ready').count(), 4)
        self.assertEqual(await self.page.locator('.pending').count(), 0)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sends'), '0')

    async def test_accept_rejection_occurs_before_any_upload_or_accept_rewrite(self):
        selectors = {**self.selectors, 'upload_audio': '#image'}
        with self.assertRaises(ProviderError) as caught:
            await attach_native(self.page, [self.ref('cat.png', 'image'), self.ref('voice.mp3', 'audio')], selectors, timeout=1)
        self.assertEqual(caught.exception.code, 'unsupported_reference')
        self.assertEqual(await self.page.locator('#image').get_attribute('accept'), 'image/*')
        self.assertEqual(await self.page.locator('#image').evaluate('(n)=>n.files.length'), 0)
        self.assertEqual(await self.page.locator('.ready').count(), 1)

    async def test_native_error_stops_and_returns_no_false_success(self):
        with self.assertRaises(ProviderError) as caught:
            await attach_native(self.page, [self.ref('bad.png', 'image')], self.selectors, timeout=1)
        self.assertEqual(caught.exception.code, 'reference_upload_failed')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sends'), '0')

    async def test_unconfirmed_upload_times_out_and_old_preview_is_not_success(self):
        await self.page.set_content('<input id="image" type="file" accept="image/*" multiple><span class="ready">old.png</span>')
        with self.assertRaises(ProviderError) as caught:
            await attach_native(self.page, [self.ref('cat.png', 'image')], self.selectors, timeout=.2)
        self.assertEqual(caught.exception.code, 'reference_upload_timeout')
        self.assertEqual(await self.page.locator('.ready').count(), 1)

    async def test_existing_error_is_not_reclassified_as_new_upload_error(self):
        await self.page.evaluate("document.querySelector('#error').innerHTML='<span class=error>old failure</span>'")
        result = await attach_native(self.page, [self.ref('cat.png', 'image')], self.selectors, timeout=1)
        self.assertTrue(result['ok'])

    async def test_reused_native_error_container_is_detected_before_ready_success(self):
        await self.page.set_content('''<input id="image" type="file" accept="image/*" multiple
          onchange="document.querySelector('#failure').textContent='new native failure';document.querySelector('#ready').innerHTML='<span class=ready>cat.png</span>'">
          <div id="ready"></div><div id="failure">old native failure</div>''')
        selectors = {**self.selectors, 'upload_error': '#failure'}
        with self.assertRaises(ProviderError) as caught:
            await attach_native(self.page, [self.ref('cat.png', 'image')], selectors, timeout=1)
        self.assertEqual(caught.exception.code, 'reference_upload_failed')

    async def chooser_fixture(self, accept):
        await self.page.set_content(f'''<body><button id="trigger" onclick="document.querySelector('#option').hidden=false">+</button>
          <button id="option" hidden onclick="const n=document.createElement('input');n.type='file';n.accept='{accept}';n.multiple=true;n.id='chosen';n.onchange=()=>{{for(const f of n.files){{const p=document.createElement('span');p.className='ready';p.textContent=f.name;document.body.append(p)}}}};document.body.append(n);n.click()">上传文件或图片</button></body>''')
        return {'upload_trigger': '#trigger', 'upload_option': '#option', 'upload_ready': '.ready'}

    async def test_menu_file_chooser_uses_native_accept_and_waits_for_upload(self):
        selectors = await self.chooser_fixture('image/*,audio/*,video/*')
        result = await attach_native(self.page, [self.ref('cat.png', 'image'), self.ref('voice.mp3', 'audio')], selectors, timeout=2)
        self.assertEqual(result['counts'], {'image': 1, 'video': 0, 'audio': 1})
        self.assertEqual(await self.page.locator('.ready').count(), 2)
        self.assertEqual(await self.page.locator('#chosen').get_attribute('accept'), 'image/*,audio/*,video/*')

    async def test_menu_file_chooser_rejects_native_type_without_attaching_files(self):
        selectors = await self.chooser_fixture('image/*')
        with self.assertRaises(ProviderError) as caught:
            await attach_native(self.page, [self.ref('voice.mp3', 'audio')], selectors, timeout=2)
        self.assertEqual(caught.exception.code, 'unsupported_reference')
        self.assertEqual(await self.page.locator('#chosen').evaluate('(n)=>n.files.length'), 0)
        self.assertEqual(await self.page.locator('.ready').count(), 0)


if __name__ == '__main__':
    unittest.main()
