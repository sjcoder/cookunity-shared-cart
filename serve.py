#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Interactive CookUnity menu browser with working add-to-cart.

The browser can't POST cross-origin to subscription.cookunity.com from a
localhost page (preflight CORS + cookies), so this script runs a tiny local
proxy. Requests to /api/cart/add from the page get forwarded to the real
CookUnity cart endpoint with the JWT + cookie from .env attached.

Usage:
    ./serve.py                      # serves the newest menu JSON in ./menus
    ./serve.py --date 2026-04-27    # pick a specific date
    ./serve.py --port 8765          # change port (default 8000)

Prereqs:
    1. Run ./scrape.py <date> first to produce menus/<date>.json
    2. .env must contain CU_AUTH_TOKEN, CU_COOKIE, CU_CART_ID
       (CU_CART_ID is the UUID in the /cart/v2/<id>/products URL.)
"""

import argparse
import html
import json
import os
import re
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date as date_cls, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scrape import fetch_menu  # noqa: E402  (re-uses GraphQL query + auth plumbing)

MENU_DIR = Path(__file__).parent / "menus"
STATE_DIR = Path(__file__).parent / "state"
CREDS_PATH = STATE_DIR / "creds.json"
CART_ADD_ENDPOINT = "https://subscription.cookunity.com/sdui-service/cart/v2/{cart_id}/products"
CART_GET_ENDPOINT = "https://subscription.cookunity.com/sdui-service/cart/v2/{date}"
PRICE_BREAKDOWN_ENDPOINT = "https://subscription.cookunity.com/sdui-service/view/v1/price-breakdown/"
CREATE_ORDER_ENDPOINT = "https://subscription.cookunity.com/subscription-back/graphql/user"
CREATE_ORDER_QUERY = """mutation createOrder($order: CreateOrderInput!, $origin: OperationOrigin) {
  createOrder(order: $order, origin: $origin) {
    __typename
    ... on OrderCreation { id deliveryDate paymentStatus __typename }
    ... on OrderCreationError { error outOfStockIds __typename }
  }
}
"""


def upcoming_mondays(n: int = 4, today: date_cls | None = None) -> list[str]:
    """Next N Monday delivery dates starting from the nearest upcoming Monday.
    If today is Monday, we keep today (ordering may still be open); otherwise
    skip forward to the next Monday.
    """
    today = today or date_cls.today()
    days = (0 - today.weekday()) % 7  # 0 = Monday
    first = today + timedelta(days=days)
    return [(first + timedelta(days=7 * i)).isoformat() for i in range(n)]


# -- Curl-paste parsing -------------------------------------------------------
# Users drop a raw `curl ...` command from DevTools. We extract auth JWT,
# cookie header, and (optionally) the cart UUID from the URL path.

def _decode_ansi_c(s: str) -> str:
    """Decode the subset of $'...' escapes that Chrome's copy-as-cURL emits."""
    escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}

    def sub(m: re.Match) -> str:
        if m.group(1):
            return chr(int(m.group(1), 16))
        if m.group(2):
            return chr(int(m.group(2), 16))
        c = m.group(3)
        return escapes.get(c, c)

    return re.sub(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})|\\(.)", sub, s)


def parse_curl(text: str) -> dict:
    """Pull auth/cookie/cart_id out of a pasted curl command.
    Raises ValueError with a human-readable reason on failure.
    """
    # Normalize $'...' blocks so we can treat them as regular single-quoted.
    def _ansi_c(m: re.Match) -> str:
        return "'" + _decode_ansi_c(m.group(1)) + "'"
    text = re.sub(r"\$'((?:[^'\\]|\\.)*)'", _ansi_c, text, flags=re.DOTALL)

    # authorization header
    auth = None
    m = re.search(r"-H\s+['\"]authorization:\s*([^'\"]+?)['\"]", text, re.IGNORECASE)
    if m:
        auth = m.group(1).strip()
        if auth.lower().startswith("bearer "):
            auth = auth[7:].strip()

    # cookie: either -b '...' or -H 'cookie: ...'
    cookie = None
    m = re.search(r"-b\s+['\"]((?:[^'\"\\]|\\.)*)['\"]", text, re.DOTALL)
    if m:
        cookie = m.group(1)
    else:
        m = re.search(r"-H\s+['\"]cookie:\s*([^'\"]+?)['\"]", text, re.IGNORECASE)
        if m:
            cookie = m.group(1)

    # cart UUID from any URL mention
    cart_id = None
    m = re.search(r"/cart/v2/([0-9a-fA-F-]{36})", text)
    if m:
        cart_id = m.group(1)

    if not auth:
        raise ValueError("Could not find an `authorization:` header in the curl.")
    if not cookie:
        raise ValueError("Could not find a cookie (`-b '...'` or `-H 'cookie: ...'`).")
    if "appSession=" not in cookie:
        raise ValueError("Cookie is missing `appSession=...` — that's the session token the API requires.")

    return {"token": auth, "cookie": cookie, "cart_id": cart_id}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _esc(v) -> str:
    return "" if v is None else html.escape(str(v))


def _meal_image(meal: dict) -> str | None:
    url = meal.get("primaryImageUrl")
    if url:
        return url
    path = meal.get("image") or meal.get("imagePath")
    if path and path.startswith("http"):
        return path
    if path:
        return f"https://cu-media.imgix.net{path}"
    return None


def _fav_key(item: dict, is_bundle: bool) -> str:
    if is_bundle:
        sku = item.get("sku") or item.get("inventoryId") or ""
        return f"b-{sku}"
    return f"m-{item.get('id')}"


def _card(item: dict, is_bundle: bool = False) -> str:
    inv_id = _esc(item.get("inventoryId") or "")
    fav_key = _esc(_fav_key(item, is_bundle))
    img = item.get("image") if is_bundle else _meal_image(item)
    if is_bundle and img and not img.startswith("http"):
        img = f"https://cu-media.imgix.net{img}"
    name = _esc(item.get("name"))
    desc = _esc(item.get("shortDescription") or item.get("subtitle") or item.get("description") or "")
    price = item.get("finalPrice") or item.get("price")
    price_html = f'<span class="price">${price:.2f}</span>' if isinstance(price, (int, float)) else ""

    chef_html = ""
    if not is_bundle:
        c = item.get("chef") or {}
        full = f"{c.get('firstName') or ''} {c.get('lastName') or ''}".strip()
        if full:
            chef_html = f'<div class="chef">Chef {_esc(full)}</div>'

    rating_html = ""
    if not is_bundle:
        stars = item.get("stars")
        reviews = item.get("reviews")
        if stars:
            rating_html = f'<span class="rating">★ {stars}</span>'
            if reviews:
                rating_html += f' <span>({reviews:,})</span>'

    nutrition_html = ""
    if not is_bundle:
        nf = item.get("nutritionalFacts") or {}
        parts = []
        if nf.get("calories"):
            parts.append(f"{nf['calories']} cal")
        if nf.get("protein"):
            parts.append(f"{nf['protein']}g protein")
        if nf.get("carbs"):
            parts.append(f"{nf['carbs']}g carbs")
        if nf.get("fat"):
            parts.append(f"{nf['fat']}g fat")
        if parts:
            nutrition_html = f'<div class="nutrition">{_esc(" · ".join(parts))}</div>'

    badges = []
    if item.get("isNewMeal") or item.get("isNewBundle"):
        badges.append('<span class="badge new">New</span>')
    if item.get("isPremium"):
        badges.append('<span class="badge premium">Premium</span>')
    badges_html = f'<div class="badges">{"".join(badges)}</div>' if badges else ""

    # Store a JSON blob per card for the cart UI so we don't re-fetch.
    payload = json.dumps({
        "inventoryId": item.get("inventoryId"),
        "name": item.get("name"),
        "image": img,
        "price": price,
        "isBundle": is_bundle,
    })
    payload_attr = _esc(payload)

    searchable = " ".join(filter(None, [
        item.get("name") or "",
        desc,
        ((item.get("chef") or {}).get("firstName") or "") + " " + ((item.get("chef") or {}).get("lastName") or ""),
        " ".join(item.get("cuisines") or []),
        (item.get("category") or {}).get("title") or "",
    ])).lower()

    thumb = (
        f'<div class="thumb"><img src="{_esc(img)}" alt="" loading="lazy"></div>'
        if img else '<div class="thumb"></div>'
    )

    return (
        f'<article class="card" data-inv="{inv_id}" data-key="{fav_key}" data-search="{_esc(searchable)}" data-item=\'{payload_attr}\'>'
        f'{thumb}'
        f'<button class="fav-btn" type="button" data-key="{fav_key}" title="Toggle favorite" aria-label="Toggle favorite">☆</button>'
        '<div class="body">'
        f'{chef_html}'
        f'<div class="name">{name}</div>'
        f'<div class="desc">{desc}</div>'
        f'<div class="meta">{price_html} {rating_html}</div>'
        f'{nutrition_html}'
        f'{badges_html}'
        f'<div class="qty-wrap">'
        f'<button class="add-btn" type="button" data-inv="{inv_id}">Add to cart</button>'
        f'<div class="stepper" role="group" aria-label="Quantity">'
        f'<button class="qty-dec" type="button" aria-label="Remove one">−</button>'
        f'<span class="qty">0</span>'
        f'<button class="qty-inc" type="button" aria-label="Add one">+</button>'
        f'</div>'
        f'</div>'
        '</div>'
        '</article>'
    )


