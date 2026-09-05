#!/usr/bin/env python3
"""Serve the panel, as one page, to a phone on the same network.

What the T-Display-S3 shows across three of its pages -- the flip clock and the
weather under it, the two Claude Code windows, the Mac's charge and cells and
cores -- drawn the same way, off the same readings, on one page tall enough for
a phone to hold all of it at once. The panel cycles because it has 320x170 and
one of them at a time; a phone has the height, so nothing here has to be a page.

It is deliberately not a second source of anything. usage_server.py and
mac_stats_server.py already hold the readings and are already up; this serves
one HTML file and fetches from them on the page's behalf, which is the same
errand tools/usb_net_bridge.py runs for the panel. Two reasons for going
through here rather than letting the page fetch those ports itself:

  - both servers refuse requests from off the machine unless they carry
    X-Panel-Token, and a browser opening a URL cannot send a header. Fetched
    from here the requests arrive from loopback, which they already trust, so
    the token is never needed and never leaves the Mac.

  - a page served from :8791 fetching :8787 is cross-origin, and neither server
    sends CORS headers. Same origin, nothing to add.

The weather goes through here too, for a third reason: a phone on the Mac's own
hotspot has no route to the internet of its own, and the Mac does.

Started by double-clicking usage-panel.command, and meant to die with the
window that opened it: no launch agent, no daemon, nothing still listening
after the Terminal window is closed. That is the whole difference between this
and the two servers it sits in front of -- they are always up because the panel
needs them to be; this is up while you are looking at it.

Stdlib only, on the system python, for the reason the other two are: it has to
keep working if the project virtualenv is rebuilt.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "index.html")

# The face the panel's .vlw fonts were baked from. Beside this script once it is
# installed, and up in tools/fonts/ when it is being run out of the repo.
FONT_CANDIDATES = [
    os.path.join(HERE, "IBMPlexSansThai-Regular.ttf"),
    os.path.join(HERE, os.pardir, "fonts", "IBMPlexSansThai-Regular.ttf"),
]


def font_path():
    for path in FONT_CANDIDATES:
        if os.access(path, os.R_OK):
            return path
    return None

USAGE_URL = "http://127.0.0.1:8787/usage"
MAC_URL   = "http://127.0.0.1:8789/mac"
CPU_URL   = "http://127.0.0.1:8789/cpu"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# Long enough for the live reading, which is the host going and asking the API:
# usage_server gives up on that at 1.5s, so this is that plus the loopback trip.
UPSTREAM_TIMEOUT = 3.0

# The one fetch that leaves the machine, so the one that gets a real timeout.
WEATHER_TIMEOUT = 8.0

DEFAULT_PORT = 8791

# How long each reading is held before it is asked for again. These are the
# panel's own intervals, and they are here rather than in the page for the
# reason the bridge exists at all: one client asking every couple of seconds
# should not become four requests a second against two servers and a weather
# API. The page polls; this decides what a poll actually costs.
#
# Usage is the odd one. usage_server holds the API to one call every 25s
# whatever is asked of it, so asking more often than that buys nothing -- and
# 20 here means the next poll after its gate opens is the one that gets through.
TTL = {"usage": 20, "mac": 4, "cpu": 1, "weather": 600}

# A weather fetch that failed is retried on the panel's own schedule rather than
# the successful one's: a minute, not ten.
WEATHER_RETRY = 60


# --- where the weather is ------------------------------------------------------

# The panel reads these out of include/secrets.h at compile time. The installed
# copy of this server is not next to that file and could not read it there
# anyway -- ~/Desktop is TCC-protected -- so install.sh lifts the four values
# into panel.json beside serve.py. Running out of the repo, the header itself is
# still the better source: no install step to forget after moving house.
CONFIG = os.path.join(HERE, "panel.json")
SECRETS = os.path.join(HERE, os.pardir, os.pardir, "include", "secrets.h")


def load_place():
    """Latitude, longitude, timezone and name, or None if nothing says."""
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
        if "lat" in cfg and "lon" in cfg:
            return cfg
    except (OSError, ValueError):
        pass
    return parse_secrets(SECRETS)


def parse_secrets(path):
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return None

    def define(name, pattern):
        m = re.search(r"^\s*#define\s+%s\s+%s" % (name, pattern), text, re.M)
        return m.group(1) if m else None

    lat = define("WEATHER_LAT", r"(-?[\d.]+)")
    lon = define("WEATHER_LON", r"(-?[\d.]+)")
    if lat is None or lon is None:
        return None
    return {
        "lat": float(lat),
        "lon": float(lon),
        "tz": define("WEATHER_TZ_URL", r'"([^"]*)"') or "auto",
        "place": define("WEATHER_PLACE", r'"([^"]*)"') or "",
    }


PLACE = load_place()


# --- the readings ---------------------------------------------------------------

def get_json(url, timeout, what):
    """A JSON body, or a dict saying why there is not one.

    Failures come back as data rather than as exceptions for the reason the
    panel draws them: an error belongs on the page, in the space the numbers
    would have had, and a fetch that raises gives the page nothing to draw.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": "%s said %d" % (what, exc.code)}
    except (urllib.error.URLError, OSError):
        return {"error": "%s not answering" % what}
    except ValueError:
        return {"error": "bad reply from %s" % what}


