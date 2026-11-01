"""浏览器参考图上传：字节图片上传服务器直连，其余流量仍走账号代理。"""
import browser
import config


def test_bypass_added_to_account_proxy(monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_PROXY_BYPASS", "*.bytevcloudapi.com,*.vodupload.com")
    monkeypatch.setattr(browser.proxy_store, "proxy_url_for", lambda name: "http://u:p@1.2.3.4:8080")
    kw = browser.proxy_kwargs_for("acc1")
    assert kw["server"] == "http://1.2.3.4:8080"
    assert kw["bypass"] == "*.bytevcloudapi.com,*.vodupload.com"


def test_bypass_off_when_empty(monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_PROXY_BYPASS", "")
    monkeypatch.setattr(browser.proxy_store, "proxy_url_for", lambda name: "http://1.2.3.4:8080")
    assert "bypass" not in browser.proxy_kwargs_for("acc1")


def test_no_proxy_stays_none(monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_PROXY_BYPASS", "*.bytevcloudapi.com")
    monkeypatch.setattr(browser.proxy_store, "proxy_url_for", lambda name: "")
    monkeypatch.setattr(config, "PROXY", "")
    assert not browser.proxy_kwargs_for("acc1")


def test_upload_host_bypassed(monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_PROXY_BYPASS", "*.bytevcloudapi.com, *.vodupload.com")
    assert browser.upload_host_bypassed("imagex-ap-southeast-1.bytevcloudapi.com")
    assert browser.upload_host_bypassed("tos-sg16-share.vodupload.com")
    assert not browser.upload_host_bypassed("www.dola.com")
    assert not browser.upload_host_bypassed("evilbytevcloudapi.com")
    assert not browser.upload_host_bypassed("")
    monkeypatch.setattr(config, "UPLOAD_PROXY_BYPASS", "")
    assert not browser.upload_host_bypassed("imagex-ap-southeast-1.bytevcloudapi.com")
