"""Environment + on-disk credential persistence.

Two sources, preferred in this order:

1. ``state/creds.json`` — written by the ``/api/creds`` endpoint when a user
   pastes a fresh curl via the UI. Survives restarts, mounts as a volume in
   Docker, and is gitignored.
2. ``.env`` at the repo root — one-time seed of ``CU_AUTH_TOKEN``,
   ``CU_COOKIE``, ``CU_CART_ID`` for first boot.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class LoadedCreds:
    token: str
    cookie: str
    cart_id: str
    source: str  # "env" | "pasted-curl"
    saved_at: str | None


def now_iso() -> str:
    """Local-timezone timestamp formatted for display."""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def load_dotenv(path: Path) -> None:
    """Populate ``os.environ`` from a ``.env`` file (without overriding).

    Mirrors python-dotenv's basic behaviour — ``KEY=value`` lines only, comments
    and blank lines ignored, surrounding quotes stripped.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def load_creds(creds_path: Path) -> LoadedCreds:
    """Read credentials, preferring a saved ``creds.json`` over env vars.

    Never raises — warnings are written to stderr so the server can still boot
    with partial/missing creds (the ``#auth`` UI lets the user paste new ones).
    """
    token = os.environ.get("CU_AUTH_TOKEN", "")
    cookie = os.environ.get("CU_COOKIE", "")
    cart_id = os.environ.get("CU_CART_ID", "")
    source = "env"
    saved_at: str | None = None

    if creds_path.exists():
        try:
            saved = json.loads(creds_path.read_text())
            token = saved.get("token") or token
            cookie = saved.get("cookie") or cookie
            cart_id = saved.get("cart_id") or cart_id
            source = "pasted-curl"
            saved_at = saved.get("saved_at")
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(f"warning: couldn't read {creds_path}: {e}\n")

    return LoadedCreds(
        token=token, cookie=cookie, cart_id=cart_id, source=source, saved_at=saved_at
    )


def save_creds(creds_path: Path, token: str, cookie: str, cart_id: str) -> str:
    """Persist the given credentials. Returns the ``saved_at`` timestamp."""
    saved_at = now_iso()
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(
        json.dumps(
            {"token": token, "cookie": cookie, "cart_id": cart_id, "saved_at": saved_at},
            ensure_ascii=False,
        )
    )
    return saved_at
