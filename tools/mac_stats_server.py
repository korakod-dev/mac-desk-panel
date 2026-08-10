#!/usr/bin/env python3
"""Serve this Mac's vitals to the T-Display-S3 as JSON.

The panel reaches it the same way it reaches usage_server.py — over the USB
bridge when one is running, over the LAN otherwise:

    GET /mac  ->  {"bat_pct": 65, "bat_state": "discharging", "bat_mins": 481,
                   "load1": 2.85, "ncpu": 11, "mem_used": 30,
                   "disk_free_gb": 179, "disk_used": 60, "display": "external",
                   "uptime_s": 219043, "age": 2, "now": 1786243278}

    GET /cpu  ->  {"cores": [12, 3, 45, ...], "avg": 27,
                   "ecores": 6, "pcores": 5, "age": 0}

`cores` is busy percentage per logical core over the last sampling window,
efficiency cores first.

It also carries the panel's notification banner, which is the one thing here
that travels towards the screen rather than away from the Mac:

    POST /notify  {"msg": "build failed", "kind": "warn", "ttl": 20}
    GET  /notify  ->  {"id": 7, "msg": "build failed", "kind": "warn",
                       "ttl": 20, "age": 3}

Requests from this machine — which is every request over the USB bridge, since
the bridge fetches on the panel's behalf — are served as they always were.
Requests from the LAN, which is the panel's WiFi fallback, must carry the token
in an X-Panel-Token header; see "who may ask". Posting a notification stays
loopback-only whatever the token says.

Anything that could not be read comes back as -1 (or "" for the battery state)
rather than being omitted, so the firmware renders "--" instead of a stale or
invented number.

Everything here shells out to stock macOS tools — no third-party dependency, so
it runs on the system python and keeps working if the project venv is rebuilt.
Every reading is taken by a background thread on a fixed period, so a request
costs a dict lookup rather than a burst of process spawns; the panel's fetch
blocks its main loop, and a spawn is the slowest thing a busy Mac does. `age`
says how old the snapshot is.

Usage:
    mac_stats_server.py [port]        default 8789; binds all interfaces, but see
                                      "who may ask" for who is served
"""

import ctypes
import ctypes.util
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

RUN_TIMEOUT = 3.0        # a stat tool that hangs must not hang the panel
SAMPLE_INTERVAL = 1.0    # per-core sampling period, and so the panel's ceiling
STATS_FAST = 5.0         # resample load and memory this often
STATS_SLOW = 30.0        # battery, disk and uptime, which move slower


def run(*args):
    """Stdout of a command, or None if it fails, times out, or isn't there."""
    try:
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=RUN_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


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


# --- individual readings -----------------------------------------------------

# CoreGraphics, for display(). Loaded once and lazily: it is the only reading
# that calls a framework rather than spawning a tool, and a machine where the
# load fails should lose that one field rather than the server.
MAX_DISPLAYS = 16

_cg = None
_cg_warned = False


def _coregraphics():
    global _cg
    if _cg is None:
        _cg = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    return _cg


def battery():
    """Charge percentage, power state, and minutes left.

    `pmset -g batt` prints the power source on its first line and one line per
    battery, e.g.

        Now drawing from 'Battery Power'
         -InternalBattery-0 (id=24838243)	65%; discharging; 8:01 remaining ...

    The time estimate is missing while the OS is still working it out ("(no
    estimate)"), which is normal right after plugging in — not an error.
    """
    out = run("pmset", "-g", "batt")
    if not out:
        return {"bat_pct": -1, "bat_state": "", "bat_mins": -1}

    # A desktop Mac has no battery line at all; report unknown rather than 0%.
    pct = re.search(r"(\d+)%", out)

    # Taken as the field between the semicolons rather than by searching for
    # state words: "discharging" contains "charging", and "not charging" is a
    # state of its own, so word matching reports the opposite of the truth.
    field = re.search(r"\d+%;\s*([^;]+);", out)
    state = field.group(1).strip() if field else (
        "ac" if "'AC Power'" in out else "")

    mins = -1
    left = re.search(r"(\d+):(\d\d) remaining", out)
    if left:
        mins = int(left.group(1)) * 60 + int(left.group(2))

    return {
        "bat_pct": int(pct.group(1)) if pct else -1,
        "bat_state": state,
        "bat_mins": mins,
    }


