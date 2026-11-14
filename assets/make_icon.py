"""Draws assets/lore.ico: a gold book with a hash sign on a dark blue tile.

Run: python assets/make_icon.py [preview.png]
Needs Pillow. Only needed to change the icon; the build uses the committed lore.ico.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # drawn at this size, then scaled down for each icon size
SIZES = [16, 24, 32, 48, 64, 128, 256]

TILE_TOP = (38, 52, 96)
TILE_BOTTOM = (18, 24, 48)
COVER = (232, 182, 76)
SPINE = (186, 132, 40)
PAGES = (246, 236, 210)
INK = TILE_BOTTOM


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # Tile with a vertical gradient, cut to a rounded square.
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(TILE_TOP, TILE_BOTTOM)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([24, 24, S - 24, S - 24], radius=200, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)

    # Book: page block showing at the bottom right, then cover and spine on top.
    left, top, right, bottom = 236, 170, 760, 830
    d.rounded_rectangle([left + 40, top + 40, right + 40, bottom + 30], radius=36, fill=PAGES)
    d.rounded_rectangle([left, top, right, bottom], radius=44, fill=COVER)
    d.rounded_rectangle([left, top, left + 100, bottom], radius=44, fill=SPINE)
    d.rectangle([left + 60, top, left + 100, bottom], fill=SPINE)

    # Hash sign on the cover.
    cx, cy = (left + 100 + right) // 2, (top + bottom) // 2
    half, w, gap = 165, 62, 70
    for dx in (-gap, gap):  # slanted verticals
        d.line([(cx + dx + 28, cy - half), (cx + dx - 28, cy + half)], fill=INK, width=w)
    for dy in (-gap, gap):  # horizontals
        d.line([(cx - half, cy + dy), (cx + half, cy + dy)], fill=INK, width=w)

    return img


def main():
    img = draw()
    out = Path(__file__).with_name("lore.ico")
    img.resize((256, 256), Image.LANCZOS).save(out, sizes=[(s, s) for s in SIZES])
    print(f"Wrote {out}")
    if len(sys.argv) > 1:  # preview sheet of every size
        sheet = Image.new("RGBA", (sum(SIZES) + 10 * len(SIZES), 256), (255, 255, 255, 255))
        x = 0
        for s in SIZES:
            sheet.paste(img.resize((s, s), Image.LANCZOS), (x, 256 - s))
            x += s + 10
        sheet.save(sys.argv[1])
        print(f"Wrote {sys.argv[1]}")


if __name__ == "__main__":
    main()
