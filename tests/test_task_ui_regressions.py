import ast
import asyncio
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_account_batch_delete_reaches_selected_accounts_only():
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    assert 'onclick="deleteSelectedAccounts()"' in html
    names = re.findall(r"^(?:async )?function (\w+)\(", html, re.M)
    assert len(names) == len(set(names)), "Duplicate global functions can silently override handlers"
    code = re.search(r"async function deleteSelectedAccounts\(\).*?(?=\n(?:async )?function )", html, re.S)[0]
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    script = """
const assert=require('assert');
const selAccounts=new Set(['acc1','acc3']),calls=[];
let approve=false;
const confirm=()=>approve, alert=()=>{}, refresh=()=>{}, updateSelButtons=()=>{};
const api=async (url,opts)=>{assert.equal(opts.method,'DELETE');calls.push(url);};
const dialogText=(parts,...values)=>parts.reduce((s,p,i)=>s+p+(values[i]||''),'');
""" + code + """
(async()=>{
 await deleteSelectedAccounts();assert.equal(calls.length,0);assert.equal(selAccounts.size,2);
 approve=true;await deleteSelectedAccounts();
 assert.deepEqual(calls,['/api/admin/accounts/acc1','/api/admin/accounts/acc3']);
 assert.equal(selAccounts.size,0);
})().catch(e=>{console.error(e);process.exit(1)});
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, timeout=15)


def test_internal_upload_timeout_keeps_reason_and_note(tmp_path, monkeypatch):
    import server
    import store
    monkeypatch.setattr(server, "store", store.TaskStore(str(tmp_path / "tasks.db")))
    monkeypatch.setattr(server, "_maybe_start_recovery", lambda *a: False)
    class Pool:
        async def generate_video(self, *a, **kw):
            kw["on_account_try"]("acc1", 1)
            kw["on_poll"](time.time(), note="Dola 的真实回话", progress="正在上传参考图")
            raise TimeoutError("参考图片上传超时: prepare=1/1, tos=0/1")
    monkeypatch.setattr(server, "pool", Pool())
    server.store.create("stage_timeout", "seedance-2.0-fast", "p", "16:9", 5)
    run(server._run_task("stage_timeout", "seedance-2.0-fast", "p", "16:9", 5, [], {}))
    row = server.store.get("stage_timeout")
    assert row["status"] == "failed"
    assert "参考图片上传超时" in row["error"] and "371" not in row["error"]
    assert row["upstream_note"] == "Dola 的真实回话"


def test_outer_timeout_still_stops_task(tmp_path, monkeypatch):
    import server
    import store
    monkeypatch.setattr(server, "store", store.TaskStore(str(tmp_path / "tasks.db")))
    monkeypatch.setattr(server, "_maybe_start_recovery", lambda *a: False)
    real_timeout = asyncio.timeout
    monkeypatch.setattr(server.asyncio, "timeout", lambda seconds: real_timeout(0.01))
    class Pool:
        async def generate_video(self, *a, **kw):
            kw["on_account_try"]("acc1", 1)
            await asyncio.sleep(10)
    monkeypatch.setattr(server, "pool", Pool())
    server.store.create("outer_timeout", "seedance-2.0-fast", "p", "16:9", 5)
    run(server._run_task("outer_timeout", "seedance-2.0-fast", "p", "16:9", 5, [], {}))
    row = server.store.get("outer_timeout")
    assert row["status"] == "failed" and "等待出片超过" in row["error"]


def test_poll_reports_upstream_note_before_quota_error():
    from browser_feedback import emit
    notes = []
    progress_notes = []
    def on_poll(now, note=None, progress=None):
        if note: notes.append(note)
        if progress: progress_notes.append(progress)
    class Context:
        async def cookies(self, *a): return []
    class Page:
        async def evaluate(self, *a):
            return {"texts": ["daily quota"], "assistantTexts": ["今日视频额度已用完"]}
    class QuotaError(Exception): pass
    async def no_wait(*a): pass
    worker = load_worker_function("poll_conversation", cookie_value=lambda *a: "",
        asyncio=SimpleNamespace(sleep=no_wait, wait_for=asyncio.wait_for),
        POLL_JS="", emit_feedback=emit, _parse_balance_texts=lambda a: (None, None, ""),
        DAILY_LIMIT_PATTERN=re.compile("daily quota"), AccountLimitedError=QuotaError)
    with pytest.raises(QuotaError):
        run(worker("acc1", Page(), Context(), "c1", 60,
                   on_poll=on_poll))
    assert notes == ["今日视频额度已用完"]
    assert progress_notes == ["已取得上游会话，正在查询视频结果"]


def test_poll_reports_download_progress_before_awaiting_download(tmp_path):
    from browser_feedback import emit
    progress_notes, notes = [], []
    output = tmp_path / 'video.mp4'
    output.write_bytes(b'video')
    def on_poll(now, note=None, progress=None):
        if note: notes.append(note)
        if progress: progress_notes.append(progress)
    class Context:
        async def cookies(self, *a): return []
    class Page:
        async def evaluate(self, *a):
            return {'assistantTexts': ['视频已生成'], 'videos': ['https://example.invalid/video.mp4']}
    async def no_wait(*a): pass
    async def higher_bitrate(*a):
        assert progress_notes[-1] == '上游已返回视频，正在获取下载地址'
    async def download(*a):
        assert progress_notes[-1] == '视频已生成，正在下载到服务器'
        return output
    worker = load_worker_function('poll_conversation', cookie_value=lambda *a: '',
        asyncio=SimpleNamespace(sleep=no_wait, wait_for=asyncio.wait_for),
        POLL_JS='', emit_feedback=emit, TERMINAL_TEXT_MAX_HITS=3, HARD_ERROR_TEXT_MAX_HITS=3,
        fetch_higher_bitrate_url=higher_bitrate, extract_unwatermarked_url=lambda vm, url: url,
        _download=download)
    result = run(worker('acc1', Page(), Context(), 'c1', 60, on_poll=on_poll))
    assert result['local_path'] == str(output)
    assert notes == ['视频已生成']
    assert progress_notes == ['已取得上游会话，正在查询视频结果',
                              '上游已返回视频，正在获取下载地址',
                              '视频已生成，正在下载到服务器',
                              '视频已下载，正在完成本地处理']


@pytest.mark.parametrize('account_test', [False, True])
def test_completed_tasks_clear_progress_and_preserve_upstream_reply(tmp_path, monkeypatch, account_test):
    import server
    import store
    task_id = 'progress_test'
    monkeypatch.setattr(server, 'store', store.TaskStore(str(tmp_path / 'tasks.db')))
    monkeypatch.setattr(server, 'TEST_JOBS', {task_id: {}})
    monkeypatch.setattr(server, '_video_metadata', lambda *a: {})
    monkeypatch.setattr(server, '_prune_downloads', lambda: None)
    class Pool:
        async def generate_video(self, *a, **kw):
            kw['on_account_try']('acc1', 1)
            server.store.set_queue_note(task_id, '已发送生成请求，等待上游会话确认')
            kw['on_conversation_id']('acc1', 'c1', time.time() + 60)
            assert server.store.get(task_id)['queue_note'] == '已取得上游会话，正在查询视频结果'
            return await self.test_generate(*a, **kw)
        async def test_generate(self, *a, **kw):
            kw['on_poll'](time.time(), note='Dola 原始回话', progress='视频已下载，正在完成本地处理')
            assert server.store.get(task_id)['queue_note'] == '视频已下载，正在完成本地处理'
            return {'ok': True, 'local_path': str(tmp_path / 'video.mp4'),
                    'video_url': 'https://example.invalid/video.mp4', 'account': 'acc1'}
    monkeypatch.setattr(server, 'pool', Pool())
    if account_test:
        run(server._run_test_generate(task_id, 'acc1', 'p', '16:9', 5, 'seedance-2.0-fast'))
    else:
        server.store.create(task_id, 'seedance-2.0-fast', 'p', '16:9', 5)
        run(server._run_task(task_id, 'seedance-2.0-fast', 'p', '16:9', 5, [], {}))
    row = server.store.get(task_id)
    assert row['status'] == 'completed'
    assert row['queue_note'] == ''
    assert row['upstream_note'] == 'Dola 原始回话'


def load_worker_function(name, **bindings):
    tree = ast.parse((ROOT / "video_worker_ui.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    scope = {"asyncio": asyncio, "time": time, **bindings}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "worker-test", "exec"), scope)
    return scope[name]


def test_prepare_upload_business_error_is_not_counted_as_http_success():
    class Page:
        def locator(self, selector): return self
        first = property(lambda self: self)
        async def wait_for(self, **kw): pass
        def on(self, event, fn):
            if event == "response": self.callback = fn
        def remove_listener(self, event, fn): self.removed = True
        async def count(self): return 1
        async def wait_for_timeout(self, ms): await asyncio.sleep(0)
        async def set_input_files(self, paths):
            async def payload(): return {"code": 710010703, "msg": "调用下游服务失败"}
            self.callback(SimpleNamespace(status=200, url="https://www.dola.com/alice/resource/prepare_upload", json=payload))
    page = Page()
    with pytest.raises(TimeoutError, match="710010703"):
        run(load_worker_function("attach_reference_images")(page, ["synthetic.png"]))
    assert page.removed


def test_apply_upload_network_failure_preserves_stage_and_redacts_url():
    class Page:
        callbacks = {}
        def locator(self, selector): return self
        first = property(lambda self: self)
        async def wait_for(self, **kw): pass
        def on(self, event, fn): self.callbacks[event] = fn
        def remove_listener(self, event, fn): self.callbacks.pop(event)
        async def count(self): return 9
        async def wait_for_timeout(self, ms): await asyncio.sleep(0)
        async def set_input_files(self, paths):
            self.callbacks["requestfailed"](SimpleNamespace(
                url="https://imagex.example.invalid/?Action=ApplyImageUpload&token=PRIVATE",
                failure="net::ERR_EMPTY_RESPONSE", method="GET"))
    page = Page()
    with pytest.raises(TimeoutError) as error:
        run(load_worker_function("attach_reference_images")(page, ["synthetic.png"]))
    from generation_errors import PreSubmissionUploadError
    assert isinstance(error.value, PreSubmissionUploadError)
    assert "ApplyImageUpload" in str(error.value) and "net::ERR_EMPTY_RESPONSE" in str(error.value)
    assert "PRIVATE" not in str(error.value) and "token" not in str(error.value)
    assert not page.callbacks


def test_upload_options_is_not_counted_as_a_file_upload():
    class Stop(Exception): pass
    class Page:
        callbacks = {}
        waits = 0
        def locator(self, selector): return self
        first = property(lambda self: self)
        async def wait_for(self, **kw): pass
        def on(self, event, fn): self.callbacks[event] = fn
        def remove_listener(self, event, fn): self.callbacks.pop(event)
        async def count(self): return 9
        async def wait_for_timeout(self, ms):
            assert ms != 800, "OPTIONS must not complete the upload"
            self.waits += 1
            if self.waits > 2: raise Stop()
            await asyncio.sleep(0)
        async def set_input_files(self, paths):
            async def data(): return {"code": 0}
            self.callbacks["response"](SimpleNamespace(status=200,
                url="https://www.dola.com/alice/resource/prepare_upload", json=data,
                request=SimpleNamespace(method="POST")))
            self.callbacks["response"](SimpleNamespace(status=200,
                url="https://storage.example.invalid/upload/v1/private", json=data,
                request=SimpleNamespace(method="OPTIONS")))
    page = Page()
    with pytest.raises(Stop):
        run(load_worker_function("attach_reference_images")(page, ["synthetic.png"]))
    assert not page.callbacks


def test_browser_assistant_reply_reaches_task_callback():
    from browser_feedback import BrowserReplies
    notes = []
    class Page:
        def on(self, *a): pass
        def remove_listener(self, *a): pass
    async def body():
        return ('event: SSE_REPLY_END\ndata: '+json.dumps({"msg_finish_attr": {"brief": "正在生成，预计等待 5 分钟"}})+'\n\n').encode()
    response = SimpleNamespace(body=body, url="https://www.dola.com/chat/completion")
    async def scenario():
        tracker = BrowserReplies(Page(), lambda now, note=None: notes.append(note))
        tracker.start()
        await tracker.read(response)
        await tracker.read(response)
        await tracker.close()
    run(scenario())
    assert notes == ["正在生成，预计等待 5 分钟"]


@pytest.mark.parametrize("model,duration,cost", [
    ("seedance-2.5", 30, 2), ("seedance-2.5", 10, 4), ("seedance-2.5", 5, 2),
    ("seedance-2.0-fast", 5, 3), ("seedance-2.0-fast", 10, 3), ("seedance-2.0-fast", 15, 3)])
def test_confirmed_costs(model, duration, cost):
    import config
    assert config.credit_cost(model, duration) == cost
