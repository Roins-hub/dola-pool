"""技能按稳定身份解析改名；名称匹配兼容界面变动，仍校验选中结果。"""
from types import SimpleNamespace

import pytest

from test_task_deadline import _run
from test_work_session import SkillPage, session, Target, PlaywrightLike
from work_reply import video_skill_labels, video_skill_reference


def listing(label="Movie Studio"):
    return {'code': 0, 'data': {'featured': [
        {'name': 'marketing-video-analysis', 'display_name': 'Creative video analysis'},
        {'name': 'dola-creative-video', 'display_name': label}]}}


def test_stable_identity_resolves_a_completely_renamed_video_skill():
    assert video_skill_labels(listing()) == ['Movie Studio']
    assert video_skill_labels({'skills': [{'skill_name': 'dola-creative-video', 'display_name': '视频制作'}]}) == ['视频制作']
    assert video_skill_labels({'name': 'marketing-video-analysis', 'display_name': 'Creative Videos'}) == []


def test_doubao_skill_reference_comes_from_the_accounts_authoritative_listing():
    import sites
    data = {'skills': [{'name': 'video-analysis', 'display_name': '创意视频', 'external_skill_id': 'wrong', 'type': 2},
                       {'name': 'doubao-creative-video', 'display_name': '创意视频新版', 'external_skill_id': 'official-id', 'type': 2}]}
    reference = video_skill_reference(data, sites.DOUBAO)
    assert reference['id'] == reference['skillId'] == 'doubao-creative-video'
    assert reference['external_skill_id'] == 'official-id' and reference['label'] == '创意视频新版'
    assert video_skill_reference({'name': 'doubao-creative-video', 'display_name': '创意视频'}, sites.DOUBAO) is None
    assert video_skill_reference(data, sites.DOLA) is None


def test_doubao_attaches_the_official_skill_through_the_live_editor(monkeypatch):
    import sites
    page = SkillPage('创意视频')
    agent, item = session(monkeypatch, page)
    item.site = sites.DOUBAO
    item.skill_labels = ['创意视频']
    item.skill_reference = video_skill_reference({'name': 'doubao-creative-video', 'display_name': '创意视频',
                                                'external_skill_id': 'official-id', 'type': 2}, sites.DOUBAO)
    original = page.evaluate
    references = []
    async def evaluate(script, args):
        if script == agent.SELECT_SKILL_JS:
            references.append(args['reference'])
            page.selected = args['reference']['label']
            return True
        return await original(script, args)
    page.evaluate = evaluate
    page.get_by_text = lambda *args, **kwargs: pytest.fail('broad text matching must not select a Doubao skill')
    _run(item.choose_skill())
    assert references == [item.skill_reference] and _run(item.skill_selected())
    assert not page.keys and not item.sent


@pytest.mark.parametrize('code,success', [(0, True), (710022002, False)])
def test_doubao_work_workspace_reads_its_skill_list_without_a_menu_button(monkeypatch, code, success):
    import json
    import sites
    page = SkillPage('创意视频新版')
    agent, item = session(monkeypatch, page)
    item.site = sites.DOUBAO
    item.base = 'https://www.doubao.com'
    item.page_query = 'https://www.doubao.com/chat/?device_id=1&aid=497858'
    original = page.evaluate
    queries = []
    listing = {'code':code,'data':{'skills':[{'name':'doubao-creative-video','display_name':'创意视频新版',
               'external_skill_id':'official-id','type':2}]}}

    async def evaluate(script, args):
        if script == agent.SKILLS_JS:
            queries.append(args['query'])
            return {'status':200,'text':json.dumps(listing)}
        if script == agent.SELECT_SKILL_JS:
            assert success
            page.selected = args['reference']['label']
            return True
        return await original(script, args)

    async def allowed():
        pass

    page.evaluate = evaluate
    page.locator = lambda selector: pytest.fail('this workspace has no skill-menu button')
    item.raise_if_blocked = allowed
    if success:
        _run(item.choose_skill())
        assert _run(item.skill_selected())
    else:
        with pytest.raises(agent.PreSubmissionParametersError, match='无法加载账号的视频技能引用'):
            _run(item.choose_skill())
    assert len(queries) == 1 and 'aid=497858' in queries[0]
    assert not item.sent and not page.typed


def test_renamed_label_is_used_for_both_filter_and_selection(monkeypatch):
    page = SkillPage('Movie Studio')
    _, item = session(monkeypatch, page)
    item.skill_labels = video_skill_labels(listing())
    _run(item.choose_skill())
    assert page.keys == '/Movie Studio' and page.selected == 'Movie Studio'
    assert _run(item.skill_selected())


def test_late_metadata_replaces_an_outdated_search(monkeypatch):
    page = SkillPage('Movie Studio')
    _, item = session(monkeypatch, page)
    async def loaded(ms):
        if ms == 1000:
            item.skill_labels = video_skill_labels(listing())
    page.wait_for_timeout = loaded
    _run(item.choose_skill())
    assert page.keys == '/Movie Studio' and page.selected == 'Movie Studio'


def test_stable_identity_takes_precedence_over_similar_titles(monkeypatch):
    page = SkillPage('Creative Videos')
    agent, item = session(monkeypatch, page)
    item.skill_labels = ['Movie Studio']
    with pytest.raises(agent.PreSubmissionParametersError, match='尚未发送'):
        _run(item.choose_skill())
    assert page.clicked == []


