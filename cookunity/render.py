"""HTML rendering for the menu page.

Pure functions — no I/O beyond reading the static CSS/JS assets once at import
time. All inputs are the GraphQL-shaped menu data + a handful of config flags,
and the output is a self-contained HTML string.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

ASSETS_DIR = Path(__file__).parent / "assets"
PAGE_CSS = (ASSETS_DIR / "page.css").read_text()
PAGE_JS = (ASSETS_DIR / "page.js").read_text()


def esc(v) -> str:
    return "" if v is None else html.escape(str(v))


def meal_image(meal: dict) -> str | None:
    """Resolve a meal's primary image URL. Returns a full imgix URL or None."""
    url = meal.get("primaryImageUrl")
    if url:
        return url
    path = meal.get("image") or meal.get("imagePath")
    if path and path.startswith("http"):
        return path
    if path:
        return f"https://cu-media.imgix.net{path}"
    return None


def fav_key(item: dict, is_bundle: bool) -> str:
    """Stable favorite identifier. Survives weekly inventoryId rotation."""
    if is_bundle:
        sku = item.get("sku") or item.get("inventoryId") or ""
        return f"b-{sku}"
    return f"m-{item.get('id')}"


def _nutrition_text(nf: dict) -> str:
    parts: list[str] = []
    if nf.get("calories"):
        parts.append(f"{nf['calories']} cal")
    if nf.get("protein"):
        parts.append(f"{nf['protein']}g protein")
    if nf.get("carbs"):
        parts.append(f"{nf['carbs']}g carbs")
    if nf.get("fat"):
        parts.append(f"{nf['fat']}g fat")
    return " · ".join(parts)


def render_card(item: dict, is_bundle: bool = False) -> str:
    """Return a single ``<article class="card">`` block."""
    inv_id = esc(item.get("inventoryId") or "")
    key = esc(fav_key(item, is_bundle))
    img = item.get("image") if is_bundle else meal_image(item)
    if is_bundle and img and not img.startswith("http"):
        img = f"https://cu-media.imgix.net{img}"
    name = esc(item.get("name"))
    desc = esc(
        item.get("shortDescription") or item.get("subtitle") or item.get("description") or ""
    )
    price = item.get("finalPrice") or item.get("price")
    price_html = (
        f'<span class="price">${price:.2f}</span>' if isinstance(price, (int, float)) else ""
    )

    chef_html = ""
    if not is_bundle:
        c = item.get("chef") or {}
        full = f"{c.get('firstName') or ''} {c.get('lastName') or ''}".strip()
        if full:
            chef_html = f'<div class="chef">Chef {esc(full)}</div>'

    rating_html = ""
    if not is_bundle:
        stars = item.get("stars")
        reviews = item.get("reviews")
        if stars:
            rating_html = f'<span class="rating">★ {stars}</span>'
            if reviews:
                rating_html += f" <span>({reviews:,})</span>"

    nutrition_html = ""
    if not is_bundle:
        nutrition_text = _nutrition_text(item.get("nutritionalFacts") or {})
        if nutrition_text:
            nutrition_html = f'<div class="nutrition">{esc(nutrition_text)}</div>'

    badges: list[str] = []
    if item.get("isNewMeal") or item.get("isNewBundle"):
        badges.append('<span class="badge new">New</span>')
    if item.get("isPremium"):
        badges.append('<span class="badge premium">Premium</span>')
    badges_html = f'<div class="badges">{"".join(badges)}</div>' if badges else ""

    payload = json.dumps(
        {
            "inventoryId": item.get("inventoryId"),
            "name": item.get("name"),
            "image": img,
            "price": price,
            "isBundle": is_bundle,
        }
    )
    payload_attr = esc(payload)

    searchable = " ".join(
        filter(
            None,
            [
                item.get("name") or "",
                desc,
                ((item.get("chef") or {}).get("firstName") or "")
                + " "
                + ((item.get("chef") or {}).get("lastName") or ""),
                " ".join(item.get("cuisines") or []),
                (item.get("category") or {}).get("title") or "",
            ],
        )
    ).lower()

    thumb = (
        f'<div class="thumb"><img src="{esc(img)}" alt="" loading="lazy"></div>'
        if img
        else '<div class="thumb"></div>'
    )

    return (
        f'<article class="card" data-inv="{inv_id}" data-key="{key}" '
        f'data-search="{esc(searchable)}" data-item=\'{payload_attr}\'>'
        f"{thumb}"
        f'<button class="fav-btn" type="button" data-key="{key}" '
        f'title="Toggle favorite" aria-label="Toggle favorite">☆</button>'
        '<div class="body">'
        f"{chef_html}"
        f'<div class="name">{name}</div>'
        f'<div class="desc">{desc}</div>'
        f'<div class="meta">{price_html} {rating_html}</div>'
        f"{nutrition_html}"
        f"{badges_html}"
        '<div class="qty-wrap">'
        f'<button class="add-btn" type="button" data-inv="{inv_id}">Add to cart</button>'
        '<div class="stepper" role="group" aria-label="Quantity">'
        '<button class="qty-dec" type="button" aria-label="Remove one">−</button>'
        '<span class="qty">0</span>'
        '<button class="qty-inc" type="button" aria-label="Add one">+</button>'
        "</div>"
        "</div>"
        "</div>"
        "</article>"
    )


