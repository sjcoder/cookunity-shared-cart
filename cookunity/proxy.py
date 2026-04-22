"""The CartProxy — all outbound HTTP to CookUnity flows through here.

Two deliberate design choices:

1. **Mutable credentials.** ``proxy.update(token=..., cookie=..., cart_id=...)``
   lets the ``/api/creds`` endpoint swap tokens at runtime without recreating
   the object or restarting the server.
2. **Per-date cart UUID.** Each delivery Monday has its own cart UUID on the
   server; add/remove POST to ``/cart/v2/<cart_id>/products`` and *must* match
   the date's cart exactly. We discover the right UUID by GETting
   ``/cart/v2/<date>`` and caching the mapping in ``cart_id_by_date``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

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

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class CartProxy:
    def __init__(self, token: str, cookie: str, cart_id: str) -> None:
        self.token = token
        self.cookie = cookie
        self.cart_id = cart_id  # seed; refined per-date once discovered
        self.cart_id_by_date: dict[str, str] = {}

    def update(
        self,
        token: str | None = None,
        cookie: str | None = None,
        cart_id: str | None = None,
    ) -> None:
        if token:
            self.token = token
        if cookie:
            self.cookie = cookie
        if cart_id:
            self.cart_id = cart_id
        # Cached per-date ids are still valid — they're keyed by delivery date,
        # not by the user's auth.

    # -- HTTP internals -------------------------------------------------------
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
            "user-agent": _USER_AGENT,
        }

    def _request(
        self,
        method: str,
        url: str,
        menu_date: str,
        body: bytes | None = None,
        extra_headers: dict | None = None,
    ) -> tuple[int, bytes]:
        headers = self._headers(menu_date)
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _cart_id_for(self, menu_date: str) -> str:
        """Resolve + cache the cart UUID for ``menu_date``."""
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
        # Fallback to the seed cart_id (valid at least for the week it was
        # captured on). Calls with the wrong id will fail upstream with a 4xx
        # which the handler will surface to the user.
        return self.cart_id

    # -- Public surface -------------------------------------------------------
    def get(self, menu_date: str) -> tuple[int, bytes]:
        return self._request("GET", CART_GET_ENDPOINT.format(date=menu_date), menu_date)

    def add(self, menu_date: str, inventory_id: str, quantity: int = 1) -> tuple[int, bytes]:
        cart_id = self._cart_id_for(menu_date)
        body = json.dumps(
            {"products": [{"inventory_id": inventory_id, "quantity": quantity}]}
        ).encode()
        return self._request("POST", CART_ADD_ENDPOINT.format(cart_id=cart_id), menu_date, body)

    def remove(self, menu_date: str, inventory_id: str, quantity: int = 1) -> tuple[int, bytes]:
        cart_id = self._cart_id_for(menu_date)
        body = json.dumps(
            {"products": [{"inventory_id": inventory_id, "quantity": quantity}]}
        ).encode()
        return self._request("DELETE", CART_ADD_ENDPOINT.format(cart_id=cart_id), menu_date, body)

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
        # createOrder needs `cu-platform: WebDesktop` (not `platform: web`) and
        # a different referer than the cart endpoints.
        headers = self._headers(menu_date)
        headers.pop("platform", None)
        headers["cu-platform"] = "WebDesktop"
        headers["referer"] = "https://subscription.cookunity.com/"
        req = urllib.request.Request(
            CREATE_ORDER_ENDPOINT,
            data=json.dumps(payload).encode(),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
