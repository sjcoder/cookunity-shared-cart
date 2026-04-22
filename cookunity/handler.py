"""HTTP request handler — the thin glue between the browser and the proxy.

Every route is small: parse inputs, call CartProxy or State, write bytes back.
No CookUnity-specific logic lives here; that's in ``cookunity.proxy``.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from cookunity.curl_paste import parse_curl
from cookunity.env import now_iso, save_creds
from cookunity.proxy import CartProxy
from cookunity.render import esc
from cookunity.state import State


def _date_from_query(path: str, default_date: str) -> str:
    qs = urllib.parse.urlparse(path).query
    d = urllib.parse.parse_qs(qs).get("date", [default_date])[0]
    date.fromisoformat(d)  # raises ValueError
    return d


def _date_from_body(payload: dict, default_date: str) -> str:
    d = payload.get("date") or default_date
    date.fromisoformat(d)
    return d


def build_handler(
    state: State,
    proxy: CartProxy,
    default_date: str,
    creds_meta: dict,
    creds_path: Path,
):
    """Construct the ``BaseHTTPRequestHandler`` subclass used by the server.

    We build it dynamically so the closure can hold references to the shared
    ``state``, ``proxy`` and config without a global.
    """

    class Handler(BaseHTTPRequestHandler):
        # -- plumbing ---------------------------------------------------------
        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _read_json(self) -> dict:
            length = int(self.headers.get("content-length") or 0)
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

        def _write(self, status: int, content_type: str, body: bytes, extra_headers: dict | None = None):
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: dict):
            self._write(status, "application/json", json.dumps(obj).encode())

        def _render_error(self, msg: str):
            body = (
                f"<h1>Error</h1><p>{esc(msg)}</p>"
                "<p><a href='/'>home</a> · <a href='/#auth'>update credentials</a></p>"
            ).encode()
            self._write(500, "text/html; charset=utf-8", body)

        def _resolve_date(self, payload: dict | None = None) -> str:
            """Prefer ``?date=`` in the URL; fall back to the JSON body; then default."""
            if "date=" in self.path:
                return _date_from_query(self.path, default_date)
            if payload is not None:
                return _date_from_body(payload, default_date)
            return default_date

        # -- routing ----------------------------------------------------------
        def do_GET(self):  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._get_index()
            if path == "/api/cart":
                return self._get_cart()
            if path == "/api/creds":
                return self._get_creds()
            self.send_error(404)

        def do_POST(self):  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            routes = {
                "/api/cart/add": self._cart_add,
                "/api/cart/remove": self._cart_remove,
                "/api/refresh": self._refresh,
                "/api/creds": self._creds_update,
                "/api/order/preview": self._order_preview,
                "/api/order/place": self._order_place,
            }
            fn = routes.get(path)
            if fn:
                return fn()
            self.send_error(404)

        # -- GET handlers -----------------------------------------------------
        def _get_index(self):
            try:
                d = _date_from_query(self.path, default_date)
                entry = state.get(d)
            except Exception as e:
                return self._render_error(f"Couldn't load menu for that date: {e}")
            self._write(
                200,
                "text/html; charset=utf-8",
                entry["page_html"],
                extra_headers={"cache-control": "no-store"},
            )

        def _get_cart(self):
            try:
                d = _date_from_query(self.path, default_date)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            status, body = proxy.get(d)
            self._write(status, "application/json", body, extra_headers={"cache-control": "no-store"})

        def _get_creds(self):
            tail = (proxy.token or "")[-8:] if proxy.token else ""
            self._json(
                200,
                {
                    "token": bool(proxy.token),
                    "token_tail": tail,
                    "cart_id": proxy.cart_id,
                    "source": creds_meta.get("source", "env"),
                    "saved_at": creds_meta.get("saved_at"),
                },
            )

        # -- cart mutations ---------------------------------------------------
        def _date_is_ordered(self, menu_date: str) -> bool:
            status, body = proxy.get(menu_date)
            if status != 200:
                return False
            try:
                return bool((json.loads(body) or {}).get("order"))
            except json.JSONDecodeError:
                return False

        def _cart_mutation(self, op: str) -> None:
            """Shared POST body for ``add`` and ``remove``."""
            payload = self._read_json()
            inv = payload.get("inventory_id")
            if not inv:
                return self._json(400, {"error": "missing inventory_id"})
            try:
                d = self._resolve_date(payload)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            if self._date_is_ordered(d):
                return self._json(
                    409,
                    {"error": f"Order for {d} is already placed — cart is locked for this week."},
                )
            qty = int(payload.get("quantity") or 1)
            fn = proxy.add if op == "add" else proxy.remove
            status, body = fn(d, inv, qty)
            self._write(status, "application/json", body)

        def _cart_add(self):
            self._cart_mutation("add")

        def _cart_remove(self):
            self._cart_mutation("remove")

        # -- menu refresh -----------------------------------------------------
        def _refresh(self):
            try:
                d = _date_from_query(self.path, default_date)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            try:
                entry = state.refresh(d)
                data = entry["data"]
                menu = (data.get("data") or {}).get("menu", {})
                return self._json(
                    200,
                    {
                        "ok": True,
                        "fetched_at": data.get("_fetched_at"),
                        "meals": len(menu.get("meals") or []),
                        "bundles": len(menu.get("bundles") or []),
                    },
                )
            except SystemExit as e:
                return self._json(502, {"error": str(e)})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        # -- creds ------------------------------------------------------------
        def _creds_update(self):
            payload = self._read_json()
            curl_text = payload.get("curl") or ""
            if not curl_text.strip():
                return self._json(400, {"error": "Paste a curl command in the `curl` field."})
            try:
                parsed = parse_curl(curl_text)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            proxy.update(token=parsed["token"], cookie=parsed["cookie"], cart_id=parsed.get("cart_id"))
            saved_at = save_creds(creds_path, proxy.token, proxy.cookie, proxy.cart_id)
            creds_meta["source"] = "pasted-curl"
            creds_meta["saved_at"] = saved_at
            state.invalidate_all()
            self._json(
                200,
                {
                    "ok": True,
                    "token_tail": proxy.token[-8:],
                    "cart_id": proxy.cart_id,
                    "saved_at": saved_at,
                },
            )

        # -- order preview / place --------------------------------------------
        def _cart_meals_for(self, menu_date: str) -> list[dict] | None:
            """Cross-reference the live cart against our cached menu to get
            ``entityId`` + ``batchId`` for every product. ``None`` means we
            couldn't resolve something cleanly — don't send a half-formed order.
            """
            status, body = proxy.get(menu_date)
            if status != 200:
                return None
            try:
                cart_data = json.loads(body)
            except json.JSONDecodeError:
                return None
            products = cart_data.get("products") or []
            try:
                entry = state.get(menu_date)
            except Exception:
                return None
            menu = (entry["data"].get("data") or {}).get("menu") or {}
            inv_to_meal: dict[str, dict] = {}
            for m in menu.get("meals") or []:
                inv = m.get("inventoryId")
                if inv:
                    inv_to_meal[inv] = {"id": m.get("id"), "batchId": m.get("batchId")}
            out: list[dict] = []
            for p in products:
                inv = p.get("inventory_id")
                meta = inv_to_meal.get(inv)
                if not meta or meta.get("id") is None:
                    return None
                out.append(
                    {
                        "entityId": meta["id"],
                        "batchId": meta.get("batchId"),
                        "inventoryId": inv,
                        "quantity": int(p.get("quantity") or 1),
                    }
                )
            return out

        def _order_preview(self):
            payload = self._read_json()
            try:
                d = self._resolve_date(payload)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            meals_full = self._cart_meals_for(d)
            if meals_full is None:
                return self._json(502, {"error": "Couldn't resolve cart items against the cached menu for that date."})
            if not meals_full:
                return self._json(400, {"error": "Cart is empty."})
            preview_meals = [
                {"entityId": m["entityId"], "quantity": m["quantity"], "inventoryId": m["inventoryId"]}
                for m in meals_full
            ]
            status, body = proxy.price_breakdown(d, preview_meals)
            self._write(status, "application/json", body)

        def _order_place(self):
            payload = self._read_json()
            try:
                d = self._resolve_date(payload)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            if self._date_is_ordered(d):
                return self._json(409, {"error": f"Order for {d} has already been placed."})
            meals_full = self._cart_meals_for(d)
            if meals_full is None:
                return self._json(502, {"error": "Couldn't resolve cart items against the cached menu for that date."})
            if not meals_full:
                return self._json(400, {"error": "Cart is empty."})
            products = [
                {"id": m["entityId"], "qty": m["quantity"], "batch_id": m["batchId"], "inventoryId": m["inventoryId"]}
                for m in meals_full
            ]
            status, body = proxy.create_order(
                d,
                products,
                time_start=payload.get("time_start") or "12:00",
                time_end=payload.get("time_end") or "20:00",
                tip=int(payload.get("tip") or 0),
            )
            self._write(status, "application/json", body)

    return Handler
