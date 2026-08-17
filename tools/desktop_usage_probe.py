#!/usr/bin/env python3
"""Mirror the Claude desktop app's own usage readings into a cache the panel
can fall back to when no Claude Code session is live to keep the CLI one warm.

usage_server.py's primary source, ~/.claude/statusline-usage.cache, is only
written when Claude Code renders a status line -- so it goes stale the moment
you close the terminal, even while you keep chatting in the desktop app. That
app polls the same plan usage on its own, in the background, and keeps a
rolling history of it at:

    ~/Library/Application Support/Claude/plan-usage-history.json

Each sample there carries a used-percentage for the 5h and 7d windows but no
reset time -- the API never gave the desktop app one to record, unlike the
`rate_limits` object Claude Code's statusline reads. So this writes only two
of the four fields usage_server.py expects; the reset columns are left at 0,
which its existing "no known reset" handling already renders as a bar with no
countdown, the same as before a session's first response.

Samples carry the org they were taken against, and an account can report
more than one: this machine's history has 3437 samples for the org actually
in use and exactly one stray, reading 0%/0%, for another. Taking simply the
newest sample would therefore serve a flat zero for as long as a stray
happened to be last -- the panel showing an empty quota against a window
that is nowhere near empty. So the org holding the most samples is treated
as the real one and the newest sample *of that org* is taken.

Meant to run periodically as a launchd StartInterval, not as a daemon -- see
com.korakod.claude-desktop-usage-probe.plist alongside this file.

Usage:
    desktop_usage_probe.py
"""

from __future__ import annotations

import collections
import json
import os
import sys

SOURCE = os.path.expanduser(
    "~/Library/Application Support/Claude/plan-usage-history.json")
CACHE = os.path.expanduser("~/.claude/statusline-usage-desktop.cache")


def latest_sample(path: str) -> dict | None:
    with open(path) as f:
        data = json.load(f)
    samples = data.get("samples") or []
    if not samples:
        return None

    counts = collections.Counter(s.get("org") for s in samples)
    main_org, _ = counts.most_common(1)[0]
    return max((s for s in samples if s.get("org") == main_org),
               key=lambda s: s.get("t", 0))


def main() -> int:
    try:
        sample = latest_sample(SOURCE)
    except (OSError, ValueError) as exc:
        print(f"cannot read {SOURCE}: {exc}", file=sys.stderr)
        return 1

    if sample is None:
        print(f"{SOURCE} has no samples yet", file=sys.stderr)
        return 0

    usage = sample.get("u", {})
    h5, d7 = usage.get("fh"), usage.get("sd")
    if h5 is None or d7 is None:
        print(f"latest sample is missing fh/sd: {sample}", file=sys.stderr)
        return 1

    try:
        sample_time = sample["t"] / 1000
    except (KeyError, TypeError):
        print(f"latest sample has no timestamp: {sample}", file=sys.stderr)
        return 1

    with open(CACHE, "w") as f:
        f.write(f"{h5} 0 {d7} 0\n")
    # Backdated to when the desktop app actually took the reading, not when
    # this script happened to run, so usage_server.py's `age` reports how
    # stale the number really is -- the same guarantee the CLI cache gives.
    os.utime(CACHE, (sample_time, sample_time))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
