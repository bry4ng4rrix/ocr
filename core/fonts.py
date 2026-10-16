"""Fonts for the rebuilt PDF: TrueType families found on disk, embedded (subset) by ReportLab.

Only families whose files are really present in `config.FONT_DIRS` are offered (bundled
`assets/fonts/` first). The generic names "Serif", "Sans" and "Mono" are aliases resolved to
Liberation, then DejaVu, then Noto. A missing style falls back to the regular face, an unknown
family to "Serif". When no TrueType file exists at all, the standard PDF font Times is used (not
embedded, Western European characters only) and a warning is logged.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from functools import lru_cache

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

import config

log = logging.getLogger("livreocr.fonts")

STYLES = ("regular", "bold", "italic", "bolditalic")
GENERIC = ("Serif", "Sans", "Mono")

# family -> files for regular, bold, italic, bold italic; licence (all allow embedding).
_OFL = "SIL Open Font License 1.1"
_DEJAVU = "Licence DejaVu (libre, intégration autorisée)"
CANDIDATES: dict[str, tuple[tuple[str, str, str, str], str]] = {
    "Liberation Serif": (("LiberationSerif-Regular.ttf", "LiberationSerif-Bold.ttf", "LiberationSerif-Italic.ttf",
                          "LiberationSerif-BoldItalic.ttf"), _OFL),
    "Liberation Sans": (("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf", "LiberationSans-Italic.ttf",
                         "LiberationSans-BoldItalic.ttf"), _OFL),
    "Liberation Mono": (("LiberationMono-Regular.ttf", "LiberationMono-Bold.ttf", "LiberationMono-Italic.ttf",
                         "LiberationMono-BoldItalic.ttf"), _OFL),
    "DejaVu Serif": (("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf", "DejaVuSerif-Italic.ttf",
                      "DejaVuSerif-BoldItalic.ttf"), _DEJAVU),
    "DejaVu Sans": (("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf",
                     "DejaVuSans-BoldOblique.ttf"), _DEJAVU),
    "DejaVu Sans Mono": (("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf", "DejaVuSansMono-Oblique.ttf",
                          "DejaVuSansMono-BoldOblique.ttf"), _DEJAVU),
    "Noto Serif": (("NotoSerif-Regular.ttf", "NotoSerif-Bold.ttf", "NotoSerif-Italic.ttf",
                    "NotoSerif-BoldItalic.ttf"), _OFL),
    "Noto Sans": (("NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "NotoSans-Italic.ttf",
                   "NotoSans-BoldItalic.ttf"), _OFL),
    "Noto Sans Mono": (("NotoSansMono-Regular.ttf", "NotoSansMono-Bold.ttf", "", ""), _OFL),
}
GENERIC_TARGETS = {
    "Serif": ("Liberation Serif", "DejaVu Serif", "Noto Serif"),
    "Sans": ("Liberation Sans", "DejaVu Sans", "Noto Sans"),
    "Mono": ("Liberation Mono", "DejaVu Sans Mono", "Noto Sans Mono"),
}
# Families tried, in this order, when the chosen one lacks characters of a text (widest coverage first).
FALLBACK_ORDER = ("DejaVu Serif", "DejaVu Sans", "Noto Serif", "Noto Sans", "Liberation Serif", "Liberation Sans")
STANDARD_TIMES = ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic")

_lock = threading.Lock()


@dataclass
class FontFamily:
    name: str
    files: dict[str, str] = field(default_factory=dict)   # style -> path
    licence: str = ""
    alias_of: str = ""                                     # for "Serif", "Sans", "Mono": the real family

    @property
    def real_name(self) -> str:
        return self.alias_of or self.name

    @property
    def missing_styles(self) -> list[str]:
        return [s for s in STYLES if s not in self.files]


@lru_cache(maxsize=1)
def _font_files() -> dict[str, str]:
    """file name -> first path found in config.FONT_DIRS (bundled fonts first)."""
    wanted = {name for files, _ in CANDIDATES.values() for name in files if name}
    found: dict[str, str] = {}
    for root_dir in config.FONT_DIRS:
        if not os.path.isdir(root_dir):
            continue
        for root, _, names in os.walk(root_dir):
            for name in names:
                if name in wanted and name not in found:
                    found[name] = os.path.join(root, name)
    return found


@lru_cache(maxsize=1)
def _families() -> dict[str, FontFamily]:
    files = _font_files()
    families: dict[str, FontFamily] = {}
    for name, (names, licence) in CANDIDATES.items():
        paths = {style: files[f] for style, f in zip(STYLES, names) if f and f in files}
        if "regular" in paths:
            families[name] = FontFamily(name, paths, licence)
    generics: dict[str, FontFamily] = {}
    for generic, targets in GENERIC_TARGETS.items():
        target = next((t for t in targets if t in families), None)
        if target:
            real = families[target]
            generics[generic] = FontFamily(generic, dict(real.files), real.licence, alias_of=target)
    return {**generics, **families}


def available_families() -> dict[str, FontFamily]:
    """Usable families: generic aliases ("Serif", "Sans", "Mono") first, then the TrueType families found."""
    return dict(_families())


def family_names() -> list[str]:
    """Names to offer in the interface (generic aliases first)."""
    return list(_families())


def refresh() -> None:
    """Forget the cached font search (after adding files to a font folder)."""
    _font_files.cache_clear()
    _families.cache_clear()


def _lookup(family: str) -> FontFamily | None:
    families = _families()
    if family in families:
        return families[family]
    lowered = (family or "").strip().lower()
    return next((f for name, f in families.items() if name.lower() == lowered), None)


def _style(bold: bool, italic: bool) -> str:
    return ("bold" if bold else "") + ("italic" if italic else "") or "regular"


def _register(fam: FontFamily, style: str) -> str:
    """Register the TrueType face once (shared by an alias and its real family) and return its name."""
    used = style if style in fam.files else "regular"
    name = f"{fam.real_name}-{used}".replace(" ", "")
    with _lock:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, fam.files[used]))
    return name


def resolve(family: str, bold: bool = False, italic: bool = False) -> str:
    """ReportLab font name for this family and style.

    Missing style -> regular face; unknown family -> "Serif"; no TrueType font at all -> Times
    (standard PDF font, not embedded) with a logged warning.
    """
    fam = _lookup(family)
    if fam is None:
        fam = _lookup("Serif") or next(iter(_families().values()), None)
        if fam is not None and family and family not in GENERIC:
            log.info("Police « %s » introuvable, remplacée par « %s ».", family, fam.name)
    if fam is None:
        log.warning("Aucune police TrueType trouvée dans %s : Times (non intégrée, accents latins seulement).",
                    config.FONT_DIRS)
        return STANDARD_TIMES[STYLES.index(_style(bold, italic))]
    return _register(fam, _style(bold, italic))


def is_embedded(font_name: str) -> bool:
    return font_name not in STANDARD_TIMES


def missing_characters(font_name: str, text: str) -> set[str]:
    """Characters of `text` (spaces excepted) that the font cannot draw."""
    chars = {c for c in text if not c.isspace()}
    if not chars:
        return set()
    font = pdfmetrics.getFont(font_name)
    face = getattr(font, "face", None)
    cmap = getattr(face, "charToGlyph", None)
    if cmap is not None:
        return {c for c in chars if ord(c) not in cmap}
    missing = set()
    for c in chars:
        try:
            c.encode("cp1252")
        except UnicodeEncodeError:
            missing.add(c)
    return missing


def fallback_font(text: str, bold: bool = False, italic: bool = False, exclude: str = "") -> tuple[str, set[str]] | None:
    """Best replacement font for `text`: the first fallback family that draws every character, else
    the one that misses the fewest. Returns (font name, characters still missing) or None."""
    best: tuple[str, set[str]] | None = None
    families = _families()
    for name in FALLBACK_ORDER:
        if name not in families:
            continue
        font_name = _register(families[name], _style(bold, italic))
        if font_name == exclude:
            continue
        missing = missing_characters(font_name, text)
        if not missing:
            return font_name, missing
        if best is None or len(missing) < len(best[1]):
            best = (font_name, missing)
    return best
