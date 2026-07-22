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


def _state(tmp_path, fetch, upcoming=("2026-04-27",)):
    return State(
        menu_dir=tmp_path,
        include_out_of_stock=False,
        proxy=CartProxy("t", "c", "seed"),
        fetch_menu=fetch,
        upcoming_fn=lambda: list(upcoming),
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
        fetch_menu=_fake_fetch_menu([]),
        upcoming_fn=lambda: ["2026-04-27"],
    )
    with pytest.raises(RuntimeError, match="No auth"):
        s.get("2026-04-27")


def test_upcoming_recomputes_on_every_access(tmp_path):
    """The server runs for weeks; ``upcoming`` must never be a startup snapshot."""
    weeks = [["2026-04-27"], ["2026-05-04"]]
    s = State(
        menu_dir=tmp_path,
        include_out_of_stock=False,
        proxy=CartProxy("t", "c", "seed"),
        fetch_menu=_fake_fetch_menu([]),
        upcoming_fn=lambda: weeks[0],
    )
    assert s.upcoming == ["2026-04-27"]
    weeks[0] = ["2026-05-04"]  # simulate the week rolling over
    assert s.upcoming == ["2026-05-04"]


def test_cached_page_rerenders_when_week_rolls_over(tmp_path):
    """The date dropdown is baked into cached HTML — a week rollover must not
    keep serving last week's dropdown."""
    (tmp_path / "2026-04-27.json").write_text(json.dumps(make_menu()))
    upcoming = [["2026-04-27", "2026-05-04"]]
    s = State(
        menu_dir=tmp_path,
        include_out_of_stock=False,
        proxy=CartProxy("t", "c", "seed"),
        fetch_menu=_fake_fetch_menu([]),
        upcoming_fn=lambda: upcoming[0],
    )
    first = s.get("2026-04-27")
    assert first is s.get("2026-04-27")  # stable while the week is unchanged
    upcoming[0] = ["2026-05-04", "2026-05-11"]
    second = s.get("2026-04-27")
    assert second is not first
    assert b"2026-05-11" in second["page_html"]


def test_latest_menu_date_picks_newest(tmp_path):
    for d in ("2026-04-27", "2026-05-04", "2026-05-11"):
        (tmp_path / f"{d}.json").write_text("{}")
    assert latest_menu_date(tmp_path) == "2026-05-11"


def test_latest_menu_date_returns_none_when_empty(tmp_path):
    assert latest_menu_date(tmp_path) is None
