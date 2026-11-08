"""CloakBrowser 也让参考图上传直连，工作模式等上传的时间按文件大小算（2026-11-08）。

起因：142.249.204.x 静态代理约 50 KB/s，工作模式在 CloakBrowser 里经代理上传参考图，
60 秒传不完就换号，连续几个号都这样；patchright 浏览器早就让上传域名直连了。
"""
import pytest

import config
from browser import cloak_proxy_settings, proxy_kwargs_for
from work_reply import upload_wait_seconds

BYPASS = "*.bytevcloudapi.com,*.vodupload.com"


@pytest.fixture(autouse=True)
def bypass(monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_PROXY_BYPASS", BYPASS)


def test_local_relay_gets_upload_bypass():
    # 工作模式传的是号池本地中转（无鉴权 HTTP）
    assert cloak_proxy_settings("http://127.0.0.1:18080") == {
        "server": "http://127.0.0.1:18080", "bypass": BYPASS}


def test_socks5_credentials_survive_and_get_bypass():
    # 严格隔离直接把带密码的 socks5 交给 CloakBrowser，它自己拼回 --proxy-server
    assert cloak_proxy_settings("socks5://user%40x:p%3Ass@gate.example:3000") == {
        "server": "socks5://gate.example:3000", "username": "user@x", "password": "p:ss",
        "bypass": BYPASS}


def test_http_proxy_default_port():
    assert cloak_proxy_settings("http://proxy.example")["server"] == "http://proxy.example:80"


def test_no_proxy_stays_none():
    assert cloak_proxy_settings(None) is None
    assert cloak_proxy_settings("") is None


def test_bypass_switched_off_keeps_plain_url(monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_PROXY_BYPASS", "")
    assert cloak_proxy_settings("http://127.0.0.1:18080") == "http://127.0.0.1:18080"


def test_same_bypass_as_patchright(monkeypatch):
    import proxy_store
    monkeypatch.setattr(proxy_store, "proxy_url_for", lambda account: "http://proxy.example:8080")
    assert proxy_kwargs_for("acc1")["bypass"] == cloak_proxy_settings("http://proxy.example:8080")["bypass"]


@pytest.mark.parametrize("size,expected", [
    (0, 60),
    (500 * 1024, 60),
    (2 * 1024 * 1024, 60),
    (3 * 1024 * 1024, 90),
    (int(3.2 * 1024 * 1024), 96),
    (5 * 1024 * 1024, 150),
    (20 * 1024 * 1024, 180),
])
def test_upload_wait_scales_with_size(size, expected):
    assert upload_wait_seconds(size) == expected