def cpu():
    """1-minute load average and the core count to read it against.

    One sysctl call for both: process spawn dominates the cost of each of these
    tools, so asking for two values at once is half the price of asking twice.
    """
    out = run("sysctl", "-n", "vm.loadavg", "hw.ncpu")
    if not out:
        return {"load1": -1, "ncpu": -1}

    lines = out.split("\n")
    load = re.search(r"([\d.]+)", lines[0]) if lines else None
    try:
        ncpu = int(lines[1])
    except (IndexError, ValueError):
        ncpu = -1

    return {"load1": round(float(load.group(1)), 2) if load else -1, "ncpu": ncpu}


def memory():
    """Percentage of memory in use.

    `memory_pressure` reports the free share; the panel shows used, to read the
    same direction as the disk and usage figures next to it.
    """
    out = run("/usr/bin/memory_pressure")
    if not out:
        return {"mem_used": -1}
    free = re.search(r"free percentage:\s*(\d+)%", out)
    return {"mem_used": 100 - int(free.group(1)) if free else -1}


def disk():
    """Free space and used share of the data volume.

    Deliberately not `/`: on APFS that is the sealed system snapshot and always
    reads about 7% full, which says nothing about the space you can actually
    use. `/System/Volumes/Data` is where the files live.
    """
    out = run("df", "-k", "/System/Volumes/Data")
    lines = out.strip().split("\n") if out else []
    if len(lines) < 2:
        return {"disk_free_gb": -1, "disk_used": -1}

    fields = lines[-1].split()
    try:
        avail_kb = int(fields[3])
        used_pct = int(fields[4].rstrip("%"))
    except (IndexError, ValueError):
        return {"disk_free_gb": -1, "disk_used": -1}

    return {"disk_free_gb": round(avail_kb / 1048576), "disk_used": used_pct}


def display():
    """Which screen the Mac is driving, as one word.

    Rolls up two questions the panel could not answer before, because the same
    call answers both: whether anything external is being driven, and whether
    the screens are awake.

        "external"  driving at least one display that is not the built-in one
        "builtin"   the laptop's own screen only
        "asleep"    awake, but every screen is off — locked, or idled out
        "none"      no active display at all, which is rare and transient
        ""          the call failed

    Note what "asleep" is not: a Mac that is itself asleep does not answer this
    request at all, because this process is not being scheduled to answer it.
    So this field says the *screens* are off while the machine is still up. The
    panel tells the other case apart by the link going quiet, which is why the
    two readings are worth having side by side.

    A closed lid is why "external" cannot be inferred from the count. In
    clamshell the built-in display drops out of the list entirely, so a Mac
    driving one external monitor reports exactly one display, the same count as
    a laptop with the lid open and nothing plugged in. What separates them is
    CGDisplayIsBuiltin, not how many came back.

    The online list rather than the active one, which is the trap here and was
    found by measuring rather than by reading: a display that goes to sleep
    stops being *active* and leaves that list altogether. Built on the active
    list this function answered "none" for a screen that had merely turned
    itself off, which is the one state it exists to report, and CGDisplayIsAsleep
    never got a display to be asked about. The online list keeps everything
    physically attached and lets the sleep flag do its job.

    The last attempt at a screen reading shelled out to `ioreg` and was dropped
    for costing 25 ms of a 30 ms sampling pass. This is the same answer from
    the framework that owns it, and asking costs nothing by comparison: the
    three calls measure 31 microseconds and this whole function 88, some 280
    times cheaper than the reading it replaces. That is what makes it
    affordable on the fast pass, where nothing else avoids a process spawn.
    """
    try:
        cg = _coregraphics()
        ids = (ctypes.c_uint32 * MAX_DISPLAYS)()
        count = ctypes.c_uint32()
        if cg.CGGetOnlineDisplayList(MAX_DISPLAYS, ids, ctypes.byref(count)):
            return {"display": ""}

        online = [ids[i] for i in range(count.value)]
        if not online:
            return {"display": "none"}

        awake = [d for d in online if not cg.CGDisplayIsAsleep(d)]
        if not awake:
            return {"display": "asleep"}

        external = any(not cg.CGDisplayIsBuiltin(d) for d in awake)
        return {"display": "external" if external else "builtin"}
    except (OSError, AttributeError) as exc:
        # A framework that will not load or has lost a symbol is worth saying
        # once rather than every five seconds forever.
        global _cg_warned
        if not _cg_warned:
            _cg_warned = True
            print(f"display reading unavailable: {exc}", flush=True)
        return {"display": ""}


