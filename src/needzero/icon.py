"""App / tray icon: icon.png from the project root or the PyInstaller bundle."""

from __future__ import annotations

import sys
from pathlib import Path


def _find_icon_png():
    """Locate icon.png (bundled next to the frozen exe, or the project root)."""
    cands = []
    if getattr(sys, "frozen", False):
        mp = getattr(sys, "_MEIPASS", None)
        if mp:
            cands.append(Path(mp) / "icon.png")
        cands.append(Path(sys.executable).parent / "icon.png")
    cands.append(Path(__file__).resolve().parents[2] / "icon.png")   # project root
    for c in cands:
        if c.is_file():
            return c
    return None


def make_icon(size: int = 64):
    from PIL import Image

    return Image.open(_find_icon_png()).convert("RGBA").resize((size, size))
