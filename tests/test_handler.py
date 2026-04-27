"""HTTP handler tests — exercise the route table against a real local server.

We spin up a ``ThreadingHTTPServer`` on an ephemeral port with stub fakes for
``CartProxy`` and ``State``, then make real HTTP calls to it. That covers the
routing/serialization layer without touching CookUnity.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cookunity.handler import build_handler


class StubProxy:
    def __init__(self, get_response: tuple[int, bytes] = (200, b'{"products":[]}')):
        self.token = "stub-token"
        self.cookie = "stub-cookie"
        self.cart_id = "stub-cart"
        self._get_response = get_response
        self.calls: list[tuple] = []

    def get(self, date):
        self.calls.append(("get", date))
        return self._get_response

    def add(self, date, inv, qty=1):
        self.calls.append(("add", date, inv, qty))
        return 200, b'{"ok":true}'

    def remove(self, date, inv, qty=1):
        self.calls.append(("remove", date, inv, qty))
        return 200, b'{"ok":true}'

    def update(self, **kw):
        for k, v in kw.items():
            if v is not None:
                setattr(self, k, v)


class StubState:
    def __init__(self):
        self.upcoming = ["2026-04-27", "2026-05-04", "2026-05-11"]
        self.invalidated = False

    def invalidate_all(self):
        self.invalidated = True


@contextmanager
def _serve(proxy, state, default_date="2026-04-27", creds_meta=None, tmp_path: Path | None = None):
    creds_meta = creds_meta or {"source": "env", "saved_at": None}
    creds_path = (tmp_path or Path("/tmp")) / "creds.json"
    handler = build_handler(state, proxy, default_date, creds_meta, creds_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url) as resp:
        return resp.status, json.loads(resp.read())


# -- /api/auth/check ----------------------------------------------------------


def test_auth_check_reports_ok_when_upstream_returns_200(tmp_path):
    proxy = StubProxy(get_response=(200, b'{"products":[]}'))
    with _serve(proxy, StubState(), tmp_path=tmp_path) as base:
        status, body = _get(base + "/api/auth/check")
    assert status == 200
    assert body == {"ok": True, "status": 200, "tested_date": "2026-04-27"}
    # Should have hit the cart endpoint for the first upcoming Monday.
    assert proxy.calls == [("get", "2026-04-27")]


def test_auth_check_reports_expired_on_401(tmp_path):
    proxy = StubProxy(get_response=(401, b'{"message":"jwt expired"}'))
    with _serve(proxy, StubState(), tmp_path=tmp_path) as base:
        status, body = _get(base + "/api/auth/check")
    assert status == 200
    assert body["ok"] is False
    assert body["status"] == 401
    assert "expired" in body["message"].lower() or "expired" in body["message"]


def test_auth_check_reports_expired_on_403(tmp_path):
    proxy = StubProxy(get_response=(403, b"forbidden"))
    with _serve(proxy, StubState(), tmp_path=tmp_path) as base:
        _, body = _get(base + "/api/auth/check")
    assert body["ok"] is False
    assert body["status"] == 403


def test_auth_check_with_no_token_reports_missing_creds(tmp_path):
    proxy = StubProxy()
    proxy.token = ""
    with _serve(proxy, StubState(), tmp_path=tmp_path) as base:
        _, body = _get(base + "/api/auth/check")
    assert body["ok"] is False
    assert body["status"] == 0
    assert "credential" in body["message"].lower()


# -- existing routes — light coverage so the table doesn't bit-rot ------------


def test_get_creds_includes_token_tail(tmp_path):
    proxy = StubProxy()
    proxy.token = "JWT.AAAAA.bbbb12345678"
    with _serve(proxy, StubState(), tmp_path=tmp_path) as base:
        _, body = _get(base + "/api/creds")
    assert body["token"] is True
    assert body["token_tail"] == "12345678"
    assert body["cart_id"] == "stub-cart"


def test_get_cart_passes_through_upstream_status(tmp_path):
    proxy = StubProxy(get_response=(409, b'{"error":"already ordered"}'))
    with _serve(proxy, StubState(), tmp_path=tmp_path) as base:
        # urllib raises on non-2xx; use a plain Request and check the response
        req = urllib.request.Request(base + "/api/cart?date=2026-04-27")
        try:
            urllib.request.urlopen(req)
            pytest.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            assert e.code == 409
            assert json.loads(e.read())["error"] == "already ordered"