def uptime():
    """Seconds since boot, derived from the kernel's boot timestamp."""
    out = run("sysctl", "-n", "kern.boottime")
    if not out:
        return {"uptime_s": -1}
    sec = re.search(r"sec\s*=\s*(\d+)", out)
    if not sec:
        return {"uptime_s": -1}
    return {"uptime_s": max(0, int(time.time()) - int(sec.group(1)))}


# --- per-core CPU ------------------------------------------------------------
#
# Nothing in the shell reports per-core busy time: `top` and `iostat` aggregate,
# and `powermetrics` breaks it out but needs root, which a login agent must not
# have. The numbers come from the kernel's own per-CPU tick counters instead,
# via host_processor_info() — the same source Activity Monitor reads, and
# reachable without privileges.

CPU_STATE_MAX = 4        # [user, system, idle, nice] per core
CPU_STATE_IDLE = 2
PROCESSOR_CPU_LOAD_INFO = 2
TICK_MASK = 0xFFFFFFFF   # the counters are 32-bit and do eventually wrap

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
libc.mach_host_self.restype = ctypes.c_uint
libc.mach_task_self.restype = ctypes.c_uint


def cpu_ticks():
    """Cumulative [user, system, idle, nice] ticks per core, or None."""
    count = ctypes.c_uint(0)
    info = ctypes.POINTER(ctypes.c_uint)()
    size = ctypes.c_uint(0)

    if libc.host_processor_info(libc.mach_host_self(), PROCESSOR_CPU_LOAD_INFO,
                                ctypes.byref(count), ctypes.byref(info),
                                ctypes.byref(size)) != 0:
        return None

    try:
        return [[info[i * CPU_STATE_MAX + s] for s in range(CPU_STATE_MAX)]
                for i in range(count.value)]
    finally:
        # The kernel hands back a fresh VM allocation on every call and it
        # belongs to the caller. Skipping this leaks about 16 KB per sample —
        # measured at 320 MB across 20k samples, which at the rate below would
        # cost this daemon well over a gigabyte a day.
        libc.vm_deallocate(libc.mach_task_self(),
                           ctypes.cast(info, ctypes.c_void_p),
                           ctypes.c_size_t(size.value *
                                           ctypes.sizeof(ctypes.c_uint)))


def topology():
    """How many efficiency and performance cores this Mac has.

    Apple silicon reports the fastest core group as perflevel0. The tick array
    orders efficiency cores first — verified by pinning load and watching which
    indices saturate — so the panel can split the bars on the E count alone.
    Intel Macs have no perflevels and come back as zeroes, which the panel
    renders as one undifferentiated group.
    """
    out = run("sysctl", "-n", "hw.perflevel0.logicalcpu",
              "hw.perflevel1.logicalcpu")
    try:
        pcores, ecores = (int(v) for v in out.split()[:2])
    except (AttributeError, ValueError):
        return {"ecores": 0, "pcores": 0}
    return {"ecores": ecores, "pcores": pcores}


_cpu = {"cores": [], "avg": -1, "at": 0.0}
_topo = None


def sample_cpu():
    """Convert the tick counters into busy percentages, forever.

    Sampled on a fixed period in the background rather than per request, so the
    window each percentage covers is the same no matter how the panel polls —
    and so a request costs a dict lookup instead of a syscall and a wait.
    """
    prev = cpu_ticks()
    while True:
        time.sleep(SAMPLE_INTERVAL)
        cur = cpu_ticks()
        if prev is None or cur is None or len(prev) != len(cur):
            prev = cur
            continue

        busy = []
        for before, after in zip(prev, cur):
            delta = [(b - a) & TICK_MASK for a, b in zip(before, after)]
            total = sum(delta)
            busy.append(round(100 * (total - delta[CPU_STATE_IDLE]) / total)
                        if total else 0)

        _cpu["cores"] = busy
        _cpu["avg"] = round(sum(busy) / len(busy)) if busy else -1
        _cpu["at"] = time.time()
        prev = cur


def read_cpu():
    """The latest per-core snapshot, with how old it is."""
    global _topo
    if _topo is None:
        _topo = topology()

    out = {"cores": _cpu["cores"], "avg": _cpu["avg"]}
    out.update(_topo)
    # Lets the panel tell a quiet machine from a sampler thread that has died.
    out["age"] = round(time.time() - _cpu["at"]) if _cpu["at"] else -1
    return out


