"""Replay the real duration setter against Dola's accessible 4-10s slider."""
import ast
import copy
from pathlib import Path
import unittest
from patchright.async_api import async_playwright
import video_worker_ui as worker


def actual_slider_setter():
    tree=ast.parse(Path(worker.__file__).read_text(encoding='utf-8'))
    generate=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='generate_video')
    setter=next(n for n in ast.walk(generate) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_set_slider_value')
    wrapper=ast.parse('async def run(page, target): pass').body[0]
    wrapper.body=copy.deepcopy(setter.body)
    namespace=dict(vars(worker))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),'actual-duration-setter','exec'),namespace)
    return namespace['run']


class Fast5SliderBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime=await async_playwright().start()
        self.browser=await self.runtime.chromium.launch(headless=True)
        self.page=await self.browser.new_page()
        await self.page.route('**/*',lambda route:route.abort())

    async def asyncTearDown(self):
        await self.browser.close();await self.runtime.stop()

    async def fixture(self,kind='accessible'):
        slider=('<span role="slider" tabindex="0" aria-label="Duration" aria-valuemin="4" aria-valuemax="10" aria-valuenow="10" style="display:block;width:320px;height:24px"></span>'
                if kind in ('accessible','indexed') else '<input aria-label="Duration" type="range" min="4" max="10" step="1" value="10">')
        if kind=='indexed':slider=slider.replace('aria-valuemin="4"','aria-valuemin="0"').replace('aria-valuemax="10"','aria-valuemax="6"').replace('aria-valuenow="10"','aria-valuenow="6"').replace('role="slider"','role="slider" data-seconds-offset="4"')
        await self.page.set_content('''<section style="width:640px"><div contenteditable="true" style="height:60px"></div>
        <button data-input-engine-actionbar-control-key="video-model">Model Dreamina Seedance 2.0 Fast</button>
        <button id="params" data-input-engine-actionbar-control-key="video-duration">16:9 · 10s</button></section>
        <div role="dialog">'''+slider+'''</div><script>
        const slider=document.querySelector('[role=slider],input[type=range]');
        function reflect(value){document.querySelector('#params').textContent='16:9 · '+value+'s';}
        slider.addEventListener('input',()=>reflect(slider.value));
        slider.addEventListener('keydown',event=>{
          if(!slider.matches('[role=slider]'))return;
          let value=Number(slider.getAttribute('aria-valuenow'));
          const min=Number(slider.getAttribute('aria-valuemin')),max=Number(slider.getAttribute('aria-valuemax'));
          if(event.key==='Home')value=min;else if(event.key==='End')value=max;
          else if(event.key==='ArrowRight')value=Math.min(max,value+1);
          else if(event.key==='ArrowLeft')value=Math.max(min,value-1);else return;
          event.preventDefault();slider.setAttribute('aria-valuenow',value);reflect(value+Number(slider.dataset.secondsOffset||0));
        });</script>''')

    async def test_accessible_slider_sets_five_before_selection_guard(self):
        await self.fixture()
        result=await actual_slider_setter()(self.page,5)
        self.assertIsInstance(result,dict,'Current Dola role=slider is ignored; composer stays at 10s')
        self.assertEqual(result['applied'],5)
        await worker._verify_video_selection(self.page,'seedance-2.0',5,'16:9')

    async def test_native_range_still_sets_five_and_ten(self):
        for seconds in (5,10):
            await self.fixture('native')
            await actual_slider_setter()(self.page,seconds)
            await worker._verify_video_selection(self.page,'seedance-2.0',seconds,'16:9')

    async def test_indexed_zero_to_six_means_four_to_ten_seconds(self):
        await self.fixture('indexed')
        result=await actual_slider_setter()(self.page,5)
        self.assertEqual(result['applied'],5)
        self.assertEqual(await self.page.locator('#params').inner_text(),'16:9 · 5s')
        await worker._verify_video_selection(self.page,'seedance-2.0',5,'16:9')

    async def test_ambiguous_visible_sliders_do_not_modify_either_control(self):
        await self.fixture()
        await self.page.evaluate("document.querySelector('[role=dialog]').append(document.querySelector('[role=slider]').cloneNode(true))")
        result=await actual_slider_setter()(self.page,5)
        self.assertIsNone(result)
        self.assertEqual(await self.page.locator('#params').inner_text(),'16:9 · 10s')

    async def test_ignored_keys_cannot_fake_a_five_second_readback(self):
        await self.fixture()
        await self.page.evaluate("const old=document.querySelector('[role=slider]');old.replaceWith(old.cloneNode(true))")
        self.assertIsNone(await actual_slider_setter()(self.page,5))
        self.assertEqual(await self.page.locator('#params').inner_text(),'16:9 · 10s')


if __name__=='__main__':unittest.main()