def fetch_usage():
    return get_json(USAGE_URL + "?live=1", UPSTREAM_TIMEOUT, "usage server")


def fetch_mac():
    return get_json(MAC_URL, UPSTREAM_TIMEOUT, "mac server")


def fetch_cpu():
    return get_json(CPU_URL, UPSTREAM_TIMEOUT, "mac server")


def fetch_weather():
    """Current conditions, exactly the three fields the panel asks for.

    Nothing more is requested for the same reason the firmware stopped
    requesting it: the humidity, the wind and the daily high and low left the
    panel when the separate weather page did, and an unread field is still a
    field somebody has to wait for.
    """
    if not PLACE:
        return {"error": "no location configured"}

    url = WEATHER_URL + "?" + urllib.parse.urlencode({
        "latitude": PLACE["lat"],
        "longitude": PLACE["lon"],
        "current": "temperature_2m,apparent_temperature,weather_code",
        "forecast_days": 1,
    }) + "&timezone=" + PLACE["tz"]

    got = get_json(url, WEATHER_TIMEOUT, "open-meteo")
    if "error" in got:
        return got

    cur = got.get("current") or {}
    if "temperature_2m" not in cur:
        return {"error": "no conditions reported"}
    return {
        "temp": cur.get("temperature_2m"),
        "feels": cur.get("apparent_temperature"),
        "code": cur.get("weather_code", -1),
        "place": PLACE.get("place", ""),
    }


# --- holding them ---------------------------------------------------------------

# One lock over the lot rather than one per reading. Every fetch behind it is
# either loopback or ten minutes apart, the page is one client, and a second
# request arriving mid-fetch is better made to wait for the answer than sent to
# ask the same question again.
_lock = threading.Lock()
_held = {}   # name -> {"at": monotonic, "data": ...}


def reading(name, fetch):
    """The held answer, or a fresh one once it has aged past its TTL.

    A weather fetch that fails keeps whatever last worked and comes back to it
    in a minute instead of ten -- the panel's own rule. The two local ones have
    nothing to hold on to: a Mac that stopped answering has no old charge worth
    reporting, and saying so is what lets the page age the numbers it still has
    on its own terms.
    """
    now = time.monotonic()
    held = _held.get(name)

    if held is not None:
        ttl = TTL[name]
        if name == "weather" and "error" in held["data"] :
            ttl = WEATHER_RETRY
        if now - held["at"] < ttl:
            return held["data"], round(now - held["at"])

    fresh = fetch()

    # A failed weather fetch does not throw away a reading that worked: it is
    # ten minutes old at worst and the temperature outside has not moved much.
    if name == "weather" and "error" in fresh and held is not None \
            and "error" not in held["data"]:
        _held[name] = {"at": now - WEATHER_RETRY, "data": held["data"]}
        return held["data"], round(now - held["at"])

    _held[name] = {"at": now, "data": fresh}
    return fresh, 0


