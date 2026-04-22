"""Render smoke tests — check the HTML contains the pieces the JS relies on."""

from __future__ import annotations

import json
import re

from cookunity.render import (
    esc,
    fav_key,
    meal_image,
    render_card,
    render_page,
)

from .conftest import make_meal, make_menu


def test_esc_escapes_html_entities():
    assert esc("<script>") == "&lt;script&gt;"
    assert esc(None) == ""


def test_meal_image_prefers_primary_url():
    url = meal_image({"primaryImageUrl": "https://x/y.jpg"})
    assert url == "https://x/y.jpg"


def test_meal_image_falls_back_to_path_prefixed_with_imgix():
    url = meal_image({"image": "/meal-service/meals/1/img.jpg"})
    assert url == "https://cu-media.imgix.net/meal-service/meals/1/img.jpg"


def test_meal_image_returns_none_when_no_source():
    assert meal_image({}) is None


def test_fav_key_is_stable_across_weekly_inventory_rotation():
    m = {"id": 9252, "inventoryId": "ii-100"}
    assert fav_key(m, False) == "m-9252"
    # Next week with a different inventoryId, same meal id → same key.
    m_next = {"id": 9252, "inventoryId": "ii-99999"}
    assert fav_key(m, False) == fav_key(m_next, False)


def test_fav_key_uses_sku_for_bundles():
    b = {"sku": "bd-1189", "inventoryId": "bd-whatever"}
    assert fav_key(b, True) == "b-bd-1189"


def test_render_card_embeds_stable_data_attrs():
    html = render_card(make_meal(id=9252, inventoryId="ii-100"))
    assert 'data-inv="ii-100"' in html
    assert 'data-key="m-9252"' in html
    assert "Kevin Meehan" in html
    assert "$13.49" in html


def test_render_card_omits_chef_for_bundles():
    bundle = {"inventoryId": "bd-1", "sku": "bd-1189", "name": "Vitality Bundle"}
    html = render_card(bundle, is_bundle=True)
    assert "Chef" not in html  # bundles don't have a chef


def test_render_page_includes_required_js_globals():
    page = render_page("2026-04-27", make_menu(), upcoming=["2026-04-27", "2026-05-04"])
    assert "window.MENU_INDEX" in page
    assert "window.FAV_INDEX" in page
    assert "window.MENU_DATE" in page
    # MENU_DATE value is JSON-serialized for safe quoting.
    assert re.search(r'window\.MENU_DATE = "2026-04-27"', page)


def test_render_page_drops_out_of_stock_by_default():
    meals = [make_meal(id=1, inventoryId="ii-1", stock=0), make_meal(id=2, inventoryId="ii-2", stock=5)]
    page = render_page("2026-04-27", make_menu(meals=meals))
    assert 'data-inv="ii-1"' not in page
    assert 'data-inv="ii-2"' in page


def test_render_page_keeps_out_of_stock_when_flag_on():
    meals = [make_meal(id=1, inventoryId="ii-1", stock=0)]
    page = render_page("2026-04-27", make_menu(meals=meals), include_out_of_stock=True)
    assert 'data-inv="ii-1"' in page


def test_render_page_groups_by_category_title():
    a = make_meal(id=1, inventoryId="ii-1", category={"title": "CRAFTED MEALS"})
    b = make_meal(id=2, inventoryId="ii-2", category={"title": "BREAKFAST"})
    page = render_page("2026-04-27", make_menu(meals=[a, b]))
    assert page.index("CRAFTED MEALS") < page.index("BREAKFAST")


def test_menu_index_exposes_box_prices_for_extras_pricing():
    page = render_page("2026-04-27", make_menu())
    m = re.search(r"window\.MENU_INDEX = (\{.*?\});", page, re.DOTALL)
    assert m is not None
    idx = json.loads(m.group(1))
    box = next(iter(idx.values()))["boxPrices"]
    # Our fixture sets BOX_8 and BOX_9 explicitly — the JS uses these to show
    # the "extras rate" once the plan is full.
    assert box["8"] == 11.15
    assert box["9"] == 13.49


def test_fav_index_is_keyed_by_stable_meal_id():
    page = render_page("2026-04-27", make_menu())
    m = re.search(r"window\.FAV_INDEX = (\{.*?\});", page, re.DOTALL)
    idx = json.loads(m.group(1))
    assert "m-9252" in idx
    entry = idx["m-9252"]
    # Favorites view needs the inventoryId so "Add to cart" still works.
    assert entry["inventoryId"] == "ii-100"
