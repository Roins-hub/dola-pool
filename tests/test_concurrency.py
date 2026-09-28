"""全局并发槽（DOLA_MAX_CONCURRENCY）与任务状态 queued/processing 的对应关系。

回归点（2.0.9）：
1) `_run_task` 曾在调用 `pool.generate_video` **之前**就写 `status=processing`，而真正
   的等待（全局并发槽 / 账号锁）发生在那之后 —— 于是被卡住的任务对客户端显示成
   「生成中」而不是「排队中」，2.0.8 的排队进度提示只在卡「每 Key 并发限制」时才触发。
   正确行为：由 `on_account_try` 在真正拿到账号（= 拿到槽位）那一刻才置 `processing`。
2) `DOLA_MAX_CONCURRENCY=0`（默认，不限）时 `workers` 会变成 0：`ahead // workers`
   会 ZeroDivisionError；而 `max(1, ...)` 又会把「不限」谎报成 1。
3) `started_at` 只记首次尝试 —— 否则换号重试会把出片耗时均值（ETA）拉长。
"""
from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

import config
import store as store_mod


@pytest.fixture(autouse=True)
def _stubs():
    """注入桩模块，避免依赖 patchright / 真实上游（与 test_queue_progress 同一套）。"""
    sys.modules.setdefault("patchright", types.ModuleType("patchright"))
    pa = types.ModuleType("patchright.async_api")

    def _no_playwright(*_a, **_k):
        raise RuntimeError("stub")

    pa.async_playwright = _no_playwright
    sys.modules.setdefault("patchright.async_api", pa)

    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError",
                "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))
    v.generate_video = _no_playwright
    v.resume_video = _no_playwright
    sys.modules["video_worker_ui"] = v

    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d
    yield


def _run(coro):
    """不用 asyncio.run：它会关掉主线程默认事件循环，带崩其它用 get_event_loop 的测试。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


CLIENT = {"api_key_hash": "h", "api_key_name": "t", "concurrency_limit": 0}


async def _build(tmp_path, monkeypatch, max_concurrency, n=3):
    """在事件循环内建号池 —— asyncio.Semaphore/Lock 绑定 loop，必须和用例同一个 loop。"""
    # 先把落盘路径挪到 tmp，避免「本用例恰好是第一个 import server 的」时在仓库根拉屎
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)

    import browser_pool
    import server

    accounts_dir = tmp_path / "accounts"
    names = [f"acc{i}" for i in range(1, n + 1)]
    for name in names:
        (accounts_dir / name).mkdir(parents=True, exist_ok=True)

    bp = browser_pool.BrowserPool(
        accounts_dir=str(accounts_dir),
        db_path=str(tmp_path / "pool.db"),
        max_concurrency=max_concurrency,
    )
    for name in names:
        bp.set_login_status(name, True)   # -> list_accounts() 里是 healthy

    monkeypatch.setattr(server, "pool", bp)
    monkeypatch.setattr(server, "store",
                        store_mod.TaskStore(str(tmp_path / "tasks.db")))
    monkeypatch.setattr(config, "MAX_CONCURRENCY", max_concurrency)
    return bp, server


def _submit(server, i):
    """入队 + 后台跑（与 _submit_task 的后半段一致），返回 task_id。"""
    tid = f"video_c{i}"
    server.store.create(tid, "seedance-2.5", f"prompt {i}", "default", 10,
                        reference_images="[]", api_key_hash="h", api_key_name="t")
    # ensure_future 而非 create_task：这里在协程内调用，直接调度即可
    asyncio.ensure_future(
        server._run_task(tid, "seedance-2.5", f"prompt {i}", None, 10, [], CLIENT))
    return tid


def _statuses(server, ids):
    return {t: server.store.get(t)["status"] for t in ids}


def _gated(bp, gate, calls, fail_with=TimeoutError):
    """把出片换成「卡在闸门上」——好让用例精确控制并发槽的占用时机。

    超过 max_attempts 的换号由 BrowserPool 自己处理；这里只在闸门放行后失败，
    这样任务会走终态、不需要 _video_metadata，也就不依赖真实成片文件。
    """
    async def fake(account, prompt, ratio, duration, model, *,
                   on_conversation_id, on_poll, on_balance,
                   reference_image_paths):
        calls.append(account)
        await gate.wait()
        raise fail_with("test-gate")

    bp._generate_effective = fake


# --------------------------------------------------------------------------- #
# 并发槽 = 0（不限）
# --------------------------------------------------------------------------- #

def test_unlimited_concurrency_lets_every_task_start(tmp_path, monkeypatch):
    """DOLA_MAX_CONCURRENCY=0：闸门换成 _UnlimitedSemaphore，3 个任务同时开跑。"""
    import browser_pool

    async def scenario():
        bp, server = await _build(tmp_path, monkeypatch, 0)
        gate, calls = asyncio.Event(), []
        _gated(bp, gate, calls)

        assert isinstance(bp.semaphore, browser_pool._UnlimitedSemaphore)

        ids = [_submit(server, i) for i in range(3)]
        await asyncio.sleep(0.3)

        st = _statuses(server, ids)
        assert set(st.values()) == {"processing"}, st
        assert len(calls) == 3, calls      # 三个都拿到了槽位，没有互相排队

        gate.set()
        await asyncio.sleep(0.3)

    _run(scenario())


def test_progress_payload_survives_unlimited_workers(tmp_path, monkeypatch):
    """回归：不限并发（workers=0）时 ahead // workers 不能除零，也不能虚报成 1。"""

    async def scenario():
        _, server = await _build(tmp_path, monkeypatch, 0)

        payload = server._progress_payload(
            status="queued", created_at=time.time(), duration=10)
        assert payload["workers"] == 0          # 0 = 不限
        assert payload["eta_seconds"] > 0
        assert payload["message"].startswith("排队中，预计")

    _run(scenario())


