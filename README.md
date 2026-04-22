# 😽 Kitty's Menu

A small local web app that lets two people share one CookUnity subscription cart.

## The backstory

My partner and I share a CookUnity subscription, and we call each other Kitty. CookUnity doesn't offer a shared-cart feature — the cart lives inside my account and they can't see or edit it unless they log in as me. That worked badly: I'd pick meals, they'd never get a say, or they'd text me screenshots of things they wanted and I'd manually find them.

So this project runs on my laptop, exposes a page on our LAN, and gives him his own view of my cart with working add/remove buttons. The auth and cookies live on the server, so he never sees credentials — he just browses meals and taps ＋. The server relays his clicks to CookUnity's real API with my session attached.

## What it does

- **Browse the real menu** — meal cards grouped by category with images, chef, price, star rating, and nutrition facts pulled live from CookUnity's GraphQL API.
- **Add / remove with steppers** — each card has a `−  count  +` pill that edits the shared cart in real time.
- **Server-of-truth cart** — the bottom panel shows exactly what CookUnity has, auto-syncs every 30 seconds and on tab focus. Whichever of us adds a meal, the other sees it.
- **Plan-aware pricing** — when the cart hits the 8-meal plan minimum, the pill flips to "plan full ✓". Past that, each card shows its per-position extras rate (`BOX_9`, `BOX_10`, …) so you know the actual cost before you add.
- **Favorites** — star any meal, see them all at `#favorites`, stored in each browser's localStorage. Keyed by stable meal ID so they persist across weekly menus (and are clearly flagged when they're off-menu that week).
- **Click-to-zoom images** — hovering a card glows pink, clicking opens a 1600px imgix-served version.
- **Date switching** — dropdown in the top bar for the next four Monday delivery dates. Each Monday has its own cart at CookUnity; the server discovers each date's cart UUID on first access and routes adds/removes to the right one.
- **Paste-a-curl auth** — CookUnity's JWT + session cookie expire every ~24h. Instead of SSHing in to edit `.env`, open `#auth`, paste a fresh curl copied from DevTools, and the server parses out the headers and saves them to `state/creds.json`.

## Setup

### Prereqs

- Docker Desktop (or Docker + docker compose)
- A signed-in CookUnity browser session to grab a curl from

### First run

1. Clone and enter the repo.
2. Seed `.env` from the template and drop in a fresh curl's values:

   ```bash
   cp .env.example .env
   # edit .env — paste CU_AUTH_TOKEN, CU_COOKIE, CU_CART_ID, CU_LAN_IP
   ```

   Grab the values from your browser: DevTools → Network → right-click any `menu-service/graphql` request → Copy as cURL. Pull out the `authorization:` header (JWT), the `-b` cookie string, and the UUID in the `/cart/v2/<UUID>/products` URL.

3. Start it:

   ```bash
   docker compose up -d --build
   ```

4. Share the LAN URL from the container log (or `http://<your-Mac-IP>:8001/`).

Your partner opens that URL on the same Wi-Fi and is up and running — no login, no setup on their end.

### Refreshing auth

Every ~24 hours CookUnity's JWT and `appSession` cookie expire, and the page starts showing "Valid user needed" errors. Fix in 30 seconds:

1. In your own browser at `subscription.cookunity.com`, open DevTools → Network.
2. Reload the menu page, pick any request, **Copy → Copy as cURL**.
3. On the app's page, click **⚙ Auth** in the top bar, paste the curl, hit **Save credentials**.

The server parses the headers, updates the live proxy, and writes them to `state/creds.json` (mounted volume, survives restarts). No container restart, no SSH.

## Architecture

Two stdlib-only Python scripts + the browser:

- **`scrape.py`** — one-shot menu exporter. Hits the GraphQL `menu` query for any delivery date and writes `menus/<date>.json`. Also renders a print-friendly `<date>.html` for PDF export. Can process a range of weeks.
- **`serve.py`** — the local HTTP server. Renders the interactive HTML, proxies cart operations to CookUnity:
  - `GET /?date=YYYY-MM-DD` — menu page (auto-fetches the menu if not cached)
  - `GET /api/cart?date=…` — current cart state from CookUnity
  - `POST /api/cart/add`, `POST /api/cart/remove` — forward to CookUnity with date-specific cart UUID
  - `POST /api/refresh` — re-pull the GraphQL menu for a date
  - `GET|POST /api/creds` — report / update saved JWT + cookie

Why a proxy? The browser can't POST cross-origin to `subscription.cookunity.com` with credentials from a `192.168.x.x` page — CORS + cookie isolation block it. The server re-attaches auth and relays.

Favorites live only in each browser's `localStorage`, keyed by stable meal ID (`m-9252`) or bundle SKU (`b-bd-1189`) so they survive when weekly inventory IDs rotate.

## File layout

```
scrape.py              # menu exporter (standalone CLI, holds fetch_menu)
serve.py               # thin entry point → cookunity.cli.main
cookunity/             # the app
  __init__.py
  dates.py             # upcoming_mondays, date parsing
  curl_paste.py        # parse a pasted curl → {token, cookie, cart_id}
  env.py               # .env loader + state/creds.json persistence
  proxy.py             # CartProxy — all outbound HTTP to CookUnity
  state.py             # per-date menu cache + lazy fetch
  render.py            # HTML rendering (reads assets/page.{css,js})
  handler.py           # HTTP routes (BaseHTTPRequestHandler subclass)
  cli.py               # argparse + server boot
  assets/
    page.css           # the interactive page's styles
    page.js            # the interactive page's client code
tests/                 # pytest suite — see "Running tests" below
Dockerfile             # python:3.12-slim, copies scripts + cookunity/
compose.yaml           # port 8001→8000, mounts menus/ + state/
swarm.yaml             # Traefik-labeled deploy for Docker Swarm
.env.example           # seed credentials
menus/                 # cached <date>.json / .html (gitignored)
state/creds.json       # runtime-updated auth (gitignored — DO NOT COMMIT)
```

### Running tests

```bash
uv run --with pytest python -m pytest tests/
```

The suite has no network calls — `CartProxy` tests patch `urllib.request.urlopen`,
and `State` tests inject a fake `fetch_menu`. Adding new features should come
with a test at the same module level (`tests/test_<module>.py`).

## Caveats

- **Your laptop needs to stay awake and on Wi-Fi** for him to reach it.
- **Auth refresh is manual** — the app can't re-login on its own because CookUnity uses Auth0 with interactive flows. Re-paste a curl every day or so.
- **Remove is per-meal**, not per-quantity-unit — CookUnity's DELETE endpoint decrements by one; rapid clicks are safe but there's no batch clear.
- **No cross-week cart view** — you see one Monday at a time. The date dropdown switches the whole page context.
- **Not hardened.** Anyone on your LAN who reaches port 8001 can edit your cart. That's the intended threat model (you trust everyone on the LAN) but worth naming.

## Made with

Pure stdlib Python (no `requirements.txt`), a single handwritten HTML+CSS+JS page, and Docker. Runs fine on a MacBook doing other things.
