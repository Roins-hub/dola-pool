"""显式工作模式：站点隔离、原生时长、固定账号和只读恢复。"""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import sites
import work_reply as wr
from test_account_throttle import _row
from test_task_deadline import _run
from test_work_mode import _agent, _work_pool
from test_work_followups import work_server
from test_work_session import Page, Target, load_agent, bot


@pytest.mark.parametrize("model,seconds,valid", [
    ("seedance-2.0-fast", 30, True), ("seedance-2.0-fast", 15, True),
    ("seedance-2.0-fast", 5, True), ("seedance-2.5", 15, False),
    ("doubao-seedance-2.0-fast", 15, True), ("doubao-seedance-2.0-mini", 4, True),
    ("doubao-seedance-2.0-fast", 30, False), ("doubao-seedance-2.5", 30, True),
])
def test_explicit_mode_does_not_depend_on_dola_15s_switch(monkeypatch, model, seconds, valid):
    monkeypatch.setattr(config, "WORK_15S_ENABLED", False)
    assert wr.uses_work_mode(model, seconds, "work") is valid
    assert wr.is_work_request(model, seconds, "work") is valid
    assert not wr.uses_work_pair(model, seconds, "work")


def test_native_thirty_prompt_never_inserts_fifteen_seconds():
    text = wr.build_prompt("白鹭飞过湖面", "16:9", 30, "seedance-2.0-fast")
    assert "30s" in text and "原生 30 秒" in text and "15" not in text
    assert "Seedance 2.0 Fast" in text and "不拆段" in text and "免费额度" in text


@pytest.mark.parametrize('allow_paid', [True, False])
def test_doubao_work_prompt_honors_existing_membership_policy(monkeypatch, allow_paid):
    monkeypatch.setattr(config, 'DOUBAO_ALLOW_PAID', allow_paid)
    text = wr.build_prompt('白鹭飞过湖面', '16:9', 15, 'doubao-seedance-2.5')
    assert ('免费或会员生成权益' in text) is allow_paid
    assert ('仅使用当前免费额度' in text) is not allow_paid
    assert 'Seedance 2.5' in text and '原生 15 秒' in text
    assert all(rule in text for rule in ('不购买', '不充值', '不升级套餐'))
    dola = wr.build_prompt('白鹭飞过湖面', '16:9', 15, 'seedance-2.0-fast')
    assert '仅使用当前免费额度' in dola and '会员生成权益' not in dola


def test_skills_and_cookies_are_selected_for_the_correct_site():
    data = [{"name": "dola-creative-video", "display_name": "Creative Videos"},
            {"name": "doubao-creative-video", "display_name": "创意视频"},
            {"name": "unrelated-video", "display_name": "视频智能处理"}]
    assert wr.video_skill_labels(data, sites.DOUBAO) == ["创意视频"]
    assert wr.video_skill_labels(data) == ["Creative Videos"]
    cookies = [{"domain": ".doubao.com", "name": "sessionid"},
               {"domain": ".dola.com", "name": "sessionid"},
               {"domain": ".notdoubao.com", "name": "sessionid"}]
    assert wr.session_cookies(cookies, sites.DOUBAO) == cookies[:1]


def test_doubao_uses_its_own_page_query_and_requested_ai_parameters(monkeypatch):
    agent = load_agent(monkeypatch)
    page = Page([])
    item = agent._WorkSession("db", page, None, duration=10, model="doubao-seedance-2.0-fast")
    assert item.base == "https://www.doubao.com"
    assert item.responder.context["duration"] == 10 and item.responder.context["site"] == "豆包"
    calls = {}
    page.on = lambda name, callback: calls.setdefault(name, []).append(callback)
    item.watch()
    calls["request"][0](SimpleNamespace(url="https://www.dola.com/x?device_id=wrong&aid=2"))
    assert not item.page_query
    calls["request"][0](SimpleNamespace(url="https://www.doubao.com/x?device_id=1&aid=2"))
    assert item.page_query.startswith(item.base)


@pytest.mark.parametrize('label,logged_out', [('登录', True), ('登录 / 注册', True), ('登录/注册', True),
                                           ('登录 ／ 注册', True), ('工作', False)])
