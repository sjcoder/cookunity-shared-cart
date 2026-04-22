"""Tests for .env loading + creds persistence."""

from __future__ import annotations

import json

from cookunity.env import LoadedCreds, load_creds, load_dotenv, now_iso, save_creds


def test_load_dotenv_populates_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("CU_EXAMPLE_FOO", raising=False)
    env = tmp_path / ".env"
    env.write_text("# comment\nCU_EXAMPLE_FOO=bar\nEMPTY=\n")
    load_dotenv(env)
    import os
    assert os.environ["CU_EXAMPLE_FOO"] == "bar"


def test_load_dotenv_strips_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("CU_EXAMPLE_Q", raising=False)
    env = tmp_path / ".env"
    env.write_text('CU_EXAMPLE_Q="wrapped value"\n')
    load_dotenv(env)
    import os
    assert os.environ["CU_EXAMPLE_Q"] == "wrapped value"


def test_load_dotenv_does_not_overwrite_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("CU_EXAMPLE_PRESET", "kept")
    env = tmp_path / ".env"
    env.write_text("CU_EXAMPLE_PRESET=overwritten\n")
    load_dotenv(env)
    import os
    assert os.environ["CU_EXAMPLE_PRESET"] == "kept"


def test_load_creds_missing_file_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CU_AUTH_TOKEN", "env-token")
    monkeypatch.setenv("CU_COOKIE", "env-cookie")
    monkeypatch.setenv("CU_CART_ID", "env-cart")
    got = load_creds(tmp_path / "creds.json")
    assert got == LoadedCreds(
        token="env-token", cookie="env-cookie", cart_id="env-cart",
        source="env", saved_at=None,
    )


def test_load_creds_prefers_saved_file_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CU_AUTH_TOKEN", "env-token")
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({
        "token": "file-token", "cookie": "file-cookie",
        "cart_id": "file-cart", "saved_at": "2026-04-22 00:00",
    }))
    got = load_creds(creds)
    assert got.token == "file-token"
    assert got.source == "pasted-curl"
    assert got.saved_at == "2026-04-22 00:00"


def test_save_creds_writes_all_fields(tmp_path):
    path = tmp_path / "sub" / "creds.json"
    saved_at = save_creds(path, "t", "c", "cart")
    data = json.loads(path.read_text())
    assert data == {"token": "t", "cookie": "c", "cart_id": "cart", "saved_at": saved_at}


def test_now_iso_format():
    # Format is stable; we just check shape (no fragile timestamp assertions).
    s = now_iso()
    assert len(s) == 16
    assert s[4] == "-" and s[7] == "-" and s[10] == " " and s[13] == ":"
