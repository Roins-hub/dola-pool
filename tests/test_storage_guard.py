"""存储防膨胀：写库截断（兜底）+ 保留期清理 + 出片目录体积上限。

对应 2026-09-22 事故：27 条任务的 reference_images 存了 523MB base64，
面板列表接口序列化整表时 MemoryError、服务卡死。
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

import config
import store as store_mod


def _store(tmp_path) -> store_mod.TaskStore:
    return store_mod.TaskStore(str(tmp_path / "tasks.db"))


def test_create_clamps_oversized_fields(tmp_path):
    st = _store(tmp_path)
    huge_refs = '["data:image/png;base64,' + "A" * 200_000 + '"]'
    st.create("t1", "seedance-2.5", "p" * 50_000, "16:9", 10,
              reference_images=huge_refs, api_key_name="k" * 5_000)
    row = st.get("t1")
    assert len(row["prompt"]) <= store_mod.TaskStore._FIELD_LIMITS["prompt"] + 20
    assert len(row["reference_images"]) <= \
        store_mod.TaskStore._FIELD_LIMITS["reference_images"] + 20
    assert len(row["api_key_name"]) <= \
        store_mod.TaskStore._FIELD_LIMITS["api_key_name"] + 20
    # 关键：单行不再可能几 MB
    assert len(row["reference_images"]) < 10_000


def test_update_clamps_error_field(tmp_path):
    st = _store(tmp_path)
    st.create("t1", "seedance-2.5", "p", "16:9", 10)
    st.update("t1", status="failed", error="E" * 100_000)
    row = st.get("t1")
    assert len(row["error"]) <= store_mod.TaskStore._FIELD_LIMITS["error"] + 20


def test_prune_finished_removes_only_old_finished(tmp_path, monkeypatch):
    st = _store(tmp_path)
    st.create("old_done", "seedance-2.5", "p", "16:9", 10)
    st.update("old_done", status="completed", video_url="http://x/videos/a.mp4",
              finished_at=time.time() - 30 * 86400)
    st.create("fresh_done", "seedance-2.5", "p", "16:9", 10)
    st.update("fresh_done", status="completed", finished_at=time.time())
    st.create("old_running", "seedance-2.5", "p", "16:9", 10)
    st.update("old_running", status="processing", started_at=time.time() - 30 * 86400)

    removed = st.prune_finished(14)
    ids = [r["id"] for r in removed]
    assert ids == ["old_done"]
    assert removed[0]["video_url"].endswith("a.mp4")
    assert st.get("fresh_done") is not None      # 未过保留期
    assert st.get("old_running") is not None     # 运行中的不删
    assert st.get("old_done") is None


def test_prune_disabled_when_retention_zero(tmp_path):
    st = _store(tmp_path)
    st.create("t", "seedance-2.5", "p", "16:9", 10)
    st.update("t", status="failed", finished_at=0)
    assert st.prune_finished(0) == []
    assert st.get("t") is not None


def test_storage_stats_reports_size(tmp_path):
    st = _store(tmp_path)
    st.create("t", "seedance-2.5", "p", "16:9", 10)
    stats = st.storage_stats()
    assert stats["tasks"] == 1
    assert stats["db_bytes"] > 0


def test_prune_downloads_drops_oldest(tmp_path, monkeypatch):
    """出片目录超限时按 mtime 从旧到新删。"""
    pa = types.ModuleType("patchright.async_api")

    def _no_playwright(*_a, **_k):
        raise RuntimeError("stub")

    pa.async_playwright = _no_playwright
    pr = types.ModuleType("patchright")
    pr.async_api = pa
    sys.modules.setdefault("patchright", pr)
    sys.modules.setdefault("patchright.async_api", pa)

    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError", "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))
    v.generate_video = _no_playwright
    v.resume_video = _no_playwright
    sys.modules["video_worker_ui"] = v
    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setattr(config, "DOWNLOAD_MAX_BYTES", 1000)
    import server

    root = Path(config.DOWNLOAD_DIR)
    root.mkdir(parents=True, exist_ok=True)
    for index in range(4):
        path = root / ("old_%d.mp4" % index)
        path.write_bytes(b"x" * 400)
        ts = time.time() - (index + 1) * 60
        import os

        os.utime(path, (ts, ts))

    result = server._prune_downloads()
    assert result["removed"] >= 1
    assert sum(p.stat().st_size for p in root.iterdir()) <= 1000
