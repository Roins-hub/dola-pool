"""多行 Cookie 头文本解析验证。"""
from __future__ import annotations

from cookie_import import (
    cookie_header_to_state,
    parse_cookie_header_text,
    parse_cookie_header_line,
    sessionid_from_line,
    sessionid_from_state,
)


SAMPLE = "passport_csrf_token=aaa; sessionid=sid-one; sid_guard=g1; uid_tt=u1; store-country-code=hk"
SAMPLE2 = "passport_csrf_token=bbb; sessionid=sid-two; sid_guard=g2; uid_tt=u2; store-country-code=hk"


def test_parse_line():
    cookies = parse_cookie_header_line(SAMPLE)
    assert any(c["name"] == "sessionid" and c["value"] == "sid-one" for c in cookies)
    assert all(c["domain"] == ".dola.com" for c in cookies)


def test_parse_line_strips_cookie_prefix():
    """抓包复制常见 "Cookie: xxx=1; sessionid=..." 形态。"""
    for prefix in ("Cookie: ", "cookie:", "COOKIE:  "):
        cookies = parse_cookie_header_line(prefix + SAMPLE)
        names = [c["name"] for c in cookies]
        assert "sessionid" in names, names
        assert not any(n.lower().startswith("cookie") for n in names), names
        assert sessionid_from_line(prefix + SAMPLE) == "sid-one"
    assert parse_cookie_header_line("Cookie:") == []


def test_parse_line_without_mstoken_or_fp():
    """只有 ttwid/passport/sessionid 的 cookie（无 msToken、无 s_v_web_id）也要能解析。"""
    line = ("ttwid=1%7Cabc; passport_csrf_token=c06051fe; odin_tt=0523a4b9; "
            "sid_guard=6158d1c6%7C1790055166; uid_tt=351b25b6; sid_tt=6158d1c6; "
            "sessionid=6158d1c633b79ee69c83092e0f9d9efb; sessionid_ss=6158d1c633b79ee69c83092e0f9d9efb; "
            "store-idc=mya; store-country-code=hk")
    state = cookie_header_to_state(line)
    assert state is not None
    assert sessionid_from_state(state) == "6158d1c633b79ee69c83092e0f9d9efb"
    assert "msToken" not in state["cookies"]
    assert "s_v_web_id" not in state["cookies"]
    assert state["cookies"]["ttwid"]["value"].startswith("1%7C")


def test_sessionid_from_line():
    assert sessionid_from_line(SAMPLE) == "sid-one"
    assert sessionid_from_line("no_cookie_here") == ""


def test_header_to_state():
    state = cookie_header_to_state(SAMPLE)
    assert state["cookie_header"].startswith("passport_csrf_token=")
    assert state["cookies"]["sessionid"]["domain"] == ".dola.com"
    assert sessionid_from_state(state) == "sid-one"


def test_header_to_state_rejects_email_line():
    assert cookie_header_to_state("you@gmail.com----password") is None
    assert cookie_header_to_state("hello world") is None


def test_parse_text_skips_comment_blank_email():
    raw = "\n".join(["# comment", "", "hello world", "you@gmail.com----password", SAMPLE, SAMPLE2])
    states = parse_cookie_header_text(raw)
    assert len(states) == 2
    assert sessionid_from_state(states[0]) == "sid-one"
    assert states[0]["cookies"]["sessionid"]["domain"] == ".dola.com"


COOKIE_JSON = (
    '[{"creation_time":"1789997155","domain":"www.dola.com","name":"hook_slardar_session_id",'
    '"value":"h1","path":"/chat","expiration_time":"0","secure":false,"http_only":false,"same_site":"-1"},'
    '{"creation_time":"1789997156","domain":".dola.com","name":"sessionid","value":"sid-json",'
    '"path":"/","expiration_time":"1821254542"},'
    '{"domain":".wikipedia.org","name":"WMF-Uniq","value":"w1","path":"/"}]'
)


def test_json_array_is_one_account():
    states = parse_cookie_header_text(COOKIE_JSON)
    assert len(states) == 1
    assert sessionid_from_state(states[0]) == "sid-json"
    assert states[0]["cookies"]["sessionid"]["value"] == "sid-json"
    assert [c["name"] for c in states[0]["cookies_list"]] == [
        "hook_slardar_session_id", "sessionid"]


def test_json_array_drops_non_dola_domains():
    cookies = parse_cookie_header_text(COOKIE_JSON)[0]["cookies_list"]
    assert all("dola.com" in c["domain"] for c in cookies)
    assert "WMF-Uniq" not in [c["name"] for c in cookies]
    assert parse_cookie_header_text('[{"domain":".example.com","name":"sid","value":"x"}]') == []


def test_json_array_keeps_only_needed_fields():
    cookies = parse_cookie_header_text(COOKIE_JSON)[0]["cookies_list"]
    assert set(cookies[0]) == {"name", "value", "domain", "path"}
    assert cookies[0]["value"] == "h1"
    assert cookies[0]["path"] == "/chat"


def test_json_array_multiple_bundles():
    two = COOKIE_JSON + "\n" + COOKIE_JSON.replace("sid-json", "sid-json-2")
    states = parse_cookie_header_text(two)
    assert [sessionid_from_state(s) for s in states] == ["sid-json", "sid-json-2"]


def test_json_wrapper_object_and_defaults():
    wrapped = '{"cookies_list":[{"name":"sessionid","value":"sid-wrap"}]}'
    states = parse_cookie_header_text(wrapped)
    assert sessionid_from_state(states[0]) == "sid-wrap"
    assert states[0]["cookies_list"][0]["domain"] == ".dola.com"
    assert states[0]["cookies_list"][0]["path"] == "/"


def test_json_tolerates_bom_and_rejects_garbage():
    assert sessionid_from_state(
        parse_cookie_header_text("\ufeff" + COOKIE_JSON)[0]) == "sid-json"
    assert parse_cookie_header_text('["not-a-cookie-object"]') == []