def state():
    """Everything the page draws, in one answer.

    One request rather than four, because they are drawn together: a page that
    fetched them separately would show a charge from one moment beside cores
    from another, and on a phone each of those is a radio wake-up.
    """
    with _lock:
        usage, _ = reading("usage", fetch_usage)
        mac, _ = reading("mac", fetch_mac)
        cpu, _ = reading("cpu", fetch_cpu)
        weather, wage = reading("weather", fetch_weather)

    # The two local servers date their own snapshots and the page adds the time
    # since; the weather has no such field of its own, so its age is how long
    # this has been holding it.
    if "error" not in weather:
        weather = dict(weather, age=wage)

    return {"now": int(time.time()), "usage": usage, "mac": mac,
            "cpu": cpu, "weather": weather}


class Handler(BaseHTTPRequestHandler):
    server_version = "panel-web/1.0"

    def send_file(self, path, ctype, cache):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(500, "cannot read %s" % os.path.basename(path))
            return
        self.reply(200, ctype, body, cache)

    def reply(self, code, ctype, body, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass   # a phone that locked its screen mid-reply

    def do_GET(self):
        route = self.path.split("?")[0]

        if route in ("/", "/index.html"):
            # Read per request rather than once at startup: editing the page
            # and pulling to refresh beats restarting the server to see a
            # change, and this serves one client.
            self.send_file(PAGE, "text/html; charset=utf-8", "no-store")

        elif route == "/font.ttf":
            # Served from here rather than from a font CDN so a phone with no
            # route to the internet -- one on the Mac's own hotspot, say --
            # still gets it. A 404 is a real answer: the page has a fallback
            # stack and will lay itself out in whatever it got.
            path = font_path()
            if path:
                self.send_file(path, "font/ttf", "public, max-age=86400")
            else:
                self.send_error(404, "font not installed beside the server")

        elif route == "/api/state":
            self.reply(200, "application/json", json.dumps(state()).encode())

        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass   # a phone polling every 30s would bury anything worth reading


def local_addresses():
    """Every address this Mac can be reached on, as the phone would use them.

    Straight out of ifconfig rather than by resolving our own hostname, which
    on a laptop that moves between a LAN and its own hotspot answers with
    whichever one it feels like. Internet Sharing's bridge is the address that
    matters when the phone is on the Mac's hotspot, and it is not the one a
    route lookup finds.
    """
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    found = []
    for ip in re.findall(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", out, re.M):
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        if ip not in found:
            found.append(ip)
    return found


def local_hostname():
    try:
        name = subprocess.run(["/usr/sbin/scutil", "--get", "LocalHostName"],
                              capture_output=True, text=True, timeout=5)
        name = name.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name + ".local" if name else ""


def banner(port):
    print()
    print("  T-Display-S3 · desk panel")
    print()
    print("  On your phone, open:")
    for ip in local_addresses():
        print("      http://%s:%d" % (ip, port))
    host = local_hostname()
    if host:
        print("      http://%s:%d      (if the network passes Bonjour)" % (host, port))
    print()
    print("  Anyone on this network can read the page -- percentages, reset")
    print("  times and this Mac's vitals. No token and no credentials.")
    if not PLACE:
        print()
        print("  No location configured, so the weather line will say so.")
        print("  Re-run install.sh, or check WEATHER_LAT in include/secrets.h.")
    print()
    print("  Close this window, or press Ctrl-C, to take it down.")
    print(flush=True)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT

    # Closing the Terminal window is the documented way to stop this, and that
    # arrives as a hangup. Default handling would be to die without unbinding,
    # which leaves the port held long enough that double-clicking again fails.
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(0))

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as exc:
        print("cannot listen on port %d: %s" % (port, exc))
        print("something else is using it, or a previous copy is still running.")
        print("\nPress Return to close.")
        try:
            input()
        except EOFError:
            pass
        return 1

    banner(port)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
        print("\nstopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
