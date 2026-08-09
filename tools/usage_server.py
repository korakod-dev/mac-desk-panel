#!/usr/bin/env python3
"""Serve the Claude Code subscription usage limits to the T-Display-S3 over LAN.

Claude Code hands its status line a `rate_limits` object on every render, and
~/.claude/statusline.sh persists the two windows it cares about to a cache file:

    <5h used %> <5h resets_at> <7d used %> <7d resets_at>

That file is the only place the numbers survive outside a running session, so it
is what this server reads. Nothing here talks to the API — no token, no network
call — it just reformats a local file as JSON:

    GET /usage  ->  {"h5": 55, "h5_reset": 1786210200, "h5_stale": false,
                     "d7": 51, "d7_reset": 1786237200, "d7_stale": false,
                     "age": 12, "now": 1786213216}

`age` is seconds since the cache was last written, so the panel can say when the
reading stopped being live. A window whose reset time has already passed is
reported as 0% with no reset time — the same correction statusline.sh makes,
kept here so the firmware doesn't have to duplicate the rule.

Usage:
    usage_server.py [port]        default 8787, binds all interfaces
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CACHE = os.path.expanduser("~/.claude/statusline-usage.cache")


def read_usage():
    """Parse the cache into a JSON-ready dict. Missing values are reported as -1."""
    now = int(time.time())
    out = {
        "h5": -1, "h5_reset": 0, "h5_stale": False,
        "d7": -1, "d7_reset": 0, "d7_stale": False,
        "age": -1, "now": now,
    }

    try:
        with open(CACHE) as f:
            parts = f.read().split()
        out["age"] = max(0, now - int(os.path.getmtime(CACHE)))
    except OSError:
        return out

    # Four whitespace-separated integers. Anything else means the file is being
    # rewritten underneath us or was never written — report "no reading" rather
    # than a half-parsed one.
    if len(parts) != 4:
        return out
    try:
        h5, h5r, d7, d7r = (int(p) for p in parts)
    except ValueError:
        return out

    # A reading whose reset time has passed describes a window that is over; the
    # window after it starts empty, and its reset time is unknown until the next
    # API response reports one.
    for pct, reset, key in ((h5, h5r, "h5"), (d7, d7r, "d7")):
        if pct >= 0 and 0 < reset <= now:
            pct, reset = 0, 0
            out[key + "_stale"] = True
        out[key] = pct
        out[key + "_reset"] = reset

    return out


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"serving {CACHE} on http://0.0.0.0:{port}/usage", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