def test_doubao_login_selector_avoids_unescaped_regex_delimiter(monkeypatch,label,logged_out):
    agent = load_agent(monkeypatch)
    page = Page([])

    class Button:
        first = None

        def __init__(self,count):
            self.value=count
            self.first=self

        async def count(self):return self.value
        async def is_visible(self):return True

    def by_role(role,name):
        # CloakBrowser serializes the regex in a /.../ selector without escaping /.
        assert '/' not in name.pattern
        return Button(int(bool(name.fullmatch(label))))

    page.get_by_role=by_role
    item=agent._WorkSession('db',page,None,model='doubao-seedance-2.0-fast')
    assert _run(item.logged_out()) is logged_out


def test_doubao_enters_visible_work_button_instead_of_hidden_matching_text(monkeypatch):
    agent = load_agent(monkeypatch)
    page = Page([])
    calls = []

    class Control:
        first = None

        def __init__(self,count=1):self.first,self.value=self,count
        async def count(self):return self.value
        async def click(self,**kwargs):calls.append('clicked')
        async def wait_for(self,**kwargs):calls.append('ready')

    def by_role(role,name,exact):
        assert (role,name,exact)==('button','工作',True)
        return Control()

    page.get_by_role=by_role
    page.get_by_text=lambda *a,**k: pytest.fail('hidden text locator must not be used')
    page.locator=lambda selector:Control(0 if 'workspace-switch-chip' in selector else 1)
    item=agent._WorkSession('db',page,None,model='doubao-seedance-2.0-fast')
    async def allowed():pass
    item.raise_if_blocked=allowed
    _run(item.enter_work_mode())
    assert calls==['clicked','ready']


def test_doubao_work_workspace_is_already_in_work_mode_without_a_toggle(monkeypatch):
    agent=load_agent(monkeypatch)
    page=Page([])
    calls=[]

    class Control:
        first=None
        def __init__(self):self.first=self
        async def count(self):return 1
        async def inner_text(self):return '豆包\n工作'
        async def wait_for(self,**kwargs):calls.append('editor_ready')

    page.locator=lambda selector:Control()
    page.get_by_role=lambda *a,**k:pytest.fail('already in work workspace; no toggle exists')
    async def goto(url, **kwargs):
        calls.append('new_work_conversation')
        assert url == 'https://www.doubao.com/chat/new-thread'
        page.url = url
    async def hydrated(script, **kwargs):
        calls.append('editor_hydrated')
        assert 'box.editor' in script
    page.goto, page.wait_for_function = goto, hydrated
    item=agent._WorkSession('db',page,None,model='doubao-seedance-2.0-fast')
    async def allowed():calls.append('checked')
    item.raise_if_blocked=allowed
    _run(item.enter_work_mode())
    assert calls==['new_work_conversation','editor_ready','editor_hydrated','checked']


@pytest.mark.parametrize('ready_on_navigation,expected_navigations', [(1, 1), (2, 2), (None, 2)])
def test_work_page_reopens_once_when_the_editor_does_not_load(monkeypatch, ready_on_navigation, expected_navigations):
    agent = load_agent(monkeypatch)
    page = Page([])
    navigations, notes = [], []

    async def goto(url, **kwargs):
        navigations.append(url)

    class Editor:
        async def count(self):
            return int(ready_on_navigation is not None and len(navigations) >= ready_on_navigation)

    async def dismiss(page):
        pass

    page.goto = goto
    page.locator = lambda selector: Editor()
    monkeypatch.setattr(agent, 'dismiss_popups', dismiss)
    item = agent._WorkSession('db', page, lambda *args, **fields: notes.append(fields), model='doubao-seedance-2.0-fast')
    if ready_on_navigation is None:
        with pytest.raises(agent.PreSubmissionParametersError, match='输入框没有加载'):
            _run(item.open_chat())
    else:
        _run(item.open_chat())
    assert navigations == ['https://www.doubao.com/chat/'] * expected_navigations
    assert sum('重新打开页面' in note.get('progress', '') for note in notes) == int(expected_navigations == 2)
    assert item.sent == [] and page.typed == [] and not item.submit_clicked