@pytest.mark.parametrize('url,status,code,expected,reads', [
    ('https://www.dola.com/alice/office/skills/list_user_and_featured', 200, 0, ['Movie Studio'], 1),
    ('https://www.dola.com/alice/office/skills/list_user_and_featured', 200, 710022002, [], 1),
    ('https://www.dola.com/alice/office/skills/list_user_and_featured', 403, 0, [], 0),
    ('https://www.dola.com/chat/completion', 200, 0, [], 0),
    ('https://unrelated.invalid/alice/office/skills/list_user_and_featured', 200, 0, [], 0),
])
def test_only_successful_dola_skill_listing_updates_names(monkeypatch, url, status, code, expected, reads):
    _, item = session(monkeypatch, SkillPage())
    received = []
    async def body():
        received.append(True)
        return {**listing(), 'code': code}
    _run(item.read_skill_response(SimpleNamespace(url=url, status=status, json=body)))
    assert item.skill_labels == expected and len(received) == reads


def test_malformed_listing_does_not_interrupt_the_browser(monkeypatch):
    _, item = session(monkeypatch, SkillPage())
    async def body():
        raise ValueError('invalid JSON')
    _run(item.read_skill_response(SimpleNamespace(
        url='https://www.dola.com/alice/office/skills/list_user_and_featured', status=200, json=body)))
    assert item.skill_labels == []


def test_menu_click_timeout_retries_only_the_presubmission_selection(monkeypatch):
    page=SkillPage('创意视频')
    _,item=session(monkeypatch,page)
    item.skill_labels=['创意视频']
    original=page.get_by_text
    clicks=[]
    def get(pattern):
        target=original(pattern)
        click=target.click
        async def attempt(**kwargs):
            clicks.append('click')
            if len(clicks)==1:
                raise PlaywrightLike('Locator.click: Timeout 5000ms exceeded.')
            await click(**kwargs)
        target.click=attempt
        return target
    page.get_by_text=get
    _run(item.choose_skill())
    assert clicks==['click','click'] and page.selected=='创意视频'
    assert item.sent==[]


def test_hidden_duplicate_label_does_not_capture_skill_click(monkeypatch):
    page=SkillPage('创意视频')
    _,item=session(monkeypatch,page)
    item.skill_labels=['创意视频']
    original=page.get_by_text
    def get(pattern):
        visible=original(pattern)
        hidden=Target()
        async def count():return 2 if '创意视频' in page.keys else 0
        async def no():return False
        async def wrong(**kwargs):pytest.fail('hidden duplicate was clicked')
        hidden.count,hidden.is_visible,hidden.click=count,no,wrong
        hidden.nth=lambda index:visible
        return hidden
    page.get_by_text=get
    _run(item.choose_skill())
    assert page.selected=='创意视频' and item.sent==[]


def test_composer_search_highlight_does_not_capture_skill_click(monkeypatch):
    page = SkillPage('创意视频')
    _, item = session(monkeypatch, page)
    item.skill_labels = ['创意视频']
    original = page.get_by_text

    def get(pattern):
        menu = original(pattern)
        highlight = Target()
        async def count():
            return 2 if '创意视频' in page.keys else 0
        async def inside_editor(script):
            return True
        async def wrong(**kwargs):
            pytest.fail('composer search text was clicked instead of the skill menu')
        highlight.count = count
        highlight.evaluate = inside_editor
        highlight.click = wrong
        highlight.nth = lambda index: menu
        return highlight

    page.get_by_text = get
    _run(item.choose_skill())
    assert page.selected == '创意视频' and item.sent == []


def test_already_selected_skill_is_kept_without_opening_the_menu(monkeypatch):
    page=SkillPage('创意视频')
    page.selected='创意视频'
    _,item=session(monkeypatch,page)
    item.skill_labels=['创意视频']
    _run(item.choose_skill())
    assert not page.keys and not page.clicked


def test_response_listener_loads_the_live_skill_label(monkeypatch):
    page, listeners = SkillPage('Movie Studio'), []
    page.on = lambda event, callback: listeners.append(callback) if event == 'response' else None
    _, item = session(monkeypatch, page)
    item.watch()
    async def body():
        return listing()
    response = SimpleNamespace(url='https://www.dola.com/alice/office/skills/list_user_and_featured', status=200, json=body)
    async def received():
        for listener in listeners:
            await listener(response)
        await item.choose_skill()
    _run(received())
    assert page.selected == 'Movie Studio'


def test_late_promotion_is_dismissed_before_retrying_work_tab(monkeypatch):
    page = SkillPage()
    agent, item = session(monkeypatch, page)
    target, events = Target(), []
    async def click(**kwargs):
        events.append('click')
        if len(events) == 1:
            raise PlaywrightLike('promotion intercepts pointer events')
    async def dismiss(page):
        events.append('dismiss')
    async def allowed():
        pass
    target.click = click
    page.get_by_text = lambda pattern: target
    monkeypatch.setattr(agent, 'dismiss_popups', dismiss)
    item.raise_if_blocked = allowed
    _run(item.enter_work_mode())
    assert events == ['click', 'dismiss', 'click']
