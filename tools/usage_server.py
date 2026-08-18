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

There is a third source, and it is the only one that talks to the network:
`GET /usage?live=1` asks the API for the figure as it stands, over the same
endpoint Claude Code's own /usage command uses and the same credential the CLI
signed in with. The panel asks for it only while it is actually showing the
usage page, and every way it can fail falls back to the two files -- see "the
live reading" for why it is here and what it costs.

    GET /usage  ->  {"h5": 55, "h5_reset": 1786210200, "h5_stale": false,
                     "d7": 51, "d7_reset": 1786237200, "d7_stale": false,
                     "age": 12, "live": false, "now": 1786213216}

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
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
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


# --- the live reading --------------------------------------------------------
#
# Both files above are written by something else on a schedule of its own, and
# the desktop app's is the slow one: it polls its plan usage about every 15
# minutes and records nothing extra when you send a message. So a panel fed
# from that file trails the true number for as long as you are working, which
# on a busy morning is a few percent. Nothing else on the machine holds a
# fresher copy -- the app's own HTTP cache is rewritten by that same poll.
#
# The number with no lag in it is the one the API will state on being asked, so
# this asks it: the endpoint Claude Code's own /usage command calls. What that
# endpoint wants is a token scoped `user:profile`, and the long-lived kind
# `claude setup-token` mints is scoped for inference alone -- it is answered
# with a scope error, at once and politely. The one credential on this machine
# that carries the right scope is the one the CLI itself signed in with, which
# is why /usage works there and why this reads the same store.
#
# That is Claude Code's own credential, not this panel's -- so it is read, never
# written, and never refreshed here. The CLI refreshes it whenever it runs, and
# this rides along; a token that has expired with no session since to renew it
# is one more way the live reading is simply unavailable, which the caches
# already answer for. On this machine it lives in the login keychain; where
# there is no keychain the CLI keeps it in a file, and both are tried.
#
# It is a credential, so it is fenced:
#
#   * The panel asks for it only while the usage page is the page on screen.
#     Off that page the two files answer, exactly as before.
#   * At most one call every LIVE_MIN_GAP seconds even then, and a failure
#     stands back for LIVE_BACKOFF rather than retrying on every poll.
#   * Every failure -- no credential, an expired token, no network, a changed
#     endpoint -- falls back to the files. The live source can be entirely
#     broken and the panel still reads what it read before.

USAGE_API = "https://api.anthropic.com/api/oauth/usage"
CC_KEYCHAIN_SERVICE = "Claude Code-credentials"
CC_CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")

LIVE_MIN_GAP = 25       # seconds between calls to the API
LIVE_BACKOFF = 120      # seconds to stand back after one fails
LIVE_TIMEOUT = 1.5      # the panel is waiting on this one; do not hold it
TOKEN_TTL = 300         # hold a read token this long before hitting the store

_live_lock = threading.Lock()
_live = {"reading": None, "at": 0, "next_try": 0, "error": ""}
_token = {"value": None, "read_at": 0}


