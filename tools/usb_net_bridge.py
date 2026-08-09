#!/usr/bin/env python3
"""Give the T-Display-S3 a network over the USB-C cable, with no WiFi involved.

The panel has no way to put IP on USB — the Arduino core's TinyUSB is built
without the NCM/ECM classes and its lwIP without PPP — so instead of moving
packets this moves requests. The firmware frames them over the same CDC link
that already carries the console (see src/dashboard/net_link.h):

    device -> host    \\n@REQ <id> <verb> [arg]\\n
    host   -> device   @RES <id> <status> <len>\\n<len bytes>

    PING            liveness, so the panel knows a bridge is listening
    TIME            epoch seconds, standing in for NTP
    GET <url>       this process fetches it and returns the body

Everything else the device prints is console output: it goes to stdout and to
any client on the passthrough port, so this replaces `pio device monitor`
rather than fighting it for the port.

The host `usb-host` always means the machine running this bridge, which is how
the firmware reaches tools/usage_server.py without knowing what network the
laptop is on today. A URL aimed at one of this machine's real addresses is
rewritten the same way, so the LAN form keeps working too.

Usage:
    usb_net_bridge.py [--port /dev/cu.usbmodem…] [--listen 8788]
                      [--quiet] [--verbose]

Passthrough port (127.0.0.1:8788 by default):
    bytes sent by a client go to the device, device output comes back, so
    tools/grab_screen.py keeps working while this is running. A line starting
    with '!' is a command to the bridge instead:

        !pause <seconds>   release the serial port, e.g. so pio can upload
"""

import argparse
import glob
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

import serial

MAX_BODY = 4096         # matches MAX_BODY in net_link.cpp
FETCH_TIMEOUT = 10
LOCAL_TTL = 60          # re-read this machine's addresses this often

# Reserved name for "whichever machine is bridging", so the firmware can address
# a service on the laptop without depending on the laptop's current IP. It must
# match USAGE_HOST_USB in the firmware.
USB_HOST_ALIAS = "usb-host"


# --- talking to the device ---------------------------------------------------


class Device:
    """The serial port, plus the fan-out to passthrough clients.

    Writes come from the reader loop, from per-request worker threads and from
    passthrough clients, so every write takes the lock. `port` is swapped out
    while paused or reconnecting, and a write with no port is dropped: the
    device will time out and retry, which is the same thing it does when no
    bridge is running at all.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.port = None
        self.clients = set()
        self.clients_lock = threading.Lock()

    def write(self, data):
        with self.lock:
            if self.port is None:
                return
            try:
                self.port.write(data)
                self.port.flush()
            except (serial.SerialException, OSError) as exc:
                log(f"write failed: {exc}")

    def respond(self, rid, status, body):
        self.write(b"@RES %d %d %d\n" % (rid, status, len(body)) + body)

    def broadcast(self, data):
        with self.clients_lock:
            dead = []
            for sock in self.clients:
                try:
                    sock.sendall(data)
                except OSError:
                    dead.append(sock)
            for sock in dead:
                self.clients.discard(sock)
                try:
                    sock.close()
                except OSError:
                    pass


def log(msg):
    print(f"[bridge] {msg}", flush=True)


# --- resolving the machine's own addresses -----------------------------------


_local = {"cache": frozenset(), "at": 0.0}


def local_addrs():
    """Addresses that mean "this machine", for rewriting to loopback.

    Cached briefly rather than computed once: a laptop changes networks, and a
    usage URL pointing at yesterday's DHCP lease should still resolve here.
    """
    now = time.time()
    if now - _local["at"] < LOCAL_TTL:
        return _local["cache"]

    found = {"127.0.0.1", "::1", "localhost", USB_HOST_ALIAS}

    # The address the default route would use — the only reliable way to learn
    # it without a dependency. No packet is sent; connect() on UDP just binds.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        found.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    try:
        hostname = socket.gethostname()
        found.add(hostname)
        for info in socket.getaddrinfo(hostname, None):
            found.add(info[4][0])
    except OSError:
        pass

    _local["cache"] = frozenset(found)
    _local["at"] = now
    return _local["cache"]


def to_loopback(url):
    """Point a URL at 127.0.0.1 when it names this machine, else leave it."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host or host not in local_addrs():
        return url
    netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# --- the three verbs ---------------------------------------------------------


