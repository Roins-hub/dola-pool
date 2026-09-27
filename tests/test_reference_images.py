"""参考图纯 API 补丁单测：上传段直连回落 + guard 已解除。用假 dola，不碰真实上游。"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import pure_api_gen


class Img:
    def __init__(self, name): self.name = name


class UploadError(Exception):
    pass


class FakeDola:
    """只实现 _upload_reference_images / generate_pure_video 用到的协议方法。"""

    def __init__(self, fail_first=None):
        self.fail_first = fail_first
        self.upload_calls = []
        self.setup_calls = []
        self.closed = 0

    def normalize_image_paths(self, paths):
        return [Img(f"img{i}.png") for i, _ in enumerate(paths, 1)]

    def upload_image(self, session, ctx, headers, img):
        self.upload_calls.append((session, img.name))
        if isinstance(self.fail_first, BaseException):
            exc, self.fail_first = self.fail_first, None
            raise exc
        if self.fail_first is False:
            raise UploadError("参考图上传失败：403 tunnel connection failed")
        return {"uri": f"uri://{img.name}"}

    def setup_session(self, **kwargs):
        self.setup_calls.append(kwargs)
        session = types.SimpleNamespace(close=self._close)
        return session, "ctx", {"h": "1"}

    def _close(self):
        self.closed += 1

    def enrich_multi_image_prompt(self, prompt, count):
        return f"{prompt} [images={count}]"


@pytest.fixture()
def proxy_block_env(monkeypatch):
    monkeypatch.setattr(pure_api_gen.config, "REFERENCE_UPLOAD_DIRECT_FALLBACK", True)


def test_upload_ok_without_fallback(proxy_block_env):
    dola = FakeDola()
    out = pure_api_gen._upload_reference_images(
        dola, "s", "c", "h", ["a", "b"],
        state_file=Path("state.json"), region="jp", pc_version="1.0")
    assert [i["uri"] for i in out] == ["uri://img1.png", "uri://img2.png"]
    assert dola.setup_calls == []
    assert dola.closed == 0


def test_proxy_403_falls_back_to_direct(proxy_block_env):
    dola = FakeDola(fail_first=UploadError("HTTPSConnectionPool: 403 CONNECT tunnel connection failed"))
    out = pure_api_gen._upload_reference_images(
        dola, "s", "c", "h", ["a"],
        state_file=Path("state.json"), region="jp", pc_version="1.0")
    assert [i["uri"] for i in out] == ["uri://img1.png"]
    assert len(dola.setup_calls) == 1
    assert dola.setup_calls[0]["proxy"] == ""      # 只有上传段换直连
    assert dola.closed == 1                          # 回落后关掉临时会话
    assert dola.upload_calls[0][0] == "s" and dola.upload_calls[1][0] != "s"


def test_non_proxy_error_does_not_fallback(proxy_block_env):
    dola = FakeDola(fail_first=ValueError("文件不是图片: /tmp/x.txt"))
    with pytest.raises(pure_api_gen.PureApiError) as excinfo:
        pure_api_gen._upload_reference_images(
            dola, "s", "c", "h", ["a"],
            state_file=Path("state.json"), region="jp", pc_version="1.0")
    assert "参考图上传失败" in str(excinfo.value)
    assert dola.setup_calls == []


def test_fallback_disabled_by_config(monkeypatch):
    monkeypatch.setattr(pure_api_gen.config, "REFERENCE_UPLOAD_DIRECT_FALLBACK", False)
    dola = FakeDola(fail_first=UploadError("403 CONNECT tunnel connection failed"))
    with pytest.raises(pure_api_gen.PureApiError):
        pure_api_gen._upload_reference_images(
            dola, "s", "c", "h", ["a"],
            state_file=Path("state.json"), region="jp", pc_version="1.0")
    assert dola.setup_calls == []


def test_direct_fallback_failure_reported(proxy_block_env):
    dola = FakeDola(fail_first=UploadError("403 CONNECT tunnel connection failed"))

    def always_fail(_session, _img):
        raise UploadError("直连也不通")
    dola.upload_image = lambda session, ctx, headers, img: always_fail(session, img)

    with pytest.raises(pure_api_gen.PureApiError) as excinfo:
        pure_api_gen._upload_reference_images(
            dola, "s", "c", "h", ["a"],
            state_file=Path("state.json"), region="jp", pc_version="1.0")
    assert "参考图上传失败" in str(excinfo.value)


def test_generate_pure_video_no_longer_rejects_reference_images(tmp_path, monkeypatch):
    """guard 已解除：带参考图时会走到上传链路（这里让它在上传处故意失败以证明已到达）。"""
    state = tmp_path / "cookie_state.json"
    state.write_text(json.dumps({"cookies": [{"name": "a", "value": "b"}]}), encoding="utf-8")

    dola = FakeDola(fail_first=UploadError("参考图上传失败：403 tunnel connection failed"))

    def fail_direct(_session, _img):
        raise UploadError("直连也不通")
    dola.upload_image = lambda session, ctx, headers, img: fail_direct(session, img)
    dola.warmup_login_session = lambda *a, **k: None
    dola.dola_api_post_json = lambda *a, **k: {}

    monkeypatch.setattr(pure_api_gen, "_dola", lambda: dola)
    monkeypatch.setattr(pure_api_gen, "install", lambda: None)

    with pytest.raises(pure_api_gen.PureApiError) as excinfo:
        pure_api_gen.generate_pure_video(
            account="acc1", prompt="p", ratio="16:9", duration=10, model="seedance-2.0",
            cookie_state_path=state, proxy="http://127.0.0.1:1080",
            reference_image_paths=["a"], timeout=5)
    msg = str(excinfo.value)
    assert "暂不支持参考图片" not in msg
    assert "参考图上传失败" in msg
