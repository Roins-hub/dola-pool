"""gzip 中间件：大 JSON / 页面压缩；视频、Range、小响应、不接受 gzip 的请求原样返回。"""
import gzip

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.testclient import TestClient

from compress import GzipTextMiddleware


def make_client(tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"\x00" * 50_000)
    app = FastAPI()
    app.add_middleware(GzipTextMiddleware)

    @app.get("/big")
    def big():
        return {"rows": [{"id": i, "prompt": "清晨的湖面" * 5} for i in range(300)]}

    @app.get("/small")
    def small():
        return {"ok": True}

    @app.get("/page")
    def page():
        return HTMLResponse("<p>dola</p>" * 500)

    @app.get("/video")
    def get_video():
        return FileResponse(video, media_type="video/mp4")

    return TestClient(app)


def raw_get(client, path, **headers):
    with client.stream("GET", path, headers=headers) as r:
        return r, b"".join(r.iter_raw())


def test_large_json_and_html_are_gzipped(tmp_path):
    client = make_client(tmp_path)
    for path in ("/big", "/page"):
        r, body = raw_get(client, path, **{"Accept-Encoding": "gzip, br"})
        assert r.headers["content-encoding"] == "gzip" and "Accept-Encoding" in r.headers["vary"]
        assert int(r.headers["content-length"]) == len(body)
        assert len(gzip.decompress(body)) > len(body) * 3
    assert client.get("/big").json()["rows"][299]["id"] == 299          # 客户端照常解出


def test_small_video_range_and_plain_clients_are_untouched(tmp_path):
    client = make_client(tmp_path)
    r, _ = raw_get(client, "/small", **{"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers
    r, body = raw_get(client, "/video", **{"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers and len(body) == 50_000
    r, body = raw_get(client, "/video", **{"Accept-Encoding": "gzip", "Range": "bytes=0-99"})
    assert r.status_code == 206 and "content-encoding" not in r.headers and len(body) == 100
    r, body = raw_get(client, "/big", **{"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers and body.startswith(b'{"rows"')