def do_get(dev, rid, url):
    target = to_loopback(url)
    try:
        req = urllib.request.Request(target, headers={"User-Agent": "t-display-s3"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            body = resp.read(MAX_BODY + 1)
            status = resp.status
    except urllib.error.HTTPError as exc:
        dev.respond(rid, 0, f"HTTP {exc.code}".encode())
        return
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        dev.respond(rid, 0, str(reason)[:120].encode("utf-8", "replace"))
        return

    if len(body) > MAX_BODY:
        # Truncating would hand the firmware invalid JSON, which reads as a
        # parse error and hides the real cause.
        dev.respond(rid, 0, b"response too large")
        return

    dev.respond(rid, status, body)


def handle_request(dev, line, quiet, verbose=False):
    """Dispatch one '@REQ …' line. Returns True if it was one."""
    if not line.startswith(b"@REQ "):
        return False
    if verbose:
        log(f"<- {line.decode('utf-8', 'replace')}")

    parts = line.split(None, 3)
    if len(parts) < 3:
        return True  # malformed: swallow it rather than echo it as console text
    try:
        rid = int(parts[1])
    except ValueError:
        return True

    verb = parts[2].decode("ascii", "replace").upper()
    arg = parts[3].decode("utf-8", "replace").strip() if len(parts) > 3 else ""

    if verb == "PING":
        dev.respond(rid, 200, b"")
    elif verb == "TIME":
        dev.respond(rid, 200, b"%d" % int(time.time()))
    elif verb == "GET":
        if not quiet:
            log(f"GET {arg}")
        # On a worker so a slow fetch does not stall the console or the next
        # request; the device is blocked on this one anyway.
        threading.Thread(target=do_get, args=(dev, rid, arg), daemon=True).start()
    else:
        dev.respond(rid, 0, b"unknown verb")
    return True


# --- passthrough port --------------------------------------------------------


def serve_client(dev, sock, control):
    """Relay one client's bytes to the device, minus any '!' bridge commands."""
    with dev.clients_lock:
        dev.clients.add(sock)
    pending = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return
            pending += chunk
            # Only a chunk that opens with '!' is held back for a full line;
            # grab_screen.py sends bare 'S'/'N' bytes and must not be delayed.
            while pending:
                if pending.startswith(b"!"):
                    if b"\n" not in pending:
                        break
                    line, _, pending = pending.partition(b"\n")
                    control(line.strip().decode("utf-8", "replace"), sock)
                else:
                    cut = pending.find(b"!")
                    if cut == -1:
                        dev.write(pending)
                        pending = b""
                    else:
                        dev.write(pending[:cut])
                        pending = pending[cut:]
    except OSError:
        pass
    finally:
        with dev.clients_lock:
            dev.clients.discard(sock)
        try:
            sock.close()
        except OSError:
            pass


def serve_passthrough(dev, listen_port, control):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", listen_port))
    except OSError as exc:
        log(f"passthrough port {listen_port} unavailable: {exc}")
        return
    srv.listen(4)
    log(f"passthrough on 127.0.0.1:{listen_port}")
    while True:
        try:
            sock, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=serve_client, args=(dev, sock, control),
                         daemon=True).start()


# --- serial loop -------------------------------------------------------------


def find_port(explicit):
    if explicit:
        return explicit
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None


def run(dev, ser, state, quiet, verbose):
    """Read the device until the port dies or a pause is requested.

    A framebuffer dump is announced by its own header and is not text, so the
    exact byte count that follows is passed through untouched — otherwise a
    pixel run that happened to contain a newline would be parsed as a line.
    """
    buf = bytearray()
    raw_left = 0

    while state["pause_until"] == 0:
        if raw_left:
            chunk = ser.read(min(raw_left, 4096))
            if chunk:
                dev.broadcast(chunk)
                raw_left -= len(chunk)
            continue

        chunk = ser.read(max(1, ser.in_waiting))
        if not chunk:
            continue
        buf += chunk

        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            text = line.rstrip(b"\r")

            if handle_request(dev, text, quiet, verbose):
                continue

            dev.broadcast(bytes(line) + b"\n")
            if text.startswith(b"SCREEN "):
                try:
                    _, w, h = text.split()
                    raw_left = int(w) * int(h) * 2
                except ValueError:
                    raw_left = 0
                if raw_left:
                    # Pixels already sitting in the buffer belong to the dump.
                    take = bytes(buf[:raw_left])
                    if take:
                        dev.broadcast(take)
                        raw_left -= len(take)
                        buf = bytearray(buf[len(take):])
                    break
            elif not quiet and text:
                # Empty lines are skipped: every request is prefixed with a
                # newline to guarantee its header starts a line, and echoing
                # those would double-space the whole console.
                sys.stdout.write(text.decode("utf-8", "replace") + "\n")
                sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", help="serial device (default: first /dev/cu.usbmodem*)")
    ap.add_argument("--baud", type=int, default=115200,
                    help="ignored by the S3's native USB CDC, kept for pyserial")
    ap.add_argument("--listen", type=int, default=8788,
                    help="passthrough port, 0 to disable")
    ap.add_argument("--quiet", action="store_true",
                    help="do not echo device output to stdout")
    ap.add_argument("--verbose", action="store_true",
                    help="log every request the device makes, not just fetches")
    args = ap.parse_args()

    dev = Device()
    # `released` is set whenever no serial port is held. !pause waits on it and
    # only then acknowledges, so an uploader knows the port is genuinely free
    # instead of racing the reader loop's next timeout.
    state = {"pause_until": 0.0, "released": threading.Event()}
    state["released"].set()

    def control(line, sock):
        parts = line.split()
        if parts and parts[0] == "!pause":
            secs = float(parts[1]) if len(parts) > 1 else 30.0
            state["pause_until"] = time.time() + secs
            log(f"releasing the port for {secs:g}s")
            ok = state["released"].wait(5.0)
            try:
                sock.sendall(b"!paused\n" if ok else b"!busy\n")
            except OSError:
                pass
        else:
            log(f"unknown command: {line}")

    if args.listen:
        threading.Thread(target=serve_passthrough,
                         args=(dev, args.listen, control), daemon=True).start()

    log("ready — the panel will show USB once it gets an answer")
    while True:
        if state["pause_until"]:
            time.sleep(max(0.0, state["pause_until"] - time.time()))
            state["pause_until"] = 0.0

        path = find_port(args.port)
        if path is None:
            time.sleep(1.0)
            continue

        try:
            ser = serial.Serial(path, args.baud, timeout=0.1)
        except (serial.SerialException, OSError) as exc:
            log(f"{path}: {exc}")
            time.sleep(1.0)
            continue

        log(f"attached to {path}")
        state["released"].clear()
        with dev.lock:
            dev.port = ser
        try:
            run(dev, ser, state, args.quiet, args.verbose)
        except (serial.SerialException, OSError) as exc:
            log(f"{path} dropped: {exc}")
        finally:
            with dev.lock:
                dev.port = None
            try:
                ser.close()
            except OSError:
                pass
            state["released"].set()
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
