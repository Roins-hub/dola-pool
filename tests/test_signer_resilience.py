"""签名器健壮性：长驻进程卡死/死掉时，不能把整个服务拖死，也不能让任务白等。

背景：线上出现过 `BDMS signer produced no output` → 熔断 60~300 秒，
期间所有任务都报 `BDMS signer cooling down`（26 次失败）。
"""
from __future__ import annotations

import json
import queue as queue_mod
import threading
from pathlib import Path

import pytest

import pure_signer


def _signer(monkeypatch) -> pure_signer.PersistentSigner:
    """不走真实 node：只验证调度/兜底逻辑。"""
    signer = pure_signer.PersistentSigner.__new__(pure_signer.PersistentSigner)
    signer.script = Path("/dev/null")
    signer.name = "test-signer"
    signer._lock = threading.Lock()
    signer._proc = None
    signer._generation = 0
    signer._fail_streak = 0
    signer._sign_count = 0
    signer._timeouts = 0
    signer._queue = queue_mod.Queue()
    signer._pending = {}
    signer._pending_lock = threading.Lock()
    signer._stderr_tail = __import__("collections").deque(maxlen=8)
    monkeypatch.setattr(pure_signer, "_circuit_until", 0.0, raising=False)
    return signer


def _ok_line(url: str) -> str:
    return json.dumps({"signed_url": url + "&a_bogus=abc", "a_bogus": "abc"})


def test_daemon_ok(monkeypatch):
    signer = _signer(monkeypatch)
    monkeypatch.setattr(signer, "_send_daemon", lambda payload: _ok_line("https://x/a"))
    out = signer("https://x/a", method="POST", body="{}", cookies={})
    assert out.endswith("&a_bogus=abc")
    assert pure_signer._circuit_until == 0.0


def test_daemon_failure_then_one_shot_fallback(monkeypatch):
    """长驻进程两轮都失败 → 自动用一次性 node 兜底，而不是直接熔断 60 秒。"""
    signer = _signer(monkeypatch)

    def boom(payload):
        raise RuntimeError("BDMS signer produced no output (exit=-9)")

    restart_calls = []
    monkeypatch.setattr(signer, "_send_daemon", boom)
    monkeypatch.setattr(signer, "_send_subprocess", lambda payload: _ok_line("https://x/b"))
    monkeypatch.setattr(signer, "_restart", lambda: restart_calls.append(1))

    out = signer("https://x/b", method="POST", body="{}", cookies={})
    assert out.endswith("&a_bogus=abc")
    assert restart_calls                       # 每轮失败都重启过长驻进程
    assert pure_signer._circuit_until == 0.0   # 没有熔断


def test_both_paths_fail_trips_circuit_then_cools_down(monkeypatch):
    signer = _signer(monkeypatch)
    monkeypatch.setattr(signer, "_send_daemon",
                        lambda payload: (_ for _ in ()).throw(RuntimeError("no output")))
    monkeypatch.setattr(signer, "_send_subprocess",
                        lambda payload: (_ for _ in ()).throw(RuntimeError("one-shot failed")))
    monkeypatch.setattr(signer, "_restart", lambda: None)

    with pytest.raises(RuntimeError):
        signer("https://x/c", method="POST", body="{}", cookies={})
    assert pure_signer._circuit_until > 0                      # 熔断了
    with pytest.raises(RuntimeError, match="cooling down"):
        signer("https://x/c", method="POST", body="{}", cookies={})
    pure_signer._circuit_until = 0.0                            # 收尾，别影响别的用例


def test_recycle_restarts_after_threshold(monkeypatch):
    """签够次数后主动重启长驻进程，避免长期运行累积状态。"""
    signer = _signer(monkeypatch)
    signer._sign_count = pure_signer.SIGN_RECYCLE_AFTER
    restarted = []
    monkeypatch.setattr(signer, "_restart", lambda: restarted.append(1))

    class _FakeProc:
        stdin = type("S", (), {"write": lambda self, s: None, "flush": lambda self: None})()

        def poll(self):
            return None

    signer._proc = _FakeProc()
    signer._queue.put(_ok_line("https://x/d"))
    line = signer._send_daemon("{}")

    assert restarted == [1]
    assert "a_bogus" in line
    assert signer._sign_count == 1


def test_timeout_is_bounded(monkeypatch):
    """readline 不再无限等：超时直接报错（带 stderr 线索）。"""
    import queue as queue_mod
    import threading

    signer = _signer(monkeypatch)

    class _FakeProc:
        stdin = type("S", (), {"write": lambda self, s: None, "flush": lambda self: None})()
        def poll(self):
            return None

    signer._proc = _FakeProc()
    monkeypatch.setattr(pure_signer, "SIGN_TIMEOUT_SECONDS", 0.05)
    signer._queue = queue_mod.Queue()   # 永远没有输出
    with pytest.raises(RuntimeError, match="timeout"):
        signer._send_daemon("{}")


