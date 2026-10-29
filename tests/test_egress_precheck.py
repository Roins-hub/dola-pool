"""A dead sticky exit is detected before dispatch, rotated once, and skipped if still dead."""
import time

from test_task_deadline import _pool, _run


def _setup(tmp_path, monkeypatch, names, alive_by_call, rotated=True):
    module, bp = _pool(tmp_path, monkeypatch, names)
    monkeypatch.setattr(module.config, "EGRESS_PRECHECK", True)
    monkeypatch.setattr(module.config, "EGRESS_AUTO_ROTATE_GAP", 120)
    bp._has_bound_proxy = lambda name: True
    checks, rotations = [], []
    def alive(name):
        checks.append(name)
        return {"ok": alive_by_call(name, len([c for c in checks if c == name])), "detail": "ProxyError"}
    monkeypatch.setattr(module, "egress_alive", alive)
    def rotate(name, reason="", force=False):
        rotations.append(name)
        return {"rotated": rotated}
    bp.rotate_account_ip = rotate
    calls = []
    async def generate(account, *a, **kw):
        calls.append(account)
        return {"account": account, "local_path": "fake.mp4"}
    bp._generate_effective = generate
    return bp, checks, rotations, calls


def test_dead_exit_is_rotated_and_the_same_account_is_used(tmp_path, monkeypatch):
    # first check fails, the check after rotation passes
    bp, checks, rotations, calls = _setup(tmp_path, monkeypatch, ("acc1",), lambda name, n: n >= 2)
    notes = []
    result = _run(bp.generate_video("p", None, 5, "seedance-2.5", deadline=time.time() + 30, on_wait=notes.append))
    assert result["account"] == "acc1" and calls == ["acc1"]
    assert rotations == ["acc1"] and checks == ["acc1", "acc1"]
    assert any("已自动换 IP" in n for n in notes)


def test_still_dead_after_rotation_skips_without_risk(tmp_path, monkeypatch):
    bp, checks, rotations, calls = _setup(tmp_path, monkeypatch, ("acc1", "acc2"),
                                          lambda name, n: name == "acc2")
    bp._ordered_candidates = lambda groups: sorted(bp.list_accounts(), key=lambda a: a["name"])
    result = _run(bp.generate_video("p", None, 5, "seedance-2.5", deadline=time.time() + 30))
    assert result["account"] == "acc2" and calls == ["acc2"]
    assert rotations == ["acc1"]
    meta = bp._meta("acc1")
    assert not meta["risk_control"] and meta["login_ok"] != 0 and bp.used_today("acc1") == 0


def test_rotation_is_rate_limited_per_account(tmp_path, monkeypatch):
    bp, checks, rotations, calls = _setup(tmp_path, monkeypatch, ("acc1",), lambda name, n: False)
    assert _run(bp.ensure_live_egress("acc1"))[0] is False
    assert _run(bp.ensure_live_egress("acc1"))[0] is False
    assert rotations == ["acc1"]          # the second failure inside the gap does not rotate again


def test_unbound_accounts_and_disabled_precheck_are_not_probed(tmp_path, monkeypatch):
    module, bp = _pool(tmp_path, monkeypatch, ("acc1",))
    probed = []
    monkeypatch.setattr(module, "egress_alive", lambda name: probed.append(name) or {"ok": False, "detail": "x"})
    bp._has_bound_proxy = lambda name: False
    assert _run(bp.ensure_live_egress("acc1")) == (True, "")
    bp._has_bound_proxy = lambda name: True
    monkeypatch.setattr(module.config, "EGRESS_PRECHECK", False)
    assert _run(bp.ensure_live_egress("acc1")) == (True, "")
    assert probed == []


def test_egress_alive_reads_relay_502_as_dead(monkeypatch):
    import account_transport, requests
    monkeypatch.setattr(account_transport, "http_proxy", lambda account: "http://127.0.0.1:1")
    class Resp:
        def __init__(self, code): self.status_code = code
        def __enter__(self): return self
        def __exit__(self, *a): pass
    codes = iter([502, 502])
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, *a, **kw): return Resp(next(codes))
    monkeypatch.setattr(requests, "Session", Session)
    assert account_transport.egress_alive("acc1") == {"ok": False, "detail": "代理返回 HTTP 502"}
    codes = iter([502, 404])
    assert account_transport.egress_alive("acc1")["ok"] is True   # one dropped attempt is not a dead exit


def test_upload_failure_reports_relay_upstream_failures(monkeypatch):
    import asyncio
    import pytest
    import socks_relay
    from types import SimpleNamespace
    from generation_errors import UploadRouteError
    from test_task_ui_regressions import load_worker_function
    monkeypatch.setattr(socks_relay, "STATS", {"TimeoutError": 1})
    class Page:
        def locator(self, selector): return self
        first = property(lambda self: self)
        async def wait_for(self, **kw): pass
        def on(self, event, fn): setattr(self, event, fn)
        def remove_listener(self, event, fn): pass
        async def count(self): return 0
        async def wait_for_timeout(self, ms): await asyncio.sleep(0)
        async def set_input_files(self, paths):
            socks_relay.STATS["socks5 CONNECT 失败（代码 5）"] = 2
            self.requestfailed(SimpleNamespace(
                url="https://imagex-ap-southeast-1.bytevcloudapi.com/?Action=ApplyImageUpload&sig=SECRET",
                failure="net::ERR_FAILED"))
    with pytest.raises(UploadRouteError) as error:
        _run(load_worker_function("attach_reference_images")(Page(), ["synthetic.png"]))
    text = str(error.value)
    assert "中转上游失败 socks5 CONNECT 失败（代码 5）×2" in text
    assert "TimeoutError" not in text and "SECRET" not in text