@pytest.mark.parametrize('submitted', [False, True])
def test_doubao_replaces_skill_examples_only_before_submission(monkeypatch, submitted):
    from test_work_session import send_session
    agent, item, page = send_session(monkeypatch)
    item.site = sites.DOUBAO
    item.submit_clicked = submitted
    page.text = 'Creative video 使用 Seedance 2.5 生成：用参考海报制作动态宣传视频'
    original = page.evaluate
    edits = []

    async def evaluate(script, args):
        if script == agent.REPLACE_DRAFT_JS:
            edits.append(args['text'])
            page.text = 'Creative video ' + args['text']
            return True
        return await original(script, args)

    page.evaluate = evaluate
    if submitted:
        with pytest.raises(TimeoutError, match='保留原会话'):
            _run(item.fill_prompt('白鹭飞过湖面，15秒'))
        assert not edits and '宣传视频' in page.text
    else:
        _run(item.fill_prompt('白鹭飞过湖面，15秒'))
        assert edits == ['白鹭飞过湖面，15秒']
        assert _run(item.composer_text()) == '白鹭飞过湖面，15秒'
        assert _run(item.skill_selected()) and not item.sent


def test_doubao_work_generation_can_deliver_a_native_high_bitrate_link(monkeypatch):
    import video_link
    agent = load_agent(monkeypatch)
    item = agent._WorkSession('db', Page([]), None, model='doubao-seedance-2.5', duration=15)
    async def direct(model, account, conversation_id):
        return {'delivery':'link','video_url':'https://cdn/high.mp4','local_path':'','video_model':model,
                'video_duration':15.07,'video_size':19390720,'account':account,'conversation_id':conversation_id}
    async def unexpected(*args, **kwargs):
        pytest.fail('direct delivery must not download the low-bitrate source')
    monkeypatch.setattr(video_link, 'from_model', direct)
    monkeypatch.setattr(agent, '_download', unexpected)
    result = _run(item.deliver('{"video_id":"v1","video_duration":15.073}', 'original-conv'))
    assert result['delivery'] == 'link' and result['account'] == 'db'
    assert result['conversation_id'] == 'original-conv' and not result.get('duration_error')


@pytest.mark.parametrize("broken_attempts", [1, 2])
def test_doubao_unavailable_work_page_reopens_once_and_reports_startup_failure(monkeypatch, broken_attempts):
    agent = load_agent(monkeypatch)
    class StartupPage(Page):
        def __init__(self):
            super().__init__([])
            self.navigations, self.waits = 0, []
        async def goto(self, *args, **kwargs):
            self.navigations += 1
        def get_by_text(self, text, exact=False):
            assert text == "该页面暂时不可用" and exact
            return Target(int(self.navigations <= broken_attempts))
        def locator(self, selector):
            return Target(int(self.navigations > broken_attempts))
        async def wait_for_timeout(self, ms):
            self.waits.append(ms)
    page = StartupPage()
    item = agent._WorkSession("db", page, None, model="doubao-seedance-2.0-fast")
    async def logged_out():
        return False
    async def dismiss(page):
        pass
    item.logged_out = logged_out
    monkeypatch.setattr(agent, "dismiss_popups", dismiss)
    if broken_attempts == 2:
        from generation_errors import PreSubmissionParametersError
        with pytest.raises(PreSubmissionParametersError, match="初始化超时或异常"):
            _run(item.open_chat())
    else:
        _run(item.open_chat())
    assert page.navigations == 2 and len(page.waits) <= 1 and not page.typed


@pytest.mark.parametrize("started", [False, True])
def test_work_reference_failure_stops_confirmations_or_flags_the_existing_video(monkeypatch, started):
    from generation_errors import UpstreamReplyError
    agent = load_agent(monkeypatch)
    item = agent._WorkSession("db", Page([]), None, attachments=1, ratio="16:9",
                              model="doubao-seedance-2.0-fast")
    messages = bot("参考图资源当前无法读取，我先按文字设定直接生成。", tool=started)["messages"]
    if started:
        item.check_references(messages)
        assert item.reference_error
    else:
        with pytest.raises(UpstreamReplyError, match="参考图不可用") as caught:
            item.check_references(messages)
        assert not caught.value.allow_account_retry
    assert not item.page.typed


def test_work_existing_video_preserves_reference_and_ratio_validation_errors(monkeypatch):
    import video_link
    agent = load_agent(monkeypatch)
    item = agent._WorkSession("db", Page([]), None, attachments=1, ratio="16:9",
                              model="doubao-seedance-2.0-fast")
    item.reference_error = "参考图资源无法读取"
    vm = json.dumps({"vid": "original", "video_duration": 15.1,
                     "video_list": {"high": {"vwidth": 960, "vheight": 720}}})
    async def direct(*args):
        return {"delivery": "link", "video_url": "https://cdn.invalid/original.mp4", "local_path": "",
                "video_duration": 15.1, "video_model": vm}
    monkeypatch.setattr(video_link, "from_model", direct)
    result = _run(item.deliver(vm, "original-conv"))
    assert "参考图未用于生成" in result["validation_error"] and "画幅" in result["validation_error"]
    assert result["delivery"] == "link" and not item.page.typed


