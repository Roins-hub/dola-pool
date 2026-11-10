"""Apple 授权、人工双重验证与账号保存的边界，不访问真实登录或生成接口。"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from test_task_deadline import _run


def test_challenge_is_single_use_and_never_publishes_the_code():
    from apple_login import AppleLoginControl
    updates = []

    async def scenario():
        control = AppleLoginControl(lambda **fields: updates.append(fields))
        pending = asyncio.create_task(control.ask("code", "等待验证码", 10))
        await asyncio.sleep(0)
        with pytest.raises(ValueError):
            control.submit("old-page", "123456")
        with pytest.raises(ValueError):
            control.submit(control.challenge_id, "123")
        challenge_id = control.challenge_id
        control.submit(challenge_id, "654321")
        with pytest.raises(ValueError):
            control.submit(challenge_id, "654321")
        assert await pending == "654321"
        assert not control.challenge_id and not control.kind and control.actions.empty()
        with pytest.raises(ValueError):
            control.submit(challenge_id, "654321")

    _run(scenario())
    assert "654321" not in json.dumps(updates)


def test_cancel_clears_an_unanswered_challenge():
    from apple_login import AppleLoginControl

    async def scenario():
        control = AppleLoginControl(lambda **fields: None)
        pending = asyncio.create_task(control.ask("code", "等待验证码", 10))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not control.challenge_id and control.actions.empty()
    _run(scenario())


def test_credentials_only_go_to_https_apple_domains():
    from apple_login import apple_host, apple_form
    for url in ("https://appleid.apple.com/auth/authorize", "https://idmsa.apple.com/appleauth/auth"):
        assert apple_host(url)
    for url in ("http://appleid.apple.com", "https://appleid.apple.com.evil.test", "https://dola.com"):
        assert not apple_host(url)
    good = SimpleNamespace(url="https://appleid.apple.com/auth/authorize")
    bad = SimpleNamespace(url="https://other.example.test")
    context = SimpleNamespace(pages=[SimpleNamespace(is_closed=lambda: False, frames=[bad, good])])
    assert _run(apple_form(context)) is good
    child = SimpleNamespace(url="https://idmsa.apple.com/appleauth/auth")
    context.pages[0].frames = [good, child]
    assert _run(apple_form(context)) is child


class Input:
    def __init__(self, form, key):
        self.form, self.key = form, key

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def fill(self, value, **kwargs):
        self.form.values[self.key] = value

    async def click(self, **kwargs):
        self.form.step += 1


class Items:
    def __init__(self, items=()):
        self.items = list(items)

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class Form:
    url = "https://appleid.apple.com/auth/authorize"

    def __init__(self):
        self.step, self.values = 0, {}

    def locator(self, selector):
        from apple_login import CODE_SELECTOR
        if "role=" in selector:
            return Items()
        if selector == CODE_SELECTOR:
            return Items([Input(self, f"digit{i}") for i in range(6)] if self.step == 1 else [])
        if "account_name" in selector:
            return Items([Input(self, "email")] if self.step == 0 else [])
        if "password_text" in selector:
            return Items([Input(self, "password")] if self.step == 0 else [])
        if selector.startswith("#sign-in"):
            return Items([Input(self, "submit")] if self.step < 2 else [])
        return Items()

    def get_by_role(self, role, **kwargs):
        return Items([Input(self, "consent")] if self.step == 2 else [])


def test_apple_credentials_code_and_first_consent_complete_in_order(monkeypatch):
    import apple_login as apple
    form, stages, prompts = Form(), [], []
    async def cookies(*args):
        return [{"name": "sessionid", "value": "test-session"}] if form.step >= 3 else []
    context = SimpleNamespace(pages=[SimpleNamespace(is_closed=lambda: False, frames=[form])], cookies=cookies)
    async def ask(kind, stage, remaining):
        prompts.append(kind)
        return "654321" if kind == "code" else "continue"
    control = SimpleNamespace(update=lambda **kw: stages.append(kw), ask=ask)
    async def no_wait(seconds):
        pass
    monkeypatch.setattr(apple.asyncio, "sleep", no_wait)
    _run(apple.complete_apple_login(context, "example@test.invalid", "test-password", control))
    assert form.values["email"] == "example@test.invalid"
    assert form.values["password"] == "test-password"
    assert "".join(form.values[f"digit{i}"] for i in range(6)) == "654321"
    assert prompts == ["code", "continue"]
    assert "test-password" not in str(stages) and "654321" not in str(stages)


def test_code_auto_submit_can_close_the_apple_frame(monkeypatch):
    import apple_login as apple
    form = Form()
    form.step = 1
    form.is_detached = lambda: True
    async def closed_frame(_form):
        raise RuntimeError("frame detached after automatic submission")
    monkeypatch.setattr(apple, "_continue", closed_frame)
    _run(apple._fill_code(form, "654321"))
    assert form.values["digit5"] == "1"


@pytest.fixture
def srv(tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server, "_admin_auth", lambda key: None)
    monkeypatch.setattr(server, "JOBS", {})
    monkeypatch.setattr(server, "APPLE_LOGINS", {})
    monkeypatch.setattr(server, "APPLE_ADD_TASKS", {})
    monkeypatch.setattr(server, "ADD_ACCOUNT_SEM", asyncio.Semaphore(1))
    monkeypatch.setattr(server, "ADD_SEM_HOLDER", {"what": ""})
    monkeypatch.setattr(server, "validate_proxy_ids", lambda ids: ids)
    monkeypatch.setattr(server, "get_account_proxy", lambda name: None)
    events = []
    monkeypatch.setattr(server, "pool", SimpleNamespace(accounts=[], find_accounts_by_email=lambda email: [],
        ensure_account=lambda n: events.append(("account", n)), set_email=lambda *a: None,
        set_source=lambda n, source: events.append(("source", source)), set_login_status=lambda *a: None))
    monkeypatch.setattr(server.cred_store, "store", lambda *a: pytest.fail("Apple password must not be saved"))
    return server, events


def test_apple_job_binds_the_proxy_before_opening_the_browser(srv, monkeypatch):
    server, events = srv
    monkeypatch.setattr(server, "bind_selected_proxy", lambda *a: events.append(("bind", a)))
    async def flow(name, email, password, control):
        assert events[0][0] == "bind" and server.JOBS[name]["source"] == "apple"
        control.update(stage="等待验证码")
        events.append(("apple", name))
    monkeypatch.setattr(server, "apple_login_flow", flow)
    _run(server._run_add_job("new_apple", "example@test.invalid", "test-password", "",
                             proxy_id="p1", login_method="apple"))
    assert server.JOBS["new_apple"]["status"] == "success"
    assert ("source", "apple") in events
    assert not server.APPLE_LOGINS and not server.ADD_SEM_HOLDER["what"]
    assert "test-password" not in json.dumps(server.JOBS)


def test_add_accepts_apple_without_saving_credentials(srv, monkeypatch):
    server, events = srv
    received = []
    async def flow(*args, **kwargs):
        received.append(kwargs)
    monkeypatch.setattr(server, "_run_add_job", flow)
    async def scenario():
        result = await server.admin_account_add(server.AccountAdd(name="new_apple", email="example@test.invalid",
            password="test-password", login_method="apple", proxy_id="p1"))
        await asyncio.sleep(0)
        return result
    assert _run(scenario())["job"] == "running"
    assert received[0]["login_method"] == "apple"
    assert "test-password" not in json.dumps(server.JOBS)


def test_apple_requires_its_own_proxy(srv):
    server, _ = srv
    with pytest.raises(HTTPException) as caught:
        _run(server.admin_account_add(server.AccountAdd(name="new_apple", email="example@test.invalid",
            password="test-password", login_method="apple")))
    assert caught.value.status_code == 400 and "代理" in caught.value.detail


def test_answer_endpoint_is_authenticated_and_does_not_return_secrets(srv, monkeypatch):
    server, _ = srv
    from apple_login import AppleLoginControl
    control = AppleLoginControl(lambda **kw: None)
    control.challenge_id, control.kind = "current", "code"
    server.APPLE_LOGINS["new_apple"] = control
    server.JOBS["new_apple"] = {"status": "running", "source": "apple"}
    denied = []
    def auth(key):
        denied.append(key)
        if key != "test-admin":
            raise HTTPException(401)
    monkeypatch.setattr(server, "_admin_auth", auth)
    body = server.AppleLoginAnswer(challenge_id="current", code="654321")
    with pytest.raises(HTTPException):
        _run(server.admin_apple_answer("new_apple", body))
    response = _run(server.admin_apple_answer("new_apple", body, "test-admin"))
    assert "no-store" in response.headers["cache-control"]
    assert b"654321" not in response.body and "654321" not in json.dumps(server.JOBS)


def test_cancel_while_waiting_clears_the_job_and_releases_the_queue(srv, monkeypatch):
    server, _ = srv
    entered = asyncio.Event()
    async def flow(name, email, password, control):
        entered.set()
        await control.ask("code", "等待验证码", 30)
    monkeypatch.setattr(server, "apple_login_flow", flow)
    async def scenario():
        task = asyncio.create_task(server._run_add_job("new_apple", "example@test.invalid", "test-password", "",
                                                      login_method="apple"))
        server.APPLE_ADD_TASKS["new_apple"] = task
        await entered.wait()
        await asyncio.sleep(0)
        await server.admin_apple_cancel("new_apple")
    _run(scenario())
    assert not server.APPLE_LOGINS and not server.APPLE_ADD_TASKS and not server.ADD_ACCOUNT_SEM.locked()
    assert server.JOBS["new_apple"]["status"] == "failed" and not server.JOBS["new_apple"]["challenge"]


def test_apple_source_is_not_automatically_relogged_through_google(srv, monkeypatch):
    server, _ = srv
    monkeypatch.setattr(server.cred_store, "all_names", lambda: ["old_apple"])
    monkeypatch.setattr(server, "pool", SimpleNamespace(list_accounts=lambda: [
        {"name": "old_apple", "source": "apple", "login_ok": 0}]))
    monkeypatch.setattr(server.cred_store, "load", lambda *a: pytest.fail("Must not load Google credentials"))
    _run(server._cred_try_refresh_once())


def test_session_snapshot_keeps_cookie_attributes_and_does_not_restore_a_logout(tmp_path):
    from browser import _materialize_apple_session
    snapshot = tmp_path / ".apple-session-cookies.json"
    cookie = {"name": "sessionid", "value": "test-session", "domain": ".dola.com", "path": "/",
              "httpOnly": True, "secure": True, "sameSite": "Lax", "expires": -1}
    snapshot.write_text(json.dumps([cookie]))
    live, restored = [], []
    async def cookies(*args):
        return live
    async def add(values):
        restored.extend(values)
        live.extend(values)
    async def close():
        pass
    context = SimpleNamespace(cookies=cookies, add_cookies=add, close=close)
    async def scenario():
        await _materialize_apple_session(context, snapshot)
        assert restored == [cookie]
        live.clear()  # 用户已退出，空状态必须保存。
        await context.close()
        await _materialize_apple_session(context, snapshot)
    _run(scenario())
    assert json.loads(snapshot.read_text()) == [] and not live


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_profile_only_enters_the_pool_after_a_completed_login(tmp_path, monkeypatch, outcome):
    import apple_login as apple
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(apple.proxy_store, "get_account_proxy", lambda name: {"enabled": True})
    monkeypatch.setattr(apple, "http_proxy", lambda name: "http://127.0.0.1:12345")
    closed, launched = [], []
    entered = asyncio.Event()
    async def no_action(*args, **kwargs):
        pass
    entry = SimpleNamespace(wait_for=no_action, click=no_action)
    page = SimpleNamespace(goto=no_action, get_by_test_id=lambda name: entry)
    cookie = {"name": "sessionid", "value": "test-session", "domain": ".dola.com", "path": "/", "expires": -1}
    async def cookies(*args):
        return [cookie]
    async def close():
        closed.append(True)
    context = SimpleNamespace(pages=[page], cookies=cookies, close=close)
    async def launch(profile, **kwargs):
        launched.append(kwargs)
        (Path(profile) / "Default").mkdir()
        assert not (tmp_path / "accounts/new_apple").exists()
        return context
    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        async def __aexit__(self, *args):
            pass
    monkeypatch.setitem(sys.modules, "patchright.async_api", SimpleNamespace(async_playwright=Playwright))
    async def complete(*args):
        entered.set()
        if outcome == "failure":
            raise RuntimeError("test-password https://appleid.apple.com/?token=test")
        if outcome == "cancel":
            await asyncio.Event().wait()
    monkeypatch.setattr(apple, "complete_apple_login", complete)
    control = apple.AppleLoginControl(lambda **fields: None)
    async def scenario():
        task = asyncio.create_task(apple.apple_login_flow("new_apple", "example@test.invalid", "test-password", control))
        if outcome == "cancel":
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "failure":
            with pytest.raises(apple.AppleLoginError) as caught:
                await task
            assert "test-password" not in str(caught.value) and "token" not in str(caught.value)
        else:
            await task
    _run(scenario())
    assert len(closed) == 1 and launched[0]["proxy"] == {"server": "http://127.0.0.1:12345"}
    assert not list((tmp_path / "accounts").glob(".apple-*"))
    target = tmp_path / "accounts/new_apple"
    assert target.exists() == (outcome == "success")
    if outcome == "success":
        assert json.loads((target / apple.SESSION_FILE).read_text()) == [cookie]


def test_panel_apple_otp_flow_handles_stale_results_and_closes_cleanly():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    html = (Path(__file__).resolve().parents[1] / "web/index.html").read_text(encoding="utf-8")
    start = html.index("let appleProgressTimer=")
    end = html.index("/* ---- 代理管理 ---- */", start)
    code = html[start:end]
    script = r'''
const assert=require('assert');
let dialogEpoch=1,job={status:'running',stage:'等待验证码',challenge:{id:'first',kind:'code'}},posted=[];
let addProxyLoading=false,addProxyError=false,singleAddProxy='p1';
const nodes={};
for(const name of ['appleStage','appleCode','appleCodeBox','appleContinue','appleCancel','appleFeedback'])nodes['#'+name]={value:'',hidden:false,textContent:'',disabled:false};
const q=s=>nodes[s],refresh=()=>{},alert=()=>{},showDlg=()=>{dialogEpoch++;stopAppleProgress();};
let api=async(url,opts)=>{if(!opts)return {jobs:{acc1:job}};posted.push(opts.body);return {ok:true};};
''' + code + r'''
(async()=>{
 appleProgressName='acc1';await pollAppleProgress(1);clearTimeout(appleProgressTimer);
 assert.equal(appleChallengeId,'first');assert.equal(q('#appleCodeBox').hidden,false);
 q('#appleCode').value='654321';await submitAppleAnswer();
 assert.deepEqual(posted,[{challenge_id:'first',code:'654321'}]);assert.equal(q('#appleCode').value,'');
 job.challenge={id:'second',kind:'code'};await pollAppleProgress(1);clearTimeout(appleProgressTimer);
 q('#appleCode').value='111111';job.challenge={id:'third',kind:'continue'};
 await pollAppleProgress(1);clearTimeout(appleProgressTimer);
 assert.equal(q('#appleCode').value,'');assert.equal(q('#appleContinue').hidden,false);
 await submitAppleAnswer();assert.equal(posted[1].code,'');
 stopAppleProgress();assert.equal(appleProgressName,'');assert.equal(appleChallengeId,'');
 Object.assign(nodes,{'#f_name':{value:'acc1'},'#f_email':{value:'example@test.invalid'},'#f_pass':{value:'test-password'}});
 let finish;
 api=()=>new Promise(resolve=>{finish=resolve;});
 const pending=submitAddApple();dialogEpoch++;finish({ok:true});await pending;
 assert.equal(appleProgressName,'');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run([node, "-e", script], check=True, capture_output=True, timeout=15)


def test_saved_apple_account_also_stops_when_its_proxy_binding_is_missing(tmp_path, monkeypatch):
    import browser
    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "accounts/old_apple"
    profile.mkdir(parents=True)
    (profile / ".apple-session-cookies.json").write_text("[]")
    monkeypatch.setattr(browser.proxy_store, "get_account_proxy", lambda name: None)
    with pytest.raises(RuntimeError, match="禁止直连"):
        _run(browser.launch_account_context(SimpleNamespace(), "old_apple"))
