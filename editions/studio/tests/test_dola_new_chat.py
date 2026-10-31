"""Replay the restored-chat/history-title failure without submitting video."""
import unittest
import ast
from pathlib import Path
from unittest.mock import AsyncMock, patch
import video_worker_ui as worker


def production_find_entry(page):
    tree=ast.parse(Path('video_worker_ui.py').read_text(encoding='utf-8'))
    node=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_find_entry')
    namespace=dict(vars(worker))
    namespace.update(page=page,_handle_birthday_modal=AsyncMock(),_dismiss_cookie_banner=AsyncMock(),
                     _dismiss_login_modal=AsyncMock(),_page_unavailable_reason=AsyncMock(return_value=None))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),
                 'production-entry-selection','exec'),namespace)
    return namespace['_find_entry']


class Element:
    def __init__(self, box, *, value='', click=None):
        self.box, self.value = box, value
        self.click = AsyncMock(side_effect=click)
    async def is_visible(self): return True
    async def bounding_box(self): return self.box
    async def input_value(self): return self.value
    async def inner_text(self): return self.value


class Elements:
    def __init__(self, elements): self.elements = elements
    async def count(self): return len(self.elements)
    def nth(self, index): return self.elements[index]


class DolaNewChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_only_rejects_any_prompt_or_reference_before_browser_launch(self):
        with patch.object(worker,'launch_account_context',new_callable=AsyncMock) as launch:
            for prompt,refs in [('must not send',None),('', ['ref.png'])]:
                with self.assertRaisesRegex(ValueError,'空提示词'):
                    await worker.generate_video('fixture',prompt,duration=10,
                                                reference_image_paths=refs,prepare_only=True)
            launch.assert_not_awaited()

    def test_new_chat_checks_precede_initial_entry_and_both_reload_recoveries(self):
        tree=ast.parse(Path('video_worker_ui.py').read_text(encoding='utf-8'))
        generate=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='generate_video')
        calls=[n for n in ast.walk(generate) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)]
        fresh=sorted(n.lineno for n in calls if n.func.id=='_ensure_new_chat')
        entry=sorted(n.lineno for n in calls if n.func.id=='_find_entry')
        self.assertEqual(len(fresh),3)
        self.assertEqual(len(entry),3)
        for fresh_line,entry_line in zip(fresh,entry):self.assertLess(fresh_line,entry_line)
        prepare=next(n for n in ast.walk(generate) if isinstance(n,ast.If) and isinstance(n.test,ast.Name)
                     and n.test.id=='prepare_only' and any(isinstance(x,ast.Return) for x in n.body))
        fill=next(n.lineno for n in calls if n.func.id=='_fill_video_prompt')
        self.assertLess(prepare.lineno,fill,'Diagnostic return must occur before prompt input')

    async def test_video_history_title_below_toolbar_is_never_chosen(self):
        editor=Element(dict(x=320,y=650,width=1000,height=48))
        history=Element(dict(x=48,y=732,width=150,height=28))
        toolbar=Element(dict(x=480,y=712,width=104,height=30))
        page=type('Page',(),{})()
        page.locator=lambda selector:Elements([editor])
        page.get_by_text=lambda *args,**kwargs:Elements([history,toolbar])
        self.assertIs(await production_find_entry(page)(max_seconds=.05),toolbar)
        history.click.assert_not_awaited()

    async def test_no_composer_entry_does_not_fall_back_to_history(self):
        editor=Element(dict(x=320,y=650,width=1000,height=48))
        history=Element(dict(x=48,y=732,width=150,height=28))
        page=type('Page',(),{})()
        page.locator=lambda selector:Elements([editor])
        page.get_by_text=lambda *args,**kwargs:Elements([history])
        self.assertIsNone(await worker._find_composer_video_entry(page))

    async def test_old_numeric_chat_must_be_left_before_preparing_video(self):
        page=type('Page',(),{})()
        page.url='https://www.dola.com/chat/123456789'
        page.wait_for_timeout=AsyncMock()
        editor=Element(dict(x=320,y=650,width=1000,height=48))
        async def new_chat(**kwargs):page.url='https://www.dola.com/chat/local_123'
        navigation=Element(dict(x=40,y=60,width=150,height=28),click=new_chat)
        history=Element(dict(x=40,y=400,width=150,height=28))
        welcome=Element(dict(x=400,y=300,width=300,height=50))
        page.locator=lambda selector:Elements([editor])
        page.get_by_text=lambda pattern,**kwargs:Elements([navigation,history] if pattern is worker.NEW_CHAT_RE else ([welcome] if '/local_' in page.url else []))
        await worker._ensure_new_chat(page,timeout=.1)
        navigation.click.assert_awaited_once()
        history.click.assert_not_awaited()

    async def test_unconfirmed_new_chat_stops_before_any_prompt(self):
        from navigation_errors import BrowserSetupError
        page=type('Page',(),{})()
        page.url='https://www.dola.com/chat/123456789'
        page.wait_for_timeout=AsyncMock()
        editor=Element(dict(x=320,y=650,width=1000,height=48))
        nav=Element(dict(x=40,y=60,width=150,height=28))
        page.locator=lambda selector:Elements([editor])
        page.get_by_text=lambda pattern,**kwargs:Elements([nav] if pattern is worker.NEW_CHAT_RE else [])
        with self.assertRaisesRegex(BrowserSetupError,'新会话'):
            await worker._ensure_new_chat(page,timeout=.02)
        nav.click.assert_awaited_once()


if __name__=='__main__':unittest.main()