# --- notifications -----------------------------------------------------------
#
# A one-slot mailbox. Anything on this machine can drop a line in it and the
# panel raises it as a banner on its next poll:
#
#     curl -sf -X POST localhost:8789/notify \
#          -d '{"msg": "Claude needs your input", "kind": "alert"}'
#
# The panel is a polling client with no state of its own, so what it needs to
# tell "a new message" from "the same message again" is the id: it rises by one
# per post and never repeats. `kind` picks the banner colour and `ttl` how long
# it stays up, 0 meaning until a button dismisses it.


NOTIFY_KINDS = ("info", "warn", "alert")
NOTIFY_MAX = 96          # what fits in the banner's three lines, with room spare
NOTIFY_DEFAULT_TTL = 20  # seconds; alerts default to staying until dismissed

_notify = {"id": 0, "msg": "", "kind": "info", "ttl": 0, "at": 0.0}


def notify_text(raw):
    """Squeeze a posted message into what the panel's fonts can actually draw.

    make_vlw.py bakes printable ASCII and the degree sign, nothing else, so a
    Thai or emoji message would arrive as a row of blank boxes. Substituting
    here rather than on the device keeps that rule in one place — and makes it
    visible to whoever posted, since they can read back what was stored.
    """
    flat = " ".join(str(raw).split())          # newlines and runs of space out
    clean = "".join(c if 0x20 <= ord(c) < 0x7F else "?" for c in flat)
    return clean[:NOTIFY_MAX]


def post_notify(fields):
    """Store a message and return what the panel will see."""
    msg = notify_text(fields.get("msg", ""))
    kind = str(fields.get("kind", "info")).lower()
    if kind not in NOTIFY_KINDS:
        kind = "info"

    try:
        ttl = int(fields.get("ttl", 0 if kind == "alert" else NOTIFY_DEFAULT_TTL))
    except (TypeError, ValueError):
        ttl = NOTIFY_DEFAULT_TTL

    # An empty message is how a poster retracts one: the id still moves, so the
    # panel notices the change and clears the banner it is showing.
    _notify.update(id=_notify["id"] + 1, msg=msg, kind=kind,
                   ttl=max(0, ttl), at=time.time())
    return read_notify()


def read_notify():
    """The current message, with how long ago it was posted."""
    return {
        "id": _notify["id"],
        "msg": _notify["msg"],
        "kind": _notify["kind"],
        "ttl": _notify["ttl"],
        "age": round(time.time() - _notify["at"]) if _notify["at"] else -1,
    }


def parse_body(body, ctype):
    """Posted fields, from JSON, a form, or a bare line of text.

    Three shapes because three kinds of caller: a hook writing JSON, a shell
    one-liner writing `-d msg=...`, and `echo something | curl --data-binary @-`.
    """
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return {}

    if "json" in ctype or text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass

    if "=" in text.split("&")[0] and " " not in text.split("=")[0]:
        return {k: v[0] for k, v in parse_qs(text).items()}

    return {"msg": text}


# --- assembly ----------------------------------------------------------------
#
# Sampled in the background rather than per request, for the reason sample_cpu()
# is: a request should cost a dict lookup, not a handful of process spawns and a
# wait.
#
# It used to be a two-second cache in front of the readings, which sounds like
# the same thing and is not — the panel polls every ten seconds, so every single
# poll missed the cache and paid the full price. Idle that was 95 ms, which is
# invisible. Measured again under a load average of 3 it was 1.2 to 2.0 seconds,
# because the cost here is process spawns and those are what a busy machine is
# slowest at. The panel's fetch blocks its main loop, so that landed as a
# two-second freeze of the clock and the buttons, arriving reliably whenever the
# Mac got busy — which is exactly when the panel switches to the Mac page to
# show you that it has.
#
# Split in two because the cost is per spawn and the readings do not move at the
# same speed: the load average drives the panel's automatic page and is worth
# five seconds, while a battery percentage or a disk figure is the same number
# half a minute later.

# display() is on the fast pass despite moving less often than the load average,
# because it is the one reading here that costs no process spawn — the whole
# argument for the two-speed split does not apply to it. Five seconds is then
# just the slowest it can be behind a lid closing.
FAST_READINGS = (cpu, memory, display)
SLOW_READINGS = (battery, disk, uptime)

