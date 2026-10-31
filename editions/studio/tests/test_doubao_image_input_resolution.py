"""Composer-local native input resolution survives harmless wrapper changes.

All browser requests are fulfilled with the existing offline attachment fixture.
"""
import unittest

from providers.base import ProviderError
from providers.doubao.dialog_images import prepare_image_references
from test_doubao_dialog_images import NATIVE_HTML, _ImageFixture


OBSERVED_INPUT = ('[data-testid="chat_input"] .guidance-input-surface '
                  '> .relative > input[type="file"]')


class ComposerImageInputResolutionTests(_ImageFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.context.unroute('**/*')
        await self.context.route('**/*', lambda route: route.fulfill(
            status=200, content_type='text/html', body=NATIVE_HTML))
        await self.page.goto('https://www.doubao.com/chat/')
        self.cap['references']['images']['native_state'] = 'attachment_state_v1'
        self.cap['selectors'] = {
            'upload_image': OBSERVED_INPUT,
            'upload_ready': '[data-testid="attachment-image-card"]',
        }
        # The native input remains in this composer, but an intervening wrapper
        # means the formerly observed direct-child CSS no longer finds it.
        await self.page.locator('.guidance-input-surface').evaluate('''surface=>{
          const host=document.createElement('div');host.dataset.testid='chat_input';
          surface.replaceWith(host);host.append(surface);
          const wrap=document.createElement('section');wrap.className='new-wrapper';
          const input=surface.querySelector('#native');surface.append(wrap);wrap.append(input);
          input.hidden=true;input.multiple=true;
          const menu=document.createElement('div');menu.setAttribute('role','menu');
          menu.innerHTML='<input type="file" data-testid="upload-file-input" accept=".png,.jpeg,.jpg,.webp" multiple hidden>';
          host.append(menu);
          menu.querySelector('input').onchange=()=>document.body.dataset.wrongUpload='yes';
        }''')
        self.assertEqual(await self.page.locator(OBSERVED_INPUT).count(), 0)

    async def test_unique_composer_image_input_survives_wrapper_change_without_using_menu(self):
        before = await self.page.locator('#native').evaluate(
            'n=>({accept:n.accept,multiple:n.multiple,hidden:n.hidden})')
        state = await prepare_image_references(self.page, self.refs, self.cap, timeout=1.5)
        self.assertEqual(state['cards'], [('native-image-key', 'cat.png', 'image')])
        self.assertEqual(await self.uploads(), '1')
        self.assertEqual(await self.page.locator('#native').evaluate(
            'n=>({accept:n.accept,multiple:n.multiple,hidden:n.hidden})'), before)
        self.assertEqual(await self.page.locator('[data-testid="upload-file-input"]').evaluate(
            'n=>n.files.length'), 0)
        self.assertIsNone(await self.page.locator('body').get_attribute('data-wrong-upload'))

    async def test_two_non_menu_composer_inputs_are_ambiguous_and_never_uploaded(self):
        await self.page.locator('#native').evaluate('''input=>{
          const other=input.cloneNode();other.id='other-native';input.parentElement.append(other);
          other.onchange=()=>document.body.dataset.wrongUpload='yes';
        }''')
        with self.assertRaises(ProviderError):
            await prepare_image_references(self.page, self.refs, self.cap, timeout=1)
        self.assertEqual(await self.uploads(), '0')
        self.assertIsNone(await self.page.locator('body').get_attribute('data-wrong-upload'))

    async def test_menu_only_input_is_never_substituted_for_composer_image_input(self):
        await self.page.locator('#native').evaluate('n=>n.remove()')
        with self.assertRaises(ProviderError):
            await prepare_image_references(self.page, self.refs, self.cap, timeout=1)
        self.assertEqual(await self.uploads(), '0')
        self.assertEqual(await self.page.locator('[data-testid="upload-file-input"]').evaluate(
            'n=>n.files.length'), 0)
        self.assertIsNone(await self.page.locator('body').get_attribute('data-wrong-upload'))

    async def test_lazy_primary_input_is_initialized_once_without_using_menu_input(self):
        await self.page.locator('#native').evaluate('''input=>{
          input.remove();
          const host=document.querySelector('[data-testid="chat_input"]');
          const menu=host.querySelector('[role="menu"]');menu.hidden=true;
          menu.insertAdjacentHTML('beforeend','<span>上传文件</span>');
          const button=document.createElement('button');button.dataset.testid='upload_file_button';
          button.textContent='添加素材';host.append(button);
          button.onclick=()=>{
            document.body.dataset.primes=String(+(document.body.dataset.primes||0)+1);
            host.querySelector('.new-wrapper').append(input);menu.hidden=false;
          };
          document.addEventListener('keydown',event=>{if(event.key==='Escape')menu.hidden=true;});
        }''', isolated_context=False)
        state=await prepare_image_references(self.page,self.refs,self.cap,timeout=1.5)
        self.assertEqual(state['cards'],[('native-image-key','cat.png','image')])
        self.assertEqual(await self.uploads(),'1')
        self.assertEqual(await self.page.locator('body').get_attribute('data-primes'),'1')
        self.assertFalse(await self.page.get_by_role('menu').is_visible())
        self.assertEqual(await self.page.locator('[data-testid="upload-file-input"]').evaluate('n=>n.files.length'),0)


if __name__ == '__main__':
    unittest.main()
