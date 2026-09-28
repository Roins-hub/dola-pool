"""参考图缩略图：任务终态会把原图清掉，面板回看只能靠这份小图。"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

import config
from media import delete_reference_thumbnails, save_reference_thumbnails, thumb_file


def _make(path: Path, size=(800, 400)) -> Path:
    Image.new("RGB", size, (10, 120, 200)).save(path)
    return path


def test_saves_only_first_thumbnail_and_resizes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(config, "REFERENCE_THUMB_COUNT", 1)
    monkeypatch.setattr(config, "REFERENCE_THUMB_MAX_PX", 64)

    first = _make(tmp_path / "a.png")
    second = _make(tmp_path / "b.png")
    names = save_reference_thumbnails([str(first), str(second)], "video_abc")

    assert len(names) == 1                       # 默认只留第一张，控制磁盘占用
    out = Path(config.THUMB_DIR) / names[0]
    assert out.is_file()
    with Image.open(out) as img:
        assert max(img.size) <= 64               # 已缩到上限以内


def test_count_zero_disables_thumbnails(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(config, "REFERENCE_THUMB_COUNT", 0)
    assert save_reference_thumbnails([str(_make(tmp_path / "a.png"))], "t") == []


def test_missing_source_is_skipped_not_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(config, "REFERENCE_THUMB_COUNT", 3)
    monkeypatch.setattr(config, "REFERENCE_THUMB_MAX_PX", 32)

    good = _make(tmp_path / "good.png")
    names = save_reference_thumbnails(
        [str(tmp_path / "nope.png"), str(good)], "video_x")
    assert len(names) == 1                       # 坏图跳过，不影响出片主流程


def test_thumb_file_stays_inside_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", str(tmp_path / "thumbs"))
    Path(config.THUMB_DIR).mkdir(parents=True, exist_ok=True)
    root = Path(config.THUMB_DIR).resolve()

    assert thumb_file("a.jpg").parent == root
    # 目录穿越只取文件名，落点仍在 THUMB_DIR 内
    assert thumb_file("../../etc/passwd").parent == root
    assert thumb_file("") is None


def test_delete_removes_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(config, "REFERENCE_THUMB_COUNT", 1)
    names = save_reference_thumbnails([str(_make(tmp_path / "a.png"))], "video_del")
    out = Path(config.THUMB_DIR) / names[0]
    assert out.is_file()

    assert delete_reference_thumbnails(names) == 1
    assert not out.exists()
    assert delete_reference_thumbnails(names) == 0     # 再删一次不报错
