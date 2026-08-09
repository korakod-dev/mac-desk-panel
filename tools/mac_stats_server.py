#!/usr/bin/env python3
"""Serve this Mac's vitals to the T-Display-S3 as JSON.

The panel reaches it the same way it reaches usage_server.py — over the USB
bridge when one is running, over the LAN otherwise:

    GET /mac  ->  {"bat_pct": 65, "bat_state": "discharging", "bat_mins": 481,
                   "load1": 2.85, "ncpu": 11, "mem_used": 30,
                   "disk_free_gb": 179, "disk_used": 60, "screen": "on",
                   "uptime_s": 219043, "now": 1786243278}

    GET /cpu  ->  {"cores": [12, 3, 45, ...], "avg": 27,
                   "ecores": 6, "pcores": 5, "age": 0}

`cores` is busy percentage per logical core over the last sampling window,
efficiency cores first.

It also carries the panel's notification banner, which is the one thing here
that travels towards the screen rather than away from the Mac:

    POST /notify  {"msg": "build failed", "kind": "warn", "ttl": 20}
    GET  /notify  ->  {"id": 7, "msg": "build failed", "kind": "warn",
                       "ttl": 20, "age": 3}

Posting is loopback-only; see do_POST.

Anything that could not be read comes back as -1 (or "" for the battery state)
rather than being omitted, so the firmware renders "--" instead of a stale or
invented number.

Everything here shells out to stock macOS tools — no third-party dependency, so
it runs on the system python and keeps working if the project venv is rebuilt.
The readings are cached for a couple of seconds because the panel's fetch is
blocking: a burst of polls should not turn into a burst of subprocesses.

Usage:
    mac_stats_server.py [port]        default 8789, binds all interfaces
"""

import ctypes
import ctypes.util
import json
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

CACHE_TTL = 2.0          # seconds a reading stays good for
RUN_TIMEOUT = 3.0        # a stat tool that hangs must not hang the panel
SAMPLE_INTERVAL = 1.0    # per-core sampling period, and so the panel's ceiling


def run(*args):
    """Stdout of a command, or None if it fails, times out, or isn't there."""
    try:
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=RUN_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


# --- individual readings -----------------------------------------------------


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


def screen():
    """Whether this Mac's own display is showing anything, and to whom.

        "on"      someone is presumably sitting in front of it
        "locked"  the screen is locked
        "off"     the display has gone to sleep

    The panel uses it to decide whether to stay lit, so an unreadable answer
    reports "on": a display that goes dark because a stat tool changed its
    output format is a worse failure than one that stays bright.

    Both readings are indirect because the direct ones are out of reach — the
    session dictionary lives behind a private framework, and pyobjc is not on
    the system python this runs on. What is reachable:

      CGSSessionScreenIsLocked appears in the console session's IORegistry
      properties only while the screen is locked, and

      the system capability set drops Graphics when the display sleeps.
    """
    users = run("ioreg", "-n", "Root", "-d1", "-a")
    # Matched with its value rather than by presence: the key is absent
    # entirely while unlocked here, but reporting a screen locked because some
    # macOS version writes it out as <false/> would blank the panel all day.
    if users and re.search(r"CGSSessionScreenIsLocked</key>\s*<true/>", users):
        return {"screen": "locked"}

    state = run("pmset", "-g", "systemstate")
    if state and "System Capabilities" in state:
        caps = state.split("\n")[0]
        return {"screen": "on" if "Graphics" in caps else "off"}

    return {"screen": "on"}


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


_cache = {"at": 0.0, "data": None}


def read_stats():
    """Every reading as one dict, recomputed at most every CACHE_TTL seconds."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]

    stats = {}
    for reading in (battery, cpu, memory, disk, screen, uptime):
        stats.update(reading())
    stats["now"] = int(now)

    _cache["data"] = stats
    _cache["at"] = now
    return stats


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
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

        # Everything else here is read-only and harmless to serve to the subnet.
        # This one puts words on a screen on someone's desk, so it is kept to
        # this machine — the posters it exists for (shell hooks, launchd jobs,
        # the panel's own bridge) all reach it over loopback anyway.
        if self.client_address[0] not in ("127.0.0.1", "::1"):
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8789

    # Started before the socket so the first /cpu request already has a window
    # behind it rather than an empty list.
    threading.Thread(target=sample_cpu, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"serving mac vitals on http://0.0.0.0:{port}/mac, /cpu and /notify",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
