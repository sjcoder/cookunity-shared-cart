"""Most of the value here is regression protection: real curls from DevTools
use ``$'...'`` ANSI-C quoting around the cookie jar, and dropping that layer
accidentally is how 'Valid user needed' errors sneak back in."""

import pytest

from cookunity.curl_paste import decode_ansi_c, parse_curl


CURL_BASIC = """curl 'https://subscription.cookunity.com/menu-service/graphql' \\
  -H 'authorization: eyJTEST.abc.def' \\
  -b 'CU_TrackUuid=xxx; appSession=fake-session; other=val'
"""

CURL_WITH_ANSI_C_COOKIE = """curl 'https://subscription.cookunity.com/menu-service/graphql' \\
  -H 'authorization: eyJTEST.abc.def' \\
  -b $'CU_TrackUuid=xxx; appSession=fake-session; CU_discountText=Week\\u00211\\u0021'
"""

CURL_WITH_CART_URL = """curl 'https://subscription.cookunity.com/sdui-service/cart/v2/00000000-0000-0000-0000-000000000000/products' \\
  -H 'authorization: eyJTEST.abc.def' \\
  -b 'appSession=x'
"""


def test_parse_curl_extracts_token_cookie_no_cart():
    got = parse_curl(CURL_BASIC)
    assert got["token"] == "eyJTEST.abc.def"
    assert "appSession=fake-session" in got["cookie"]
    assert got["cart_id"] is None


def test_parse_curl_decodes_ansi_c_cookie():
    got = parse_curl(CURL_WITH_ANSI_C_COOKIE)
    # ! should be decoded to `!`
    assert "Week!1!" in got["cookie"]
    assert "\\u0021" not in got["cookie"]


def test_parse_curl_extracts_cart_uuid():
    got = parse_curl(CURL_WITH_CART_URL)
    assert got["cart_id"] == "00000000-0000-0000-0000-000000000000"


def test_parse_curl_strips_bearer_prefix():
    curl = """curl 'x' -H 'authorization: Bearer eyJTEST.abc.def' -b 'appSession=x'"""
    assert parse_curl(curl)["token"] == "eyJTEST.abc.def"


def test_parse_curl_rejects_missing_auth():
    with pytest.raises(ValueError, match="authorization"):
        parse_curl("curl 'x' -b 'appSession=x'")


def test_parse_curl_rejects_missing_cookie():
    with pytest.raises(ValueError, match="cookie"):
        parse_curl("curl 'x' -H 'authorization: abc'")


def test_parse_curl_rejects_cookie_without_app_session():
    with pytest.raises(ValueError, match="appSession"):
        parse_curl("curl 'x' -H 'authorization: abc' -b 'CU_TrackUuid=y'")


def test_decode_ansi_c_handles_common_escapes():
    assert decode_ansi_c("a\\u0021b") == "a!b"
    assert decode_ansi_c("a\\x21b") == "a!b"
    assert decode_ansi_c("a\\nb") == "a\nb"
    assert decode_ansi_c("a\\'b") == "a'b"


def test_decode_ansi_c_passes_unknown_escape_through():
    assert decode_ansi_c("a\\qb") == "aqb"  # unknown `\q` → just the `q`