def test_startup_resource_diagnostics_keep_only_hosts_and_timing(monkeypatch):
    agent = load_agent(monkeypatch)
    page = Page([])
    events = {}
    page.on = lambda event, handler: events.setdefault(event, []).append(handler)
    item = agent._WorkSession("db", page, None, model="doubao-seedance-2.0-fast")
    item.watch()
    events["requestfinished"][0](SimpleNamespace(resource_type="script", timing={"responseEnd": 31000},
        url="https://lf-doubao.example/assets/chat.js?token=secret"))
    events["requestfailed"][0](SimpleNamespace(resource_type="stylesheet", failure="net::ERR_TIMED_OUT",
        url="https://lf-doubao.example/assets/chat.css?signature=secret"))
    detail = " | ".join(item.startup_resources)
    assert "31.0 秒" in detail and "加载失败" in detail and "lf-doubao.example" in detail
    assert "secret" not in detail and "chat.js" not in detail and "chat.css" not in detail


def test_native_thirty_dispatch_uses_one_account_without_pair(tmp_path, monkeypatch):
    bp_module, pool = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    monkeypatch.setattr(bp_module, "ensure_web_playable", lambda p: p)
    calls = []

    async def generate(account, prompt, ratio, **kwargs):
        calls.append((account, kwargs["duration"], kwargs["model"]))
        output = tmp_path / (account + ".mp4")
        output.write_bytes(b"clip")
        return {"local_path": str(output), "account": account}

    _agent(monkeypatch, generate)
    result = _run(pool.generate_video("湖面", "16:9", 30, model="seedance-2.0-fast",
                                      execution_mode="work", max_attempts=1))
    assert calls == [(result["account"], 30, "seedance-2.0-fast")]
    assert not result.get("merge_mode")
    assert _row(pool, result["account"])["work_blocked_until"] > time.time()
    assert sum(_row(pool, name)["used_today"] for name in pool.accounts) == 0


def test_doubao_work_cookie_account_uses_browser_and_its_own_quota(tmp_path, monkeypatch):
    bp_module, pool = _work_pool(tmp_path, monkeypatch, ["db"])
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    state = Path(pool.accounts_dir) / "db" / "cookie_state.json"
    state.write_text(json.dumps({"cookies_list": [{"domain": ".doubao.com", "name": "sessionid", "value": "test"}]}), encoding="utf-8")
    pool._conn.execute("UPDATE accounts_meta SET source='cookie' WHERE name='db'")
    monkeypatch.setattr(bp_module, "ensure_web_playable", lambda p: p)
    calls = []

    async def generate(account, prompt, ratio, **kwargs):
        calls.append(kwargs)
        output = tmp_path / "doubao.mp4"
        output.write_bytes(b"clip")
        return {"local_path": str(output), "account": account}

    _agent(monkeypatch, generate)
    result = _run(pool.test_generate("db", "湖面", "16:9", 15, "doubao-seedance-2.0-fast", execution_mode="work"))
    assert result["ok"] and len(calls) == 1
    assert calls[0]["model"] == "doubao-seedance-2.0-fast"
    assert not _row(pool, "db")["work_blocked_until"]
    assert _row(pool, "db")["used_today"] > 0


def test_doubao_work_only_cookie_forces_work_mode_in_fixed_account_test(tmp_path, monkeypatch):
    bp_module, pool = _work_pool(tmp_path, monkeypatch, ["db"])
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    state = Path(pool.accounts_dir) / "db" / "cookie_state.json"
    state.write_text(json.dumps({"cookies_list": [
        {"domain": ".doubao.com", "name": "sessionid", "value": "test"},
        {"domain": ".doubao.com", "name": "has_biz_token", "value": "true"},
    ]}), encoding="utf-8")
    pool.set_source("db", "cookie")
    assert pool.execution_mode_for("db") == "work"
    assert next(row for row in pool.list_accounts() if row["name"] == "db")["doubao_account_type"] == "work"
    monkeypatch.setattr(bp_module, "ensure_web_playable", lambda p: p)
    calls = []

    async def generate(account, prompt, ratio, **kwargs):
        calls.append(kwargs)
        output = tmp_path / "work-only.mp4"
        output.write_bytes(b"clip")
        return {"local_path": str(output), "account": account}

    _agent(monkeypatch, generate)
    result = _run(pool.test_generate("db", "湖面", "16:9", 15,
                                     "doubao-seedance-2.0-fast", execution_mode="auto"))
    assert result["ok"] and calls[0]["model"] == "doubao-seedance-2.0-fast"