UNKNOWN = {"bat_pct": -1, "bat_state": "", "bat_mins": -1, "load1": -1,
           "ncpu": -1, "mem_used": -1, "disk_free_gb": -1, "disk_used": -1,
           "uptime_s": -1, "display": ""}

_stats = {"data": None, "at": 0.0}


def sample_stats(ready=None):
    """Refresh the vitals on a fixed period, forever.

    `ready` is set once the first pass has landed, so startup can hold the
    socket back until there is something real to serve.
    """
    last_slow = 0.0
    while True:
        now = time.time()
        due = list(FAST_READINGS)
        if now - last_slow >= STATS_SLOW:
            due += SLOW_READINGS
            last_slow = now

        fresh = {}
        for reading in due:
            fresh.update(reading())

        # Merged onto the last snapshot, so a fast pass keeps the slow group's
        # values rather than blanking them for the twenty-five seconds until
        # they are read again.
        _stats["data"] = {**(_stats["data"] or UNKNOWN), **fresh}
        _stats["at"] = now
        if ready is not None:
            ready.set()
        time.sleep(STATS_FAST)


def read_stats():
    """The latest snapshot, with how old it is.

    `age` matters more than it looks: the readings now come from a thread, and a
    thread can die in a way a request path cannot. Without it a panel polling a
    dead sampler would show a battery percentage from last Tuesday and have no
    way to know. The panel adds its own elapsed time to it, the same way it
    already does for the usage window.
    """
    now = time.time()
    if _stats["data"] is None:
        return {**UNKNOWN, "age": -1, "now": int(now)}

    return {**_stats["data"], "age": round(now - _stats["at"]), "now": int(now)}


class Handler(BaseHTTPRequestHandler):
    def loopback(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def authorised(self):
        if self.loopback():
            return True
        return bool(TOKEN) and hmac.compare_digest(
            self.headers.get(TOKEN_HEADER, ""), TOKEN)

    def do_GET(self):
        if not self.authorised():
            self.send_error(403, "token required from off this machine")
            return

        route = self.path.split("?")[0]
        if route in ("/mac", "/"):
            payload = read_stats()
        elif route == "/cpu":
            payload = read_cpu()
        elif route == "/notify":
            payload = read_notify()
        else:
            self.send_error(404)
            return

        self.send_json(payload)

    def do_POST(self):
        if self.path.split("?")[0] != "/notify":
            self.send_error(404)
            return

        # Reading now needs the token from off-machine, but posting stays
        # loopback-only regardless: this one puts words on a screen on someone's
        # desk, and the posters it exists for (shell hooks, launchd jobs, the
        # panel's own bridge) all reach it over loopback anyway. No reason to
        # let a shared secret that also travels over WiFi be enough for it.
        if not self.loopback():
            self.send_error(403, "notify is loopback only")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length > 8192:
            self.send_error(413)
            return

        fields = parse_body(self.rfile.read(length) if length else b"",
                            self.headers.get("Content-Type", ""))
        self.send_json(post_notify(fields))

    def send_json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # Silent for the same reason usage_server.py is: the panel polls on a timer,
    # and one access-log line per poll buries anything worth reading.
    def log_message(self, fmt, *args):
        pass


def main():
    global TOKEN
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8789

    TOKEN = load_token()
    if TOKEN:
        # The value itself is deliberately not printed: stdout here is a log
        # file in /tmp. Read it from the path when it is needed for secrets.h.
        print(f"LAN requests need {TOKEN_HEADER}, from {TOKEN_PATH}", flush=True)

    # Both started before the socket so the first request has a reading behind
    # it rather than an empty one. /cpu cannot be waited for — it needs two
    # tick samples to have a window to difference, so an early request there
    # gets an empty list and the panel says so.
    #
    # /mac can be, and is: one pass is about 150 ms idle, but these are process
    # spawns and a machine busy enough to be worth looking at took longer than
    # three seconds to finish the first one. Serving before then answers a row
    # of -1, which the panel faithfully renders as "no reading" — a restart of
    # this agent should not put that on the screen for ten seconds.
    ready = threading.Event()
    threading.Thread(target=sample_cpu, daemon=True).start()
    threading.Thread(target=sample_stats, args=(ready,), daemon=True).start()
    if not ready.wait(10.0):
        print("first sample did not land in 10s; serving anyway", flush=True)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"serving mac vitals on http://0.0.0.0:{port}/mac, /cpu and /notify",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
