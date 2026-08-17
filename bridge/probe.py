#!/usr/bin/env python3
"""Phase 0 probe: call the DTU-Pro-S with a handful of hoymiles-wifi commands,
one at a time, and print/log exactly what comes back.

Nothing here is used to design the JSON schema yet -- this only exists to
capture real output so that step can be based on fact instead of guesswork.

Usage:
    .venv/bin/python bridge/probe.py --host 192.168.1.50

Requires the `hoymiles-wifi` CLI on PATH (pip install hoymiles-wifi).
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
import time

# NOTE: CLAUDE.md's original list included "get-real-data-hms", but the
# current suaveolent/hoymiles-wifi CLI (checked against its source on GitHub,
# 2026-08-14) has no such command -- "hms" only shows up inside the
# unrelated "time_ymd_hms" timestamp field. Dropped rather than guessed at.
COMMANDS = [
    "get-real-data-new",
    "get-real-data",
    "app-information-data",
    "get-version-info",
]

SECONDS_BETWEEN_CALLS = 40


def run_command(host: str, command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["hoymiles-wifi", "--host", host, command, "--as-json"],
        capture_output=True,
        text=True,
        timeout=30,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="DTU-Pro-S IP address")
    parser.add_argument(
        "--log",
        default="bridge/probe_output.log",
        help="where to append raw output (default: bridge/probe_output.log)",
    )
    args = parser.parse_args()

    with open(args.log, "a") as log_file:
        for i, command in enumerate(COMMANDS):
            timestamp = datetime.datetime.now().isoformat(timespec="seconds")
            header = f"\n=== {timestamp}  {command} ==="
            print(header)
            log_file.write(header + "\n")

            try:
                result = run_command(args.host, command)
            except subprocess.TimeoutExpired:
                msg = "TIMEOUT after 30s"
                print(msg)
                log_file.write(msg + "\n")
            except FileNotFoundError:
                print(
                    "ERROR: 'hoymiles-wifi' not found on PATH."
                    " Run: pip install hoymiles-wifi",
                    file=sys.stderr,
                )
                return 1
            else:
                print(f"exit code: {result.returncode}")
                if result.stdout:
                    print(result.stdout)
                    log_file.write(result.stdout + "\n")
                if result.stderr:
                    print("stderr:", result.stderr, file=sys.stderr)
                    log_file.write("stderr: " + result.stderr + "\n")

            log_file.flush()

            is_last = i == len(COMMANDS) - 1
            if not is_last:
                print(f"(waiting {SECONDS_BETWEEN_CALLS}s before next command...)")
                time.sleep(SECONDS_BETWEEN_CALLS)

    print(f"\nDone. Full output also saved to {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