def test_personal_doubao_cookie_keeps_auto_mode(tmp_path, monkeypatch):
    bp_module, pool = _work_pool(tmp_path, monkeypatch, ["db"])
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    state = Path(pool.accounts_dir) / "db" / "cookie_state.json"
    state.write_text(json.dumps({"cookies_list": [
        {"domain": ".doubao.com", "name": "sessionid", "value": "test"},
        {"domain": ".doubao.com", "name": "has_biz_token", "value": "false"},
    ]}), encoding="utf-8")
    pool.set_source("db", "cookie")
    assert pool.execution_mode_for("db") == "auto"
    assert pool.execution_mode_for("db", "work") == "auto"
    monkeypatch.setattr(bp_module, "ensure_web_playable", lambda p: p)
    output = tmp_path / "personal.mp4"
    output.write_bytes(b"clip")
    work_calls = []

    async def work_generate(*args, **kwargs):
        work_calls.append((args, kwargs))
        return {"local_path": str(output)}

    _agent(monkeypatch, work_generate)
    import doubao_gen
    monkeypatch.setattr(doubao_gen, "generate_doubao_video", lambda **kwargs: {
        "local_path": str(output), "account": "db"})
    result = _run(pool.test_generate("db", "湖面", "16:9", 15,
                                     "doubao-seedance-2.0-fast", execution_mode="work"))
    assert result["ok"] and not work_calls


def test_short_work_result_preserves_video_and_claims_only_one_quota(tmp_path, monkeypatch):
    bp_module, pool = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    monkeypatch.setattr(bp_module, "ensure_web_playable", lambda p: p)
    output = tmp_path / "short.mp4"
    output.write_bytes(b"clip")
    calls = []

    async def generate(account, *args, **kwargs):
        calls.append(account)
        return {"account": account, "local_path": str(output), "video_duration": 15,
                "duration_error": "请求30秒，实际15秒"}

    _agent(monkeypatch, generate)
    result = _run(pool.test_generate("acc1", "湖面", "16:9", 30, "seedance-2.0-fast", execution_mode="work"))
    assert result["ok"] is False and result["video_url"].endswith("short.mp4")
    assert result["video_duration"] == 15 and output.is_file() and calls == ["acc1"]
    assert _row(pool, "acc1")["work_blocked_until"] > time.time()
    assert not _row(pool, "acc2")["work_blocked_until"]


def test_fixed_account_work_quota_refusal_saves_reopen_time_without_daily_charge_or_rotation(tmp_path, monkeypatch):
    _, pool = _work_pool(tmp_path, monkeypatch, ["acc1", "acc2"])
    from generation_errors import WorkQuotaError
    until = time.time() + 2 * 86400
    calls = []

    async def generate(account, *args, **kwargs):
        calls.append(account)
        raise WorkQuotaError("7日間の無料利用分の上限に到達しました。", until=until)

    _agent(monkeypatch, generate)
    result = _run(pool.test_generate("acc1", "湖面", "16:9", 15, "seedance-2.0-fast", execution_mode="work"))
    row = _row(pool, "acc1")
    assert result["ok"] is False and not result.get("recoverable")
    assert calls == ["acc1"] and row["work_blocked_until"] == until
    assert row["used_today"] == 0 and not row["quota_blocked"] and not row["rate_limited"]
    assert not pool._fail_streak.get("acc1") and not _row(pool, "acc2")["work_blocked_until"]