def stored_credential():
    """Claude Code's stored OAuth credential, as the CLI wrote it: the login
    keychain on this machine, the file where there is no keychain."""
    reason = "no keychain item"
    try:
        done = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-w", "-s", CC_KEYCHAIN_SERVICE],
            capture_output=True, text=True, timeout=30)
        if done.returncode == 0 and done.stdout.strip():
            return json.loads(done.stdout)
        reason = done.stderr.strip() or reason
    except (OSError, ValueError) as exc:
        reason = str(exc)

    try:
        with open(CC_CREDENTIALS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        raise RuntimeError("no Claude Code credential (" + reason + ")")


def oauth_token():
    """The access token, held for TOKEN_TTL so a page left open does not run
    `security` on every poll. Raises rather than send one already known to be
    no good: a request refused still costs the round trip and the backoff."""
    now = time.time()
    if _token["value"] and now - _token["read_at"] < TOKEN_TTL:
        return _token["value"]

    stored = stored_credential()
    oauth = stored.get("claudeAiOauth") or stored
    token = oauth.get("accessToken")
    if not token:
        raise RuntimeError("credential carries no access token")

    scopes = oauth.get("scopes") or []
    if scopes and "user:profile" not in scopes:
        raise RuntimeError("token is scoped " + ",".join(scopes) +
                           " -- this endpoint wants user:profile")

    expires = oauth.get("expiresAt") or 0
    if expires and expires / 1000 <= now:
        raise RuntimeError("token expired; run a Claude Code session to renew")

    _token["value"], _token["read_at"] = token, now
    return token


def window(obj):
    """One window of the response as this server's two fields. `resets_at` is
    an ISO stamp there and an epoch everywhere in this file."""
    if not isinstance(obj, dict):
        return -1, 0

    pct = obj.get("utilization")
    if pct is None:
        pct = obj.get("percent")
    pct = -1 if pct is None else int(round(pct))

    stamp, reset = obj.get("resets_at"), 0
    if stamp:
        try:
            reset = int(datetime.fromisoformat(
                stamp.replace("Z", "+00:00")).timestamp())
        except ValueError:
            reset = 0
    return pct, reset


def windows(data):
    """The 5h and 7d windows out of a usage response.

    Named objects first, and the `limits` array as the fallback: the shape
    carries both, and which one an endpoint fills in is not something to have
    a panel depend on.
    """
    h5, h5_reset = window(data.get("five_hour"))
    d7, d7_reset = window(data.get("seven_day"))
    if h5 >= 0 and d7 >= 0:
        return h5, h5_reset, d7, d7_reset

    for limit in data.get("limits") or []:
        if not isinstance(limit, dict):
            continue
        if limit.get("kind") == "session" and h5 < 0:
            h5, h5_reset = window(limit)
        elif limit.get("kind", "").startswith("weekly") and d7 < 0:
            d7, d7_reset = window(limit)
    return h5, h5_reset, d7, d7_reset


def fetch_live():
    """Ask the API. Raises on anything at all going wrong."""
    request = urllib.request.Request(
        USAGE_API,
        headers={"Authorization": "Bearer " + oauth_token(),
                 # The flag that tells the API this Bearer is an OAuth token
                 # rather than an API key; without it the token is not read as
                 # one. It is a documented request header, not a disguise.
                 "anthropic-beta": "oauth-2025-04-20",
                 "Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "t-display-s3-panel"})
    try:
        with urllib.request.urlopen(request, timeout=LIVE_TIMEOUT) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        # Worth reading rather than counting: a 401 wants a new token and a
        # 404 means the endpoint moved, and they look the same from a status
        # code alone.
        detail = exc.read(200).decode("utf-8", "replace").replace("\n", " ")
        raise RuntimeError(f"HTTP {exc.code}: {detail.strip()}")

    h5, h5_reset, d7, d7_reset = windows(data)
    if h5 < 0 and d7 < 0:
        raise RuntimeError("response carries neither window: " +
                           ", ".join(sorted(data)[:8]))
    return {"h5": h5, "h5_reset": h5_reset,
            "d7": d7, "d7_reset": d7_reset, "age": 0}


def live_reading(now):
    """The reading from the API in the shape parse_cache() returns, or None if
    it cannot be had. Never raises: the caller has two files to fall back on,
    and a panel showing the older number beats a panel showing an error."""
    with _live_lock:
        if _live["reading"] is not None and now - _live["at"] < LIVE_MIN_GAP:
            return dict(_live["reading"], age=now - _live["at"])
        if now < _live["next_try"]:
            return None

        try:
            reading = fetch_live()
        except Exception as exc:
            _live["next_try"] = now + LIVE_BACKOFF
            # The held token is not ours to hand back, but a dead one should
            # not sit in the cache for the whole TTL either -- the CLI may
            # renew it in the store before then.
            _token["value"] = None
            # Only when it changes: this is asked for every 30s while the page
            # is up, and one broken token would otherwise fill the log.
            if str(exc) != _live["error"]:
                _live["error"] = str(exc)
                print(f"live reading unavailable, using the caches: {exc}",
                      flush=True)
            return None

        if _live["error"]:
            print("live reading working again", flush=True)
        _live.update(reading=reading, at=now, next_try=0, error="")
        return dict(reading, age=0)


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


def read_usage(live=False):
    """The three sources as a JSON-ready dict, keeping whichever is freshest.
    Missing values are reported as -1. `live` asks the API as well, and is set
    by the panel only while the usage page is the one on screen."""
    now = int(time.time())
    out = {
        "h5": -1, "h5_reset": 0, "h5_stale": False,
        "d7": -1, "d7_reset": 0, "d7_stale": False,
        "age": -1, "live": False, "now": now,
    }

    primary = parse_cache(CACHE, now)
    desktop = parse_cache(DESKTOP_CACHE, now)
    fresh = live_reading(now) if live else None

    chosen, other = primary, desktop
    if desktop is not None and (primary is None or desktop["age"] < primary["age"]):
        chosen, other = desktop, primary
    # Ties go to the live reading: seconds old at the worst, and the only
    # source that states both reset times itself.
    if fresh is not None and (chosen is None or fresh["age"] <= chosen["age"]):
        chosen, other = fresh, chosen
    if chosen is None:
        return out

    out["age"] = chosen["age"]
    out["live"] = chosen is fresh

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

        # ?live=1 is the panel saying the usage page is what is on screen.
        # Anything else is answered from the files alone.
        live = "live=1" in self.path.partition("?")[2].split("&")
        body = json.dumps(read_usage(live)).encode()
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
    print(f"?live=1 asks the API directly, at most every {LIVE_MIN_GAP}s",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
