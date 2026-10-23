"""Tile captured device cards into one docs screenshot, three across, on a transparent background.

    python3 tools/banner/tile.py <out.png> <card.png> [<card.png> ...]

Cards come from capture_cards.js (with their footer row, as a user sees them). Each capture
already carries a 10 px margin for its shadow, so the cards are placed edge to edge; every card is
scaled to the first one's size so the rows line up. Needs Pillow.
"""

import sys
from pathlib import Path

from PIL import Image

COLUMNS = 3
# Captures are taken at device scale 2; the docs show them at 1x.
SCALE = 0.5


def main() -> None:
    out, *paths = sys.argv[1:]
    if not paths:
        raise SystemExit(__doc__)
    cards = [Image.open(p).convert("RGBA") for p in paths]
    w, h = round(cards[0].width * SCALE), round(cards[0].height * SCALE)
    rows = -(-len(cards) // COLUMNS)
    sheet = Image.new("RGBA", (COLUMNS * w, rows * h), (0, 0, 0, 0))
    for i, card in enumerate(cards):
        resized = card.resize((w, h), Image.Resampling.LANCZOS)
        sheet.alpha_composite(resized, ((i % COLUMNS) * w, (i // COLUMNS) * h))
    sheet.save(out, optimize=True)
    print(f"{Path(out).name}  {sheet.width}x{sheet.height}  {len(cards)} cards")


if __name__ == "__main__":
    main()