@pytest.mark.parametrize("model,seconds", [("seedance-2.0-fast", 30), ("doubao-seedance-2.0-fast", 15)])
def test_explicit_work_recovery_reads_original_without_pair_or_resubmission(tmp_path, monkeypatch, model, seconds):
    server, pool = work_server(tmp_path, monkeypatch, [{"local_path": str(tmp_path / "late.mp4")}])
    server.store.update("video_t1", model=model, duration=seconds, execution_mode="work")
    _run(server._recover_loop("video_t1"))
    row = server.store.get("video_t1")
    assert row["status"] == "completed" and row["recover_state"] == "recovered"
    assert not pool.merged and not pool.continued and pool.calls == 1
    assert server.store.get("video_t1")["execution_mode"] == "work"


def test_paid_or_wrong_duration_confirmation_stops_before_reply(monkeypatch):
    agent = load_agent(monkeypatch)
    item = agent._WorkSession("acc", Page([]), None, duration=30, model="seedance-2.0-fast")
    from generation_errors import UpstreamReplyError
    for text in ("请确认：时长：15秒，模型：Seedance 2.0 Fast", "是否继续使用付费额度？", "请确认升级会员后生成"):
        with pytest.raises(UpstreamReplyError) as caught:
            _run(item.next_answer(text))
        assert caught.value.allow_account_retry is False
    assert not item.page.typed


def test_no_matching_native_choice_stops_without_accepting_split(monkeypatch):
    agent = load_agent(monkeypatch)
    item = agent._WorkSession("acc", Page([]), None, duration=30, model="seedance-2.0-fast")
    from generation_errors import UpstreamReplyError
    with pytest.raises(UpstreamReplyError):
        _run(item.choice_labels([{"title": "请选择时长", "options": ["两段15秒", "单条15秒（推荐）"]}]))


def test_doubao_cookie_copy_never_opens_dola_profile(tmp_path, monkeypatch):
    agent = load_agent(monkeypatch)
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "accounts" / "db"
    root.mkdir(parents=True)
    (root / "cookie_state.json").write_text(json.dumps({"cookies_list": [
        {"domain": ".doubao.com", "name": "sessionid", "value": "test"},
        {"domain": ".dola.com", "name": "sessionid", "value": "other"}]}), encoding="utf-8")
    result = _run(agent._copied_session(None, "db"))
    assert len(result) == 1 and result[0]["domain"] == ".doubao.com"


def api_server(tmp_path, monkeypatch):
    import server
    from store import TaskStore
    monkeypatch.setattr(server, "store", TaskStore(str(tmp_path / "tasks.db")))
    monkeypatch.setattr(server, "pool", SimpleNamespace(available=True, accounts=["acc1"], site_of=lambda account: sites.DOLA))
    monkeypatch.setattr(config, "WORK_15S_ENABLED", False)
    monkeypatch.setattr(config, "LONG_VIDEO_MODE", "reject")
    client = {"allowed_durations": [5, 10, 15, 30], "api_key_hash": "test", "api_key_name": "test",
              "daily_limit": 0, "concurrency_limit": 0}
    monkeypatch.setattr(server, "_auth", lambda key: client)
    monkeypatch.setattr(server.asyncio, "create_task", lambda coro: coro.close())
    return server


def test_public_json_endpoint_accepts_and_persists_native_thirty_with_auto_switch_off(tmp_path, monkeypatch):
    server = api_server(tmp_path, monkeypatch)

    class Request:
        headers = {"content-type": "application/json"}

        async def json(self):
            return {"prompt": "湖面白鹭", "model": "seedance-2.0-fast", "duration": 30, "execution_mode": "work"}

    response = _run(server.create_video(Request(), "test"))
    row = server.store.get(response.id)
    assert row["execution_mode"] == "work" and row["duration"] == 30
    assert row["model"] == "seedance-2.0-fast" and row["status"] == "queued"


def test_ark_keeps_explicit_native_thirty_instead_of_rounding_to_normal_chat_duration(tmp_path, monkeypatch):
    server = api_server(tmp_path, monkeypatch)
    response = _run(server.ark_create_task({"model": "seedance-2.0-fast", "duration": 30, "execution_mode": "work",
                                            "content": [{"type": "text", "text": "湖面白鹭"}]}, "test"))
    row = server.store.get(response["id"])
    assert row["duration"] == 30 and row["execution_mode"] == "work"


