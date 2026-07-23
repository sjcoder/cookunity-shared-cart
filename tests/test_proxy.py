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


def test_get_refreshes_cart_id_mapping():
    """Autopilot regenerates cart ids; every GET must resync the mapping so
    mutations target the cart the user is looking at."""
    p = _make_proxy()
    p.cart_id_by_date["2026-04-27"] = "stale-uuid"
    with _mocked_urlopen(b'{"cart_id":"fresh-uuid"}'):
        p.get("2026-04-27")
    assert p.cart_id_by_date["2026-04-27"] == "fresh-uuid"


# -- get_true_cart: seeing through autopilot's suggestion ---------------------


def _scripted_urlopen(responses: list[tuple[bytes, int]]):
    """Return (patch_ctx, calls) where each urlopen pops the next response."""
    calls: list = []
    idx = [0]

    def fake(req, timeout=None):
        calls.append(req)
        body, status = responses[min(idx[0], len(responses) - 1)]
        idx[0] += 1
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        resp.status = status
        resp.read.return_value = body
        return resp

    return patch("cookunity.proxy.urllib.request.urlopen", side_effect=fake), calls


def _cart_body(ids: list[str], cart_id: str = "c-1", qty: int = 1) -> bytes:
    return json.dumps(
        {"cart_id": cart_id, "products": [{"inventory_id": i, "quantity": qty} for i in ids]}
    ).encode()


def _rec_body(date: str, ids: list[str]) -> bytes:
    return json.dumps(
        {"data": {"upcomingDays": [
            {"date": date, "recommendation": {"meals": [{"inventoryId": i} for i in ids]}}
        ]}}
    ).encode()


def test_get_true_cart_nudges_when_cart_equals_recommendation():
    p = _make_proxy()
    suggestion = ["ii-1", "ii-2", "ii-3"]
    real = ["ii-9"]
    ctx, calls = _scripted_urlopen([
        (_cart_body(suggestion), 200),                 # GET → suggestion
        (_rec_body("2026-04-27", suggestion), 200),    # GraphQL recommendation
        (b"{}", 200),                                  # nudge add
        (b"{}", 200),                                  # nudge remove
        (_cart_body(real, cart_id="c-2"), 200),        # re-GET → real cart
    ])
    with ctx:
        status, body = p.get_true_cart("2026-04-27")
    assert status == 200
    assert json.loads(body)["products"][0]["inventory_id"] == "ii-9"
    methods = [c.method for c in calls]
    assert methods == ["GET", "POST", "POST", "DELETE", "GET"]


def test_get_true_cart_passes_through_when_cart_differs_from_recommendation():
    p = _make_proxy()
    ctx, calls = _scripted_urlopen([
        (_cart_body(["ii-1", "ii-9"]), 200),           # GET → user-looking cart
        (_rec_body("2026-04-27", ["ii-1", "ii-2"]), 200),
    ])
    with ctx:
        status, body = p.get_true_cart("2026-04-27")
    assert status == 200
    assert len(json.loads(body)["products"]) == 2
    assert [c.method for c in calls] == ["GET", "POST"]  # no nudge mutations


def test_get_true_cart_respects_nudge_cooldown():
    """If the user's real cart legitimately equals the recommendation, nudge
    once and then leave it alone."""
    p = _make_proxy()
    suggestion = ["ii-1", "ii-2"]
    responses = [
        (_cart_body(suggestion), 200),
        (_rec_body("2026-04-27", suggestion), 200),
        (b"{}", 200), (b"{}", 200),
        (_cart_body(suggestion), 200),   # re-GET: unchanged (it WAS the real cart)
        (_cart_body(suggestion), 200),   # second get_true_cart's GET
    ]
    ctx, calls = _scripted_urlopen(responses)
    with ctx:
        p.get_true_cart("2026-04-27")
        n_after_first = len(calls)
        p.get_true_cart("2026-04-27")
    # Second call: exactly one more request (the plain GET), no second dance.
    assert len(calls) == n_after_first + 1


def test_get_true_cart_skips_ordered_and_empty_carts():
    p = _make_proxy()
    ctx, calls = _scripted_urlopen([
        (json.dumps({"cart_id": "c", "products": [], "order": None}).encode(), 200),
    ])
    with ctx:
        p.get_true_cart("2026-04-27")
    assert [c.method for c in calls] == ["GET"]  # no GraphQL, no nudge


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
