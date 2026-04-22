#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Export CookUnity menus for one or more dates via the menu-service GraphQL API.

Usage:
    ./scrape.py 2026-04-27
    ./scrape.py 2026-04-27 2026-05-04 2026-05-11
    ./scrape.py --range 2026-04-27 2026-05-18          # weekly dates, inclusive
    ./scrape.py 2026-04-27 --format json               # skip the HTML render
    ./scrape.py 2026-04-27 --include-out-of-stock      # keep stock==0 meals

Auth: set CU_AUTH_TOKEN and CU_COOKIE in .env (or the environment). Grab both
from a signed-in browser request in DevTools: CU_AUTH_TOKEN is the JWT from the
`authorization:` header, CU_COOKIE is the full value of the `cookie:` request
header (the API rejects requests that are missing the `appSession` cookie).
Both expire roughly every 24h, so refresh when you get a 401 / "Valid user
needed" response.

By default, meals with stock == 0 (and bundles with stock == 0) are dropped
from the HTML output so you don't print unavailable items. The JSON dump
always contains the full, unfiltered API response.
"""

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ENDPOINT = "https://subscription.cookunity.com/menu-service/graphql"
OUT_DIR = Path(__file__).parent / "menus"

QUERY = """query getMenu($date: String!, $filters: MenuFilters) {
  menu(date: $date, filters: $filters) {
    categories {
      id title subtitle label tag icon
      coverInfo { title text image imageMobile imageMacro __typename }
      __typename
    }
    meals {
      id batchId name shortDescription image imagePath primaryImageUrl
      price
      prices { type finalPrice premiumFee basePrice priceAfterDelta __typename }
      finalPrice premiumFee premiumSpecial isPremium sku source stock
      isNewMeal isBackInTheMenu isMealRestaurantExclusive
      isExclusiveMembershipProduct membershipPremiumDiscount
      membershipAddAndSaveDiscount isUnityPass hasRecipe entityType
      userRating showInBundlesOnly warning
      productDetails {
        wineVintage wineSweetness wineBottleCount wineVolume wineVarietal
        wineRegion wineCountry wineAbv wineColor __typename
      }
      searchBy {
        cuisines chefFirstName chefLastName dietTags ingredients mealName
        merchandisingFilters preferences proteinTags __typename
      }
      ingredients { id name value __typename }
      warnings {
        message
        restrictionsApplied { name __typename }
        dietsNotMatching { name __typename }
        allergensNotMatching { name __typename }
        __typename
      }
      dietsMatching { name __typename }
      reviews stars qty categoryId
      category { id title label __typename }
      chef { id firstName lastName bannerPic profileImageUrl __typename }
      meatType meatCategory
      nutritionalFacts {
        calories fat carbs sodium fiber protein sugar __typename
      }
      specificationsDetails { label __typename }
      sideDish { id name __typename }
      feature { name description icon color background __typename }
      weight filter_by cuisines sidesSubCategoryNames
      media { secondaryImage __typename }
      categories { label id __typename }
      relatedMeal {
        id categoryId name shortDescription image imagePath price premiumFee
        premiumSpecial isPremium sku stock isNewMeal userRating warning batchId
        __typename
      }
      typesTags
      tags { type name __typename }
      inventoryId
      promotions {
        amount { value type __typename }
        type
        constraints {
          categoryId subCategoryId
          capAmount { type value __typename }
          __typename
        }
        __typename
      }
      __typename
    }
    bundles {
      inventoryId entityType name subtitle description stock image sku price
      finalPrice priceWithoutPlanMeal originalPriceWithoutPlanMeal isNewBundle
      filter_by
      meals { mealExternalId __typename }
      categories { label id __typename }
      category { id label __typename }
      __typename
    }
    promotions {
      amount { value type __typename }
      type
      constraints {
        categoryId subCategoryId
        capAmount { type value __typename }
        amountToChoose items __typename
      }
      __typename
    }
    filters { selectedChefs __typename }
    sorting { type sort sortedMealBundles __typename }
    __typename
  }
}
"""


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


def fetch_menu(menu_date: str, token: str, cookie: str | None) -> dict:
    payload = json.dumps({
        "operationName": "getMenu",
        "variables": {"date": menu_date, "filters": {"onlyRegularMeals": False}},
        "query": QUERY,
    }).encode("utf-8")

    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "authorization": token,
        "cu-platform": "MobileWeb",
        "x-client": "SUBSCRIPTION-FRONT",
        "origin": "https://subscription.cookunity.com",
        "referer": f"https://subscription.cookunity.com/meals/collection/605?date={menu_date}",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
    }
    if cookie:
        headers["cookie"] = cookie

    req = urllib.request.Request(ENDPOINT, data=payload, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"HTTP {e.code} for {menu_date}: {detail[:500]}\n"
            "If this is 401/403, refresh CU_AUTH_TOKEN in .env from a signed-in browser."
        )

    data = json.loads(body)
    if "errors" in data:
        raise SystemExit(f"GraphQL errors for {menu_date}: {json.dumps(data['errors'], indent=2)}")
    return data


HTML_CSS = """
:root { --fg: #1a1a1a; --muted: #6b6b6b; --line: #e5e5e5; --accent: #d94a2f; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: var(--fg); background: #fff; }
.page { max-width: 1100px; margin: 0 auto; padding: 28px 32px 48px; }
header.doc { border-bottom: 2px solid var(--fg); padding-bottom: 12px; margin-bottom: 20px; }
header.doc h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.01em; }
header.doc .meta { font-size: 13px; color: var(--muted); }
section.category { margin: 28px 0 10px; page-break-inside: auto; }
section.category > h2 { font-size: 18px; margin: 0 0 10px; padding-bottom: 6px; border-bottom: 1px solid var(--line); text-transform: uppercase; letter-spacing: 0.04em; }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
.card { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; display: flex; flex-direction: column; page-break-inside: avoid; background: #fff; }
.card .thumb { aspect-ratio: 4 / 3; background: #f4f4f4; overflow: hidden; }
.card .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.card .body { padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 4px; }
.card .name { font-size: 14px; font-weight: 600; line-height: 1.25; }
.card .chef { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.card .desc { font-size: 12px; color: #333; margin-top: 2px; }
.card .meta { display: flex; flex-wrap: wrap; gap: 8px; font-size: 11px; color: var(--muted); margin-top: 6px; }
.card .meta .price { color: var(--fg); font-weight: 600; }
.card .meta .rating { color: var(--accent); font-weight: 600; }
.card .badges { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.card .badge { font-size: 10px; padding: 2px 6px; border-radius: 3px; background: #f0f0f0; color: #333; text-transform: uppercase; letter-spacing: 0.04em; }
.card .badge.new { background: #fff3e0; color: #b35600; }
.card .badge.premium { background: #f3ecff; color: #6b2fbd; }
.card .nutrition { font-size: 10px; color: var(--muted); margin-top: 4px; }
.summary { display: flex; gap: 16px; margin: 8px 0 6px; font-size: 12px; color: var(--muted); }
@media print {
  @page { size: Letter; margin: 0.5in; }
  body { font-size: 11px; }
  .grid { grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .card { break-inside: avoid; border-color: #ccc; }
  section.category > h2 { break-after: avoid; }
  header.doc { break-after: avoid; }
}
"""


def _esc(v) -> str:
    if v is None:
        return ""
    return html.escape(str(v))


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


def _render_card(item: dict, is_bundle: bool = False) -> str:
    img = item.get("image") if is_bundle else _meal_image(item)
    if is_bundle and img and not img.startswith("http"):
        img = f"https://cu-media.imgix.net{img}"
    name = _esc(item.get("name"))
    desc = _esc(item.get("shortDescription") or item.get("subtitle") or item.get("description") or "")
    price = item.get("finalPrice") or item.get("price")
    price_html = f'<span class="price">${price:.2f}</span>' if isinstance(price, (int, float)) else ""

    chef = ""
    if not is_bundle:
        c = item.get("chef") or {}
        first = c.get("firstName") or ""
        last = c.get("lastName") or ""
        full = f"{first} {last}".strip()
        if full:
            chef = f'<div class="chef">Chef {_esc(full)}</div>'

    rating_html = ""
    if not is_bundle:
        stars = item.get("stars")
        reviews = item.get("reviews")
        if stars:
            rating_html = f'<span class="rating">★ {stars}</span>'
            if reviews:
                rating_html += f' <span>({reviews:,})</span>'

    badges = []
    if item.get("isNewMeal") or item.get("isNewBundle"):
        badges.append('<span class="badge new">New</span>')
    if item.get("isPremium"):
        badges.append('<span class="badge premium">Premium</span>')
    badges_html = f'<div class="badges">{"".join(badges)}</div>' if badges else ""

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

    thumb = f'<div class="thumb"><img src="{_esc(img)}" alt="" loading="eager"></div>' if img else '<div class="thumb"></div>'

    return (
        '<article class="card">'
        f'{thumb}'
        '<div class="body">'
        f'{chef}'
        f'<div class="name">{name}</div>'
        f'<div class="desc">{desc}</div>'
        f'<div class="meta">{price_html} {rating_html}</div>'
        f'{nutrition_html}'
        f'{badges_html}'
        '</div>'
        '</article>'
    )


def render_html(menu_date: str, data: dict, include_out_of_stock: bool = False) -> str:
    menu = (data.get("data") or {}).get("menu") or {}
    raw_meals = menu.get("meals") or []
    raw_bundles = menu.get("bundles") or []
    categories = menu.get("categories") or []

    def in_stock(item: dict) -> bool:
        if include_out_of_stock:
            return True
        stock = item.get("stock")
        return stock is None or (isinstance(stock, (int, float)) and stock > 0)

    meals = [m for m in raw_meals if in_stock(m) and not m.get("showInBundlesOnly")]
    bundles = [b for b in raw_bundles if in_stock(b)]
    dropped_meals = len(raw_meals) - len(meals)
    dropped_bundles = len(raw_bundles) - len(bundles)

    # Group by the meal's category.title (more granular than top-level categoryId,
    # which buckets everything into CRAFTED MEALS vs Bundles).
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for m in meals:
        title = ((m.get("category") or {}).get("title")) or "Uncategorized"
        if title not in grouped:
            order.append(title)
            grouped[title] = []
        grouped[title].append(m)

    sections: list[str] = []
    for title in order:
        items = grouped[title]
        cards = "\n".join(_render_card(m) for m in items)
        sections.append(
            f'<section class="category"><h2>{_esc(title)} <span style="color:var(--muted);font-weight:400;font-size:12px">({len(items)})</span></h2><div class="grid">{cards}</div></section>'
        )

    if bundles:
        cards = "\n".join(_render_card(b, is_bundle=True) for b in bundles)
        sections.append(
            f'<section class="category"><h2>Bundles <span style="color:var(--muted);font-weight:400;font-size:12px">({len(bundles)})</span></h2><div class="grid">{cards}</div></section>'
        )

    dropped_note = ""
    if dropped_meals or dropped_bundles:
        parts = []
        if dropped_meals:
            parts.append(f"{dropped_meals} meals")
        if dropped_bundles:
            parts.append(f"{dropped_bundles} bundles")
        dropped_note = f'<span>hidden (out of stock): {", ".join(parts)}</span>'

    summary = (
        f'<div class="summary">'
        f'<span><strong>{len(meals)}</strong> meals</span>'
        f'<span><strong>{len(bundles)}</strong> bundles</span>'
        f'<span><strong>{len(grouped)}</strong> categories</span>'
        f'{dropped_note}'
        f'</div>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CookUnity Menu — {_esc(menu_date)}</title>
<style>{HTML_CSS}</style>
</head>
<body>
<div class="page">
<header class="doc">
<h1>CookUnity Menu</h1>
<div class="meta">Delivery week of {_esc(menu_date)}</div>
</header>
{summary}
{''.join(sections)}
</div>
</body>
</html>
"""


def expand_range(start: str, end: str, step_days: int = 7) -> list[str]:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    if d1 < d0:
        raise SystemExit(f"End date {end} is before start date {start}")
    out = []
    cur = d0
    while cur <= d1:
        out.append(cur.isoformat())
        cur += timedelta(days=step_days)
    return out


def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")

    parser = argparse.ArgumentParser(description="Export CookUnity menus to JSON.")
    parser.add_argument("dates", nargs="*", help="One or more YYYY-MM-DD dates (menu delivery date).")
    parser.add_argument(
        "--range",
        nargs=2,
        metavar=("START", "END"),
        help="Emit every Nth date from START to END inclusive (default step: 7 days).",
    )
    parser.add_argument("--step", type=int, default=7, help="Step in days when using --range (default 7).")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="Output directory (default: ./menus).")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the JSON output.")
    parser.add_argument(
        "--format",
        choices=["html", "json", "both"],
        default="both",
        help="What to write per date (default: both).",
    )
    parser.add_argument(
        "--include-out-of-stock",
        action="store_true",
        help="Include meals/bundles with stock==0 in the HTML output.",
    )
    args = parser.parse_args()

    token = os.environ.get("CU_AUTH_TOKEN")
    cookie = os.environ.get("CU_COOKIE")
    if not token:
        sys.stderr.write(
            "Missing CU_AUTH_TOKEN. Create .env with CU_AUTH_TOKEN and CU_COOKIE\n"
            "from a signed-in browser request (see .env.example).\n"
        )
        return 1

    dates: list[str] = list(args.dates)
    if args.range:
        dates.extend(expand_range(args.range[0], args.range[1], args.step))
    if not dates:
        parser.error("Provide at least one date or --range START END.")

    args.out.mkdir(parents=True, exist_ok=True)

    for d in dates:
        # Validate format early.
        date.fromisoformat(d)
        print(f"→ fetching {d} ...", flush=True)
        data = fetch_menu(d, token, cookie)
        menu = data.get("data", {}).get("menu") or {}
        meals = menu.get("meals") or []
        bundles = menu.get("bundles") or []

        in_stock_meals = [
            m for m in meals
            if (args.include_out_of_stock or (m.get("stock") or 0) > 0)
            and not m.get("showInBundlesOnly")
        ]
        in_stock_bundles = [
            b for b in bundles if args.include_out_of_stock or (b.get("stock") or 0) > 0
        ]

        wrote: list[Path] = []
        if args.format in ("json", "both"):
            j = args.out / f"{d}.json"
            with j.open("w") as f:
                if args.pretty:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                else:
                    json.dump(data, f, ensure_ascii=False)
            wrote.append(j)
        if args.format in ("html", "both"):
            h = args.out / f"{d}.html"
            h.write_text(
                render_html(d, data, include_out_of_stock=args.include_out_of_stock),
                encoding="utf-8",
            )
            wrote.append(h)

        print(
            f"  {', '.join(str(p) for p in wrote)} — "
            f"{len(in_stock_meals)}/{len(meals)} meals, "
            f"{len(in_stock_bundles)}/{len(bundles)} bundles in stock"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
