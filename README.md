# T-Display-S3 desk panel

A LilyGO T-Display-S3 on a desk, showing the handful of things worth a glance
while you work: the time and the weather, how much of the Claude Code usage
window is left, what the Mac beside it is doing, and a banner the Mac can raise
when something actually needs you.

It runs off the USB-C cable with no WiFi at all — the host does the fetching on
the panel's behalf over the same serial link that carries the console. WiFi is
there as a fallback, not a requirement.

| | |
|---|---|
| ![clock and weather](docs/pages/1-now.png) | ![Claude Code usage](docs/pages/2-usage.png) |
| **now** — time, date, temperature, conditions | **usage** — the 5h and 7d windows, and when they roll |
| ![Mac vitals and cores](docs/pages/3-mac.png) | ![the panel's own vitals](docs/pages/4-vitals.png) |
| **mac** — charge, screen, memory, disk, a column per core | **vitals** — not a page: hold `IO14` for the panel's own |

`IO14` moves to the next page, and held, brings up the panel's own vitals over
whatever is showing. `BOOT` refreshes the page in front of you, and held, dims
or brightens the panel.

The vitals are behind a hold rather than in the cycle because every figure on
them answers "is this thing working" — a question you ask on purpose after
noticing something wrong, never one you answer in a glance. As a page they cost
a press on every trip round to skip past three constants and two fields that
read `--` whenever the link is the cable.

## How the pieces fit

```
   ┌─────────────────┐                       ┌──────────────────────────────┐
   │  T-Display-S3   │                       │            macOS             │
   │                 │   USB-C, CDC serial   │                              │
   │  dashboard  ────┼───────────────────────┤  usb_net_bridge.py           │
   │  net_link       │  @REQ / @RES framing  │    ├── PING  liveness        │
   │                 │                       │    ├── TIME  stands in for   │
   │                 │                       │    │         NTP            │
   │                 │                       │    └── GET   fetches on the  │
   └─────────────────┘                       │              panel's behalf  │
            │                                │              │               │
            │ WiFi, only if the bridge       │              ├─→ the internet│
            │ is not answering               │              │   (Open-Meteo)│
            └────────────────────────────────┤              │               │
                                             │              ├─→ :8787 usage │
                                             │              └─→ :8789 mac   │
                                             └──────────────────────────────┘
```

The panel does not know or care which link it got. `net_link.h` picks whichever
one is answering, preferring the cable; the status bar says which.

**Why a request bridge and not IP over USB.** The Arduino core's TinyUSB is built
without the NCM/ECM classes and its lwIP without PPP, so the panel has no way to
put packets on the cable. Moving *requests* instead needs nothing the stock core
does not already have.

## Hardware

LilyGO T-Display-S3 — ESP32-S3 with 16 MB flash and 8 MB PSRAM, driving a
320×170 ST7789 over an 8-bit parallel (i8080) bus. No wiring: every pin is fixed
on the board and declared in `platformio.ini`.

Two things about it that cost an evening each if you don't know them:

- **GPIO15 gates the LCD rail.** It must be driven high before `tft.init()` or
  the panel never lights up, whatever else is right.
- **The display setup lives in `build_flags`,** not in TFT_eSPI's `User_Setup.h`.
  `-DUSER_SETUP_LOADED` tells the library to use ours. Editing `User_Setup.h`
  under `.pio/libdeps/` does nothing and is erased on the next dependency fetch.

## Getting it running

```bash
python3 -m venv .venv && .venv/bin/pip install platformio pyserial pillow freetype-py
cp include/secrets.h.example include/secrets.h    # then edit it
.venv/bin/pio run -e dashboard -t upload
```

`secrets.h` holds the WiFi credentials, where the two host servers live, and the
location and timezone for the clock and weather. It is gitignored; the example
beside it is not. Leaving the WiFi placeholders as they are is a supported
configuration — the panel then runs on the cable alone.

There is a second environment for bringing up a new board:

```bash
.venv/bin/pio run -e selftest -t upload      # backlight, colour, bus, buttons
```

## The host side

Three Python programs, all installed as launch agents. The plists sit beside
each script and carry their own install instructions in a comment at the top.

| | port | what it does |
|---|---|---|
| `tools/usb_net_bridge.py` | 8788 | Answers the panel's requests over the cable. Needs pyserial; **replaces** `pio device monitor`, since it holds the port. |
| `tools/usage_server.py` | 8787 | Serves the Claude Code 5h/7d limits. Stdlib only. |
| `tools/mac_stats_server.py` | 8789 | Serves battery, load, memory, disk, which screen is being driven, per-core CPU, and the notification mailbox. Stdlib only. |

The two servers run on the **system** python on purpose — stdlib only, so they
keep working when the project venv is rebuilt or deleted.

### Who they answer

Both bind all interfaces, because the panel's WiFi fallback needs to reach them
from the LAN. What that used to mean is that anything else on the network could
read them too — including `/notify`, whose text names project directories and
whatever Claude stopped to ask about.

So requests from **this machine** are served as they always were, which covers
every request over the USB bridge: the bridge does the fetching, so it arrives
from loopback. Requests from **the LAN** must carry `X-Panel-Token`. Posting a
notification stays loopback-only regardless.

The token is minted on first run and kept at `~/.config/t-display-s3/token`,
mode 0600, outside the repo. Copy it into `secrets.h` as `HOST_TOKEN` to let the
WiFi fallback work; leave the placeholder and the panel simply runs on the cable
alone. Clock and weather are unaffected either way — neither touches your
machine.

It is a shared secret over plain HTTP, so it is no defence against someone
reading the traffic, and is not meant to be. It stops the network being able to
just read the port.

They each run a *copy* of their script, from `~/Library/Application Support/`,
because `~/Desktop` is TCC-protected and a launch agent pointed straight at it
dies on startup with "Operation not permitted". **After editing a server, copy
it across and kick the agent** — the plist comments give the exact commands.

### The two scripts that live in ~/.claude

These are installed outside the repo but the panel depends on them, so copies
are kept here:

- **`tools/statusline.sh`** — the Claude Code status line. It writes
  `~/.claude/statusline-usage.cache`, which is the only place the usage numbers
  survive outside a running session, and therefore the only thing
  `usage_server.py` has to read. No cache, no usage page.
- **`tools/panel-notify.sh`** — wired to four Claude Code hooks; raises and
  retracts the banner. This is what makes the banner worth having.

Copy them to `~/.claude/` and point `settings.json` at them.

## Notifications

`mac_stats_server.py` holds one message. Anything on the machine can post one
and the panel raises it over whatever page is showing, flashing the backlight,
until a button acknowledges it:

```bash
curl -sf -X POST localhost:8789/notify -d 'msg=build failed' -d kind=warn -d ttl=30
```

`kind` is `info`, `warn` or `alert` and picks the colour. `ttl` is seconds, `0`
meaning until dismissed — the default for `alert`, which is the difference
between an alert and a note. Posting an empty message retracts. Posting is
loopback-only; everything else the server does is read-only and served to the
subnet.

Messages are squeezed to printable ASCII, because that is all the fonts carry.

## Fonts

The three smooth fonts are generated from a system TTF into TFT_eSPI's VLW
format and checked in as C headers:

```bash
.venv/bin/python tools/make_vlw.py \
    /System/Library/Fonts/Supplemental/SukhumvitSet.ttc \
    16 src/fonts/ui16.h UiFont16 --face 2 --set ascii
.venv/bin/python tools/preview_vlw.py src/fonts/ui16.vlw /tmp/ui16.png "20:45  28°"
```

`make_vlw.py` writes the raw `.vlw` beside the header so `preview_vlw.py` can
check what was actually emitted rather than re-deriving it from the TTF. Only
the headers are checked in — the `.vlw` files are build byproducts and
gitignored, so regenerate before previewing a fresh clone.

The glyph set is printable ASCII plus the degree sign. That is a real constraint
and it reaches further than it looks: the middle dots on the usage and Mac pages
are *drawn* rather than typed, and `mac_stats_server.py` substitutes non-ASCII
out of notification text before storing it.

Thai works, but only with a font whose combining marks already carry
`xAdvance == 0` and a negative `dX` — TFT_eSPI does no OpenType shaping.
Sukhumvit Set does; Thonburi does not. See the comment in `make_vlw.py`.

## Talking to a running panel

The firmware takes single-byte commands on the console, and
`tools/grab_screen.py` uses them to pull the actual framebuffer — not a mock-up
of it — as a PNG:

```bash
.venv/bin/python tools/grab_screen.py shot 3      # capture all three pages
```

It captures from wherever the panel is rather than from the first page, so which
page lands in `shot0.png` is whichever one it was resting on — the usage page
while the Mac is answering, the clock once it stops.

| | |
|---|---|
| `N` | next page |
| `S` | dump the framebuffer as raw RGB565 |
| `A` | toggle automatic page selection |
| `V` | toggle the vitals overlay |

`V` exists because the overlay's own way in is a finger on a button, which is no
use to a host trying to photograph it.

While the bridge is running it owns the serial port, so `grab_screen.py` talks
to the bridge's passthrough socket instead. Uploads pause the bridge
automatically — `pio_bridge_pause.py` is registered as a pre-upload hook in
`platformio.ini`, so `pio run -t upload` just works with the bridge up.

## Tests

```bash
./tests/run.sh
```

No board, no PlatformIO — just a C++17 compiler. It covers `layout.h`, which is
where the two pieces of arithmetic worth being wrong about live: the banner's
word wrap and the core columns' sizing.

Two things make it worth having rather than decorative.

It tests the **firmware's own code**. `layout.h` is templated on the string type
and on the function that measures text, which are the only two things that
cannot leave the device, so the test compiles the same header the panel does
rather than a copy that quietly drifts.

And it measures text with **TFT_eSPI's real algorithm over the real fonts** —
`textWidth` transcribed from the library, reading metrics out of the generated
`.vlw` files. That is not pedantry: the width of a string is not the sum of its
characters' widths. The first glyph's negative left bearing is added back and
the last contributes its ink extent rather than its advance, so a wrap checked
against a simpler model passes on the host and overflows on the panel.

What it asserts, over a corpus and 200,000 random inputs at the banner's real
280px: no line exceeds its box, no line is empty or carries a stray space, the
line count is respected, and the characters that come out are the characters
that went in — entire when the text fitted, a prefix when it was cut, with the
loss marked. Then that every core count from 1 to `MAX_CORES` lands on the panel
in full, and that eleven cores still come out at the 22px this was built around.

The suite has been checked against deliberate breakage — lines allowed to run
over, bars pinned back to a fixed width, the truncation marker dropped, a
character lost per word, a stray separator, an off-by-one on the line limit —
and each is caught by a different assertion.

The `.vlw` files are gitignored build byproducts; regenerate them with
`make_vlw.py` before running the tests on a fresh clone. The test says so if
they are missing.

## What the panel decides for itself

It picks its own page: to the Mac page, where the core columns are, when that
machine's load passes three quarters of its core count, and to the usage page
when a window goes over 85%. Any button
hands control back for two minutes; `A` turns it off entirely. The lit dot in
the status bar says which mode it is in — cyan for automatic, warm while you
have it, white when automatic is off.

It also picks where to rest, off the same fact the brightness carries: the usage
page while the Mac is answering, the clock once it stops. The two states are read
by a person doing different things. A sleeping Mac means nobody is working, and
the clock is the only thing on this panel still moving; a Mac that has started
answering means somebody sat down, and what is left of the 5h window is the first
thing that changes what they do next. So the wake is the panel's cue to have the
usage figures already up, before anyone asks for them.

That is a floor, not an interruption: the crossing moves where automatic falls
back to, rather than borrowing the screen and handing it back. Both alert
conditions above are ignored while the Mac is asleep — they are made of numbers
that stopped arriving, so a full window found then is a reading from before it
went, and leaving the clock for it would be old news presented as if it had just
happened, at exactly the hours the clock is what the panel is for. A crossing
that lands during a banner or a manual hold waits for it, and if the Mac woke and
went back to sleep in between, only where it ended up is arrived on.

It sets its own brightness, and that level says the same thing on the one channel
you take in without reading anything: 75% of full while readings are still
arriving, 25% once they stop. Across a room, at an angle, out of the corner of an
eye — it carries the fact before you have decided to look, and the page you find
when you do look agrees with it.

It never blanks at the low end. The hours the Mac is away are exactly the hours
nothing else on the desk is showing the time.

This used to follow sunset instead, off the sunrise and sunset in the forecast,
which was the wrong axis twice over: it dimmed the panel at eight in the evening
while you were still sitting there working, and left it bright all night
whenever the sky was the only thing that had changed. Keyed on the Mac it still
dims when you go to bed — going to bed is what puts the Mac to sleep — without
the panel needing to know the hour. The one case it does not cover is a machine
left awake overnight on a long build; hold `BOOT` to dim it by hand, and the
override lifts by itself the next time the automatic level moves.

And it restarts itself if it wedges. The ESP32's task watchdog watches the loop
task at a 30-second timeout — generous on purpose, because the longest thing
loop() legitimately does is a weather fetch over WiFi, which blocks inside
HTTPClient where there is no callback to feed from. A watchdog that trips on a
slow morning is one that gets switched off. Requests over the USB link are fed
throughout by the same idle hook that keeps the clock moving.

A restart it did not choose raises a banner, the same one the Mac uses to
interrupt you, because the alternative is learning nothing: a watchdog that
quietly recovers the panel at four in the morning leaves no other trace but an
uptime that went back to zero while nobody was looking. The reason stays in the
vitals overlay afterwards for as long as the panel is up. `panic = true`, so a
trip also leaves a decoded backtrace on the serial console on its way out.

## Telling whether the Mac is awake

The short answer is the **brightness**: full-ish means the Mac is answering,
dim means it stopped. That is a deliberate choice of channel — it is the only
thing here readable without looking directly at the panel. The rest of this
section is what the screen adds once you do look.

Two readings answer it in detail, and neither does it alone.

The **link name** in the status bar is the first. `usb_net_bridge.py` is a
process on the Mac, so a Mac that is asleep stops answering its pings; twelve
seconds of silence and the word flips to `WiFi`. Read the *word*, not the
colour — `WiFi` is green too, because `online()` only asks whether there is a
link at all. `USB` therefore means something on that machine was scheduled
within the last twelve seconds, which is as close to "awake" as the panel can
get from this side of the cable. The reverse does not hold: `WiFi` is equally
what an unplugged cable or a stopped bridge looks like.

The **screen word** on the Mac page is the second — `external`, `built-in`,
`screen off`. It comes from CoreGraphics rather than a shelled-out tool, which
is what makes it cheap enough to sample every five seconds; the reading it
replaces cost 25 ms of a 30 ms pass and was removed for it, where this one costs
88 microseconds. Note the asymmetry it covers: a Mac that is itself asleep
cannot answer at all, so `screen off` always means the machine is up and only
the display has gone dark. That state is invisible to the link, which still
reads `USB` throughout.

That word is the one thing on the page that is never allowed to go stale. Past
thirty seconds without a fresh reading it is replaced by `not answering` in
warm, because a charge figure a minute old is still roughly true while
`external` a minute after the Mac slept is simply false — and false in the
direction that matters, since it claims the machine is up. Thirty is chosen
against a known ceiling: the host resamples every five seconds and the panel
asks every ten, so a current reading cannot be older than fifteen.

Nothing new has to be probed for this. The panel already pings the bridge every
four seconds and gives up after twelve, which is a liveness check running on its
own processor, off USB power, against a process that has to be scheduled on the
Mac to answer at all. Do not be tempted to add a network probe instead: macOS
runs a Bonjour sleep proxy whose entire job is answering the network on behalf
of a sleeping Mac, so pinging over WiFi would report awake when it is not.

Together: `USB` plus a screen word is a Mac that is awake, and the word says
whether anyone is likely looking at it. `not answering` means the panel asked
and got nothing — asleep, unplugged, or a stopped server, and it deliberately
does not guess which. If the clock and weather keep updating while only the
Mac-sourced pages go quiet, it is the Mac rather than the network, because the
forecast does not travel through it.

## Notes for later

Nothing outstanding. Things worth knowing about the shape of it:

- The LAN token is a shared secret over plain HTTP. It keeps the network from
  reading the ports; it would not survive someone watching the traffic. TLS
  would mean a certificate to mint and renew for a device with no clock at
  boot, which is a worse trade for what is on the wire.
- `net_link`'s idle hook only covers the USB path. A fetch over WiFi blocks
  inside HTTPClient, which offers no callback, so the clock does stop for the
  length of a slow weather fetch on the radio. The watchdog timeout is sized
  around exactly that.
