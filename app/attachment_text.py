"""Normalize extracted attachment text and model quotes with the same rules."""
import unicodedata

from .radicals import RADICALS

SYMBOLS = str.maketrans({**dict.fromkeys('∕⁄╱', '/'), **dict.fromkeys('‐‑‒–—―−', '-')})


def normalize_attachment_text(text):
    return unicodedata.normalize('NFKC', text).translate(RADICALS).translate(SYMBOLS)
