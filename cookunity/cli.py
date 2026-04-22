"""CLI entry point — ``python serve.py`` and ``uv run serve.py`` both land here.

Responsibilities: parse flags, wire together proxy + state + handler, print the
startup banner (with the LAN URL to share with your partner), and run the HTTP
server until Ctrl+C.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import webbrowser
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

from cookunity.dates import upcoming_mondays
from cookunity.env import load_creds, load_dotenv
from cookunity.handler import build_handler
from cookunity.proxy import CartProxy
from cookunity.state import State, latest_menu_date

ROOT_DIR = Path(__file__).resolve().parent.parent
MENU_DIR = ROOT_DIR / "menus"
STATE_DIR = ROOT_DIR / "state"
CREDS_PATH = STATE_DIR / "creds.json"


def _lan_ip() -> str | None:
    """Best-effort discovery of the host's LAN IP for the startup banner."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def _pick_default_date(explicit: str | None) -> str:
    """Pick the default date shown when the URL has no ``?date=``.

    Priority: explicit CLI flag, then the next upcoming Monday, then the newest
    JSON we've cached on disk.
    """
    if explicit:
        date.fromisoformat(explicit)
        return explicit
    upcoming = upcoming_mondays(1)
    if (MENU_DIR / f"{upcoming[0]}.json").exists():
        return upcoming[0]
    latest = latest_menu_date(MENU_DIR)
    return latest or upcoming[0]


def main() -> int:
    load_dotenv(ROOT_DIR / ".env")

    parser = argparse.ArgumentParser(
        description="Serve an interactive CookUnity menu with a cart proxy."
    )
    parser.add_argument("--date", help="Default date shown when no ?date= is given (YYYY-MM-DD).")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Bind address. Default 0.0.0.0 so the LAN can reach it.",
    )
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open a browser tab.")
    parser.add_argument("--include-out-of-stock", action="store_true")
    args = parser.parse_args()

    upcoming = upcoming_mondays(4)
    default_date = _pick_default_date(args.date)

    creds = load_creds(CREDS_PATH)
    creds_meta = {"source": creds.source, "saved_at": creds.saved_at}

    # Late import to avoid a hard dependency on scrape.py being importable
    # during unit tests that only exercise pure modules.
    from scrape import fetch_menu  # noqa: E402

    proxy = CartProxy(creds.token, creds.cookie, creds.cart_id)
    state = State(
        menu_dir=MENU_DIR,
        include_out_of_stock=args.include_out_of_stock,
        proxy=proxy,
        upcoming=upcoming,
        fetch_menu=fetch_menu,
    )

    if proxy.token:
        state.preload(default_date)
    else:
        sys.stderr.write(
            "! no credentials loaded yet — open the UI and use #auth to paste a curl.\n"
        )

    handler = build_handler(state, proxy, default_date, creds_meta, CREDS_PATH)
    server = ThreadingHTTPServer((args.host, args.port), handler)

    lan_ip = os.environ.get("CU_LAN_IP") or _lan_ip()
    print(f"→ default date: {default_date} · upcoming: {', '.join(upcoming)}")
    print(f"  local:  http://127.0.0.1:{args.port}/")
    if lan_ip:
        print(f"  LAN:    http://{lan_ip}:{args.port}/   ← share this with your partner")
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


if __name__ == "__main__":
    raise SystemExit(main())