PAGE_CSS = """
:root { color-scheme: light; --fg:#2d1b2e; --muted:#8a7a8e; --line:#f3dae4; --accent:#e85a8e; --ok:#2aa06a; --bg:#fff6fa; --paw:#ff92b8; }
* { box-sizing: border-box; }
html, body { margin:0; padding:0; background-color:#fff6fa; color-scheme: light; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif; color:var(--fg); background-color:#fff6fa; background-image: radial-gradient(1200px 600px at 15% -10%, #ffe3ef 0%, rgba(255,227,239,0) 60%), radial-gradient(1000px 500px at 90% 110%, #ffd9e8 0%, rgba(255,217,232,0) 55%); background-attachment: fixed; padding-bottom:72px; }
body.cart-open { padding-bottom:calc(min(40vh, 360px) + 72px); }
.topbar { position:sticky; top:0; z-index:20; background:rgba(255,255,255,.92); backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px); border-bottom:1px solid var(--line); padding:10px 20px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
.topbar h1 { font-size:20px; margin:0; display:flex; align-items:center; gap:8px; font-weight:700; letter-spacing:-0.01em; }
.topbar h1 .emoji { font-size:26px; filter: drop-shadow(0 1px 2px rgba(232,90,142,.25)); animation: bop 3.2s ease-in-out infinite; display:inline-block; }
@keyframes bop { 0%,100% { transform: translateY(0) rotate(-4deg); } 50% { transform: translateY(-3px) rotate(4deg); } }
.topbar h1 .kitty { color:var(--accent); }
.topbar .meta { color:var(--muted); font-size:13px; }
.topbar input[type=search], .topbar select { padding:6px 10px; border:1px solid var(--line); border-radius:6px; font-size:14px; }
.topbar input[type=search] { flex:1; min-width:220px; }
.topbar label { font-size:13px; color:var(--muted); display:inline-flex; align-items:center; gap:6px; }
.topbar button#refresh { padding:6px 12px; border:1px solid var(--fg); background:#fff; border-radius:6px; cursor:pointer; font-size:13px; font-weight:600; }
.topbar button#refresh:hover { background:var(--fg); color:#fff; }
.topbar button#refresh[disabled] { opacity:.6; cursor:wait; }
.page { max-width:1200px; margin:0 auto; padding:20px 24px; }
section.category { margin: 20px 0 8px; }
section.category > h2 { font-size:16px; margin:0 0 10px; padding-bottom:6px; border-bottom:1px solid var(--line); text-transform:uppercase; letter-spacing:.04em; }
section.category.hidden { display:none; }
.grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap:14px; }
.card { position:relative; border:1px solid var(--line); border-radius:14px; overflow:hidden; background:#fff; display:flex; flex-direction:column; transition: transform .12s ease, box-shadow .12s ease; }
.card:hover { transform: translateY(-2px); box-shadow: 0 6px 18px rgba(232,90,142,.12); }
.card.in-cart { outline:2px solid var(--ok); outline-offset:-2px; box-shadow:0 4px 14px rgba(42,160,106,.15); }
.card.hidden { display:none; }
.card .thumb { aspect-ratio: 4/3; background:#f0f0f0; overflow:hidden; cursor:zoom-in; outline:3px solid transparent; outline-offset:-3px; transition:outline-color .12s, box-shadow .12s; }
.card .thumb:hover { outline-color:var(--accent); box-shadow:inset 0 0 0 3px var(--accent); }
.card .thumb img { width:100%; height:100%; object-fit:cover; display:block; transition:transform .2s; }
.card .thumb:hover img { transform:scale(1.03); }
.lightbox { position:fixed; inset:0; background:rgba(0,0,0,.92); display:none; align-items:center; justify-content:center; z-index:100; cursor:zoom-out; }
.lightbox.open { display:flex; }
.lightbox img { max-width:95vw; max-height:90vh; box-shadow:0 12px 48px rgba(0,0,0,.5); border-radius:4px; }
.lightbox .caption { position:absolute; bottom:24px; left:50%; transform:translateX(-50%); color:#fff; font-size:14px; background:rgba(0,0,0,.6); padding:8px 16px; border-radius:6px; max-width:80vw; text-align:center; }
.lightbox .close { position:absolute; top:16px; right:20px; color:#fff; font-size:28px; background:none; border:none; cursor:pointer; padding:6px 12px; }
.lightbox .close:hover { color:var(--accent); }
.card .body { padding:10px 12px 12px; display:flex; flex-direction:column; gap:4px; flex:1; }
.card .name { font-size:14px; font-weight:600; line-height:1.25; }
.card .chef { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
.card .desc { font-size:12px; color:#333; }
.card .meta { display:flex; flex-wrap:wrap; gap:8px; font-size:12px; color:var(--muted); margin-top:4px; }
.card .meta .price { color:var(--fg); font-weight:600; }
.card .meta .rating { color:var(--accent); font-weight:600; }
.card .nutrition { font-size:10px; color:var(--muted); }
.card .badges { display:flex; flex-wrap:wrap; gap:4px; }
.card .badge { font-size:10px; padding:2px 6px; border-radius:3px; background:#f0f0f0; color:#333; text-transform:uppercase; letter-spacing:.04em; }
.card .badge.new { background:#fff3e0; color:#b35600; }
.card .badge.premium { background:#f3ecff; color:#6b2fbd; }
.card .qty-wrap { margin-top:auto; }
.card .add-btn { display:block; width:100%; padding:8px 10px; font-size:13px; font-weight:600; border:1px solid var(--accent); background:#fff; color:var(--accent); border-radius:999px; cursor:pointer; transition: background .12s, color .12s; }
.card .add-btn:hover { background:var(--accent); color:#fff; }
.card .add-btn[disabled] { opacity:.6; cursor:wait; }
.card .stepper { display:none; align-items:center; justify-content:space-between; gap:6px; width:100%; padding:2px 4px; border:1.5px solid var(--fg); border-radius:999px; font-weight:700; }
.card .stepper button { background:transparent; border:none; font-size:18px; line-height:1; width:30px; height:30px; display:flex; align-items:center; justify-content:center; cursor:pointer; border-radius:50%; color:inherit; padding:0; }
.card .stepper button:hover { background:rgba(0,0,0,.05); }
.card .stepper button:disabled { opacity:.4; cursor:wait; }
.card .stepper .qty { font-size:14px; min-width:20px; text-align:center; }
.card.in-cart .add-btn { display:none; }
.card.in-cart .stepper { display:flex; border-color:var(--ok); color:var(--ok); }
.card.in-cart .stepper button:hover { background:rgba(42,160,106,.1); }
.fav-btn { position:absolute; top:8px; right:8px; z-index:2; width:32px; height:32px; border-radius:999px; border:none; background:rgba(255,255,255,.9); color:#999; font-size:18px; line-height:1; cursor:pointer; box-shadow:0 1px 4px rgba(0,0,0,.15); display:flex; align-items:center; justify-content:center; padding:0; }
.fav-btn:hover { color:#e6a800; background:#fff; }
.card.fav .fav-btn { color:#e6a800; }
.card.fav .fav-btn::before { content:'★'; }
.card.fav .fav-btn { font-size:0; }
.card.fav .fav-btn::before { font-size:18px; }
.nav-link { padding:6px 10px; border-radius:6px; color:var(--fg); text-decoration:none; font-size:13px; font-weight:600; border:1px solid var(--line); }
.nav-link:hover { background:var(--fg); color:#fff; border-color:var(--fg); }
.nav-link .count { display:inline-block; margin-left:4px; font-size:11px; background:#e6a800; color:#fff; padding:1px 6px; border-radius:999px; }
#favorites-view { display:none; }
body.view-favorites #favorites-view { display:block; }
body.view-favorites main.page { display:none; }
#favorites-view .page { max-width:900px; margin:0 auto; padding:24px; }
#favorites-view h2 { margin:0 0 12px; font-size:22px; }
#favorites-view .empty { padding:40px 0; color:var(--muted); text-align:center; font-size:14px; }
#favorites-view .fav-list { display:flex; flex-direction:column; gap:10px; }
#favorites-view .fav-row { display:flex; gap:14px; align-items:center; padding:10px 12px; border:1px solid var(--line); border-radius:10px; background:#fff; }
#favorites-view .fav-row img { width:80px; height:60px; object-fit:cover; border-radius:6px; background:#f0f0f0; }
#favorites-view .fav-row .info { flex:1; display:flex; flex-direction:column; gap:2px; }
#favorites-view .fav-row .info .name { font-weight:600; font-size:14px; }
#favorites-view .fav-row .info .meta { font-size:12px; color:var(--muted); }
#favorites-view .fav-row .unavail { color:#b35600; font-weight:600; font-size:12px; }
#favorites-view .fav-row button { padding:6px 12px; font-size:13px; border:1px solid var(--line); background:#fff; border-radius:6px; cursor:pointer; font-weight:600; }
#favorites-view .fav-row button.add { border-color:var(--fg); }
#favorites-view .fav-row button.add:hover { background:var(--fg); color:#fff; }
#favorites-view .fav-row button.remove { color:#b00020; border-color:#f4c7ce; }
#favorites-view .fav-row button.remove:hover { background:#b00020; color:#fff; }
#favorites-view .actions { display:flex; gap:10px; margin:14px 0 6px; }
#auth-view { display:none; }
body.view-auth #auth-view { display:block; }
body.view-auth main.page { display:none; }
body.view-auth #favorites-view { display:none !important; }
#auth-view .page { max-width:820px; margin:0 auto; padding:24px; }
#auth-view h2 { margin:0 0 6px; font-size:22px; }
#auth-view p { color:var(--muted); font-size:13px; line-height:1.5; margin:0 0 12px; }
#auth-view textarea { width:100%; min-height:200px; padding:12px; border:1px solid var(--line); border-radius:8px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size:11px; line-height:1.4; }
#auth-view .actions { display:flex; gap:10px; margin-top:10px; }
#auth-view button { padding:8px 16px; font-size:14px; font-weight:600; border-radius:6px; cursor:pointer; }
#auth-view button.primary { border:1px solid var(--fg); background:var(--fg); color:#fff; }
#auth-view button.primary:hover { background:#000; }
#auth-view button.primary[disabled] { opacity:.6; cursor:wait; }
#auth-view .status { margin-top:14px; padding:10px 14px; border-radius:6px; font-size:13px; display:none; }
#auth-view .status.ok { display:block; background:#e7f5eb; color:#157a3c; border:1px solid #c1e6cb; }
#auth-view .status.err { display:block; background:#fde4e4; color:#b00020; border:1px solid #f4c7ce; white-space:pre-wrap; }
#auth-view .creds-info { margin-top:18px; padding:12px 14px; background:#fff; border:1px solid var(--line); border-radius:8px; font-size:12px; color:var(--muted); }
#auth-view .creds-info code { font-family: ui-monospace, Menlo, monospace; background:#f0f0f0; padding:1px 6px; border-radius:3px; color:#333; }
.topbar select#date-picker { font-weight:600; padding:6px 10px; border:1px solid var(--fg); border-radius:6px; font-size:13px; }
#cart { position:fixed; bottom:0; left:0; right:0; background:#fff; border-top:1px solid var(--line); box-shadow:0 -6px 20px rgba(232,90,142,.08); z-index:30; display:flex; flex-direction:column; overflow:hidden; }
#cart header { display:flex; align-items:center; gap:12px; padding:10px 20px; border-bottom:1px solid var(--line); cursor:pointer; user-select:none; flex-wrap:wrap; }
#cart header h3 { margin:0; font-size:14px; text-transform:uppercase; letter-spacing:.04em; display:flex; align-items:center; gap:6px; }
#cart header h3::before { content:'🐾'; font-size:16px; }
#cart header .chevron { margin-left:6px; color:var(--muted); font-size:12px; transition:transform .2s; }
#cart.collapsed .chevron { transform:rotate(-180deg); }
#cart .items { display:block; }
#cart.collapsed .items { display:none; }
#cart header .count { background:var(--ok); color:#fff; font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; }
#cart header .plan-progress { font-size:12px; color:var(--muted); padding:2px 10px; border-radius:999px; }
#cart header .plan-progress.short { color:#b35600; font-weight:600; background:#fff3e0; }
#cart header .plan-progress.met { color:#fff; font-weight:600; background:var(--ok); }
#cart header .plan-progress.extras { color:#7a4b00; font-weight:600; background:#ffe0a3; }
.extra-tag { display:inline-block; font-size:10px; padding:1px 6px; border-radius:3px; background:#ffe0a3; color:#7a4b00; text-transform:uppercase; letter-spacing:.04em; margin-left:4px; }
.extras-hint { font-size:11px; color:#7a4b00; background:#fff3d6; padding:1px 6px; border-radius:3px; font-weight:600; }
#order-banner { margin:16px auto 0; max-width:1200px; padding:0 24px; }
.order-banner-inner { display:flex; align-items:center; gap:12px; padding:12px 16px; background:linear-gradient(90deg,#e8f7ee,#d7f0e0); border:1px solid #aed9bf; border-radius:12px; color:#0f5d35; box-shadow:0 2px 8px rgba(42,160,106,.08); }
.order-ico { display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; border-radius:50%; background:var(--ok); color:#fff; font-weight:700; flex-shrink:0; }
.order-text { display:flex; flex-direction:column; gap:2px; }
.order-title { font-weight:600; font-size:14px; }
.order-sub { font-size:12px; color:#2c6f4e; }
/* When this week's order is locked in, dim and disable cart editing. */
body.ordered .card .add-btn, body.ordered .card .stepper, body.ordered #cart .row-stepper { opacity:.45; pointer-events:none; }
body.ordered .card.in-cart::after { content:'ORDERED'; position:absolute; top:8px; left:8px; z-index:2; font-size:10px; font-weight:700; letter-spacing:.08em; background:var(--ok); color:#fff; padding:3px 8px; border-radius:3px; }
body.ordered #review-order { display:none !important; }
.review-btn { padding:6px 14px; border:none; background:var(--accent); color:#fff; font-weight:700; font-size:13px; border-radius:999px; cursor:pointer; box-shadow:0 2px 6px rgba(232,90,142,.2); }
.review-btn:hover { background:#d3497d; }
.modal { display:none; position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:200; align-items:center; justify-content:center; padding:20px; }
.modal.open { display:flex; }
.modal-card { background:#fff; border-radius:16px; width:100%; max-width:500px; max-height:85vh; overflow-y:auto; padding:24px; position:relative; box-shadow:0 20px 60px rgba(0,0,0,.2); }
.modal-card h3 { margin:0 0 4px; font-size:20px; }
.modal-close { position:absolute; top:12px; right:14px; background:none; border:none; font-size:20px; cursor:pointer; color:var(--muted); padding:6px 10px; }
.modal-close:hover { color:var(--fg); }
.modal-meta { color:var(--muted); font-size:13px; margin-bottom:14px; }
.breakdown { padding:12px 0; border-top:1px solid var(--line); border-bottom:1px solid var(--line); font-size:14px; min-height:40px; }
.breakdown .row { display:flex; justify-content:space-between; padding:6px 0; }
.breakdown .row.total { border-top:1px solid var(--line); margin-top:6px; padding-top:10px; font-weight:700; font-size:16px; }
.breakdown .row.discount { color:var(--ok); }
.breakdown .placeholder { color:var(--muted); text-align:center; padding:14px 0; }
.modal-actions { display:flex; gap:10px; justify-content:flex-end; margin-top:16px; }
.modal-actions button { padding:10px 18px; font-size:14px; font-weight:600; border-radius:999px; cursor:pointer; border:1px solid var(--line); background:#fff; }
.modal-actions .modal-confirm { background:var(--ok); border-color:var(--ok); color:#fff; }
.modal-actions .modal-confirm:hover:not(:disabled) { background:#1f8955; }
.modal-actions .modal-confirm:disabled { opacity:.5; cursor:wait; }
.modal-actions .modal-cancel:hover { background:var(--line); }
.modal-status { margin-top:12px; font-size:13px; text-align:center; }
.modal-status.ok { color:var(--ok); font-weight:600; }
.modal-status.err { color:#b00020; white-space:pre-wrap; }
#cart header .total { margin-left:auto; font-weight:600; }
#cart header .synced { font-size:11px; color:var(--muted); }
#cart header button { padding:4px 10px; font-size:12px; border:1px solid var(--line); background:#fff; border-radius:4px; cursor:pointer; }
#cart .items { overflow-y:auto; padding: 4px 20px 14px; max-height:min(40vh, 360px); }
#cart .item { display:flex; align-items:center; gap:10px; padding:6px 0; border-bottom:1px dashed var(--line); font-size:13px; min-width:0; }
#cart .item img { width:36px; height:36px; object-fit:cover; border-radius:4px; flex-shrink:0; }
#cart .item .name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#cart .item .price { color:var(--muted); flex-shrink:0; }
#cart .item .row-stepper { flex-shrink:0; }
#cart .item button.remove-solo { flex-shrink:0; font-size:11px; padding:2px 6px; border:1px solid var(--line); background:#fff; border-radius:4px; cursor:pointer; }
#cart .item .row-stepper { display:flex; align-items:center; gap:2px; border:1.5px solid var(--fg); border-radius:999px; padding:1px 3px; }
#cart .item .row-stepper button { font-size:14px; width:22px; height:22px; padding:0; display:flex; align-items:center; justify-content:center; background:transparent; border:none; border-radius:50%; cursor:pointer; color:var(--fg); font-weight:700; }
#cart .item .row-stepper button:hover { background:rgba(0,0,0,.06); }
#cart .item .row-stepper button:disabled { opacity:.4; cursor:wait; }
#cart .item .row-stepper .qty { font-size:12px; min-width:16px; text-align:center; font-weight:700; }
#toast { position:fixed; top:60px; left:50%; transform:translateX(-50%); background:#222; color:#fff; padding:10px 16px; border-radius:8px; font-size:13px; opacity:0; pointer-events:none; transition: opacity .2s; z-index:40; }
#toast.show { opacity:1; }
#toast.err { background:#b00020; }
"""


