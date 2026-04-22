"""State cache tests — proves the lazy-fetch + refresh paths work without
talking to CookUnity. We pass a fake ``fetch_menu`` so no network is involved.
"""

from __future__ import annotations

import json

import pytest

from cookunity.proxy import CartProxy
from cookunity.state import State, latest_menu_date

from .conftest import make_menu


def _fake_fetch_menu(expected_call_count: list[int]):
    """Return a fetcher that counts calls and returns a canned menu."""

    def fetch(date, token, cookie):
        expected_call_count.append((date, bool(token), bool(cookie)))
        return make_menu()

    return fetch


def _state(tmp_path, fetch):
    return State(
        menu_dir=tmp_path,
        include_out_of_stock=False,
        proxy=CartProxy("t", "c", "seed"),
        upcoming=["2026-04-27"],
        fetch_menu=fetch,
    )


def test_get_reads_from_disk_when_json_exists(tmp_path):
    (tmp_path / "2026-04-27.json").write_text(json.dumps(make_menu()))
    calls: list = []
    s = _state(tmp_path, _fake_fetch_menu(calls))
    entry = s.get("2026-04-27")
    assert "page_html" in entry
    # Disk hit → no live fetch.
    assert calls == []


def test_get_fetches_live_when_disk_empty(tmp_path):
    calls: list = []
    s = _state(tmp_path, _fake_fetch_menu(calls))
    s.get("2026-04-27")
    assert calls == [("2026-04-27", True, True)]
    # Fetch should have persisted to disk.
    assert (tmp_path / "2026-04-27.json").exists()


def test_get_is_cached_after_first_call(tmp_path):
    calls: list = []
    s = _state(tmp_path, _fake_fetch_menu(calls))
    s.get("2026-04-27")
    s.get("2026-04-27")
    s.get("2026-04-27")
    # Still only one fetch.
    assert len(calls) == 1


def test_refresh_always_hits_live_even_if_cached(tmp_path):
    calls: list = []
    s = _state(tmp_path, _fake_fetch_menu(calls))
    (tmp_path / "2026-04-27.json").write_text(json.dumps(make_menu()))
    s.get("2026-04-27")           # disk hit
    assert len(calls) == 0
    s.refresh("2026-04-27")       # always live
    assert len(calls) == 1


def test_invalidate_all_forces_rerender_on_next_get(tmp_path):
    (tmp_path / "2026-04-27.json").write_text(json.dumps(make_menu()))
    s = _state(tmp_path, _fake_fetch_menu([]))
    first = s.get("2026-04-27")
    s.invalidate_all()
    second = s.get("2026-04-27")
    # Dropping the cache should re-render — same content but different object.
    assert first is not second


def test_get_raises_without_creds_when_disk_empty(tmp_path):
    s = State(
        menu_dir=tmp_path,
        include_out_of_stock=False,
        proxy=CartProxy("", "", ""),  # no auth
        upcoming=["2026-04-27"],
        fetch_menu=_fake_fetch_menu([]),
    )
    with pytest.raises(RuntimeError, match="No auth"):
        s.get("2026-04-27")


def test_latest_menu_date_picks_newest(tmp_path):
    for d in ("2026-04-27", "2026-05-04", "2026-05-11"):
        (tmp_path / f"{d}.json").write_text("{}")
    assert latest_menu_date(tmp_path) == "2026-05-11"


def test_latest_menu_date_returns_none_when_empty(tmp_path):
    assert latest_menu_date(tmp_path) is None