def test_health_queue_reports_unlimited_workers(tmp_path, monkeypatch):
    """GET /health 的 queue.workers 在「不限」时必须是 0（= 不限），不是 1。"""

    async def scenario():
        _, server = await _build(tmp_path, monkeypatch, 0)

        data = await server.health()
        assert data["queue"]["workers"] == 0
        assert data["queue"]["queued"] == 0
        assert data["queue"]["running"] == 0

    _run(scenario())


# --------------------------------------------------------------------------- #
# 并发槽 = 1：本用例是这次修复的核心回归
# --------------------------------------------------------------------------- #

def test_limited_concurrency_keeps_waiting_tasks_queued(tmp_path, monkeypatch):
    """3 个任务抢 1 个并发槽：只有拿到槽位的是 processing，其余必须还是 queued。

    修复前 _run_task 在 pool.generate_video 之前就写 processing，于是这里会看到
    processing/processing/processing —— 2 个任务一个槽都没拿到却报成在跑。
    """

    async def scenario():
        bp, server = await _build(tmp_path, monkeypatch, 1)
        gate, calls = asyncio.Event(), []
        _gated(bp, gate, calls)

        ids = [_submit(server, i) for i in range(3)]
        await asyncio.sleep(0.4)

        st = _statuses(server, ids)
        running = [t for t, v in st.items() if v == "processing"]
        queued = [t for t, v in st.items() if v == "queued"]
        assert len(running) == 1, st        # 只有一个真正拿到槽位
        assert len(queued) == 2, st         # 其余必须保持 queued，不能谎报 processing
        assert len(calls) == 1, calls       # 出片函数只被调了一次

        # /health 的口径必须和上面一致
        data = await server.health()
        assert data["queue"]["running"] == 1
        assert data["queue"]["queued"] == 2

        # 放闸 -> 槽位释放 -> 队列要能继续流动（不能卡死）
        gate.set()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if all(v != "queued" for v in _statuses(server, ids).values()):
                break
        assert set(_statuses(server, ids).values()) == {"failed"}, _statuses(server, ids)
        assert len(calls) == 3, calls

    _run(scenario())


# --------------------------------------------------------------------------- #
# started_at 只记首次尝试
# --------------------------------------------------------------------------- #

def test_started_at_is_not_pushed_back_by_account_switch(tmp_path, monkeypatch):
    """换号重试时 started_at 必须保持首次尝试的时间，否则 ETA 均值被拉长。"""

    async def scenario():
        bp, server = await _build(tmp_path, monkeypatch, 0)
        import browser_pool

        seen = []
        holder = {}

        async def fake(account, prompt, ratio, duration, model, *,
                       on_conversation_id, on_poll, on_balance,
                       reference_image_paths):
            row = server.store.get(holder["id"])
            seen.append((account, row["started_at"]))
            if len(seen) == 1:
                await asyncio.sleep(0.05)       # 让时间明确往前走
                raise browser_pool.CreditInsufficientError("test")   # 触发换号
            raise TimeoutError("test")

        bp._generate_effective = fake
        holder["id"] = _submit(server, 0)

        for _ in range(40):
            await asyncio.sleep(0.05)
            if server.store.get(holder["id"])["status"] in ("completed", "failed"):
                break

        assert len(seen) >= 2, seen             # 确实发生了换号重试
        assert seen[0][0] != seen[1][0], seen   # 第二次换了另一个号
        assert seen[0][1] > 0                   # started_at 有被写进去
        assert seen[1][1] == seen[0][1], seen   # 但换号没有把它往后推

    _run(scenario())