PAGE_JS = """
// Server-of-truth cart. `cartState` mirrors what CookUnity returned on the
// last GET /api/cart. Map<inv_id, {quantity, ...serverFields}>.
let cartState = new Map();
let cartTotal = 0;
let cartMinItems = 0;
const MENU_INDEX = window.MENU_INDEX || {};
const FAV_INDEX = window.FAV_INDEX || {};
const MENU_DATE = window.MENU_DATE;

function withDate(path) {
  const u = new URL(path, location.origin);
  u.searchParams.set('date', MENU_DATE);
  return u.pathname + u.search;
}

// Favorites are local-only, keyed by stable meal/bundle id so they survive
// across weeks when inventoryIds change. Shape: {key: {name,image,price,chef,addedAt,isBundle}}.
function loadFavs() {
  try { return JSON.parse(localStorage.getItem('cu_favs') || '{}'); }
  catch { return {}; }
}
function saveFavs(f) { localStorage.setItem('cu_favs', JSON.stringify(f)); }
let favs = loadFavs();

function isFav(key) { return Object.prototype.hasOwnProperty.call(favs, key); }

function toggleFav(key, snapshot) {
  if (isFav(key)) delete favs[key];
  else favs[key] = { ...snapshot, addedAt: new Date().toISOString() };
  saveFavs(favs);
  syncFavUI();
}

function syncFavUI() {
  document.querySelectorAll('.card').forEach(c => {
    c.classList.toggle('fav', isFav(c.dataset.key));
  });
  const count = Object.keys(favs).length;
  const countEl = document.getElementById('favs-count');
  if (countEl) {
    countEl.textContent = count;
    countEl.style.display = count ? '' : 'none';
  }
  if (document.body.classList.contains('view-favorites')) renderFavoritesView();
}

function renderFavoritesView() {
  const list = document.getElementById('favs-list');
  const empty = document.getElementById('favs-empty');
  list.innerHTML = '';
  const keys = Object.keys(favs).sort((a, b) => (favs[b].addedAt || '').localeCompare(favs[a].addedAt || ''));
  if (keys.length === 0) {
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  for (const key of keys) {
    const snap = favs[key];
    const current = FAV_INDEX[key];           // non-null = on this week's menu
    const row = document.createElement('div');
    row.className = 'fav-row';
    const img = (current && current.image) || snap.image || '';
    const name = (current && current.name) || snap.name || key;
    const price = (current && current.price) ?? snap.price;
    const chef = (current && current.chef) || snap.chef || '';
    const priceStr = typeof price === 'number' ? '$' + price.toFixed(2) : '';
    row.innerHTML = `
      <img src="${img}" alt="" onerror="this.style.visibility='hidden'">
      <div class="info">
        <div class="name">${name}</div>
        <div class="meta">${chef ? 'Chef ' + chef + ' · ' : ''}${priceStr}${!current ? ' · <span class="unavail">Not on this week\\'s menu</span>' : ''}</div>
      </div>
      ${current ? `<button class="add" data-inv="${current.inventoryId}">Add to cart</button>` : ''}
      <button class="remove" data-key="${key}">Remove</button>
    `;
    const addBtn = row.querySelector('button.add');
    if (addBtn) addBtn.addEventListener('click', () => addToCart(addBtn.dataset.inv));
    row.querySelector('button.remove').addEventListener('click', () => {
      delete favs[key];
      saveFavs(favs);
      syncFavUI();
    });
    list.appendChild(row);
  }
}

function applyRoute() {
  const isFavs = location.hash === '#favorites';
  const isAuth = location.hash === '#auth';
  document.body.classList.toggle('view-favorites', isFavs);
  document.body.classList.toggle('view-auth', isAuth);
  document.getElementById('favs-link').style.display = (isFavs || isAuth) ? 'none' : '';
  document.getElementById('auth-link').style.display = (isFavs || isAuth) ? 'none' : '';
  document.getElementById('menu-link').style.display = (isFavs || isAuth) ? '' : 'none';
  if (isFavs) renderFavoritesView();
  if (isAuth) loadCredsInfo();
}

async function loadCredsInfo() {
  try {
    const res = await fetch('/api/creds');
    const body = await res.json();
    const el = document.getElementById('creds-info');
    el.innerHTML = body.token
      ? `Current creds: token ends <code>…${body.token_tail}</code>, cart <code>${body.cart_id || '(none)'}</code>, source: <code>${body.source}</code>, saved: <code>${body.saved_at || '—'}</code>`
      : 'No credentials loaded yet.';
  } catch {}
}

async function saveCreds() {
  const btn = document.getElementById('auth-save');
  const status = document.getElementById('auth-status');
  status.className = 'status';
  status.textContent = '';
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Saving…';
  try {
    const curl = document.getElementById('auth-curl').value.trim();
    if (!curl) throw new Error('Paste a curl command first.');
    const res = await fetch('/api/creds', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ curl }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
    status.className = 'status ok';
    status.textContent = `Saved. Token tail …${body.token_tail}. Cart ${body.cart_id || '(unchanged)'}. Reloading…`;
    setTimeout(() => { location.href = withDate('/'); }, 900);
  } catch (e) {
    status.className = 'status err';
    status.textContent = String(e.message || e);
    btn.disabled = false;
    btn.textContent = orig;
  }
}

function toast(msg, isErr=false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.toggle('err', isErr);
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), 2500);
}

function fmtTime() {
  const d = new Date();
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function cartTotalQuantity() {
  let n = 0;
  for (const v of cartState.values()) n += (v.quantity || 1);
  return n;
}

function renderCart() {
  const wrap = document.querySelector('#cart .items');
  const countEl = document.querySelector('#cart .count');
  const totalEl = document.querySelector('#cart .total');
  const planEl = document.querySelector('#cart .plan-progress');
  wrap.innerHTML = '';
  let count = 0;
  for (const [inv, item] of cartState) {
    count += (item.quantity || 1);
    const meta = MENU_INDEX[inv] || {};
    const name = meta.name || inv;
    const img = meta.image || '';
    const isExtra = !!item.is_extra;
    const unitPrice = typeof meta.price === 'number' ? meta.price : null;
    const linePrice = unitPrice !== null ? unitPrice * (item.quantity || 1) : null;
    const row = document.createElement('div');
    row.className = 'item';
    row.innerHTML = `
      <img src="${img}" alt="" onerror="this.style.visibility='hidden'">
      <div class="name">${name}${meta.chef ? ' <span style=\"color:var(--muted);font-size:11px\">· ' + meta.chef + '</span>' : ''}${isExtra ? ' <span class=\"extra-tag\">extra</span>' : ''}</div>
      <div class="row-stepper" data-inv="${inv}">
        <button class="qty-dec" aria-label="Remove one">−</button>
        <span class="qty">${item.quantity || 1}</span>
        <button class="qty-inc" aria-label="Add one">+</button>
      </div>
      <div class="price">${linePrice !== null ? '$' + linePrice.toFixed(2) : ''}</div>
    `;
    row.querySelector('.qty-dec').addEventListener('click', () => removeFromCart(inv));
    row.querySelector('.qty-inc').addEventListener('click', () => addToCart(inv));
    wrap.appendChild(row);
  }
  countEl.textContent = count;
  countEl.style.display = count ? '' : 'none';
  totalEl.textContent = cartTotal ? '$' + cartTotal.toFixed(2) : '';

  // Plan state: empty → hidden; short → red; met → green; extras → amber.
  planEl.classList.remove('short', 'met', 'extras');
  const extras = [...cartState.values()].filter(v => v.is_extra).reduce((a, v) => a + (v.quantity || 1), 0);
  if (!cartMinItems) {
    planEl.textContent = '';
  } else if (count < cartMinItems) {
    planEl.textContent = `${count} / ${cartMinItems} plan min`;
    planEl.classList.add('short');
  } else if (extras > 0) {
    planEl.textContent = `plan full ✓ · ${extras} extra${extras > 1 ? 's' : ''}`;
    planEl.classList.add('extras');
  } else {
    planEl.textContent = `plan full ✓`;
    planEl.classList.add('met');
  }

  // Decorate each card with its current tier's extras price when we're past the plan.
  const atPlan = cartMinItems && count >= cartMinItems;
  document.querySelectorAll('.card').forEach(c => {
    const inv = c.dataset.inv;
    const inCart = cartState.has(inv);
    c.classList.toggle('in-cart', inCart);
    const qtyEl = c.querySelector('.stepper .qty');
    if (qtyEl) qtyEl.textContent = inCart ? (cartState.get(inv).quantity || 1) : 0;
    // Extras price hint: price of the NEXT meal (position count+1).
    const hint = c.querySelector('.extras-hint');
    const meta = MENU_INDEX[inv];
    if (atPlan && meta && meta.boxPrices) {
      const nextIdx = String(count + 1);
      const nextPrice = meta.boxPrices[nextIdx];
      if (typeof nextPrice === 'number') {
        const text = `extras rate: $${nextPrice.toFixed(2)}`;
        if (hint) hint.textContent = text;
        else {
          const meta_el = c.querySelector('.meta');
          if (meta_el) {
            const span = document.createElement('span');
            span.className = 'extras-hint';
            span.textContent = text;
            meta_el.appendChild(span);
          }
        }
      } else if (hint) {
        hint.remove();
      }
    } else if (hint) {
      hint.remove();
    }
  });
}

async function syncCart(showToast=false) {
  try {
    const res = await fetch(withDate('/api/cart'), { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const body = await res.json();
    const map = new Map();
    for (const p of body.products || []) {
      map.set(p.inventory_id, p);
    }
    cartState = map;
    cartTotal = (((body.metadata || {}).pricing || {}).total) || 0;
    cartMinItems = ((((body.metadata || {}).pricing || {}).base_plan || {}).min_items) || 0;
    const order = body.order || null;
    document.body.classList.toggle('ordered', !!order);
    renderOrderBanner(order);
    updateReviewButton(order);
    document.querySelector('#cart .synced').textContent = 'synced ' + fmtTime();
    renderCart();
    if (showToast) toast('Cart synced');
  } catch (e) {
    document.querySelector('#cart .synced').textContent = 'sync failed';
    if (showToast) toast('Sync failed: ' + e.message, true);
  }
}

function updateReviewButton(order) {
  const btn = document.getElementById('review-order');
  const count = cartTotalQuantity();
  const canReview = !order && count > 0 && (!cartMinItems || count >= cartMinItems);
  btn.style.display = canReview ? '' : 'none';
}

function extractBreakdownRows(data) {
  // Server-driven response; find dollar amounts + labels in the usual nesting
  // without being too strict — fall back to a simple total + raw dump.
  const rows = [];
  function walk(node) {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(walk); return; }
    const attrs = node.attributes || node;
    const label = attrs.label || attrs.title || attrs.name;
    const value = attrs.value || attrs.amount || attrs.price;
    if (typeof label === 'string' && (typeof value === 'string' || typeof value === 'number')) {
      if (/\\$[\\d,.]+/.test(String(value)) || /total|subtotal|fee|tax|tip|discount|delivery|extras|plan/i.test(label)) {
        rows.push({ label, value: String(value) });
      }
    }
    for (const k of Object.keys(node)) walk(node[k]);
  }
  walk(data);
  // Dedupe adjacent duplicates
  const seen = new Set();
  return rows.filter(r => {
    const k = r.label + '|' + r.value;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

function renderBreakdown(data) {
  const el = document.getElementById('breakdown');
  el.innerHTML = '';
  const rows = extractBreakdownRows(data);
  if (!rows.length) {
    el.innerHTML = '<div class="placeholder">No itemized breakdown returned — total below is authoritative.</div>';
  } else {
    for (const r of rows) {
      const div = document.createElement('div');
      div.className = 'row' + (/total/i.test(r.label) && !/subtotal/i.test(r.label) ? ' total' : '') + (/discount|save|promo/i.test(r.label) ? ' discount' : '');
      div.innerHTML = `<span>${r.label}</span><span>${r.value}</span>`;
      el.appendChild(div);
    }
  }
  if (typeof cartTotal === 'number' && cartTotal > 0) {
    const div = document.createElement('div');
    div.className = 'row total';
    div.innerHTML = `<span>Cart total</span><span>$${cartTotal.toFixed(2)}</span>`;
    el.appendChild(div);
  }
}

async function openReviewModal() {
  const modal = document.getElementById('order-modal');
  const status = document.getElementById('order-status');
  const confirmBtn = modal.querySelector('.modal-confirm');
  status.className = 'modal-status';
  status.textContent = '';
  confirmBtn.disabled = true;
  document.querySelector('.modal-meta').textContent = `Delivery ${MENU_DATE} · ${cartTotalQuantity()} meal${cartTotalQuantity() > 1 ? 's' : ''}`;
  document.getElementById('breakdown').innerHTML = '<div class="placeholder">Loading breakdown…</div>';
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  try {
    const res = await fetch(withDate('/api/order/preview'), { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
    renderBreakdown(body);
    confirmBtn.disabled = false;
  } catch (e) {
    document.getElementById('breakdown').innerHTML = '';
    status.className = 'modal-status err';
    status.textContent = 'Preview failed: ' + e.message;
  }
}

function closeReviewModal() {
  const modal = document.getElementById('order-modal');
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
}

async function placeOrder() {
  const status = document.getElementById('order-status');
  const confirmBtn = document.querySelector('#order-modal .modal-confirm');
  const cancelBtn = document.querySelector('#order-modal .modal-cancel');
  confirmBtn.disabled = true;
  cancelBtn.disabled = true;
  confirmBtn.textContent = 'Placing…';
  status.className = 'modal-status';
  status.textContent = '';
  try {
    const res = await fetch(withDate('/api/order/place'), { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
    const node = ((body.data || {}).createOrder) || body;
    const err = node && (node.error || node.__typename === 'OrderCreationError');
    if (err) {
      const oos = (node.outOfStockIds || []).join(', ');
      throw new Error((node.error || 'Order rejected') + (oos ? ' · out of stock: ' + oos : ''));
    }
    status.className = 'modal-status ok';
    status.textContent = `Order placed ✓ #${(node.id || '') || (body.id || '')} · syncing…`;
    await syncCart();
    setTimeout(closeReviewModal, 1200);
  } catch (e) {
    status.className = 'modal-status err';
    status.textContent = 'Order failed: ' + e.message;
    confirmBtn.disabled = false;
    confirmBtn.textContent = 'Place order';
    cancelBtn.disabled = false;
  }
}

function renderOrderBanner(order) {
  let el = document.getElementById('order-banner');
  if (!order) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement('div');
    el.id = 'order-banner';
    document.querySelector('main.page').insertAdjacentElement('beforebegin', el);
  }
  const addr = order.address || {};
  const window_ = (order.time_start && order.time_end) ? `${order.time_start}–${order.time_end}` : '';
  const totalStr = typeof cartTotal === 'number' && cartTotal > 0 ? ' · $' + cartTotal.toFixed(2) : '';
  el.innerHTML = `
    <div class="order-banner-inner">
      <span class="order-ico">✓</span>
      <div class="order-text">
        <div class="order-title">Order placed for ${order.delivery_date || MENU_DATE}${totalStr}</div>
        <div class="order-sub">#${order.id}${window_ ? ' · window ' + window_ : ''}${addr.city ? ' · to ' + addr.city : ''}</div>
      </div>
    </div>
  `;
}

async function addToCart(inv) {
  const card = document.querySelector(`.card[data-inv="${inv}"]`);
  const btn = card && card.querySelector('.add-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Adding…'; }
  try {
    const res = await fetch(withDate('/api/cart/add'), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ inventory_id: inv, quantity: 1 }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
    const name = (MENU_INDEX[inv] && MENU_INDEX[inv].name) || inv;
    toast(`Added: ${name}`);
    await syncCart();
  } catch (e) {
    toast('Add failed: ' + e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function removeFromCart(inv) {
  try {
    const res = await fetch(withDate('/api/cart/remove'), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ inventory_id: inv }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
    toast('Removed');
    await syncCart();
  } catch (e) {
    toast('Remove failed: ' + e.message, true);
  }
}

function applyFilters() {
  const q = document.getElementById('search').value.trim().toLowerCase();
  const cat = document.getElementById('cat').value;
  const onlyCart = document.getElementById('only-cart').checked;
  document.querySelectorAll('.card').forEach(card => {
    const matchQ = !q || card.dataset.search.includes(q);
    const matchCat = !cat || card.closest('section').dataset.cat === cat;
    const matchCart = !onlyCart || cartState.has(card.dataset.inv);
    card.classList.toggle('hidden', !(matchQ && matchCat && matchCart));
  });
  document.querySelectorAll('section.category').forEach(sec => {
    const anyVisible = [...sec.querySelectorAll('.card')].some(c => !c.classList.contains('hidden'));
    sec.classList.toggle('hidden', !anyVisible);
  });
}

function highResImage(url) {
  if (!url) return '';
  if (!url.includes('imgix.net')) return url;
  const base = url.split('?')[0];
  return base + '?w=1600&auto=format,compress';
}

function openLightbox(card) {
  const snap = JSON.parse(card.dataset.item || '{}');
  const img = snap.image || (card.querySelector('.thumb img') || {}).src || '';
  if (!img) return;
  const imgEl = document.getElementById('lightbox-img');
  imgEl.src = highResImage(img);
  const chef = (card.querySelector('.chef') || {}).textContent || '';
  const name = snap.name || '';
  document.getElementById('lightbox-caption').textContent = chef ? name + ' · ' + chef : name;
  const box = document.getElementById('lightbox');
  box.classList.add('open');
  box.setAttribute('aria-hidden', 'false');
}

function closeLightbox() {
  const box = document.getElementById('lightbox');
  box.classList.remove('open');
  box.setAttribute('aria-hidden', 'true');
  document.getElementById('lightbox-img').src = '';
}

document.addEventListener('click', (e) => {
  const favBtn = e.target.closest('.fav-btn');
  if (favBtn) {
    const card = favBtn.closest('.card');
    const snap = JSON.parse(card.dataset.item || '{}');
    toggleFav(card.dataset.key, {
      name: snap.name,
      image: snap.image,
      price: snap.price,
      chef: (card.querySelector('.chef') || {}).textContent?.replace(/^Chef\\s+/, '') || '',
      isBundle: !!snap.isBundle,
    });
    return;
  }
  const addBtn = e.target.closest('.add-btn');
  if (addBtn) {
    addToCart(addBtn.closest('.card').dataset.inv);
    return;
  }
  const incBtn = e.target.closest('.card .stepper .qty-inc');
  if (incBtn) {
    addToCart(incBtn.closest('.card').dataset.inv);
    return;
  }
  const decBtn = e.target.closest('.card .stepper .qty-dec');
  if (decBtn) {
    removeFromCart(decBtn.closest('.card').dataset.inv);
    return;
  }
  const thumb = e.target.closest('.card .thumb');
  if (thumb) {
    openLightbox(thumb.closest('.card'));
    return;
  }
  if (e.target.closest('.lightbox')) {
    closeLightbox();
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeLightbox();
});

async function refresh() {
  const btn = document.getElementById('refresh');
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Refreshing…';
  try {
    const res = await fetch(withDate('/api/refresh'), { method: 'POST' });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
    toast('Menu refreshed — reloading');
    setTimeout(() => location.reload(), 400);
  } catch (e) {
    toast('Refresh failed: ' + e.message, true);
    btn.disabled = false;
    btn.textContent = orig;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('search').addEventListener('input', applyFilters);
  document.getElementById('cat').addEventListener('change', applyFilters);
  document.getElementById('only-cart').addEventListener('change', applyFilters);
  document.getElementById('refresh').addEventListener('click', refresh);
  document.getElementById('cart-reload').addEventListener('click', (e) => { e.stopPropagation(); syncCart(true); });
  document.getElementById('review-order').addEventListener('click', (e) => { e.stopPropagation(); openReviewModal(); });
  document.querySelector('#order-modal .modal-close').addEventListener('click', closeReviewModal);
  document.querySelector('#order-modal .modal-cancel').addEventListener('click', closeReviewModal);
  document.querySelector('#order-modal .modal-confirm').addEventListener('click', placeOrder);
  document.getElementById('order-modal').addEventListener('click', (e) => {
    if (e.target.id === 'order-modal') closeReviewModal();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeReviewModal(); });
  document.getElementById('cart-toggle').addEventListener('click', () => {
    const cart = document.getElementById('cart');
    cart.classList.toggle('collapsed');
    document.body.classList.toggle('cart-open', !cart.classList.contains('collapsed'));
  });
  document.getElementById('date-picker').addEventListener('change', (e) => {
    const d = e.target.value;
    const hash = location.hash || '';
    location.href = '/?date=' + encodeURIComponent(d) + hash;
  });
  document.getElementById('auth-save').addEventListener('click', saveCreds);
  document.getElementById('favs-clear').addEventListener('click', () => {
    if (Object.keys(favs).length === 0) return;
    if (!confirm('Clear all ' + Object.keys(favs).length + ' favorites?')) return;
    favs = {};
    saveFavs(favs);
    syncFavUI();
  });
  window.addEventListener('hashchange', applyRoute);
  syncFavUI();
  applyRoute();
  syncCart();
  // Re-sync when the tab regains focus and every 30s while visible.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) syncCart(); });
  setInterval(() => { if (!document.hidden) syncCart(); }, 30000);
});
"""


