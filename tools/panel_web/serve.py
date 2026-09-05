#!/usr/bin/env python3
"""Serve the panel's usage page to a phone on the same network.

The same two windows the T-Display-S3 draws on its usage page, drawn the same
way, off the same reading -- for the times the panel is not the thing in front
of you.

It is deliberately not a second source of anything. usage_server.py already
holds the reading and already runs (a launch agent keeps it up on :8787); this
serves one HTML file and proxies that server's /usage through to it. Two
reasons for the proxy rather than letting the page fetch :8787 itself:

  - usage_server refuses requests from off the machine unless they carry
    X-Panel-Token, and a browser opening a URL cannot send a header. Fetched
    from here the request arrives from loopback, which it already trusts, so
    the token is never needed and never leaves the Mac.

  - a page served from :8791 fetching :8787 is cross-origin, and usage_server
    sends no CORS headers. Same origin, nothing to add.

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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "index.html")
FONT = os.path.join(HERE, os.pardir, "fonts", "IBMPlexSansThai-Regular.ttf")

USAGE_URL = "http://127.0.0.1:8787/usage"

# Long enough for the live reading, which is the host going and asking the API:
# usage_server gives up on that at 1.5s, so this is that plus the loopback trip.
UPSTREAM_TIMEOUT = 3.0

DEFAULT_PORT = 8791


def fetch_usage(live):
    """usage_server's answer, or a reason it could not be had.

    A failure comes back as a 200 carrying `error` rather than as an HTTP
    error, because the page renders it the way the panel does -- as a line of
    text where the numbers would be -- and a fetch that rejects gives it
    nothing to render.
    """
    url = USAGE_URL + ("?live=1" if live else "")
    try:
        with urllib.request.urlopen(url, timeout=UPSTREAM_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": "usage server said %d" % exc.code}
    except (urllib.error.URLError, OSError):
        return {"error": "usage server not running"}
    except (ValueError, json.JSONDecodeError):
        return {"error": "bad reply from usage server"}


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
            # The face the panel's own .vlw fonts were baked from, so the web
            # page is the same type rather than something that looks like it.
            # Served from here so a phone with no route to the internet -- one
            # on the Mac's hotspot, say -- still gets it.
            self.send_file(FONT, "font/ttf", "public, max-age=86400")

        elif route == "/api/usage":
            live = "live=1" in self.path.partition("?")[2].split("&")
            body = json.dumps(fetch_usage(live)).encode()
            self.reply(200, "application/json", body)

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
    print("  T-Display-S3 · Claude Code usage")
    print()
    print("  On your phone, open:")
    for ip in local_addresses():
        print("      http://%s:%d" % (ip, port))
    host = local_hostname()
    if host:
        print("      http://%s:%d      (if the network passes Bonjour)" % (host, port))
    print()
    print("  Anyone on this network can read the page -- it is percentages")
    print("  and reset times, no token and no credentials.")
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
