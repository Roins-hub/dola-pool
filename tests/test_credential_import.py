"""账号凭据解析与分配验证。"""
from __future__ import annotations

from credential_import import allocate_credentials, parse_account_line, parse_account_text


def test_parse_line_separators():
    assert parse_account_line("abc@gmail.com----123456").password == "123456"
    assert parse_account_line("abc@gmail.com|pass").password == "pass"
    assert parse_account_line("abc@gmail.com,pass").password == "pass"
    assert parse_account_line("abc@gmail.com:pass").password == "pass"
    assert parse_account_line("abc@gmail.com----pw----TOTP").totp == "TOTP"
    assert parse_account_line("# comment") is None
    assert parse_account_line("not-an-email----pw") is None


def test_parse_account_text_and_json():
    rows = parse_account_text("a@gmail.com----one\nb@gmail.com|two\n")
    assert [r.email for r in rows] == ["a@gmail.com", "b@gmail.com"]
    json_rows = parse_account_text('[{"email":"c@gmail.com","password":"three","id":"acc3"}]')
    assert json_rows[0].account_id == "acc3"
    assert json_rows[0].password == "three"


def test_allocate_dedup_and_auto_name():
    records = parse_account_text("a@gmail.com----1\nb@gmail.com----2\na@gmail.com----dup\n")
    pairs = allocate_credentials(records, ["acc1", "acc2"], name_prefix="acc")
    assert [p[0] for p in pairs] == ["acc3", "acc4"]
    assert [p[1].email for p in pairs] == ["a@gmail.com", "b@gmail.com"]


def test_allocate_uses_explicit_id():
    records = parse_account_text('[{"email":"x@gmail.com","password":"p","id":"acc9"}]')
    pairs = allocate_credentials(records, ["acc1"], name_prefix="acc")
    assert pairs[0][0] == "acc9"
