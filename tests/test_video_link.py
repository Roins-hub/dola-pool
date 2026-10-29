"""DOLA_VIDEO_DELIVERY=link: stable /v/<task>.mp4 redirects to the upstream CDN, re-signed before expiry."""
import asyncio
import json
import re
import sys
import time
import types
from types import SimpleNamespace

import pytest

import video_link
from test_task_deadline import _run

SIGNED = "https://v16-dola.dola.com/0123456789abcdef0123456789abcdef/{:08x}/video/tos/mya/x/y/?a=1&lr=unwatermarked"
VIDEO_MODEL = json.dumps({"video_duration": 10.08, "video_list": {
    "video_1": {"vwidth": 1280, "vheight": 720, "bitrate": 1299506}}})


def test_expiry_is_read_from_the_cdn_path():
    at = int(time.time()) + 6 * 3600
    assert video_link.link_expiry(SIGNED.format(at)) == float(at)
    assert video_link.link_expiry("https://v16-dola.dola.com/videos/plain.mp4") is None


def test_metadata_comes_from_the_video_model_and_probed_size():
    meta = video_link.meta_from_model(VIDEO_MODEL, 5_897_617, 10, "16:9")
    assert (meta["video_width"], meta["video_height"], meta["video_duration"]) == (1280, 720, 10.08)
    assert meta["video_size"] == 5_897_617 and meta["video_bitrate"] == int(5_897_617 * 8 / 10.08)
    assert video_link.meta_from_model("", None, 5, None)["video_duration"] == 5.0


class Store:
    def __init__(self, row): self.row = dict(row); self.updates = []
    def get(self, task_id): return dict(self.row)
    def update(self, task_id, **fields): self.updates.append(fields); self.row.update(fields)


def _stub_worker(monkeypatch, new_url):
    calls = []
    async def fetch(vm, account):
        calls.append((vm, account))
        return new_url
    monkeypatch.setitem(sys.modules, "video_worker_ui", types.SimpleNamespace(fetch_higher_bitrate_url=fetch))
    return calls


def test_fresh_link_is_served_as_is(monkeypatch):
    calls = _stub_worker(monkeypatch, "unused")
    link = SIGNED.format(int(time.time()) + 5 * 3600)
    store = Store({"id": "t1", "video_link": link, "video_link_expires_at": video_link.link_expiry(link)})
    assert _run(video_link.current_link(store.row, store)) == link
    assert calls == [] and store.updates == []


def test_link_close_to_expiry_is_re_signed_with_the_stored_model(monkeypatch):
    new = SIGNED.format(int(time.time()) + 6 * 3600)
    calls = _stub_worker(monkeypatch, new)
    old = SIGNED.format(int(time.time()) + 300)
    store = Store({"id": "t2", "video_link": old, "video_link_expires_at": video_link.link_expiry(old),
                   "video_model": VIDEO_MODEL, "account": "acc13"})
    assert _run(video_link.current_link(store.row, store)) == new
    assert calls == [(VIDEO_MODEL, "acc13")]
    assert store.row["video_link_expires_at"] == video_link.link_expiry(new)


def test_failed_re_sign_keeps_serving_the_old_link(monkeypatch):
    _stub_worker(monkeypatch, None)
    old = SIGNED.format(int(time.time()) + 300)
    store = Store({"id": "t3", "video_link": old, "video_link_expires_at": video_link.link_expiry(old),
                   "video_model": VIDEO_MODEL, "account": "acc13"})
    assert _run(video_link.current_link(store.row, store)) == old


def test_short_link_route_redirects_without_proxying(tmp_path, monkeypatch):
    import server
    from store import TaskStore
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server, "store", TaskStore(str(tmp_path / "tasks.db")))
    link = SIGNED.format(int(time.time()) + 5 * 3600)
    server.store.create("video_link1", "seedance-2.0-fast", "p", "16:9", 10)
    server.store.update("video_link1", status="completed", video_url="http://x/v/video_link1.mp4",
                        video_link=link, video_link_expires_at=video_link.link_expiry(link), video_model=VIDEO_MODEL)
    server.store.create("video_file1", "seedance-2.0-fast", "p", "16:9", 10)
    server.store.update("video_file1", status="completed", video_url="http://x/videos/a.mp4")
    client = TestClient(server.app)
    response = client.get("/v/video_link1.mp4", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == link
    assert client.get("/v/video_file1.mp4", follow_redirects=False).status_code == 404
    assert client.get("/v/missing.mp4", follow_redirects=False).status_code == 404


def test_link_delivery_returns_without_downloading(monkeypatch):
    from browser_feedback import emit
    from test_task_ui_regressions import load_worker_function
    import config
    monkeypatch.setattr(config, "VIDEO_DELIVERY", "link")
    link = SIGNED.format(int(time.time()) + 6 * 3600)
    monkeypatch.setattr(video_link, "probe_direct", lambda url: {"size": 123, "expires_at": video_link.link_expiry(url)})
    class Page:
        async def evaluate(self, *a):
            return {"latestAssistant": {"index": "7", "text": "", "finished": True, "resCode": "0",
                                        "tools": [{"status": 4, "failCode": "0"}]},
                    "videos": ["https://cdn.invalid/watermarked.mp4"], "videoModels": [VIDEO_MODEL],
                    "texts": [], "assistantTexts": []}
        async def cookies(self, *a): return []
    async def no_wait(*a): pass
    async def hi(vm, account): return link
    async def must_not_download(*a): raise AssertionError("link delivery must not download")
    fn = load_worker_function("poll_conversation", cookie_value=lambda *a: "", emit_feedback=emit,
        asyncio=SimpleNamespace(sleep=no_wait, wait_for=asyncio.wait_for, to_thread=asyncio.to_thread),
        POLL_JS="", DAILY_LIMIT_PATTERN=re.compile("x^"), FREE_LIMIT_REPLY=re.compile("x^"), _parse_balance_texts=lambda x: (None, None, ""),
        fetch_higher_bitrate_url=hi, extract_unwatermarked_url=lambda vm, url: url, _download=must_not_download,
        CreditError=RuntimeError, AccountLimitedError=RuntimeError)
    page = Page()
    result = _run(fn("acc13", page, page, "c1", 1800, duration=10))
    assert result["delivery"] == "link" and result["video_url"] == link and result["local_path"] == ""
    assert result["video_model"] == VIDEO_MODEL and result["video_size"] == 123