def test_account_endpoint_accepts_native_thirty_and_preserves_fixed_account(tmp_path, monkeypatch):
    server = api_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_admin_auth", lambda key: "owner")
    monkeypatch.setattr(server, "TEST_JOBS", {})
    body = server.TestGenRequest(prompt="湖面白鹭", model="seedance-2.0-fast", duration=30,
                                 execution_mode="work", force=True)
    response = _run(server.admin_test_generate("acc1", body, None))
    job = server.TEST_JOBS[response["task_id"]]
    assert job["account"] == "acc1" and job["execution_mode"] == "work" and job["duration"] == 30


def test_account_endpoint_records_cookie_classified_doubao_work_mode(tmp_path, monkeypatch):
    server = api_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_admin_auth", lambda key: "owner")
    monkeypatch.setattr(server, "TEST_JOBS", {})
    monkeypatch.setattr(server, "pool", SimpleNamespace(
        available=True, accounts=["db"], site_of=lambda _name: sites.DOUBAO,
        execution_mode_for=lambda _name, _requested: "work",
        test_generate_warnings=lambda *args, **kwargs: [],
    ))
    body = server.TestGenRequest(prompt="湖面白鹭", model="doubao-seedance-2.0-fast", duration=15,
                                 execution_mode="auto", force=True)
    response = _run(server.admin_test_generate("db", body, None))
    assert server.TEST_JOBS[response["task_id"]]["execution_mode"] == "work"


def batch_chain(*vids, finished=False):
    value = bot('本任务已完成，免费额度用完，未额外生成' if finished else '正在处理', finished=finished, tool=True)
    value['results'] = [{'video_model': json.dumps({'vid': vid, 'video_duration':15})} for vid in vids]
    return value


def test_batch_prompt_requests_two_independent_files_without_single_clip_override():
    text = wr.build_prompt('总长30秒的湖面画面', '16:9', 15, 'seedance-2.0-fast', 2)
    assert '两条独立视频文件' in text and '每条原生 15 秒' in text
    assert '只生成一条' not in text and '不购买' in text


def test_batch_waits_for_two_distinct_videos_and_does_not_answer_after_first(monkeypatch):
    agent = load_agent(monkeypatch)
    page = Page([batch_chain('a'), batch_chain('a','b')])
    item = agent._WorkSession('acc', page, None, video_count=2)
    item.page_query = 'https://www.dola.com/x?device_id=1&aid=2'
    _run(item.wait_for_video('123', time.time()+30))
    assert [json.loads(v)['vid'] for v in item.found_videos] == ['a','b']
    assert not page.typed and not item.batch_error
    repeated = batch_chain('same','same')
    repeated['results'][1]['video_model'] = json.dumps({'vid':'same', 'video_duration':15, 'bitrate':100})
    assert len(item.find_videos(repeated)) == 1


def test_batch_finished_with_one_video_retains_partial_result_without_resubmission(monkeypatch):
    agent = load_agent(monkeypatch)
    page = Page([batch_chain('a', finished=True)])
    item = agent._WorkSession('acc', page, None, video_count=2)
    item.page_query = 'https://www.dola.com/x?device_id=1&aid=2'
    _run(item.wait_for_video('123', time.time()+30))
    assert len(item.found_videos) == 1 and '1/2' in item.batch_error
    assert not page.typed


def test_batch_downloads_preserve_both_files_even_when_downloader_reuses_timestamp(tmp_path, monkeypatch):
    agent = load_agent(monkeypatch)
    item = agent._WorkSession('acc', Page([]), None, video_count=2)

    async def deliver(vm, conv):
        path = tmp_path/'acc_timestamp.mp4'
        path.write_bytes(vm.encode())
        return {'local_path':str(path),'account':'acc','conversation_id':conv}

    monkeypatch.setattr(item, 'deliver', deliver)
    result = _run(item.deliver_all(['first','second'], 'conv'))
    assert [Path(v['local_path']).read_bytes() for v in result['videos']] == [b'first',b'second']
    assert not result.get('duration_error')
    item.batch_error = '第二条没有生成'
    partial = _run(item.deliver_all(['only'], 'conv'))
    assert len(partial['videos']) == 1 and partial['duration_error'] == '第二条没有生成'


def test_batch_public_request_persists_count_and_limits_scope(tmp_path, monkeypatch):
    server = api_server(tmp_path, monkeypatch)
    client = server._auth(None)
    response = _run(server._submit_task(client,'seedance-2.0-fast','湖面',None,15,[],execution_mode='work',video_count=2))
    assert server.store.get(response.id)['video_count'] == 2
    with pytest.raises(server.HTTPException):
        _run(server._submit_task(client,'seedance-2.0-fast','湖面',None,15,[],video_count=2))
    with pytest.raises(server.HTTPException):
        _run(server._submit_task(client,'doubao-seedance-2.0-fast','湖面',None,15,[],execution_mode='work',video_count=2))


