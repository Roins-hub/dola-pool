"""内联参考图（data: URL）必须落盘：几 MB base64 写进 tasks.db 会把面板顶死。

背景：画布/客户端会把参考图内联成 data: URL 传进来，早期实现直接把它塞进
tasks.reference_images —— 27 条任务就把库撑到 587MB，`/api/admin/videos` 序列化时
MemoryError、整个面板卡死。这里锁住「落盘 + 库里只存路径」的行为。
"""
from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path

import pytest
from PIL import Image

import config
import media


def _png_bytes(size: int = 48) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _data_url(data: bytes, mime: str = "image/png") -> str:
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode())


@pytest.fixture()
def refs_dir(tmp_path, monkeypatch):
    target = tmp_path / "refs"
    monkeypatch.setattr(config, "REFERENCE_FILE_DIR", str(target))
    return target


def _run(coro):
    # 不用 asyncio.run：它会关掉主线程默认事件循环，带崩其它用 get_event_loop 的测试
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_data_url_is_written_to_disk(refs_dir):
    data = _png_bytes()
    out = media.materialize_data_urls([_data_url(data)])
    assert len(out) == 1
    path = Path(out[0])
    assert path.is_file() and path.suffix == ".png"
    assert path.read_bytes() == data
    # 存进 DB 的是短路径，不是几 MB 的 base64
    assert len(out[0]) < 200
    assert "base64" not in out[0]


def test_http_urls_pass_through(refs_dir):
    url = "https://example.com/a.png"
    assert media.materialize_data_urls([url]) == [url]


def test_total_size_cap(refs_dir, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_TOTAL_MAX_BYTES", 10)
    with pytest.raises(ValueError):
        media.materialize_data_urls([_data_url(_png_bytes())])


def test_local_reference_is_accepted_and_reused(refs_dir):
    path = media.materialize_data_urls([_data_url(_png_bytes())])[0]

    normalized = _run(media.validate_reference_urls([path]))
    assert normalized == [str(Path(path).resolve())]

    root, paths = _run(media.download_reference_images([path], "task-x"))
    assert root is None            # 不用建临时目录
    assert paths == [str(Path(path).resolve())]
    assert Path(paths[0]).is_file()


def test_cleanup_only_removes_our_files(refs_dir, tmp_path):
    path = media.materialize_data_urls([_data_url(_png_bytes())])[0]
    outsider = tmp_path / "keep.png"
    outsider.write_bytes(b"x")
    media.cleanup_local_references([path, str(outsider)])
    assert not Path(path).exists()
    assert outsider.is_file()


def test_sweep_removes_stale_files(refs_dir):
    import os
    import time

    path = Path(media.materialize_data_urls([_data_url(_png_bytes())])[0])
    os.utime(path, (time.time() - 100000, time.time() - 100000))
    assert media.sweep_stale_references(max_age_seconds=3600) == 1
    assert not path.exists()


def test_missing_local_reference_says_file_lost(refs_dir):
    """落盘文件没了要报「文件已丢失」，别再报成「只支持 http/https 公网 URL」。

    线上就是这样误导过一次：任务被重排/清理后再跑，用户看到的是格式错误提示。
    """
    ghost = "refs/ref_notexist_0.png"
    with pytest.raises(ValueError, match="文件已丢失"):
        _run(media.validate_reference_urls([ghost]))
    with pytest.raises(ValueError, match="文件已丢失"):
        media.validate_public_url(ghost)


def test_local_reference_survives_task_end_until_cleaned(refs_dir):
    """任务非终态时不删文件（重启后要留给 resume 用），终态才清。"""
    path = media.materialize_data_urls([_data_url(_png_bytes())])[0]
    assert Path(path).is_file()
    # 模拟服务重启后重新校验：文件还在 → 校验通过
    assert _run(media.validate_reference_urls([path])) == [str(Path(path).resolve())]
    media.cleanup_local_references([path])
    assert not Path(path).exists()
    with pytest.raises(ValueError, match="文件已丢失"):
        _run(media.validate_reference_urls([path]))
