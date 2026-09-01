import json

import pytest

from app.cookies import CookieStore, parse_cookies


def test_parse_object_list():
    payload = json.dumps(
        [
            {"name": "__Secure-1PSID", "value": "abc", "domain": ".google.com"},
            {"name": "__Secure-1PSIDTS", "value": "def"},
        ]
    )
    assert parse_cookies(payload) == {"__Secure-1PSID": "abc", "__Secure-1PSIDTS": "def"}


def test_parse_cookie_wrapper_object():
    payload = json.dumps({"cookie": "__Secure-1PSID=abc; __Secure-1PSIDTS=def"})
    assert parse_cookies(payload) == {"__Secure-1PSID": "abc", "__Secure-1PSIDTS": "def"}


def test_parse_raw_header_string():
    assert parse_cookies("a=1; b=2;  c=3 ") == {"a": "1", "b": "2", "c": "3"}


def test_parse_flat_mapping():
    assert parse_cookies(json.dumps({"a": "1", "b": 2})) == {"a": "1", "b": "2"}


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_cookies("")
    with pytest.raises(ValueError):
        parse_cookies("no-pairs-here")


def test_store_missing_file_is_not_an_error(tmp_path):
    store = CookieStore(tmp_path / "nope.json")
    assert store.load() == {}
    assert store.has_session_cookies() is False


def test_store_picks_up_changes_by_mtime(tmp_path):
    f = tmp_path / "cookies.json"
    f.write_text("a=1")
    store = CookieStore(f)
    assert store.load() == {"a": "1"}
    f.write_text("a=2; __Secure-1PSID=x")
    # mtime/size changed -> re-read
    assert store.load() == {"a": "2", "__Secure-1PSID": "x"}
    assert store.has_session_cookies() is True


def test_store_ignores_unreadable_content(tmp_path):
    f = tmp_path / "cookies.json"
    f.write_text("{ this is not valid")
    store = CookieStore(f)
    assert store.load() == {}
