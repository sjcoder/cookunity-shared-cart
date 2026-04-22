#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Interactive CookUnity menu browser with a cart proxy.

Thin entry point — the real code lives in the ``cookunity`` package. Kept at
the repo root so ``python serve.py`` and the Dockerfile keep working.
"""

from cookunity.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
