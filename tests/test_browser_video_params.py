import asyncio
import re

import pytest

from browser_video_params import (PARAMS_TRIGGER, _select_slider, duration_labels,
                                  select_video_parameters, summary_duration)
from generation_errors import PreSubmissionParametersError


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Control:
    def __init__(self, page, kind, count=1, name=None):
        self.page, self.kind, self.number, self.name = page, kind, count, name

    async def count(self): return self.number
    async def wait_for(self, **kwargs): pass

    async def get_attribute(self, name):
        if name == 'aria-expanded': return 'true' if self.page.open else 'false'
        if name == 'data-creation-params-panel-id':
            return 'other' if self.kind == 'menu' and self.page.wrong_panel else 'panel1'
        return None

    async def inner_text(self):
        if self.kind == 'section':
            return f'長さ\n{self.page.seconds}秒\n4秒\n10秒'
        if self.kind == 'legacy': return f'{self.page.seconds}s'
        return f'{self.page.ratio} · {10 if self.page.bad_summary else self.page.seconds}秒'

    def locator(self, selector):
        assert selector == 'xpath=ancestor::section[1]'
        return Control(self.page, 'section')

    def get_by_role(self, role, name=None, exact=False):
        if role == 'slider': return Control(self.page, 'slider')
        assert role == 'button' and exact
        return Control(self.page, 'ratio', name=name,
                       count=1 if name in ['16:9', '9:16', '1:1'] else 0)

    def filter(self, **kwargs): return self
    first = property(lambda self: self)

    async def click(self, **kwargs):
        if self.kind == 'trigger': self.page.open = True
        elif self.kind == 'ratio': self.page.ratio = self.name
        elif self.kind == 'legacy': self.page.open = True
        elif self.kind == 'legacy_target': self.page.seconds = self.name; self.page.open = False

    async def press(self, key, **kwargs):
        self.page.keys.append(key)
        if key == 'Home': self.page.seconds = 4
        elif key == 'ArrowRight' and not self.page.stuck:
            self.page.seconds = min(10, self.page.seconds + 1)
        else:
            assert key == 'ArrowRight'


class Page:
    def __init__(self, modern=True):
        self.modern, self.open = modern, False
        self.seconds, self.ratio = 10, '自動'
        self.bad_summary = self.stuck = self.wrong_panel = False
        self.keys = []
        self.keyboard = self

    def locator(self, selector):
        if selector == PARAMS_TRIGGER + ':visible':
            return Control(self, 'trigger', count=1 if self.modern else 0)
        assert selector == '[role="menu"][data-creation-params-panel-id]:visible'
        return Control(self, 'menu')

    def get_by_text(self, pattern, **kwargs):
        assert isinstance(pattern, re.Pattern)
        if self.open:
            target = next(d for d in (5, 10, 15, 30) if pattern.fullmatch(f'{d}s'))
            return Control(self, 'legacy_target', name=target)
        return Control(self, 'legacy')

    async def wait_for_timeout(self, ms): await asyncio.sleep(0)
    async def press(self, key):
        assert key == 'Escape'
        self.keys.append(key); self.open = False


def test_seconds_are_read_from_visible_labels_not_slider_indices():
    assert duration_labels('長さ\n10秒\n4秒\n10秒') == [10, 4, 10]
    assert summary_duration('自動 · 5秒') == 5
    assert summary_duration('16:9 · 10 秒') == 10
    assert summary_duration('5s') == 5
    assert summary_duration('seedance 2.5') is None


@pytest.mark.parametrize('duration', [5, 10])
def test_modern_slider_and_ratio_confirmed_before_submission(duration):
    page = Page()
    run(select_video_parameters(page, duration, '9:16'))
    assert page.seconds == duration and page.ratio == '9:16' and not page.open
    assert 'Enter' not in page.keys
    if duration == 5:
        assert page.keys.count('Home') == 1 and page.keys.count('ArrowRight') == 1


def test_unsupported_duration_does_not_fall_back_to_ten_seconds():
    page = Page()
    with pytest.raises(PreSubmissionParametersError, match='4～10'):
        run(select_video_parameters(page, 15))
    assert not page.open and page.seconds == 10
    assert 'Enter' not in page.keys


def test_mismatched_summary_does_not_claim_selection_success():
    page = Page(); page.bad_summary = True
    with pytest.raises(PreSubmissionParametersError, match='回读不符'):
        run(select_video_parameters(page, 5, '16:9'))


def test_stuck_slider_and_wrong_menu_are_not_accepted():
    page = Page(); page.stuck = True
    with pytest.raises(PreSubmissionParametersError, match='未能选中'):
        run(select_video_parameters(page, 5))
    assert not page.open
    page = Page(); page.wrong_panel = True
    with pytest.raises(PreSubmissionParametersError, match='不匹配'):
        run(select_video_parameters(page, 5))


def test_legacy_exact_duration_is_still_supported():
    page = Page(modern=False)
    run(select_video_parameters(page, 5))
    assert page.seconds == 5 and not page.open


def test_missing_ratio_option_is_safe_failure():
    page = Page()
    with pytest.raises(PreSubmissionParametersError, match='没有可选比例'):
        run(select_video_parameters(page, 5, '2:1'))
    assert not page.open


def test_generic_parameter_failure_names_step_without_browser_text():
    class Missing:
        async def count(self): return 0
        async def wait_for(self, **kw): raise TimeoutError("https://signed.invalid/?token=SECRET")
        def filter(self, **kw): return self
        first = property(lambda self: self)
        async def click(self, **kw): raise TimeoutError("https://signed.invalid/?token=SECRET")
    class Page:
        def locator(self, selector): return Missing()
        def get_by_text(self, *a, **kw): return Missing()
        async def wait_for_timeout(self, ms): pass
    with pytest.raises(PreSubmissionParametersError) as error:
        run(select_video_parameters(Page(), 10, '16:9'))
    assert '旧版控件' in str(error.value) and 'TimeoutError' in str(error.value)
    assert 'SECRET' not in str(error.value)


def test_video_mode_requires_model_or_params_control():
    from browser_video_params import video_mode_ready
    class Loc:
        def __init__(self, n): self.n = n
        async def count(self): return self.n
        def filter(self, **kw): return self
    class Page:
        def __init__(self, trigger, model): self.t, self.m = trigger, model
        def locator(self, selector): return Loc(self.t)
        def get_by_text(self, *a, **kw): return Loc(self.m)
        async def wait_for_timeout(self, ms): pass
    assert run(video_mode_ready(Page(1, 0), 500))
    assert run(video_mode_ready(Page(0, 1), 500))
    assert not run(video_mode_ready(Page(0, 0), 500))
