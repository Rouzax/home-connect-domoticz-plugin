"""Build the README/docs hero and the GitHub social preview from captured device cards.

    python3 tools/banner/hero.py

Needs Pillow and cairosvg, and the Roboto fonts (ROBOTO_DIR, default ~/.local/share/fonts/Roboto).
Reads the cards in tools/banner/cards (captured with capture_cards.js --no-footer) and the BSH
appliance drawings in tools/banner/appliances. Writes docs/assets/hero.png (1600 wide) and
tools/banner/social-preview.png (1280x640; upload it by hand under Settings > General > Social
preview, GitHub has no API for it).

The cards carry their own rounded corners and drop shadow in the alpha channel, so nothing is
drawn under them.
"""

import io
import os
import re
from pathlib import Path

import cairosvg
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CARDS = HERE / "cards"
ICONS = HERE / "appliances"
FONTS = Path(os.environ.get("ROBOTO_DIR", Path.home() / ".local/share/fonts/Roboto"))

TITLE, SUBTITLE = "Home Connect", "for Domoticz"
TAGLINE = "Your Bosch, Siemens, Neff and Gaggenau appliances, as Domoticz devices."
# Card files (device idx on the capture instance) in reading order: a running washer, its program
# and remaining time, the dryer next to it, and the lifetime water meter. Not the energy meter:
# its headline is a live power the washer never reports, so it reads 0 Watt.
GRID = [["252", "299", "298"], ["246", "297", "339"]]
APPLIANCES = ["washer", "dryer", "dishwasher", "oven", "microwave", "fridge-freezer", "hood"]

INK, MUTED, ACCENT = (23, 42, 63), (99, 118, 138), (0, 99, 184)
# The drawings' single dark fill becomes this, so they sit behind the title instead of on it.
ICON_TINT = "#7a9cc0"
LANCZOS = Image.Resampling.LANCZOS


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"Roboto-{name}.ttf"), size)


def background(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        k = y / h
        d.line([(0, y), (w, y)], fill=(round(240 - 12 * k), round(244 - 7 * k), round(250 - 4 * k)))
    return img.convert("RGBA")


def icon(name: str, size: int) -> Image.Image:
    svg = (ICONS / f"{name}.svg").read_text(encoding="utf-8")
    svg = re.sub(r"#181B2B", ICON_TINT, svg, flags=re.IGNORECASE)
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size)
    assert png is not None  # None only when a write_to target is given
    return Image.open(io.BytesIO(png)).convert("RGBA")


def compose(
    w: int, h: int, margin: int, gap: int, title_px: int, cards_top: int, icon_px: int, icon_y: int
) -> tuple[Image.Image, int]:
    """A w x h banner: title block top left, appliance row top right, 2x3 cards below. Returns
    the image and the y where the cards end."""
    bg = background(w, h)
    d = ImageDraw.Draw(bg)
    f_title, f_sub = font("Bold", title_px), font("Light", round(title_px * 0.7))
    f_tag = font("Regular", round(title_px * 0.44))
    d.text((margin, margin), TITLE, font=f_title, fill=INK)
    sub_x = margin + d.textlength(TITLE, font=f_title) + title_px * 0.27
    d.text((sub_x, margin + title_px * 0.27), SUBTITLE, font=f_sub, fill=ACCENT)
    d.text((margin, margin + title_px * 1.25), TAGLINE, font=f_tag, fill=MUTED)

    # The appliance row ends where the card grid ends.
    step = icon_px + round(icon_px * 0.25)
    x = w - margin - len(APPLIANCES) * step + (step - icon_px)
    for name in APPLIANCES:
        bg.alpha_composite(icon(name, icon_px), (x, icon_y))
        x += step

    cards = [[Image.open(CARDS / f"{stem}.png").convert("RGBA") for stem in row] for row in GRID]
    card_w = (w - 2 * margin - 2 * gap) // 3
    # Every card the same size: captures differ by a few pixels, and unequal rows read as a mistake.
    card_h = round(cards[0][0].height * card_w / cards[0][0].width)
    for r, row in enumerate(cards):
        for c, card in enumerate(row):
            pos = (margin + c * (card_w + gap), cards_top + r * (card_h + gap))
            bg.alpha_composite(card.resize((card_w, card_h), LANCZOS), pos)
    return bg, cards_top + 2 * card_h + gap


def main() -> None:
    # README/docs hero: drawn at 2400 and scaled down to 1600.
    w = 2400
    drawn, bottom = compose(w, 2000, 90, 36, 96, 330, 120, 96)
    hero = drawn.crop((0, 0, w, bottom + 110)).convert("RGB")
    hero = hero.resize((1600, round(hero.height * 1600 / w)), LANCZOS)
    out = REPO / "docs" / "assets" / "hero.png"
    hero.save(out, optimize=True)
    print(f"{out.relative_to(REPO)}  {hero.width}x{hero.height}")

    # Social preview: 1280x640, drawn at 2x, everything inside GitHub's 80 px safe border.
    drawn, bottom = compose(2560, 1280, 160, 40, 112, 470, 104, 160)
    if bottom > 1280 - 160:
        raise SystemExit(f"cards end at {bottom}, past the safe border")
    social = drawn.convert("RGB").resize((1280, 640), LANCZOS)
    out = HERE / "social-preview.png"
    social.save(out, optimize=True)
    print(f"{out.relative_to(REPO)}  {social.width}x{social.height}")


if __name__ == "__main__":
    main()
