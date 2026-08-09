"""Ask a running usb_net_bridge.py to let go of the serial port before an upload.

The bridge holds /dev/cu.usbmodem* open for as long as it runs, which is exactly
what esptool needs. Rather than making that a manual step, this nudges the
bridge to release the port; it reconnects on its own once the pause expires. No
bridge running means nothing to do, so a refused connection is not an error.
"""

import socket
import time

Import("env")  # noqa: F821 — injected by PlatformIO

BRIDGE_PORT = 8788
PAUSE_SECONDS = 60
# Closing the port drops DTR/RTS, which bounces the S3 through a reset; esptool
# opening it during that window sees a chip that is not listening yet and gives
# up with "No serial data received".
SETTLE_SECONDS = 2.0


def pause_bridge(source, target, env):
    try:
        with socket.create_connection(("127.0.0.1", BRIDGE_PORT), timeout=0.5) as sock:
            sock.sendall(b"!pause %d\n" % PAUSE_SECONDS)
            # The bridge answers only once the port is actually closed.
            sock.settimeout(6.0)
            ack = sock.recv(32)
    except OSError:
        return  # no bridge running: nothing holds the port

    if ack.startswith(b"!paused"):
        print(f"usb bridge: released the port for {PAUSE_SECONDS}s")
        time.sleep(SETTLE_SECONDS)
    else:
        print("usb bridge: did not release the port — upload may fail")


env.AddPreAction("upload", pause_bridge)  # noqa: F821