def test_batch_metadata_and_both_downloads_survive_store_reload_and_delete_together(tmp_path, monkeypatch):
    server = api_server(tmp_path, monkeypatch)
    from store import TaskStore
    monkeypatch.setattr(config,'DOWNLOAD_DIR',str(tmp_path))
    monkeypatch.setattr(server,'_video_metadata',lambda *args:{'video_duration':15,'video_width':1280,'video_height':720})
    files = [tmp_path/'a.mp4', tmp_path/'b.mp4']
    for path in files:path.write_bytes(b'video')
    result = {'videos':[{'local_path':str(p)} for p in files]}
    fields = _run(server._completion_fields('batch',result,15,'16:9'))
    server.store.create('batch','seedance-2.0-fast','湖面','16:9',15,execution_mode='work',video_count=2)
    server.store.update('batch',status='completed',**fields)
    reloaded = TaskStore(server.store.db_path).get('batch')
    assert len(server._task_videos(reloaded)) == 2 and reloaded['video_count'] == 2
    assert all(v['video_duration']==15 for v in server._task_videos(reloaded))
    server._delete_task_files_and_record(reloaded)
    assert not any(p.exists() for p in files) and server.store.get('batch') is None


def test_two_video_choices_keep_two_clips_and_reject_single_clip_recommendation(monkeypatch):
    agent = load_agent(monkeypatch)
    monkeypatch.setattr(agent.dola_responder,'enabled',lambda:False)
    item = agent._WorkSession('acc',Page([]),None,video_count=2)
    choices = [{'title':'选择生成数量','options':['单条15秒（推荐）','两条各15秒']}]
    assert _run(item.choice_labels(choices)) == ['两条各15秒']
    assert '两条独立视频' in item.responder.context['prompt']


def test_batch_second_download_failure_keeps_first_file_and_does_not_generate_again(tmp_path, monkeypatch):
    agent = load_agent(monkeypatch)
    item = agent._WorkSession('acc',Page([]),None,video_count=2)
    calls = []

    async def deliver(vm, conv):
        calls.append(vm)
        if vm=='second':raise RuntimeError('网络下载失败')
        path=tmp_path/'first.mp4'
        path.write_bytes(b'first')
        return {'local_path':str(path),'account':'acc','conversation_id':conv}

    monkeypatch.setattr(item,'deliver',deliver)
    result=_run(item.deliver_all(['first','second'],'original'))
    assert len(result['videos'])==1 and Path(result['local_path']).read_bytes()==b'first'
    assert '第 2 条交付失败' in result['duration_error'] and calls==['first','second']


def test_doubao_uses_send_button_without_accepting_an_enter_suggestion(monkeypatch):
    from test_work_session import send_session
    _,item,page=send_session(monkeypatch,button_sends=False)
    item.site=sites.DOUBAO
    assert _run(item.press_send()) is True
    assert page.clicks==1 and page.enters==0
    assert item.submit_clicked and item.went_out() is False


def test_full_prompt_check_rejects_a_skill_example_containing_the_requested_text(monkeypatch):
    from test_work_session import send_session
    agent,item,page=send_session(monkeypatch)
    prompt='清晨湖面薄雾缓缓散开，一只白鹭贴着水面飞过'
    page.text='Creative video 使用 Seedance 2.5 生成婚礼回顾。'+prompt+'先确认背景音乐。'
    with pytest.raises(agent.PreSubmissionParametersError,match='非本次任务内容'):
        _run(item.fill_prompt(prompt))
    assert page.clicks==0 and page.enters==0 and not item.sent


def test_changed_text_after_insertion_is_rejected_before_send(monkeypatch):
    from test_work_session import send_session
    agent,item,page=send_session(monkeypatch)
    async def mixed(text):page.text='Creative video 婚礼示例'+text
    page.insert_text=mixed
    with pytest.raises(agent.PreSubmissionParametersError,match='内容与本次任务不一致'):
        _run(item.fill_prompt('清晨湖面白鹭'))
    assert not page.enters and not item.sent