def _box_prices(meal: dict) -> dict[str, float]:
    """Extract ``BOX_N`` finalPrices into ``{"N": price}`` for the JS cart UI."""
    out: dict[str, float] = {}
    for p in meal.get("prices") or []:
        t = p.get("type") or ""
        if t.startswith("BOX_") and p.get("finalPrice") is not None:
            try:
                out[str(int(t.split("_", 1)[1]))] = p["finalPrice"]
            except ValueError:
                continue
    return out


def _build_indexes(
    meals: list[dict], bundles: list[dict]
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return ``(menu_index, fav_index)`` — lookup tables the JS embeds."""
    menu_index: dict[str, dict] = {}
    fav_index: dict[str, dict] = {}

    for m in meals:
        inv = m.get("inventoryId")
        if not inv:
            continue
        chef = m.get("chef") or {}
        entry = {
            "name": m.get("name"),
            "image": meal_image(m),
            "price": m.get("finalPrice") or m.get("price"),
            "chef": f"{chef.get('firstName') or ''} {chef.get('lastName') or ''}".strip(),
            "boxPrices": _box_prices(m),
        }
        menu_index[inv] = entry
        fav_index[fav_key(m, False)] = {**entry, "inventoryId": inv}

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
        fav_index[fav_key(b, True)] = {**entry, "inventoryId": inv}

    return menu_index, fav_index


def _group_by_category(meals: list[dict]) -> tuple[list[str], dict[str, list[dict]]]:
    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for m in meals:
        title = ((m.get("category") or {}).get("title")) or "Uncategorized"
        if title not in grouped:
            order.append(title)
            grouped[title] = []
        grouped[title].append(m)
    return order, grouped


def render_page(
    menu_date: str,
    data: dict,
    include_out_of_stock: bool = False,
    upcoming: list[str] | None = None,
) -> str:
    """Render the full interactive menu page for ``menu_date``."""
    menu = (data.get("data") or {}).get("menu") or {}
    raw_meals = menu.get("meals") or []
    raw_bundles = menu.get("bundles") or []

    def in_stock(item: dict) -> bool:
        if include_out_of_stock:
            return True
        s = item.get("stock")
        return s is None or (isinstance(s, (int, float)) and s > 0)

    meals = [m for m in raw_meals if in_stock(m) and not m.get("showInBundlesOnly")]
    bundles = [b for b in raw_bundles if in_stock(b)]

    order, grouped = _group_by_category(meals)

    sections: list[str] = []
    for title in order:
        items = grouped[title]
        cards = "\n".join(render_card(m) for m in items)
        sections.append(
            f'<section class="category" data-cat="{esc(title)}">'
            f'<h2>{esc(title)} '
            f'<span style="color:var(--muted);font-weight:400;font-size:12px">({len(items)})</span></h2>'
            f'<div class="grid">{cards}</div>'
            f"</section>"
        )
    if bundles:
        cards = "\n".join(render_card(b, is_bundle=True) for b in bundles)
        sections.append(
            f'<section class="category" data-cat="Bundles">'
            f'<h2>Bundles '
            f'<span style="color:var(--muted);font-weight:400;font-size:12px">({len(bundles)})</span></h2>'
            f'<div class="grid">{cards}</div>'
            f"</section>"
        )

    cat_options = "".join(
        f'<option value="{esc(t)}">{esc(t)}</option>'
        for t in order + (["Bundles"] if bundles else [])
    )

    total_count = len(meals) + len(bundles)

    menu_index, fav_index = _build_indexes(meals, bundles)
    menu_index_json = json.dumps(menu_index)
    fav_index_json = json.dumps(fav_index)

    fetched_at = data.get("_fetched_at") or "—"
    upcoming = upcoming or [menu_date]
    if menu_date not in upcoming:
        upcoming = [menu_date] + upcoming
    date_options = "".join(
        f'<option value="{esc(d)}"{" selected" if d == menu_date else ""}>{esc(d)}</option>'
        for d in upcoming
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light only">
<meta name="theme-color" content="#fff6fa">
<title>😽 Kitty's Menu · {esc(menu_date)}</title>
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
  <span class="meta">{total_count} items · data from <span id="fetched-at">{esc(fetched_at)}</span></span>
  <a id="favs-link" class="nav-link" href="#favorites">★ Favorites <span class="count" id="favs-count">0</span></a>
  <a id="auth-link" class="nav-link" href="#auth" title="Update credentials">⚙ Auth <span id="auth-dot" data-state="unknown" aria-hidden="true"></span></a>
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
      <button id="auth-test" type="button">Test connection</button>
      <a href="#" class="nav-link">← Cancel</a>
    </div>
    <div id="auth-status" class="status"></div>
    <div id="auth-test-result" class="status"></div>
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
