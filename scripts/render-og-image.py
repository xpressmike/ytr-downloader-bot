#!/usr/bin/env python3
"""Render docs/assets/og-image.png (1200x630) for Open Graph previews.

Usage:
    .venv/bin/python scripts/render-og-image.py

Requires Pillow (`pip install Pillow`). Renders a simplified version of
docs/assets/og-image.html — dark gradient, brand label, title, subtitle.
The HTML source is the canonical design; this script produces a fallback
PNG when a headless Chrome is not available.
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
OUT = Path(__file__).resolve().parent.parent / "docs" / "assets" / "og-image.png"

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        raise SystemExit(
            f"Font not found: {path}\n"
            "Install DejaVu (Debian/Ubuntu: `sudo apt install fonts-dejavu`; "
            "macOS: download from dejavu-fonts.github.io) or edit FONT_BOLD / "
            "FONT_REG at the top of this script to point at an available font."
        )


def gradient_bg():
    img = Image.new("RGB", (W, H), (13, 17, 23))
    px = img.load()
    c1 = (26, 31, 46)
    c2 = (13, 17, 23)
    for x in range(W):
        for y in range(H):
            t = (x / W + y / H) / 2
            r = int(c1[0] * (1 - t) + c2[0] * t)
            g = int(c1[1] * (1 - t) + c2[1] * t)
            b = int(c1[2] * (1 - t) + c2[2] * t)
            px[x, y] = (r, g, b)
    return img


def main():
    img = gradient_bg()
    d = ImageDraw.Draw(img)

    label_font = load_font(FONT_BOLD, 28)
    title_font = load_font(FONT_BOLD, 70)
    sub_font = load_font(FONT_REG, 28)

    x = 80
    y = 180
    d.text(
        (x, y),
        "SELF-HOSTED  ·  OPEN SOURCE",
        fill=(139, 148, 158),
        font=label_font,
    )
    y += 60
    d.text(
        (x, y),
        "Твой личный архив",
        fill=(230, 237, 243),
        font=title_font,
    )
    y += 85
    d.text(
        (x, y),
        "видео из соцсетей",
        fill=(230, 237, 243),
        font=title_font,
    )
    y += 110
    d.text(
        (x, y),
        "YouTube  ·  Instagram  ·  TikTok  ·  Twitter",
        fill=(139, 148, 158),
        font=sub_font,
    )
    y += 40
    d.text(
        (x, y),
        "→ твой Telegram",
        fill=(37, 99, 235),
        font=load_font(FONT_BOLD, 30),
    )

    tape_x = W - 260
    tape_y = 210
    body = (40, 45, 60)
    d.rounded_rectangle(
        (tape_x, tape_y, tape_x + 200, tape_y + 160),
        radius=12,
        fill=body,
        outline=(80, 90, 110),
        width=2,
    )
    d.ellipse(
        (tape_x + 30, tape_y + 35, tape_x + 85, tape_y + 90),
        outline=(150, 160, 180),
        width=3,
    )
    d.ellipse(
        (tape_x + 115, tape_y + 35, tape_x + 170, tape_y + 90),
        outline=(150, 160, 180),
        width=3,
    )
    d.rectangle(
        (tape_x + 20, tape_y + 110, tape_x + 180, tape_y + 145),
        fill=(25, 28, 38),
    )
    d.text(
        (tape_x + 55, tape_y + 117),
        "ARCHIVE",
        fill=(100, 110, 130),
        font=load_font(FONT_BOLD, 22),
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