def render_page(menu_date: str, data: dict, include_out_of_stock: bool = False, upcoming: list[str] | None = None) -> str:
    menu = (data.get("data") or {}).get("menu") or {}
    raw_meals = menu.get("meals") or []
    raw_bundles = menu.get("bundles") or []

    def in_stock(i: dict) -> bool:
        if include_out_of_stock:
            return True
        s = i.get("stock")
        return s is None or (isinstance(s, (int, float)) and s > 0)

    meals = [m for m in raw_meals if in_stock(m) and not m.get("showInBundlesOnly")]
    bundles = [b for b in raw_bundles if in_stock(b)]

    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for m in meals:
        title = ((m.get("category") or {}).get("title")) or "Uncategorized"
        if title not in grouped:
            order.append(title)
            grouped[title] = []
        grouped[title].append(m)

    sections = []
    for title in order:
        items = grouped[title]
        cards = "\n".join(_card(m) for m in items)
        sections.append(
            f'<section class="category" data-cat="{_esc(title)}">'
            f'<h2>{_esc(title)} <span style="color:var(--muted);font-weight:400;font-size:12px">({len(items)})</span></h2>'
            f'<div class="grid">{cards}</div>'
            f'</section>'
        )
    if bundles:
        cards = "\n".join(_card(b, is_bundle=True) for b in bundles)
        sections.append(
            f'<section class="category" data-cat="Bundles">'
            f'<h2>Bundles <span style="color:var(--muted);font-weight:400;font-size:12px">({len(bundles)})</span></h2>'
            f'<div class="grid">{cards}</div>'
            f'</section>'
        )

    cat_options = "".join(
        f'<option value="{_esc(t)}">{_esc(t)}</option>' for t in order + (["Bundles"] if bundles else [])
    )

    total_count = len(meals) + len(bundles)

    # Inventory-id → {name, image, price, chef} for the JS cart panel.
    # Also: stable favorite key → {inventoryId, name, image, price, chef} so the
    # favorites view can add this week's version to cart.
    menu_index: dict[str, dict] = {}
    fav_index: dict[str, dict] = {}
    for m in meals:
        inv = m.get("inventoryId")
        if not inv:
            continue
        chef = m.get("chef") or {}
        # Per-BOX-tier prices. BOX_N is indexed at 1..11 — prices by position.
        # We map box_N -> finalPrice for quick tier lookup in the UI.
        box_prices: dict[str, float] = {}
        for p in m.get("prices") or []:
            t = p.get("type") or ""
            if t.startswith("BOX_") and p.get("finalPrice") is not None:
                try:
                    n = int(t.split("_", 1)[1])
                    box_prices[str(n)] = p["finalPrice"]
                except ValueError:
                    pass
        entry = {
            "name": m.get("name"),
            "image": _meal_image(m),
            "price": m.get("finalPrice") or m.get("price"),
            "chef": f"{chef.get('firstName') or ''} {chef.get('lastName') or ''}".strip(),
            "boxPrices": box_prices,
        }
        menu_index[inv] = entry
        fav_index[_fav_key(m, False)] = {**entry, "inventoryId": inv}
    for b in bundles:
        inv = b.get("inventoryId")
        if not inv:
            continue
        img = b.get("image")
        if img and not img.startswith("http"):
            img = f"https://cu-media.imgix.net{img}"
        entry = {
            "name": b.get("name"),
            "image": img,
            "price": b.get("finalPrice") or b.get("price"),
            "chef": "",
            "isBundle": True,
        }
        menu_index[inv] = entry
        fav_index[_fav_key(b, True)] = {**entry, "inventoryId": inv}
    menu_index_json = json.dumps(menu_index)
    fav_index_json = json.dumps(fav_index)

    fetched_at = data.get("_fetched_at") or "—"
    upcoming = upcoming or [menu_date]
    if menu_date not in upcoming:
        upcoming = [menu_date] + upcoming
    date_options = "".join(
        f'<option value="{_esc(d)}"{" selected" if d == menu_date else ""}>{_esc(d)}</option>'
        for d in upcoming
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light only">
<meta name="theme-color" content="#fff6fa">
<title>😽 Kitty's Menu · {_esc(menu_date)}</title>
<link rel="icon" href="data:image/svg+xml,&lt;svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'&gt;&lt;text y='.9em' font-size='90'&gt;😽&lt;/text&gt;&lt;/svg&gt;">
<style>{PAGE_CSS}</style>
</head>
<body>
<div id="toast"></div>
<div class="lightbox" id="lightbox" role="dialog" aria-modal="true" aria-hidden="true">
  <button class="close" type="button" aria-label="Close">✕</button>
  <img id="lightbox-img" alt="">
  <div class="caption" id="lightbox-caption"></div>
</div>
<header class="topbar">
  <h1><span class="emoji" aria-hidden="true">😽</span> <span class="kitty">Kitty's</span> Menu</h1>
  <select id="date-picker" title="Delivery date (Monday)">{date_options}</select>
  <span class="meta">{total_count} items · data from <span id="fetched-at">{_esc(fetched_at)}</span></span>
  <a id="favs-link" class="nav-link" href="#favorites">★ Favorites <span class="count" id="favs-count">0</span></a>
  <a id="auth-link" class="nav-link" href="#auth" title="Update credentials">⚙ Auth</a>
  <a id="menu-link" class="nav-link" href="#" style="display:none">← Menu</a>
  <button id="refresh" type="button" title="Re-fetch menu from CookUnity">↻ Refresh</button>
  <input id="search" type="search" placeholder="Search meals, chefs, cuisines…">
  <select id="cat"><option value="">All categories</option>{cat_options}</select>
  <label><input id="only-cart" type="checkbox"> Only in cart</label>
</header>
<main class="page">
{''.join(sections)}
</main>
<section id="favorites-view">
  <div class="page">
    <h2>★ Favorites</h2>
    <div class="actions">
      <button id="favs-clear" type="button" class="remove">Clear all favorites</button>
    </div>
    <div class="fav-list" id="favs-list"></div>
    <div class="empty" id="favs-empty">No favorites yet. Tap the ☆ on any meal to save it here.</div>
  </div>
</section>
<section id="auth-view">
  <div class="page">
    <h2>⚙ Update CookUnity credentials</h2>
    <p>
      The JWT + cookie from CookUnity expire every ~24h. Open <b>subscription.cookunity.com</b> in your browser while signed in,
      open DevTools → Network, right-click any <code>menu-service/graphql</code> or <code>sdui-service/cart</code> request,
      <b>Copy → Copy as cURL</b>, then paste the whole thing below. The server extracts the <code>authorization:</code> header,
      the cookie jar, and the cart UUID.
    </p>
    <textarea id="auth-curl" placeholder="curl 'https://subscription.cookunity.com/menu-service/graphql' \\&#10;  -H 'authorization: ...' \\&#10;  -b 'CU_TrackUuid=...; appSession=...' \\&#10;  ..."></textarea>
    <div class="actions">
      <button id="auth-save" type="button" class="primary">Save credentials</button>
      <a href="#" class="nav-link">← Cancel</a>
    </div>
    <div id="auth-status" class="status"></div>
    <div class="creds-info" id="creds-info"></div>
  </div>
</section>
<aside id="cart" class="collapsed">
  <header id="cart-toggle">
    <h3>Cart</h3>
    <span class="count">0</span>
    <span class="plan-progress"></span>
    <span class="total"></span>
    <button id="review-order" type="button" class="review-btn" style="display:none">Review & Order →</button>
    <span class="synced" title="Last sync with CookUnity">—</span>
    <button id="cart-reload" type="button" title="Re-fetch cart state from CookUnity">↻</button>
    <span class="chevron" aria-hidden="true">▲</span>
  </header>
  <div class="items"></div>
</aside>
<div id="order-modal" class="modal" aria-hidden="true" role="dialog">
  <div class="modal-card">
    <button class="modal-close" type="button" aria-label="Close">✕</button>
    <h3>Review your order</h3>
    <div class="modal-meta"></div>
    <div class="breakdown" id="breakdown"></div>
    <div class="modal-actions">
      <button type="button" class="modal-cancel">Cancel</button>
      <button type="button" class="modal-confirm">Place order</button>
    </div>
    <div id="order-status" class="modal-status"></div>
  </div>
</div>
<script>
window.MENU_INDEX = {menu_index_json};
window.FAV_INDEX = {fav_index_json};
window.MENU_DATE = {json.dumps(menu_date)};
</script>
<script>{PAGE_JS}</script>
</body>
</html>
"""


class CartProxy:
    """Relays cart calls to CookUnity with live credentials. Creds are mutable
    so they can be updated at runtime from the /api/creds endpoint.

    Each delivery Monday has its own cart_id on the server. We discover it by
    GETting /cart/v2/<date> and caching the mapping, since add/remove target
    /cart/v2/<cart_id>/products and must match the date's cart exactly.
    """

    def __init__(self, token: str, cookie: str, cart_id: str):
        self.token = token
        self.cookie = cookie
        self.cart_id = cart_id  # seed; refined per-date once discovered
        self.cart_id_by_date: dict[str, str] = {}

    def update(self, token: str | None = None, cookie: str | None = None, cart_id: str | None = None) -> None:
        if token:
            self.token = token
        if cookie:
            self.cookie = cookie
        if cart_id:
            self.cart_id = cart_id
        # Cached per-date ids are still valid — they're keyed by delivery date,
        # not by the user's auth.

    def _headers(self, menu_date: str) -> dict:
        return {
            "accept": "*/*",
            "accept-version": "1.25.0",
            "content-type": "application/json",
            "authorization": self.token,
            "cookie": self.cookie,
            "platform": "web",
            "origin": "https://subscription.cookunity.com",
            "referer": f"https://subscription.cookunity.com/meals/collection/605?date={menu_date}",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            ),
        }

    def _request(self, method: str, url: str, menu_date: str, body: bytes | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method, headers=self._headers(menu_date))
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _cart_id_for(self, menu_date: str) -> str:
        """Resolve (and cache) the cart UUID for the given delivery date."""
        cid = self.cart_id_by_date.get(menu_date)
        if cid:
            return cid
        status, body = self.get(menu_date)
        if status == 200:
            try:
                data = json.loads(body)
                cid = data.get("cart_id")
                if cid:
                    self.cart_id_by_date[menu_date] = cid
                    return cid
            except json.JSONDecodeError:
                pass
        # Fallback to the seed cart_id (works at least for the week it was captured on).
        return self.cart_id

    def add(self, menu_date: str, inventory_id: str, quantity: int = 1) -> tuple[int, bytes]:
        cart_id = self._cart_id_for(menu_date)
        body = json.dumps({"products": [{"inventory_id": inventory_id, "quantity": quantity}]}).encode()
        return self._request("POST", CART_ADD_ENDPOINT.format(cart_id=cart_id), menu_date, body)

    def remove(self, menu_date: str, inventory_id: str, quantity: int = 1) -> tuple[int, bytes]:
        cart_id = self._cart_id_for(menu_date)
        body = json.dumps({"products": [{"inventory_id": inventory_id, "quantity": quantity}]}).encode()
        return self._request("DELETE", CART_ADD_ENDPOINT.format(cart_id=cart_id), menu_date, body)

    def get(self, menu_date: str) -> tuple[int, bytes]:
        return self._request("GET", CART_GET_ENDPOINT.format(date=menu_date), menu_date)

    def price_breakdown(self, menu_date: str, meals: list[dict]) -> tuple[int, bytes]:
        cart_id = self._cart_id_for(menu_date)
        body = json.dumps({"date": menu_date, "meals": meals, "cartId": cart_id}).encode()
        return self._request("POST", PRICE_BREAKDOWN_ENDPOINT, menu_date, body)

    def create_order(
        self,
        menu_date: str,
        products: list[dict],
        time_start: str = "12:00",
        time_end: str = "20:00",
        tip: int = 0,
    ) -> tuple[int, bytes]:
        cart_id = self._cart_id_for(menu_date)
        payload = {
            "operationName": "createOrder",
            "variables": {
                "order": {
                    "deliveryDate": menu_date,
                    "start": time_start,
                    "end": time_end,
                    "products": products,
                    "freeProducts": [],
                    "tip": tip,
                    "comment": None,
                    "cartId": cart_id,
                },
                "origin": "customer_web_desktop",
            },
            "query": CREATE_ORDER_QUERY,
        }
        # createOrder needs `cu-platform: WebDesktop` (not `platform: web`) and a different referer.
        headers = self._headers(menu_date)
        headers.pop("platform", None)
        headers["cu-platform"] = "WebDesktop"
        headers["referer"] = "https://subscription.cookunity.com/"
        req = urllib.request.Request(
            CREATE_ORDER_ENDPOINT, data=json.dumps(payload).encode(),
            method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


def latest_menu_date() -> str | None:
    files = sorted(MENU_DIR.glob("*.json"))
    if not files:
        return None
    return files[-1].stem


class State:
    """Per-date cache of (menu data, rendered HTML). Lazy-fetches on first use."""

    def __init__(self, include_out_of_stock: bool, proxy: CartProxy, upcoming: list[str]):
        self.include_out_of_stock = include_out_of_stock
        self.proxy = proxy
        self.upcoming = upcoming
        self.cache: dict[str, dict] = {}  # date -> {data, page_html}
        self.lock = threading.Lock()

    def _load_or_fetch(self, menu_date: str) -> dict:
        """Return {data, page_html} for menu_date. Reads disk cache first, then
        falls back to a live GraphQL fetch. Caller must hold self.lock."""
        if menu_date in self.cache:
            return self.cache[menu_date]
        json_path = MENU_DIR / f"{menu_date}.json"
        if json_path.exists():
            data = json.loads(json_path.read_text())
            if "_fetched_at" not in data:
                data["_fetched_at"] = f"cached file ({json_path.name})"
        else:
            # Need live auth to fetch.
            if not self.proxy.token:
                raise RuntimeError("No auth credentials; paste a curl via the UI first.")
            data = fetch_menu(menu_date, self.proxy.token, self.proxy.cookie)
            data["_fetched_at"] = _now_iso()
            MENU_DIR.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(data, ensure_ascii=False))
        entry = {
            "data": data,
            "page_html": render_page(menu_date, data, self.include_out_of_stock, self.upcoming).encode("utf-8"),
        }
        self.cache[menu_date] = entry
        return entry

    def get(self, menu_date: str) -> dict:
        with self.lock:
            return self._load_or_fetch(menu_date)

    def refresh(self, menu_date: str) -> dict:
        """Force a live re-fetch, update disk + cache, return entry."""
        if not self.proxy.token:
            raise RuntimeError("No auth credentials; paste a curl via the UI first.")
        with self.lock:
            data = fetch_menu(menu_date, self.proxy.token, self.proxy.cookie)
            data["_fetched_at"] = _now_iso()
            MENU_DIR.mkdir(parents=True, exist_ok=True)
            (MENU_DIR / f"{menu_date}.json").write_text(json.dumps(data, ensure_ascii=False))
            entry = {
                "data": data,
                "page_html": render_page(menu_date, data, self.include_out_of_stock, self.upcoming).encode("utf-8"),
            }
            self.cache[menu_date] = entry
            return entry

    def invalidate_all(self) -> None:
        """Drop cached HTML so next request re-renders (e.g., after creds change)."""
        with self.lock:
            self.cache.clear()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def build_handler(state: State, proxy: CartProxy, default_date: str, creds_meta: dict):
    def _parse_date(path_or_body: str | dict) -> str:
        if isinstance(path_or_body, dict):
            d = path_or_body.get("date") or default_date
        else:
            qs = urllib.parse.urlparse(path_or_body).query
            d = urllib.parse.parse_qs(qs).get("date", [default_date])[0]
        try:
            date_cls.fromisoformat(d)
        except ValueError:
            raise ValueError(f"invalid date: {d!r}")
        return d

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def do_GET(self):  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                try:
                    d = _parse_date(self.path)
                    entry = state.get(d)
                except Exception as e:
                    return self._render_error(f"Couldn't load menu for that date: {e}")
                body = entry["page_html"]
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.send_header("cache-control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/cart":
                try:
                    d = _parse_date(self.path)
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                status, body = proxy.get(d)
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.send_header("cache-control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/creds":
                tail = (proxy.token or "")[-8:] if proxy.token else ""
                return self._json(200, {
                    "token": bool(proxy.token),
                    "token_tail": tail,
                    "cart_id": proxy.cart_id,
                    "source": creds_meta.get("source", "env"),
                    "saved_at": creds_meta.get("saved_at"),
                })
            self.send_error(404)

        def do_POST(self):  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/cart/add":
                return self._cart_add()
            if path == "/api/cart/remove":
                return self._cart_remove()
            if path == "/api/refresh":
                return self._refresh()
            if path == "/api/creds":
                return self._creds_update()
            if path == "/api/order/preview":
                return self._order_preview()
            if path == "/api/order/place":
                return self._order_place()
            self.send_error(404)

        def _cart_meals_for(self, menu_date: str) -> list[dict] | None:
            """Build a list of {entityId, batchId, inventoryId, qty} for the
            items currently in the remote cart, cross-referenced with our
            locally cached menu data (which has stable id + batchId)."""
            status, body = proxy.get(menu_date)
            if status != 200:
                return None
            try:
                cart_data = json.loads(body)
            except json.JSONDecodeError:
                return None
            products = cart_data.get("products") or []
            # Build inv_id -> (id, batchId) lookup from the cached menu.
            try:
                entry = state.get(menu_date)
            except Exception:
                return None
            menu = ((entry["data"].get("data") or {}).get("menu") or {})
            inv_to_meal: dict[str, dict] = {}
            for m in menu.get("meals") or []:
                inv = m.get("inventoryId")
                if inv:
                    inv_to_meal[inv] = {"id": m.get("id"), "batchId": m.get("batchId")}
            out = []
            for p in products:
                inv = p.get("inventory_id")
                meta = inv_to_meal.get(inv)
                if not meta or meta.get("id") is None:
                    return None  # unknown item → bail rather than send bad payload
                out.append({
                    "entityId": meta["id"],
                    "batchId": meta.get("batchId"),
                    "inventoryId": inv,
                    "quantity": int(p.get("quantity") or 1),
                })
            return out

        def _order_preview(self):
            payload = self._read_json()
            try:
                d = _parse_date(payload) if not ("date=" in self.path) else _parse_date(self.path)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            meals_full = self._cart_meals_for(d)
            if meals_full is None:
                return self._json(502, {"error": "Couldn't resolve cart items against the cached menu for that date."})
            if not meals_full:
                return self._json(400, {"error": "Cart is empty."})
            preview_meals = [{"entityId": m["entityId"], "quantity": m["quantity"], "inventoryId": m["inventoryId"]} for m in meals_full]
            status, body = proxy.price_breakdown(d, preview_meals)
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _order_place(self):
            payload = self._read_json()
            try:
                d = _parse_date(payload) if not ("date=" in self.path) else _parse_date(self.path)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            if self._date_is_ordered(d):
                return self._json(409, {"error": f"Order for {d} has already been placed."})
            meals_full = self._cart_meals_for(d)
            if meals_full is None:
                return self._json(502, {"error": "Couldn't resolve cart items against the cached menu for that date."})
            if not meals_full:
                return self._json(400, {"error": "Cart is empty."})
            products = [{"id": m["entityId"], "qty": m["quantity"], "batch_id": m["batchId"], "inventoryId": m["inventoryId"]} for m in meals_full]
            time_start = payload.get("time_start") or "12:00"
            time_end = payload.get("time_end") or "20:00"
            tip = int(payload.get("tip") or 0)
            status, body = proxy.create_order(d, products, time_start=time_start, time_end=time_end, tip=tip)
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("content-length") or 0)
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

        def _date_is_ordered(self, menu_date: str) -> bool:
            status, body = proxy.get(menu_date)
            if status != 200:
                return False
            try:
                return bool((json.loads(body) or {}).get("order"))
            except json.JSONDecodeError:
                return False

        def _cart_add(self):
            payload = self._read_json()
            inv = payload.get("inventory_id")
            if not inv:
                return self._json(400, {"error": "missing inventory_id"})
            try:
                d = _parse_date(self.path) if "date=" in self.path else _parse_date(payload)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            if self._date_is_ordered(d):
                return self._json(409, {"error": f"Order for {d} is already placed — cart is locked for this week."})
            qty = int(payload.get("quantity") or 1)
            status, body = proxy.add(d, inv, qty)
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _cart_remove(self):
            payload = self._read_json()
            inv = payload.get("inventory_id")
            if not inv:
                return self._json(400, {"error": "missing inventory_id"})
            try:
                d = _parse_date(self.path) if "date=" in self.path else _parse_date(payload)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            if self._date_is_ordered(d):
                return self._json(409, {"error": f"Order for {d} is already placed — cart is locked for this week."})
            qty = int(payload.get("quantity") or 1)
            status, body = proxy.remove(d, inv, qty)
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _refresh(self):
            try:
                d = _parse_date(self.path)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            try:
                entry = state.refresh(d)
                data = entry["data"]
                meals = (data.get("data") or {}).get("menu", {}).get("meals") or []
                bundles = (data.get("data") or {}).get("menu", {}).get("bundles") or []
                return self._json(200, {
                    "ok": True,
                    "fetched_at": data.get("_fetched_at"),
                    "meals": len(meals),
                    "bundles": len(bundles),
                })
            except SystemExit as e:
                return self._json(502, {"error": str(e)})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

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
            saved_at = _now_iso()
            creds_meta["source"] = "pasted-curl"
            creds_meta["saved_at"] = saved_at
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            CREDS_PATH.write_text(json.dumps({
                "token": proxy.token,
                "cookie": proxy.cookie,
                "cart_id": proxy.cart_id,
                "saved_at": saved_at,
            }, ensure_ascii=False))
            state.invalidate_all()
            return self._json(200, {
                "ok": True,
                "token_tail": proxy.token[-8:],
                "cart_id": proxy.cart_id,
                "saved_at": saved_at,
            })

        def _render_error(self, msg: str):
            body = f"<h1>Error</h1><p>{_esc(msg)}</p><p><a href='/'>home</a> · <a href='/#auth'>update credentials</a></p>".encode()
            self.send_response(500)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler


def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")

    parser = argparse.ArgumentParser(description="Serve an interactive CookUnity menu with cart proxy.")
    parser.add_argument("--date", help="Default date shown when no ?date= is given (YYYY-MM-DD).")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0", help="Bind address. Default 0.0.0.0 so the LAN can reach it.")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open a browser tab.")
    parser.add_argument("--include-out-of-stock", action="store_true")
    args = parser.parse_args()

    upcoming = upcoming_mondays(4)
    default_date = args.date or (latest_menu_date() if (MENU_DIR / f"{upcoming[0]}.json").exists() is False else upcoming[0]) or upcoming[0]
    date_cls.fromisoformat(default_date)

    # Credentials: prefer state/creds.json (updated via UI) over .env.
    creds_meta = {"source": "env", "saved_at": None}
    token = os.environ.get("CU_AUTH_TOKEN")
    cookie = os.environ.get("CU_COOKIE")
    cart_id = os.environ.get("CU_CART_ID")
    if CREDS_PATH.exists():
        try:
            saved = json.loads(CREDS_PATH.read_text())
            token = saved.get("token") or token
            cookie = saved.get("cookie") or cookie
            cart_id = saved.get("cart_id") or cart_id
            creds_meta = {"source": "pasted-curl", "saved_at": saved.get("saved_at")}
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(f"warning: couldn't read {CREDS_PATH}: {e}\n")

    proxy = CartProxy(token or "", cookie or "", cart_id or "")
    state = State(args.include_out_of_stock, proxy, upcoming)

    # Eager-load the default date so the banner shows meal counts.
    if proxy.token:
        try:
            state.get(default_date)
        except Exception as e:
            sys.stderr.write(f"warning: couldn't preload menu for {default_date}: {e}\n")
    else:
        sys.stderr.write("! no credentials loaded yet — open the UI and use #auth to paste a curl.\n")

    handler = build_handler(state, proxy, default_date, creds_meta)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    lan_ip = os.environ.get("CU_LAN_IP") or _lan_ip()
    print(f"→ default date: {default_date} · upcoming: {', '.join(upcoming)}")
    print(f"  local:  http://127.0.0.1:{args.port}/")
    if lan_ip:
        print(f"  LAN:    http://{lan_ip}:{args.port}/   ← share this with your boyfriend")
    print(f"  creds source: {creds_meta['source']}  (update via the ⚙ Auth link in the UI)")
    print("  Ctrl+C to stop.")
    if not args.no_open:
        try:
            webbrowser.open_new_tab(f"http://127.0.0.1:{args.port}/")
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


def _lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
