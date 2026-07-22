"""Parse a pasted ``curl`` command into the pieces we need: JWT, cookie jar,
and (optionally) a cart UUID. Users grab the command from DevTools "Copy as
cURL", which on Chrome emits bash-ANSI-C (``$'...'``) escape sequences in the
cookie block — we normalise those before regex-matching.
"""

from __future__ import annotations

import re
import shlex


_ANSI_C_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}


def decode_ansi_c(s: str) -> str:
    """Decode the ``$'...'`` escape subset Chrome's copy-as-cURL emits.

    Handles ``\\uHHHH``, ``\\xHH``, and the common single-char escapes
    (``\\n \\t \\r \\\\ \\' \\"``). Unknown escapes pass through as the
    escaped character verbatim.
    """

    def sub(m: re.Match) -> str:
        if m.group(1):
            return chr(int(m.group(1), 16))
        if m.group(2):
            return chr(int(m.group(2), 16))
        c = m.group(3)
        return _ANSI_C_ESCAPES.get(c, c)

    return re.sub(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})|\\(.)", sub, s)


def parse_curl(text: str) -> dict:
    """Return ``{token, cookie, cart_id}`` from a pasted curl command.

    Raises ``ValueError`` with a human-readable reason if required pieces
    are missing. ``cart_id`` is ``None`` when the pasted command doesn't
    reference a ``/cart/v2/<UUID>`` URL (e.g. the user pasted a menu query
    instead of a cart one — both work for auth).
    """
    # Normalise $'...' blocks so the rest of the regexes can treat them as
    # regular single-quoted strings.
    def _ansi_c(m: re.Match) -> str:
        return "'" + decode_ansi_c(m.group(1)) + "'"

    text = re.sub(r"\$'((?:[^'\\]|\\.)*)'", _ansi_c, text, flags=re.DOTALL)

    auth: str | None = None
    cookie: str | None = None

    try:
        args = shlex.split(text, posix=True)
    except ValueError:
        args = []

    i = 0
    while i < len(args):
        arg = args[i]
        value: str | None = None
        if arg in ("-H", "--header") and i + 1 < len(args):
            value = args[i + 1]
            i += 1
        elif arg.startswith("--header="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-H") and len(arg) > 2:
            value = arg[2:]

        if value and ":" in value:
            name, header_value = value.split(":", 1)
            if name.strip().lower() == "authorization":
                auth = header_value.strip()
            elif name.strip().lower() == "cookie":
                cookie = header_value.strip()

        value = None
        if arg in ("-b", "--cookie") and i + 1 < len(args):
            value = args[i + 1]
            i += 1
        elif arg.startswith("--cookie="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-b") and len(arg) > 2:
            value = arg[2:]
        if value:
            cookie = value

        i += 1

    if not auth:
        m = re.search(r"-H\s+['\"]authorization:\s*([^'\"]+?)['\"]", text, re.IGNORECASE)
        if m:
            auth = m.group(1).strip()
    if auth and auth.lower().startswith("bearer "):
        auth = auth[7:].strip()

    if not cookie:
        m = re.search(r"-b\s+['\"]((?:[^'\"\\]|\\.)*)['\"]", text, re.DOTALL)
        if m:
            cookie = m.group(1)
        else:
            m = re.search(r"-H\s+['\"]cookie:\s*([^'\"]+?)['\"]", text, re.IGNORECASE)
            if m:
                cookie = m.group(1)

    cart_id: str | None = None
    m = re.search(r"/cart/v2/([0-9a-fA-F-]{36})", text)
    if m:
        cart_id = m.group(1)

    if not auth:
        raise ValueError(
            "Could not find an `authorization:` header in the curl. "
            "Pick an authenticated API request from `subscription.cookunity.com`, "
            "such as `sdui-service/cart`, `sdui-service/incentives`, or "
            "`menu-service/graphql`; tracking requests like `martech.cookunity.com/v1/t` "
            "do not include the credentials this app needs."
        )
    if not cookie:
        raise ValueError("Could not find a cookie (`-b '...'` or `-H 'cookie: ...'`).")
    if "appSession=" not in cookie:
        raise ValueError(
            "Cookie is missing `appSession=...` — that's the session token the API requires."
        )

    return {"token": auth, "cookie": cookie, "cart_id": cart_id}
