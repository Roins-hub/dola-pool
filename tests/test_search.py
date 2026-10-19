"""2.4.2：管理面板各页搜索。任务 / 视频走服务端查整个历史。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store as store_mod


def _store(tmp_path):
    s = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    rows = [
        ("video_a1", "一只橘猫在窗台晒太阳", "acc1", "completed"),
        ("video_b2", "海边日落，海浪拍打礁石", "acc2", "failed"),
        ("video_c3", "橘猫追蝴蝶", "acc12", "completed"),
        ("video_d4", "100%_纯色背景", "acc3", "completed"),
    ]
    for i, (tid, prompt, acc, status) in enumerate(rows):
        s.create(tid, "seedance-2.0", prompt, "16:9", 5, account=acc)
        s.update(tid, status=status, created_at=1000 + i)
    return s


def _ids(rows):
    return [r["id"] for r in rows]


def test_search_matches_all_terms_across_columns(tmp_path):
    s = _store(tmp_path)
    assert _ids(s.search_tasks("橘猫")) == ["video_c3", "video_a1"]          # 新的在前
    assert _ids(s.search_tasks("橘猫 acc12")) == ["video_c3"]               # 每个词都要命中
    assert _ids(s.search_tasks("ACC2")) == ["video_b2"]                     # 不区分大小写
    assert _ids(s.search_tasks("failed")) == ["video_b2"]                   # 状态也能搜
    assert s.search_tasks("不存在的词") == []


def test_search_escapes_like_wildcards(tmp_path):
    s = _store(tmp_path)
    # % 和 _ 按字面匹配，不当通配符
    assert _ids(s.search_tasks("100%_")) == ["video_d4"]
    assert _ids(s.search_tasks("%")) == ["video_d4"]


def test_search_status_filter_and_limit(tmp_path):
    s = _store(tmp_path)
    assert s.search_tasks("海", status="completed") == []
    assert len(s.search_tasks("video", limit=2)) == 2
    # 空查询 = 最近的任务
    assert _ids(s.search_tasks("", limit=2)) == ["video_d4", "video_c3"]
