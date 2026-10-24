"""Cookie downloads use authenticated responses, safe snapshots and import-compatible files."""
import asyncio
import io
import json
import time
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from cookie_export import account_bundle, build_export, CookieExportError
from cookie_import import parse_cookie_header_text


def save(root, name="acc1", domain=".doubao.com"):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    cookies = [{"name": "sessionid", "value": "synthetic-session", "domain": domain, "path": "/"},
               {"name": "sessionid", "value": "synthetic-subpath", "domain": domain, "path": "/chat"}]
    (folder / "cookie_state.json").write_text(json.dumps({"cookies_list": cookies,
        "password": "never-export", "storage": {"localStorage": {"secret": "never-export"}}}))
    return cookies


def test_single_roundtrip_and_no_unrelated_secrets(tmp_path):
    cookies = save(tmp_path)
    data, mime, name, count, failed = build_export(tmp_path, ["acc1"], "cloak-profile")
    assert mime == "application/json" and name == "acc1.json" and (count, failed) == (1, 0)
    assert b"never-export" not in data
    assert parse_cookie_header_text(data.decode())[0]["cookies_list"] == cookies


def test_batch_reports_partial_failure_and_keeps_names(tmp_path):
    save(tmp_path, "acc1")
    save(tmp_path, "acc2", ".dola.com")
    data, mime, _, count, failed = build_export(tmp_path, ["acc1", "acc2", "missing"], "cloak-profile")
    assert mime == "application/zip" and (count, failed) == (2, 1)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert set(z.namelist()) == {"acc1.json", "acc2.json", "_export/manifest.json"}
        assert json.loads(z.read("_export/manifest.json"))["failed"][0]["account"] == "missing"
        assert parse_cookie_header_text(z.read("acc2.json").decode())


def test_cloak_snapshot_takes_precedence_including_logout(tmp_path):
    save(tmp_path)
    folder = tmp_path / "acc1" / "cloak-profile"
    folder.mkdir()
    snapshot = folder / ".cloak-session-cookies.json"
    cookie = {"name": "sessionid", "value": "new-synthetic", "domain": ".dola.com", "expires": -1}
    snapshot.write_text(json.dumps([cookie]))
    assert account_bundle(tmp_path, "acc1", "cloak-profile")["cookies"][0]["value"] == "new-synthetic"
    snapshot.write_text("[]")
    with pytest.raises(CookieExportError, match="退出登录"):
        account_bundle(tmp_path, "acc1", "cloak-profile")


def test_bad_paths_and_corruption_do_not_leak(tmp_path):
    save(tmp_path)
    for name in ("../acc1", "a/b", "a\\b", "x\r\n"):
        with pytest.raises(CookieExportError):
            account_bundle(tmp_path, name, "cloak-profile")
    (tmp_path / "acc1" / "cookie_state.json").write_text('{"secret": "DO-NOT-ECHO"')
    with pytest.raises(CookieExportError) as error:
        account_bundle(tmp_path, "acc1", "cloak-profile")
    assert "DO-NOT-ECHO" not in str(error.value)


def test_cookie_domain_filter(tmp_path):
    save(tmp_path, domain="evil-doubao.com")
    with pytest.raises(CookieExportError):
        account_bundle(tmp_path, "acc1", "cloak-profile")


def test_manifest_cannot_overwrite_account_named_like_report(tmp_path):
    save(tmp_path, "_export_manifest")
    save(tmp_path, "acc1")
    data, *_ = build_export(tmp_path, ["_export_manifest", "acc1"], "cloak-profile")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert json.loads(z.read("_export_manifest.json"))["cookies"]
        assert "exported" in json.loads(z.read("_export/manifest.json"))


def test_authenticated_download_and_selection_validation(tmp_path, monkeypatch):
    import server
    save(tmp_path)
    monkeypatch.setattr(server, "pool", SimpleNamespace(accounts=["acc1"], accounts_dir=tmp_path))
    monkeypatch.setattr(server, "ADMIN_SESSIONS", {"test-token": {
        "username": "test-admin", "expires_at": time.time() + 600}})
    monkeypatch.setattr(server, "user_get", lambda _: {"enabled": True})
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as c:
            url = "/api/admin/accounts/export-cookies"
            assert (await c.post(url, json={"names": ["acc1"]})).status_code == 401
            headers = {"X-Admin-Key": "test-token"}
            for names, status in (([], 422), (["../acc1"], 400), (["missing"], 404)):
                assert (await c.post(url, json={"names": names}, headers=headers)).status_code == status
            r = await c.post(url, json={"names": ["acc1", "acc1"]}, headers=headers)
            assert r.status_code == 200 and r.json()["cookies"]
            assert "no-store" in r.headers["cache-control"]
            assert r.headers["content-disposition"] == 'attachment; filename="acc1.json"'
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()
