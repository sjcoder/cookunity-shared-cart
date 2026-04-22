"""Shared fixtures + path setup so tests can ``import cookunity``."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def make_meal(**overrides) -> dict:
    """A minimal meal shaped like CookUnity's GraphQL response."""
    meal = {
        "id": 9252,
        "sku": "LA-EXAMPLE",
        "inventoryId": "ii-100",
        "batchId": 2031440,
        "name": "Example Meal",
        "shortDescription": "with Spanish Rice",
        "primaryImageUrl": "https://cu-media.imgix.net/example.jpg",
        "image": "/meal-service/meals/1/main_image/1.jpg",
        "price": 13.49,
        "finalPrice": 13.49,
        "stock": 100,
        "categoryId": 3,
        "category": {"id": 0, "title": "CRAFTED MEALS", "label": "Meals"},
        "chef": {"firstName": "Kevin", "lastName": "Meehan"},
        "stars": 4.7,
        "reviews": 4435,
        "nutritionalFacts": {
            "calories": "640",
            "protein": "31",
            "carbs": "63",
            "fat": "29",
        },
        "cuisines": ["latin american"],
        "prices": [
            {"type": "BOX_1", "finalPrice": 12.19},
            {"type": "BOX_8", "finalPrice": 11.15},
            {"type": "BOX_9", "finalPrice": 13.49},
            {"type": "BOX_10", "finalPrice": 12.14},
        ],
    }
    meal.update(overrides)
    return meal


def make_menu(meals: list[dict] | None = None, bundles: list[dict] | None = None) -> dict:
    """Wrap meals in the ``{"data": {"menu": {...}}}`` shape."""
    return {
        "data": {
            "menu": {
                "meals": meals or [make_meal()],
                "bundles": bundles or [],
                "categories": [{"id": 0, "title": "CRAFTED MEALS", "label": "Meals"}],
            }
        }
    }
