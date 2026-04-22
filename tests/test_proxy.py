"""Unit tests for CartProxy.

We patch ``urllib.request.urlopen`` so nothing hits the network; each test
captures the outgoing ``Request`` and asserts on method, URL, headers, body.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from cookunity.proxy import (
    CART_ADD_ENDPOINT,
    CART_GET_ENDPOINT,
    CREATE_ORDER_ENDPOINT,
    CREATE_ORDER_QUERY,
    PRICE_BREAKDOWN_ENDPOINT,
    CartProxy,
)


@contextmanager
def _mocked_urlopen(response_body: bytes, status: int = 200):
    """Patch urlopen, record the ``Request`` it was called with, yield a list
    containing that request so tests can assert on it."""
    calls: list = []
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: None
    resp.status = status
    resp.read.return_value = response_body

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return resp

    with patch("cookunity.proxy.urllib.request.urlopen", side_effect=fake_urlopen):
        yield calls


def _make_proxy() -> CartProxy:
    return CartProxy(token="JWT.x.y", cookie="appSession=abc; a=b", cart_id="seed-uuid")


# -- header construction ------------------------------------------------------


def test_headers_include_required_auth_and_meta():
    p = _make_proxy()
    h = p._headers("2026-04-27")
    assert h["authorization"] == "JWT.x.y"
    assert h["cookie"] == "appSession=abc; a=b"
    assert h["platform"] == "web"
    assert h["accept-version"] == "1.25.0"
    assert "2026-04-27" in h["referer"]


def test_update_rotates_credentials():
    p = _make_proxy()
    p.update(token="NEW.x.y", cookie="appSession=NEW")
    assert p.token == "NEW.x.y"
    assert p.cookie == "appSession=NEW"
    # cart_id not passed → unchanged
    assert p.cart_id == "seed-uuid"


def test_update_preserves_per_date_cache():
    p = _make_proxy()
    p.cart_id_by_date["2026-04-27"] = "known-date-uuid"
    p.update(token="NEW.x.y")
    assert p.cart_id_by_date["2026-04-27"] == "known-date-uuid"


# -- GET cart -----------------------------------------------------------------


def test_get_cart_shape():
    p = _make_proxy()
    with _mocked_urlopen(b'{"cart_id":"from-server"}') as calls:
        status, body = p.get("2026-04-27")
    assert status == 200
    assert calls[0].method == "GET"
    assert calls[0].full_url == CART_GET_ENDPOINT.format(date="2026-04-27")
    assert body == b'{"cart_id":"from-server"}'


# -- cart UUID discovery ------------------------------------------------------


def test_add_looks_up_cart_id_from_date_before_posting():
    """First call should be GET /cart/v2/<date> to discover the UUID; second
    should be POST /cart/v2/<discovered-uuid>/products."""
    p = _make_proxy()
    responses = [
        (b'{"cart_id":"discovered-uuid"}', 200),  # GET to resolve
        (b'{"ok":true}', 200),  # POST to add
    ]
    call_idx = [0]

    def fake_urlopen(req, timeout=None):
        body, status = responses[call_idx[0]]
        call_idx[0] += 1
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        resp.status = status
        resp.read.return_value = body
        return resp

    calls: list = []
    _orig = fake_urlopen

    def tracking(req, timeout=None):
        calls.append(req)
        return _orig(req, timeout=timeout)

    with patch("cookunity.proxy.urllib.request.urlopen", side_effect=tracking):
        status, body = p.add("2026-04-27", "ii-999", quantity=1)

    assert len(calls) == 2
    assert calls[0].method == "GET"
    assert "/cart/v2/2026-04-27" in calls[0].full_url
    assert calls[1].method == "POST"
    assert calls[1].full_url == CART_ADD_ENDPOINT.format(cart_id="discovered-uuid")
    payload = json.loads(calls[1].data)
    assert payload == {"products": [{"inventory_id": "ii-999", "quantity": 1}]}
    # UUID cached for subsequent calls.
    assert p.cart_id_by_date["2026-04-27"] == "discovered-uuid"


def test_add_falls_back_to_seed_cart_id_when_get_fails():
    p = _make_proxy()
    responses = [
        (b"nope", 500),  # GET fails
        (b"{}", 200),    # POST uses seed cart_id
    ]
    idx = [0]

    def fake(req, timeout=None):
        body, status = responses[idx[0]]
        idx[0] += 1
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        resp.status = status
        resp.read.return_value = body
        return resp

    with patch("cookunity.proxy.urllib.request.urlopen", side_effect=fake) as m:
        p.add("2026-04-27", "ii-1")

    second_req = m.call_args_list[1].args[0]
    assert second_req.full_url == CART_ADD_ENDPOINT.format(cart_id="seed-uuid")


# -- remove -------------------------------------------------------------------


def test_remove_uses_delete_method():
    p = _make_proxy()
    p.cart_id_by_date["2026-04-27"] = "cached-uuid"  # skip discovery round-trip
    with _mocked_urlopen(b"{}") as calls:
        p.remove("2026-04-27", "ii-9")
    assert calls[0].method == "DELETE"
    assert calls[0].full_url == CART_ADD_ENDPOINT.format(cart_id="cached-uuid")


# -- price_breakdown ----------------------------------------------------------


def test_price_breakdown_sends_cart_id_and_meals():
    p = _make_proxy()
    p.cart_id_by_date["2026-04-27"] = "uuid"
    with _mocked_urlopen(b"{}") as calls:
        p.price_breakdown("2026-04-27", [{"entityId": 1, "inventoryId": "ii-1", "quantity": 1}])
    assert calls[0].method == "POST"
    assert calls[0].full_url == PRICE_BREAKDOWN_ENDPOINT
    body = json.loads(calls[0].data)
    assert body["cartId"] == "uuid"
    assert body["date"] == "2026-04-27"
    assert body["meals"][0]["entityId"] == 1


# -- create_order -------------------------------------------------------------


def test_create_order_uses_webdesktop_platform_and_root_referer():
    p = _make_proxy()
    p.cart_id_by_date["2026-04-27"] = "uuid"
    with _mocked_urlopen(b"{}") as calls:
        p.create_order(
            "2026-04-27",
            [{"id": 1, "qty": 1, "batch_id": 99, "inventoryId": "ii-1"}],
        )
    req = calls[0]
    assert req.method == "POST"
    assert req.full_url == CREATE_ORDER_ENDPOINT
    # Only createOrder uses this header flavor; cart endpoints use `platform: web`.
    assert req.headers.get("Cu-platform") == "WebDesktop"
    assert "platform" not in {h.lower() for h in req.headers}
    assert req.headers.get("Referer") == "https://subscription.cookunity.com/"
    body = json.loads(req.data)
    assert body["operationName"] == "createOrder"
    assert body["query"] == CREATE_ORDER_QUERY
    order = body["variables"]["order"]
    assert order["deliveryDate"] == "2026-04-27"
    assert order["cartId"] == "uuid"
    assert order["products"][0]["batch_id"] == 99


# -- error passthrough --------------------------------------------------------


def test_http_error_returns_status_and_body_instead_of_raising():
    import urllib.error

    class _FakeResp:
        headers = {}

    err = urllib.error.HTTPError(
        url="http://x",
        code=409,
        msg="Conflict",
        hdrs=_FakeResp.headers,
        fp=None,
    )
    err.read = lambda: b'{"error":"already ordered"}'

    def raising(req, timeout=None):
        raise err

    with patch("cookunity.proxy.urllib.request.urlopen", side_effect=raising):
        p = _make_proxy()
        p.cart_id_by_date["2026-04-27"] = "uuid"
        status, body = p.add("2026-04-27", "ii-1")
    assert status == 409
    assert body == b'{"error":"already ordered"}'
