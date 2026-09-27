"""TaskStore 的认领/恢复语义验证（api-pool job store 移植）。"""
from __future__ import annotations

from store import TaskStore


def _job(store: TaskStore, job_id: str, account: str | None = None, status: str = "queued",
         conversation_id: str | None = None):
    store.create(
        job_id,
        "seedance_v2.0",
        "a cat",
        "9:16",
        5,
        account=account,
    )
    if status == "processing":
        store.update(job_id, status="processing", account=account)
    if conversation_id:
        store.update(job_id, conversation_id=conversation_id)


def test_claim_is_sticky(tmp_path):
    store = TaskStore(str(tmp_path / "jobs.db"))
    _job(store, "video_1")
    claimed = store.claim("acc1")
    assert claimed["id"] == "video_1"
    assert claimed["status"] == "processing"
    assert claimed["account"] == "acc1"
    assert store.claim("acc1") is None


def test_waiting_unassigned(tmp_path):
    store = TaskStore(str(tmp_path / "jobs.db"))
    _job(store, "video_1")
    _job(store, "video_2", account="acc1", status="processing")
    waiting = store.waiting_unassigned()
    assert [job["id"] for job in waiting] == ["video_1"]


def test_busy_account_ids(tmp_path):
    store = TaskStore(str(tmp_path / "jobs.db"))
    _job(store, "video_1", account="acc2", status="processing")
    assert store.busy_account_ids() == {"acc2"}


def test_recover_runtime_keeps_conversation(tmp_path):
    store = TaskStore(str(tmp_path / "jobs.db"))
    _job(store, "video_resume", account="acc1", status="processing", conversation_id="c-live")
    _job(store, "video_fresh", account="acc2", status="processing")
    recovered = store.recover_runtime_jobs()
    assert set(recovered) == {"video_resume", "video_fresh"}
    kept = store.get("video_resume")
    assert kept["status"] == "queued"
    assert kept["account"] == "acc1"
    assert kept["conversation_id"] == "c-live"
    fresh = store.get("video_fresh")
    assert fresh["status"] == "queued"
    assert fresh["account"] is None
