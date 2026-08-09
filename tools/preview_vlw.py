#!/usr/bin/env python3
"""Render text from a .vlw file the way TFT_eSPI's drawGlyph does, to a PNG.

The point is to check the generated font on the host before flashing. It mirrors
TFT_eSPI's positioning exactly:

    xs = cursor_x + gdX
    ys = cursor_y + maxAscent - gdY
    cursor_x += gxAdvance

so combining marks (gxAdvance == 0, negative gdX) land back over their base
character. If Thai looks right here, it looks right on the panel.

Usage: preview_vlw.py <font.vlw> <out.png> [--width N] "line one" "line two" ...

--width defaults to 320, the panel's landscape width.
"""

import sys
import struct
from PIL import Image


def load(path):
    b = open(path, "rb").read()
    count, _ver, size, _u, ascent, descent = struct.unpack_from(">6i", b, 0)

    glyphs, ptr = {}, 24 + count * 28
    max_ascent, max_descent = ascent, descent
    for i in range(count):
        cp, h, w, adv, dy, dx, _ = struct.unpack_from(">7i", b, 24 + i * 28)
        glyphs[cp] = (h, w, adv, dy, dx, ptr)
        ptr += w * h
        # TFT_eSPI grows these from the metrics as it loads
        if cp > 255 or (31 < cp < 128):
            max_ascent = max(max_ascent, dy)
            max_descent = max(max_descent, h - dy)
    return b, glyphs, size, max_ascent, max_descent


def render(path, out, lines, width=320, scale=3):
    blob, glyphs, size, max_ascent, max_descent = load(path)
    y_advance = max_ascent + max_descent
    space = y_advance // 4

    W, H = width, y_advance * len(lines) + 8
    img = Image.new("L", (W, H), 0)
    px = img.load()

    for row, text in enumerate(lines):
        cx = 2
        cy = row * y_advance + 4
        for ch in text:
            cp = ord(ch)
            if cp == 0x20:
                cx += space
                continue
            if cp not in glyphs:
                print(f"  missing glyph U+{cp:04X} ({ch})")
                continue
            h, w, adv, dy, dx, ptr = glyphs[cp]
            xs, ys = cx + dx, cy + max_ascent - dy
            for gy in range(h):
                for gx in range(w):
                    v = blob[ptr + gy * w + gx]
                    x, y = xs + gx, ys + gy
                    if v and 0 <= x < W and 0 <= y < H:
                        px[x, y] = max(px[x, y], v)   # marks blend over the base
            cx += adv
        if cx > W:
            # Name the text — with several lines it is easy to misread which
            # one this belongs to.
            print(f"  OVERFLOW {cx}px > {W}px: {text!r}")

    img.resize((W * scale, H * scale), Image.NEAREST).save(out)
    print(f"{out}: {W}x{H} @ {size}px, ascent={max_ascent} descent={max_descent}, "
          f"line height={y_advance}")


if __name__ == "__main__":
    argv, width = sys.argv[1:], 320
    if "--width" in argv:
        i = argv.index("--width")
        width = int(argv[i + 1])
        del argv[i:i + 2]
    render(argv[0], argv[1], argv[2:], width=width)
