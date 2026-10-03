"""Kiểm thử: Dola từ chối nội dung (Content refusal) phải dừng ngay, không đổi account, không phạt quota/risk."""
import asyncio
import time
import pytest
from pathlib import Path
from browser_pool import BrowserPool, ContentRefusalError
from store import TaskStore


class FakeUpstreamRefusalException(Exception):
    def __init__(self, message, status_code="712017001", request_id="req_upstream_abc123"):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


@pytest.mark.asyncio
async def test_content_refusal_stops_immediately_no_rotation(tmp_path, monkeypatch):
    db_path = str(tmp_path / "pool_test.db")
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()

    # Tạo 3 tài khoản trong pool
    (accounts_dir / "acc1").mkdir()
    (accounts_dir / "acc2").mkdir()
    (accounts_dir / "acc3").mkdir()

    pool = BrowserPool(accounts_dir=str(accounts_dir), db_path=db_path)
    for name in ("acc1", "acc2", "acc3"):
        pool.ensure_account(name)
        pool.set_login_status(name, True)

    call_count = 0
    attempted_accounts = []

    async def fake_generate_effective(account, prompt, ratio, duration, **kwargs):
        nonlocal call_count
        call_count += 1
        attempted_accounts.append(account)
        # Giả lập ByteDance trả về lỗi bản quyền/nội dung
        raise FakeUpstreamRefusalException(
            "生成的视频可能涉及版权限制，请更换输入内容后重试",
            status_code="712017001",
            request_id="req_bytedance_999",
        )

    # Monkeypatch hello probe để pass ngay
    async def fake_probe(account):
        return {"ok": True, "probed": False}

    monkeypatch.setattr(pool, "probe_hello_async", fake_probe)
    monkeypatch.setattr(pool, "_generate_effective", fake_generate_effective)

    with pytest.raises(ContentRefusalError) as exc_info:
        await pool.generate_video(
            prompt="Video có yếu tố vi phạm bản quyền",
            duration=30,
            model="seedance-2.5",
            max_attempts=3,
        )

    err = exc_info.value
    assert "版权限制" in str(err)
    assert err.failure_code == "712017001"
    assert err.upstream_request_id == "req_bytedance_999"
    assert err.failure_group == "版权/侵权"

    # QUAN TRỌNG: Chỉ thử đúng 1 lần, KHÔNG đổi sang acc2 hay acc3!
    assert call_count == 1
    assert attempted_accounts == ["acc1"]

    # Tài khoản acc1 KHÔNG bị đánh dấu risk, không bị khóa quota
    acc1_row = next(a for a in pool.list_accounts() if a["name"] == "acc1")
    assert acc1_row.get("risk_control") in (0, None, False)
    assert acc1_row.get("quota_blocked") is False
    assert acc1_row.get("rate_limited") is False


@pytest.mark.asyncio
async def test_run_task_content_refusal_metadata(tmp_path, monkeypatch):
    import server

    db_path = str(tmp_path / "tasks_test.db")
    store = TaskStore(db_path)
    monkeypatch.setattr(server, "store", store)

    task_id = "video_refusal_test_1"
    store.create(
        task_id=task_id,
        model="seedance-2.5",
        prompt="Nội dung vi phạm",
        ratio="16:9",
        duration=30,
        api_key_name="test_admin",
    )

    async def fake_generate_video(*args, **kwargs):
        raise ContentRefusalError(
            "生成的视频可能涉及版权限制，请更换输入内容后重试",
            failure_code="712017001",
            failure_group="版权/侵权",
            upstream_request_id="req_upstream_999",
        )

    monkeypatch.setattr(server.pool, "generate_video", fake_generate_video)

    await server._run_task(
        task_id=task_id,
        model="seedance-2.5",
        prompt="Nội dung vi phạm",
        ratio="16:9",
        duration=30,
        reference_images=[],
        client={"api_key_hash": None, "concurrency_limit": 0},
    )

    task = store.get(task_id)
    assert task["status"] == "failed"
    assert "版权" in task["error"]
    assert task["failure_code"] == "712017001"
    assert task["failure_group"] == "版权/侵权"
    assert task["upstream_request_id"] == "req_upstream_999"
