#!/usr/bin/env python3
"""Pull the dashboard's framebuffer over USB and save it as a PNG.

The sketch answers 'S' with a header line and then width*height raw RGB565
pixels, so this is what the panel is actually showing — not a mock-up of it.
'N' advances to the next page, which lets one run capture every page.

When tools/usb_net_bridge.py is running it holds the serial port, so this talks
to its passthrough socket instead; either way the bytes are the same.

Usage:
    grab_screen.py <out-prefix> [page-count]
"""

import sys
import time
import glob
import socket
import serial
from PIL import Image

BRIDGE_PORT = 8788


def rgb565_to_rgb(buf, w, h):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for i in range(w * h):
        # TFT_eSPI keeps sprite pixels in the panel's own byte order, big-endian
        v = (buf[i * 2] << 8) | buf[i * 2 + 1]
        px[i % w, i // w] = (
            ((v >> 11) & 0x1F) * 255 // 31,
            ((v >> 5) & 0x3F) * 255 // 63,
            (v & 0x1F) * 255 // 31,
        )
    return img


class BridgeLink:
    """The bridge's passthrough socket, shaped like the pyserial API grab() uses.

    Unbuffered on purpose: reset_input_buffer() has to be able to throw away
    everything queued, and a file wrapper would keep its own copy out of reach.
    """

    def __init__(self, sock):
        self.sock = sock
        self.sock.settimeout(2)

    def reset_input_buffer(self):
        self.sock.setblocking(False)
        try:
            while self.sock.recv(65536):
                pass
        except OSError:
            pass
        self.sock.settimeout(2)

    def write(self, data):
        self.sock.sendall(data)

    def flush(self):
        pass

    def read(self, n):
        try:
            return self.sock.recv(n)
        except (socket.timeout, TimeoutError):
            return b""

    def readline(self):
        # Only used for the short header lines, so byte-at-a-time is fine.
        out = bytearray()
        while not out.endswith(b"\n"):
            b = self.read(1)
            if not b:
                break
            out += b
        return bytes(out)

    def close(self):
        self.sock.close()


def open_link():
    """Prefer the bridge — if it is up, the raw port is not available anyway."""
    try:
        return BridgeLink(socket.create_connection(("127.0.0.1", BRIDGE_PORT),
                                                   timeout=0.5))
    except OSError:
        pass

    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if not ports:
        raise RuntimeError("no /dev/cu.usbmodem* and no bridge on "
                           f"127.0.0.1:{BRIDGE_PORT}")
    return serial.Serial(ports[0], 115200, timeout=2)


def grab(ser):
    ser.reset_input_buffer()
    ser.write(b"S")
    ser.flush()

    # Skip anything already queued until the header shows up.
    deadline = time.time() + 5
    while time.time() < deadline:
        line = ser.readline().decode("utf-8", "replace").strip()
        if line.startswith("SCREEN"):
            _, w, h = line.split()
            w, h = int(w), int(h)
            break
    else:
        raise RuntimeError("no SCREEN header — is the dashboard firmware running?")

    need = w * h * 2
    buf = bytearray()
    while len(buf) < need:
        chunk = ser.read(need - len(buf))
        if not chunk:
            raise RuntimeError(f"short read: {len(buf)}/{need} bytes")
        buf += chunk
    return rgb565_to_rgb(buf, w, h)


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else "screen"
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    ser = open_link()
    time.sleep(0.3)

    for i in range(pages):
        img = grab(ser)
        out = f"{prefix}{i}.png"
        img.save(out)
        print(f"{out}: {img.width}x{img.height}")
        if i + 1 < pages:
            ser.write(b"N")
            ser.flush()
            time.sleep(0.4)
    ser.close()


if __name__ == "__main__":
    main()
