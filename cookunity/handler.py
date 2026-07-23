"""HTTP request handler — the thin glue between the browser and the proxy.

Every route is small: parse inputs, call CartProxy or State, write bytes back.
No CookUnity-specific logic lives here; that's in ``cookunity.proxy``.
"""

from __future__ import annotations

import json
import sys
import time
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
    default_date_fn,  # Callable[[], str] — re-evaluated per request so a
    # server left running for weeks never serves a date frozen at startup.
    creds_meta: dict,
    creds_path: Path,
):
    """Construct the ``BaseHTTPRequestHandler`` subclass used by the server.

    We build it dynamically so the closure can hold references to the shared
    ``state``, ``proxy`` and config without a global.
    """

    # Tiny TTL cache for "which upcoming Monday should `/` land on?". Without
    # this every bare `/` reload would call CookUnity once per upcoming Monday
    # to ask "is it ordered?". Mutable so the closure below can update it.
    landing_cache: dict[str, object] = {"date": None, "expires": 0.0}
    LANDING_CACHE_TTL = 60.0

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

        def _render_auth_bootstrap(self):
            body = b"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Update CookUnity credentials</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#fff6fa;color:#24161b}
main{max-width:760px;margin:8vh auto;padding:0 20px}
h1{font-size:28px;margin:0 0 12px}
p{line-height:1.5;color:#5f4a52}
textarea{box-sizing:border-box;width:100%;min-height:260px;border:1px solid #d8c2cb;border-radius:8px;padding:12px;font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:white}
.actions{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap}
button{border:1px solid #24161b;background:#24161b;color:white;border-radius:6px;padding:9px 14px;font-weight:700;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}
.status{margin-top:12px;padding:10px 12px;border-radius:6px;display:none}
.ok{display:block;background:#e8f6ee;color:#125b32}
.err{display:block;background:#fdecef;color:#8a1026}
</style>
</head>
<body>
<main>
<h1>Update CookUnity credentials</h1>
<p>No credentials are loaded yet. Paste a fresh authenticated API request copied from DevTools as cURL, then save. Good choices are requests to <code>subscription.cookunity.com/sdui-service/cart</code>, <code>subscription.cookunity.com/sdui-service/incentives</code>, or <code>subscription.cookunity.com/menu-service/graphql</code>. Tracking requests such as <code>martech.cookunity.com/v1/t</code> do not include the token and cookie this app needs.</p>
<textarea id="curl" autofocus placeholder="curl 'https://subscription.cookunity.com/...' \
  -H 'authorization: ...' \
  -b '... appSession=...'"></textarea>
<div class="actions">
  <button id="test" type="button">Test connection</button>
  <button id="save" type="button">Save credentials</button>
</div>
<div id="status" class="status"></div>
</main>
<script>
const save = document.getElementById('save');
const test = document.getElementById('test');
const statusEl = document.getElementById('status');
async function postCurl(path) {
  const curl = document.getElementById('curl').value.trim();
  if (!curl) throw new Error('Paste a curl command first.');
  const res = await fetch(path, {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({curl})
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
  return body;
}
test.addEventListener('click', async () => {
  statusEl.className = 'status';
  statusEl.style.display = 'none';
  test.disabled = true;
  save.disabled = true;
  const original = test.textContent;
  test.textContent = 'Testing...';
  try {
    const body = await postCurl('/api/auth/check');
    if (!body.ok) throw new Error(body.message || ('Auth failed (HTTP ' + body.status + ')'));
    statusEl.className = 'status ok';
    statusEl.textContent = 'Auth OK. Credentials have not been saved yet.';
    statusEl.style.display = 'block';
  } catch (e) {
    statusEl.className = 'status err';
    statusEl.textContent = String(e.message || e);
    statusEl.style.display = 'block';
  } finally {
    test.disabled = false;
    save.disabled = false;
    test.textContent = original;
  }
});
save.addEventListener('click', async () => {
  statusEl.className = 'status';
  statusEl.style.display = 'none';
  save.disabled = true;
  test.disabled = true;
  const original = save.textContent;
  save.textContent = 'Saving...';
  try {
    await postCurl('/api/creds');
    statusEl.className = 'status ok';
    statusEl.textContent = 'Saved. Loading menu...';
    statusEl.style.display = 'block';
    setTimeout(() => { location.href = '/'; }, 700);
  } catch (e) {
    statusEl.className = 'status err';
    statusEl.textContent = String(e.message || e);
    statusEl.style.display = 'block';
    save.disabled = false;
    test.disabled = false;
    save.textContent = original;
  }
});
</script>
</body>
</html>"""
            self._write(
                200,
                "text/html; charset=utf-8",
                body,
                extra_headers={"cache-control": "no-store"},
            )

        def _resolve_date(self, payload: dict | None = None) -> str:
            """Prefer ``?date=`` in the URL; fall back to the JSON body; then default."""
            if "date=" in self.path:
                return _date_from_query(self.path, default_date_fn())
            if payload is not None:
                return _date_from_body(payload, default_date_fn())
            return default_date_fn()

        # -- routing ----------------------------------------------------------
        def do_GET(self):  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._get_index()
            if path == "/api/cart":
                return self._get_cart()
            if path == "/api/day":
                return self._get_day()
            if path == "/api/creds":
                return self._get_creds()
            if path == "/api/auth/check":
                return self._auth_check()
            self.send_error(404)

        def do_POST(self):  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            routes = {
                "/api/cart/add": self._cart_add,
                "/api/cart/remove": self._cart_remove,
                "/api/refresh": self._refresh,
                "/api/creds": self._creds_update,
                "/api/auth/check": self._auth_check,
                "/api/order/preview": self._order_preview,
                "/api/order/place": self._order_place,
            }
            fn = routes.get(path)
            if fn:
                return fn()
            self.send_error(404)

        # -- GET handlers -----------------------------------------------------
        def _pick_landing_date(self) -> str:
            """Walk upcoming Mondays and return the first one without an order.

            Falls back to the default date if everything's ordered or upstream
            fails. Result is cached briefly so back-to-back reloads don't each
            spawn N CookUnity round-trips.
            """
            now = time.time()
            cached = landing_cache
            if cached["date"] and cached["expires"] > now:
                return cached["date"]  # type: ignore[return-value]

            dates = state.upcoming or [default_date_fn()]
            chosen = dates[0]
            for d in dates:
                status, body = proxy.get(d)
                if status != 200:
                    # Auth busted or upstream blip — show whatever we'd default
                    # to and let the JS auth indicator surface the problem.
                    continue
                try:
                    if not (json.loads(body) or {}).get("order"):
                        chosen = d
                        break
                except json.JSONDecodeError:
                    continue

            landing_cache["date"] = chosen
            landing_cache["expires"] = now + LANDING_CACHE_TTL
            return chosen

        def _get_index(self):
            if not proxy.token:
                return self._render_auth_bootstrap()
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if "date" not in qs:
                # Bare `/` — pick smart default and redirect so the URL bar
                # reflects what's being shown. Cheap on the second visit
                # thanks to the 60s landing cache.
                chosen = self._pick_landing_date()
                self.send_response(302)
                self.send_header("location", f"/?date={chosen}")
                self.send_header("cache-control", "no-store")
                self.end_headers()
                return
            try:
                d = _date_from_query(self.path, default_date_fn())
            except ValueError as e:
                return self._render_error(f"Couldn't load menu for that date: {e}")
            if d < date.today().isoformat():
                # Stale bookmark or a tab left open across a week boundary —
                # bounce to the landing redirect, which picks a current week.
                self.send_response(302)
                self.send_header("location", "/")
                self.send_header("cache-control", "no-store")
                self.end_headers()
                return
            try:
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
                d = _date_from_query(self.path, default_date_fn())
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            status, body = proxy.get_true_cart(d)
            self._write(status, "application/json", body, extra_headers={"cache-control": "no-store"})

        def _get_day(self):
            """Per-date metadata the cart payload doesn't carry: the order cutoff."""
            try:
                d = _date_from_query(self.path, default_date_fn())
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            self._json(200, {"date": d, "cutoff": proxy.cutoff_for(d)})

        def _auth_check(self):
            """Lightweight ping: hit the cart endpoint for the next upcoming
            Monday. 200 → creds work; 401/403 → expired; anything else → some
            other upstream issue. We never raise on this path so the UI can
            always render the result."""
            tested_unsaved = False
            restore_creds: tuple[str, str, str] | None = None
            if self.command == "POST":
                payload = self._read_json()
                curl_text = payload.get("curl") or ""
                if curl_text.strip():
                    try:
                        parsed = parse_curl(curl_text)
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    restore_creds = (proxy.token, proxy.cookie, proxy.cart_id)
                    proxy.update(
                        token=parsed["token"],
                        cookie=parsed["cookie"],
                        cart_id=parsed.get("cart_id"),
                    )
                    tested_unsaved = True
            if not proxy.token:
                return self._json(200, {
                    "ok": False,
                    "status": 0,
                    "message": "No credentials loaded — paste a curl in #auth.",
                })
            test_date = (state.upcoming or [default_date_fn()])[0]
            try:
                status, body = proxy.get(test_date)
            finally:
                if restore_creds:
                    proxy.token, proxy.cookie, proxy.cart_id = restore_creds
            if status == 200:
                response = {"ok": True, "status": 200, "tested_date": test_date}
                if tested_unsaved:
                    response["tested_unsaved"] = True
                return self._json(200, response)
            if status in (401, 403):
                response = {
                    "ok": False,
                    "status": status,
                    "tested_date": test_date,
                    "message": "Auth expired — paste a fresh curl in #auth.",
                }
                if tested_unsaved:
                    response["tested_unsaved"] = True
                return self._json(200, response)
            # Could be a transient upstream blip; surface the body for context.
            try:
                detail = json.loads(body).get("message") or json.loads(body).get("error") or ""
            except (json.JSONDecodeError, AttributeError):
                detail = body[:200].decode(errors="replace") if body else ""
            response = {
                "ok": False,
                "status": status,
                "tested_date": test_date,
                "message": f"Upstream returned {status}. {detail}".strip(),
            }
            if tested_unsaved:
                response["tested_unsaved"] = True
            return self._json(200, response)

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
                d = _date_from_query(self.path, default_date_fn())
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            try:
                landing_cache["date"] = None
                landing_cache["expires"] = 0.0
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
            # New creds can change what's ordered/visible: drop rendered pages
            # and the landing choice. The upcoming list itself needs no poke —
            # state.upcoming recomputes on every access.
            state.invalidate_all()
            landing_cache["date"] = None
            landing_cache["expires"] = 0.0
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
            status, body = proxy.get_true_cart(menu_date)
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
