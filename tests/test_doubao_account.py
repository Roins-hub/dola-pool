import json

import pytest

import doubao_account as account_kind


@pytest.mark.parametrize(
    "domain,value,expected",
    [
        (".doubao.com", "true", "work"),
        ("www.doubao.com", "1", "work"),
        (".doubao.com", "false", "personal"),
        ("www.doubao.com", "0", "personal"),
        (".dola.com", "true", ""),
        (".notdoubao.com", "true", ""),
    ],
)
def test_cookie_marker_distinguishes_doubao_account_kind(domain, value, expected):
    assert account_kind.cookie_type([{"domain": domain, "name": "has_biz_token", "value": value}]) == expected


def test_account_kind_from_launch_modes_overrides_cookie_fallback():
    launch = {
        "code": 0,
        "data": {"conversation_mode_config": {
            "default_conversation_mode_key_new_chat": "chat",
            "default_conversation_mode_key_new_office": "office",
        }},
    }
    assert account_kind.launch_type(launch, "work") == "personal"
    assert account_kind.launch_type({"code": 0, "data": {
        "conversation_mode_config": {"conversation_mode_order_map": {"doubao_office": ["office"]}}
    }}, "") == "work"
    assert account_kind.launch_type({"code": 17}, "personal") == "personal"


def test_imported_cookie_state_type_is_persisted_without_changing_cookie_values(tmp_path):
    path = tmp_path / "cookie_state.json"
    original = {"cookies_list": [
        {"domain": ".doubao.com", "name": "has_biz_token", "value": "true"},
        {"domain": ".doubao.com", "name": "sessionid", "value": "private"},
    ]}
    path.write_text(json.dumps(original), encoding="utf-8")
    account_kind.remember(path, account_kind.from_file(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["doubao_account_type"] == "work"
    assert saved["cookies_list"] == original["cookies_list"]


def test_invalid_classification_does_not_overwrite_state(tmp_path):
    path = tmp_path / "cookie_state.json"
    path.write_text(json.dumps({"doubao_account_type": "personal"}), encoding="utf-8")
    account_kind.remember(path, "unknown")
    assert json.loads(path.read_text(encoding="utf-8"))["doubao_account_type"] == "personal"


def test_login_verification_cross_checks_official_mode_config(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import doubao_gen

    path = tmp_path / "cookie_state.json"
    path.write_text(json.dumps({"cookies_list": [
        {"domain": ".doubao.com", "name": "has_biz_token", "value": "true"},
        {"domain": ".doubao.com", "name": "sessionid", "value": "private"},
    ]}), encoding="utf-8")
    dp = SimpleNamespace(
        BASE="https://www.doubao.com",
        dola_api_post_response=lambda *args, **kwargs: object(),
        request_json=lambda *_args, **_kwargs: {"code": "0"},
        dola_api_post_json=lambda *_args, **_kwargs: {"code": 0, "data": {
            "conversation_mode_config": {
                "default_conversation_mode_key_new_chat": "chat",
                "default_conversation_mode_key_new_office": "office",
            },
        }},
    )
    session = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(doubao_gen, "_site", lambda: dp)
    monkeypatch.setattr(doubao_gen, "_session", lambda *_args: (session, {}, {}))
    monkeypatch.setattr(doubao_gen, "_load_state", lambda *_args: None)
    assert doubao_gen.probe_login(path, proxy="http://proxy") == (True, "logged_in")
    assert json.loads(path.read_text(encoding="utf-8"))["doubao_account_type"] == "personal"
