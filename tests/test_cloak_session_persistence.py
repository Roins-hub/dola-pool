"""Session migration regressions using synthetic cookies; no browser or network."""
import asyncio
import importlib.util
import json
from pathlib import Path
import types

import pytest


def run(coro):
    # Do not reset the process-wide event loop used by the legacy test modules.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "browser_session_regression", Path(__file__).resolve().parents[1] / "browser.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod.config, "COOKIE_RUNTIME", "cloak")
    monkeypatch.setattr(mod.config, "COOKIE_PROFILE_SUBDIR", "cloak-profile")
    root = tmp_path / "accounts" / "example"
    profile = root / "cloak-profile"
    profile.mkdir(parents=True)
    cookie = {"name": "sessionid", "value": "synthetic", "domain": ".dola.com", "path": "/"}
    (root / "cookie_state.json").write_text(json.dumps({"cookies_list": [cookie]}))
    return mod, root, profile, cookie


class Context:
    def __init__(self, cookies=()):
        self.data = [dict(c) for c in cookies]
        self.closed = False

    async def cookies(self, *args):
        return [dict(c) for c in self.data]

    async def add_cookies(self, cookies):
        self.data.extend(dict(c) for c in cookies)

    async def close(self, *args, **kwargs):
        self.closed = True


def test_session_survives_restart_and_logout_is_not_replayed(runtime):
    mod, root, profile, cookie = runtime

    async def scenario():
        first = Context()
        await mod._materialize_cloak_cookie_profile("example", first)
        assert first.data == [cookie]
        assert "expires" not in first.data[0]
        first.data[0]["value"] = "refreshed-session"
        await first.close()
        second = Context()
        await mod._materialize_cloak_cookie_profile("example", second)
        assert second.data[0]["value"] == "refreshed-session"
        second.data.clear()
        await second.close()
        third = Context()
        await mod._materialize_cloak_cookie_profile("example", third)
        assert third.data == []
        await third.close()
        assert first.closed and second.closed and third.closed
        assert json.loads((root / "cookie_state.json").read_text())["cookies_list"] == [cookie]

    run(scenario())


def test_live_cookie_wins_and_persistent_cookies_are_not_replayed(runtime):
    mod, _, profile, cookie = runtime
    persistent = dict(cookie, name="persistent", expires=1)
    (profile / ".cloak-session-cookies.json").write_text(json.dumps([cookie, persistent]))

    async def scenario():
        ctx = Context([dict(cookie, value="new-live-session")])
        await mod._materialize_cloak_cookie_profile("example", ctx)
        assert len(ctx.data) == 1
        assert ctx.data[0]["value"] == "new-live-session"
        await ctx.close()

    run(scenario())


def test_legacy_marker_does_not_prevent_repair(runtime):
    mod, _, profile, cookie = runtime
    (profile / ".cookie-state-materialized").write_text("materialized\n")

    async def scenario():
        ctx = Context()
        await mod._materialize_cloak_cookie_profile("example", ctx)
        assert ctx.data == [cookie]
        await ctx.close()

    run(scenario())


def test_domains_are_bounded(runtime):
    mod, _, _, _ = runtime
    assert mod._dola_cookie({"domain": ".dola.com"})
    assert mod._dola_cookie({"domain": "www.dola.com"})
    assert not mod._dola_cookie({"domain": "notdola.com"})
    assert not mod._dola_cookie({"domain": "dola.com.example.org"})


def test_snapshot_failure_still_closes_browser(runtime, monkeypatch):
    mod, _, _, _ = runtime

    async def scenario():
        ctx = Context()
        await mod._materialize_cloak_cookie_profile("example", ctx)
        def fail(*args):
            raise OSError("disk unavailable")
        monkeypatch.setattr(mod, "_save_cloak_cookies", fail)
        with pytest.raises(OSError):
            await ctx.close()
        assert ctx.closed

    run(scenario())


@pytest.mark.parametrize("bound,url", [(None, ""), ({"id": "proxy"}, "")])
def test_unbound_or_unavailable_proxy_cannot_launch(runtime, monkeypatch, bound, url):
    mod, _, _, _ = runtime
    async def forbidden(*args, **kwargs):
        pytest.fail("browser must not start without an account proxy")
    monkeypatch.setitem(__import__("sys").modules, "cloakbrowser",
                        types.SimpleNamespace(launch_persistent_context_async=forbidden))
    monkeypatch.setattr(mod.proxy_store, "get_account_proxy", lambda account: bound)
    monkeypatch.setattr(mod.proxy_store, "proxy_url_for", lambda account: url)
    with pytest.raises(RuntimeError, match="proxy"):
        run(mod.launch_account_context(None, "example"))


def test_migration_failure_closes_launched_browser(runtime, monkeypatch):
    mod, root, _, _ = runtime
    (root / "cookie_state.json").write_text("invalid json")
    ctx = Context()
    async def launch(*args, **kwargs):
        return ctx
    monkeypatch.setitem(__import__("sys").modules, "cloakbrowser",
                        types.SimpleNamespace(launch_persistent_context_async=launch))
    monkeypatch.setattr(mod.proxy_store, "get_account_proxy", lambda account: {"id": "proxy"})
    monkeypatch.setattr(mod.proxy_store, "proxy_url_for", lambda account: "http://127.0.0.1:9999")
    with pytest.raises(ValueError):
        run(mod.launch_account_context(None, "example"))
    assert ctx.closed


def test_accounts_keep_distinct_profiles_and_bound_proxies(runtime, monkeypatch):
    mod, root, profile, cookie = runtime
    other = root.parent / "other"
    other.mkdir()
    (other / "cookie_state.json").write_text(json.dumps({"cookies_list": [cookie]}))
    launches = []
    async def launch(path, **kwargs):
        launches.append((str(Path(path).resolve()), kwargs["proxy"]))
        return Context()
    async def materialize(*args): pass
    monkeypatch.setitem(__import__("sys").modules, "cloakbrowser",
                        types.SimpleNamespace(launch_persistent_context_async=launch))
    monkeypatch.setattr(mod, "_materialize_cloak_cookie_profile", materialize)
    monkeypatch.setattr(mod.proxy_store, "get_account_proxy", lambda n: {"id":n})
    monkeypatch.setattr(mod.proxy_store, "proxy_url_for", lambda n: "http://"+n+".invalid:8080")
    async def scenario():
        for n in ("example", "other", "example"):
            ctx = await mod.launch_account_context(None, n)
            await ctx.close()
    run(scenario())
    assert launches[0] == launches[2]
    assert launches[0][0] != launches[1][0]
    assert launches[0][1] == "http://example.invalid:8080"
    assert launches[1][1] == "http://other.invalid:8080"
