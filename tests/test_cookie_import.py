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
