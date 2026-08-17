#!/usr/bin/env python3
"""Serve the Claude Code subscription usage limits to the T-Display-S3 over LAN.

Claude Code hands its status line a `rate_limits` object on every render, and
~/.claude/statusline.sh persists the two windows it cares about to a cache file:

    <5h used %> <5h resets_at> <7d used %> <7d resets_at>

That file is the only place the numbers survive outside a running session --
which means it goes stale the moment the terminal closes, even while the
Claude desktop app keeps reporting the same account's usage on its own.
desktop_usage_probe.py mirrors that into a second cache, same four-field
shape but with the resets always 0 (the desktop app's history never carries
one). Whichever of the two caches was written more recently wins; see
read_usage().

Nothing here talks to the API — no token, no network call — it just
reformats local files as JSON:

    GET /usage  ->  {"h5": 55, "h5_reset": 1786210200, "h5_stale": false,
                     "d7": 51, "d7_reset": 1786237200, "d7_stale": false,
                     "age": 12, "now": 1786213216}

`age` is seconds since the cache was last written, so the panel can say when the
reading stopped being live. A window whose reset time has already passed is
reported as 0% with no reset time — the same correction statusline.sh makes,
kept here so the firmware doesn't have to duplicate the rule.

Usage:
    usage_server.py [port]        default 8787; binds all interfaces, but see
                                      "who may ask" for who is served
"""

import hmac
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CACHE = os.path.expanduser("~/.claude/statusline-usage.cache")
DESKTOP_CACHE = os.path.expanduser("~/.claude/statusline-usage-desktop.cache")


# --- who may ask -------------------------------------------------------------
#
# The panel arrives two ways. Over the USB bridge the fetch is made by the
# bridge process on this machine, so it comes from loopback and is trusted the
# way anything else running here is. Over WiFi it comes from the LAN — and so
# could everything else on the network, which until now could read the banner
# text along with the rest. That text names project directories and whatever
# Claude stopped to ask about.
#
# So loopback stays open and anything else must carry the token. A shared
# secret in a header over plain HTTP is not much against someone reading the
# traffic and is not meant to be; it is against the network being able to
# simply read the port.
#
# Duplicated in the other server rather than shared. Each is copied to
# ~/Library/Application Support/ as one file by its launch agent, and staying
# one self-contained stdlib-only file is worth twenty repeated lines.

TOKEN_PATH = os.path.expanduser("~/.config/t-display-s3/token")
TOKEN_HEADER = "X-Panel-Token"

TOKEN = None


def load_token():
    """The shared secret, minted on first run. None if it cannot be stored."""
    try:
        with open(TOKEN_PATH) as f:
            existing = f.read().strip()
        if existing:
            return existing
    except OSError:
        pass

    fresh = secrets.token_hex(16)
    try:
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(fresh + "\n")
    except OSError as exc:
        # Without a token there is no way to tell an allowed LAN request from
        # any other, so they are all refused. Loopback is unaffected, which
        # leaves the USB bridge — the path that matters — working.
        print(f"cannot write {TOKEN_PATH}: {exc}", flush=True)
        print("LAN requests will be refused; the USB bridge is unaffected",
              flush=True)
        return None
    return fresh


def parse_cache(path, now):
    """A cache file's four fields, plus its age in seconds. None if the file
    is missing, mid-write, or was never written."""
    try:
        with open(path) as f:
            parts = f.read().split()
        age = max(0, now - int(os.path.getmtime(path)))
    except OSError:
        return None

    # Four whitespace-separated integers. Anything else means the file is being
    # rewritten underneath us or was never written — report "no reading" rather
    # than a half-parsed one.
    if len(parts) != 4:
        return None
    try:
        h5, h5r, d7, d7r = (int(p) for p in parts)
    except ValueError:
        return None

    return {"h5": h5, "h5_reset": h5r, "d7": d7, "d7_reset": d7r, "age": age}


def read_usage():
    """Parse both caches into a JSON-ready dict, keeping whichever is fresher.
    Missing values are reported as -1."""
    now = int(time.time())
    out = {
        "h5": -1, "h5_reset": 0, "h5_stale": False,
        "d7": -1, "d7_reset": 0, "d7_stale": False,
        "age": -1, "now": now,
    }

    primary = parse_cache(CACHE, now)
    desktop = parse_cache(DESKTOP_CACHE, now)

    chosen, other = primary, desktop
    if desktop is not None and (primary is None or desktop["age"] < primary["age"]):
        chosen, other = desktop, primary
    if chosen is None:
        return out

    out["age"] = chosen["age"]

    for key in ("h5", "d7"):
        pct, reset = chosen[key], chosen[key + "_reset"]

        # The desktop app's history records no reset time, so the fresher
        # reading routinely arrives without one while the older cache still
        # holds a usable boundary. A reset is an absolute epoch: one still in
        # the future describes the window in progress whatever the age of the
        # file it came from, so it is worth more than no countdown at all.
        if reset == 0 and other is not None and other[key + "_reset"] > now:
            reset = other[key + "_reset"]

        # A reading whose reset time has passed describes a window that is over;
        # the window after it starts empty, and its reset time is unknown until
        # the next API response reports one.
        if pct >= 0 and 0 < reset <= now:
            pct, reset = 0, 0
            out[key + "_stale"] = True

        out[key] = pct
        out[key + "_reset"] = reset

    return out


class Handler(BaseHTTPRequestHandler):
    def authorised(self):
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        return bool(TOKEN) and hmac.compare_digest(
            self.headers.get(TOKEN_HEADER, ""), TOKEN)

    def do_GET(self):
        if not self.authorised():
            self.send_error(403, "token required from off this machine")
            return

        if self.path.split("?")[0] not in ("/usage", "/"):
            self.send_error(404)
            return

        body = json.dumps(read_usage()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # One line per request on stdout drowns out anything useful when the panel
    # polls every 30s, so only errors are logged.
    def log_message(self, fmt, *args):
        pass


def main():
    global TOKEN
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787

    TOKEN = load_token()
    if TOKEN:
        # Not the value: stdout here is a log file in /tmp. Read it from the
        # path when it is needed for secrets.h.
        print(f"LAN requests need {TOKEN_HEADER}, from {TOKEN_PATH}", flush=True)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"serving {CACHE} on http://0.0.0.0:{port}/usage", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
