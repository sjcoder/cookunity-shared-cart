"""Per-date menu cache for the server.

The State holds, for each delivery date, the raw GraphQL menu JSON and its
rendered HTML. Both are populated lazily — a request for a date not yet seen
loads from ``menus/<date>.json`` if cached on disk, otherwise fetches live
from the GraphQL API.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from cookunity.env import now_iso
from cookunity.proxy import CartProxy
from cookunity.render import render_page


def latest_menu_date(menu_dir: Path) -> str | None:
    """Return the most recent ``YYYY-MM-DD`` with a cached JSON, or ``None``."""
    files = sorted(menu_dir.glob("*.json"))
    return files[-1].stem if files else None


class State:
    """Thread-safe cache: ``date -> {data, page_html}``."""

    def __init__(
        self,
        menu_dir: Path,
        include_out_of_stock: bool,
        proxy: CartProxy,
        upcoming: list[str],
        fetch_menu,  # Callable[[str, str, str], dict]
    ) -> None:
        self.menu_dir = menu_dir
        self.include_out_of_stock = include_out_of_stock
        self.proxy = proxy
        self.upcoming = upcoming
        self.fetch_menu = fetch_menu
        self.cache: dict[str, dict] = {}
        self.lock = threading.Lock()

    # -- private --------------------------------------------------------------
    def _render(self, menu_date: str, data: dict) -> bytes:
        return render_page(
            menu_date, data, self.include_out_of_stock, self.upcoming
        ).encode("utf-8")

    def _fetch_live(self, menu_date: str) -> dict:
        if not self.proxy.token:
            raise RuntimeError("No auth credentials; paste a curl via the UI first.")
        data = self.fetch_menu(menu_date, self.proxy.token, self.proxy.cookie)
        data["_fetched_at"] = now_iso()
        self.menu_dir.mkdir(parents=True, exist_ok=True)
        (self.menu_dir / f"{menu_date}.json").write_text(
            json.dumps(data, ensure_ascii=False)
        )
        return data

    def _load_or_fetch(self, menu_date: str) -> dict:
        """Caller must hold ``self.lock``."""
        if menu_date in self.cache:
            return self.cache[menu_date]
        json_path = self.menu_dir / f"{menu_date}.json"
        if json_path.exists():
            data = json.loads(json_path.read_text())
            if "_fetched_at" not in data:
                data["_fetched_at"] = f"cached file ({json_path.name})"
        else:
            data = self._fetch_live(menu_date)
        entry = {"data": data, "page_html": self._render(menu_date, data)}
        self.cache[menu_date] = entry
        return entry

    # -- public ---------------------------------------------------------------
    def get(self, menu_date: str) -> dict:
        with self.lock:
            return self._load_or_fetch(menu_date)

    def refresh(self, menu_date: str) -> dict:
        """Force a live re-fetch, update disk + cache, and return the entry."""
        with self.lock:
            data = self._fetch_live(menu_date)
            entry = {"data": data, "page_html": self._render(menu_date, data)}
            self.cache[menu_date] = entry
            return entry

    def invalidate_all(self) -> None:
        """Drop all cached HTML (e.g. after creds change mid-session)."""
        with self.lock:
            self.cache.clear()

    def preload(self, menu_date: str) -> None:
        """Best-effort warm of the cache; log and swallow failures."""
        try:
            self.get(menu_date)
        except Exception as e:  # noqa: BLE001 — boot-time is best-effort
            sys.stderr.write(f"warning: couldn't preload menu for {menu_date}: {e}\n")