class _FakeProc:
    """只有 stdin/poll 的假长驻进程。"""

    def __init__(self):
        self.stdin = type("S", (), {"write": lambda self, s: None,
                                    "flush": lambda self: None})()
        self._alive = True

    def poll(self):
        return None if self._alive else -9

    def kill(self):
        self._alive = False

    def terminate(self):
        self._alive = False

    def wait(self, timeout=None):
        return 0


def _reader_thread(signer, lines):
    """把 lines 按 id 路由，模拟 node 守护进程的回包。"""
    def _run():
        for line in lines:
            rid = pure_signer._extract_id(line)
            with signer._pending_lock:
                slot = signer._pending.get(rid)
            if slot is not None:
                slot[0].put(line)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def test_concurrent_responses_are_paired_by_id(monkeypatch):
    """并发下必须按请求 id 配对，A 线程不能拿到 B 线程的签名（老实现是共享队列）。"""
    signer = _signer(monkeypatch)
    signer._proc = _FakeProc()
    monkeypatch.setattr(pure_signer.PersistentSigner, "_start", lambda self: None)

    got: dict[str, str] = {}

    def worker(tag: str):
        payload = json.dumps({"id": tag, "url": tag, "method": "POST", "body": "{}"})
        got[tag] = signer._send_daemon(payload)

    # 回包故意**乱序**（先 B 后 A）
    monkeypatch.setattr(pure_signer, "_extract_id", pure_signer._extract_id)

    th_a = threading.Thread(target=worker, args=("aaa",))
    th_b = threading.Thread(target=worker, args=("bbb",))
    th_a.start()
    # 等 aaa 注册进 pending，再让 B 注册、然后按 B→A 的顺序回包
    for _ in range(200):
        with signer._pending_lock:
            if "aaa" in signer._pending:
                break
        threading.Event().wait(0.005)
    th_b.start()
    for _ in range(200):
        with signer._pending_lock:
            if "bbb" in signer._pending:
                break
        threading.Event().wait(0.005)

    for rid in ("bbb", "aaa"):
        with signer._pending_lock:
            signer._pending[rid][0].put(json.dumps({"id": rid, "signed_url": f"https://x/{rid}&a_bogus=z"}))

    th_a.join(5)
    th_b.join(5)
    assert "aaa" in got["aaa"] and "bbb" in got["bbb"]


def test_timeout_does_not_kill_shared_process_or_fail_others(monkeypatch):
    """一个请求超时只失败它自己：不 kill 共享进程、不牵连其他在途请求。"""
    signer = _signer(monkeypatch)
    signer._proc = _FakeProc()
    restarts = []
    monkeypatch.setattr(signer, "_restart", lambda: restarts.append(1))
    monkeypatch.setattr(signer, "_start", lambda: None)
    monkeypatch.setattr(pure_signer, "SIGN_TIMEOUT_SECONDS", 0.05)

    def _slow_call():
        try:
            signer._send_daemon(json.dumps({"id": "slow", "url": "u"}))
        except RuntimeError:
            pass        # 它就是要超时的那个

    slow = threading.Thread(target=_slow_call, daemon=True)
    slow.start()
    for _ in range(200):
        with signer._pending_lock:
            if "slow" in signer._pending:
                break
        threading.Event().wait(0.005)
    try:
        signer._send_daemon(json.dumps({"id": "slow", "url": "u"}))
    except RuntimeError:
        pass
    # 超时不得引发进程重启（老实现每次失败都 kill 共享进程 → SIGKILL 风暴）
    assert restarts == []
    assert signer._timeouts >= 1


def test_pool_picks_least_loaded_signer(monkeypatch):
    """进程池按"在途最少"分配，避免请求全压在一个进程上。"""
    pool = pure_signer.SignerPool.__new__(pure_signer.SignerPool)
    pool._script = Path("/dev/null")
    pool._lock = threading.Lock()
    pool._turn = 0
    a, b = _signer(monkeypatch), _signer(monkeypatch)
    a.name, b.name = "a", "b"
    pool._signers = [a, b]
    with b._pending_lock:
        b._pending["x"] = (queue_mod.Queue(), 1)     # b 有 1 个在途
    assert pool._pick() is a
    with a._pending_lock:
        a._pending["y"] = (queue_mod.Queue(), 1)
        a._pending["z"] = (queue_mod.Queue(), 1)
    assert pool._pick() is b


def test_recover_only_restarts_when_process_is_dead(monkeypatch):
    """进程还活着且没连续超时的失败，不该重启（老实现是无条件 kill）。"""
    signer = _signer(monkeypatch)
    signer._proc = _FakeProc()
    restarts = []
    monkeypatch.setattr(signer, "_restart", lambda: restarts.append(1))
    signer._timeouts = 0
    signer._recover_after_failure()
    assert restarts == []                       # 活着 → 不重启
    signer._proc = None
    signer._recover_after_failure()
    assert restarts == [1]                      # 真死了 → 重启一次
