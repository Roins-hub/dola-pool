import asyncio

import pytest

import browser_video_params
import video_worker_ui
from generation_errors import PreSubmissionParameterTimeout


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Page:
    def __init__(self):
        self.navigations = []

    async def goto(self, url, **kwargs):
        self.navigations.append(url)

    async def wait_for_timeout(self, _):
        return None


def setup_recovery(monkeypatch, parameter_selector):
    page = Page()
    models = []
    entered = []
    uploads = []

    async def select_model(_page, account, model):
        models.append((account, model))

    async def enter_mode(_page, account):
        entered.append(account)

    async def dismiss(_page):
        return None

    async def logged_out(_page):
        return False

    async def upload(_page, account, paths, _on_poll):
        uploads.append((account, paths))

    monkeypatch.setattr(video_worker_ui, '_select_video_model', select_model)
    monkeypatch.setattr(video_worker_ui, 'enter_video_mode', enter_mode)
    monkeypatch.setattr(video_worker_ui, 'dismiss_popups', dismiss)
    monkeypatch.setattr(video_worker_ui, 'is_logged_out', logged_out)
    monkeypatch.setattr(video_worker_ui, 'upload_with_retry', upload)
    monkeypatch.setattr(video_worker_ui, 'raise_if_region_restricted', lambda *_: None)
    monkeypatch.setattr(browser_video_params, 'select_video_parameters', parameter_selector)
    return page, models, entered, uploads


def test_parameter_timeout_refreshes_and_retries_same_account_before_submit(monkeypatch):
    parameter_calls = []

    async def select_parameters(_page, duration, ratio):
        parameter_calls.append((duration, ratio))
        if len(parameter_calls) == 1:
            raise PreSubmissionParameterTimeout('菜单超时')

    page, models, entered, uploads = setup_recovery(monkeypatch, select_parameters)
    run(video_worker_ui.confirm_video_parameters_with_retry(
        page, 'acc3', 'seedance_v2.0', 5, '16:9', reference_image_paths=['ref.png']))

    assert page.navigations == ['https://www.dola.com/chat']
    assert parameter_calls == [(5, '16:9'), (5, '16:9')]
    assert models == [('acc3', 'seedance_v2.0'), ('acc3', 'seedance_v2.0')]
    assert entered == ['acc3']
    assert uploads == [('acc3', ['ref.png'])]


def test_parameter_timeout_stops_after_one_same_account_recovery(monkeypatch):
    async def select_parameters(_page, _duration, _ratio):
        raise PreSubmissionParameterTimeout('菜单超时')

    page, models, _, uploads = setup_recovery(monkeypatch, select_parameters)
    with pytest.raises(PreSubmissionParameterTimeout, match='刷新后重试仍超时'):
        run(video_worker_ui.confirm_video_parameters_with_retry(
            page, 'acc3', 'seedance_v2.0', 5, '16:9'))

    assert len(page.navigations) == 1
    assert models == [('acc3', 'seedance_v2.0'), ('acc3', 'seedance_v2.0')]
    assert uploads == []
