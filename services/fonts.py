"""Polices : correspondance nom de famille Word <-> fichiers TrueType/OpenType installés.

Les familles sont lues dans la table `name` des fichiers (identifiant 1 = famille « héritée »,
celle qu'affichent Word et LibreOffice, regroupant régulier / gras / italique / gras italique).
Le style (gras, italique) vient du champ `fsSelection` de la table `OS/2`, sinon du nom de style.

Les collections (.ttc) et les polices web (.woff, .woff2) sont ignorées : Word et
`insert_htmlbox` (PyMuPDF) ne peuvent pas les utiliser directement depuis un fichier.

Replis (toujours signalés par la valeur de retour, jamais silencieux) :
- famille absente : équivalent métrique (Times New Roman -> Liberation Serif, Arial -> Liberation Sans,
  Courier New -> Liberation Mono), sinon `config.DEFAULT_FONT_FAMILY` ;
- variante absente : gras italique -> gras -> italique -> régulier ; gras ou italique -> régulier.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pymupdf

import config
from models.errors import ExportError

logger = logging.getLogger("bookocr.fonts")

FONT_SUFFIXES = {".ttf", ".otf"}

# Équivalents métriques (mêmes largeurs de caractères) utilisés si la famille demandée est absente.
METRIC_ALIASES = {
    "times new roman": "Liberation Serif",
    "times": "Liberation Serif",
    "serif": "Liberation Serif",
    "arial": "Liberation Sans",
    "helvetica": "Liberation Sans",
    "sans-serif": "Liberation Sans",
    "sans": "Liberation Sans",
    "courier new": "Liberation Mono",
    "courier": "Liberation Mono",
    "monospace": "Liberation Mono",
}


@dataclass(frozen=True)
class FontFamily:
    name: str
    regular: Path
    bold: Path | None = None
    italic: Path | None = None
    bold_italic: Path | None = None

    def exact_variant(self, bold: bool, italic: bool) -> Path | None:
        """Fichier de la variante demandée, ou None si la famille ne la possède pas."""
        if bold and italic:
            return self.bold_italic
        if bold:
            return self.bold
        if italic:
            return self.italic
        return self.regular

    def nearest_variant(self, bold: bool, italic: bool) -> Path:
        """Variante demandée, sinon la plus proche (gras italique -> gras -> italique -> régulier)."""
        candidates = [self.exact_variant(bold, italic)]
        if bold and italic:
            candidates += [self.bold, self.italic]
        candidates.append(self.regular)
        return next(path for path in candidates if path is not None)


@dataclass(frozen=True)
class FontFace:
    """Un fichier de police décrit par ses tables `name` et `OS/2`."""

    family: str
    subfamily: str
    bold: bool
    italic: bool
    path: Path


@dataclass(frozen=True)
class FontChoice:
    """Résultat détaillé d'une résolution de police (pour les rapports d'export)."""

    requested_family: str
    family: str
    path: Path
    family_fallback: bool
    variant_fallback: bool


# --------------------------------------------------------------------------- lecture des tables


def _decode_name(platform_id: int, encoding_id: int, raw: bytes) -> str:
    if platform_id in (0, 3):
        return raw.decode("utf-16-be", errors="replace")
    return raw.decode("latin-1", errors="replace")


def _read_tables(data: bytes) -> dict[bytes, tuple[int, int]]:
    """Répertoire des tables d'un fichier sfnt : étiquette -> (décalage, longueur)."""
    if len(data) < 12:
        raise ValueError("fichier de police tronqué")
    signature = data[:4]
    if signature not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        raise ValueError(f"signature sfnt inconnue {signature!r}")
    (table_count,) = struct.unpack_from(">H", data, 4)
    tables = {}
    for index in range(table_count):
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * index)
        tables[tag] = (offset, length)
    return tables


def _read_names(data: bytes, offset: int) -> dict[int, str]:
    """Noms 1 (famille), 2 (style), 16 et 17 (typographiques) ; Windows anglais de préférence."""
    _format, count, string_offset = struct.unpack_from(">HHH", data, offset)
    best: dict[int, tuple[int, str]] = {}
    for index in range(count):
        platform_id, encoding_id, language_id, name_id, length, value_offset = struct.unpack_from(
            ">HHHHHH", data, offset + 6 + 12 * index)
        if name_id not in (1, 2, 16, 17):
            continue
        if platform_id == 3 and encoding_id in (0, 1, 10):
            rank = 0 if language_id == 0x409 else 1
        elif platform_id == 1 and encoding_id == 0 and language_id == 0:
            rank = 2
        elif platform_id == 0:
            rank = 3
        else:
            continue
        start = offset + string_offset + value_offset
        text = _decode_name(platform_id, encoding_id, data[start:start + length]).strip()
        if text and (name_id not in best or rank < best[name_id][0]):
            best[name_id] = (rank, text)
    return {name_id: text for name_id, (_rank, text) in best.items()}


def read_font_face(path: Path) -> FontFace:
    """Lit la famille et le style d'un fichier TTF/OTF. ValueError si le fichier n'est pas exploitable."""
    data = path.read_bytes()
    tables = _read_tables(data)
    if b"name" not in tables:
        raise ValueError("table 'name' absente")
    names = _read_names(data, tables[b"name"][0])
    family = names.get(1) or names.get(16)
    if not family:
        raise ValueError("nom de famille absent")
    subfamily = names.get(2, "Regular")
    lowered = subfamily.lower()
    bold = "bold" in lowered
    italic = "italic" in lowered or "oblique" in lowered
    if b"OS/2" in tables:
        os2_offset, os2_length = tables[b"OS/2"]
        if os2_length >= 64:
            (selection,) = struct.unpack_from(">H", data, os2_offset + 62)
            bold = bool(selection & 0x20)
            italic = bool(selection & 0x01) or bool(selection & 0x200)
    return FontFace(family=family, subfamily=subfamily, bold=bold, italic=italic, path=path)


# --------------------------------------------------------------------------- inventaire


def _font_files(directories: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for directory in directories:
        root = Path(directory).expanduser()
        if not root.is_dir():
            continue
        files.extend(sorted(path for path in root.rglob("*")
                            if path.suffix.lower() in FONT_SUFFIXES and path.is_file()))
    return files


def group_faces(faces: list[FontFace]) -> dict[str, FontFamily]:
    """Regroupe des fichiers par famille. Le premier fichier trouvé pour une variante l'emporte."""
    variants: dict[str, dict[tuple[bool, bool], Path]] = {}
    for face in faces:
        slots = variants.setdefault(face.family, {})
        slots.setdefault((face.bold, face.italic), face.path)
    families = {}
    for name, slots in variants.items():
        regular = slots.get((False, False))
        if regular is None:
            # famille sans variante régulière (ex. « Light » seule) : la première variante sert de base
            regular = next(iter(slots.values()))
        families[name] = FontFamily(name=name, regular=regular, bold=slots.get((True, False)),
                                    italic=slots.get((False, True)), bold_italic=slots.get((True, True)))
    return families


@lru_cache(maxsize=4)
def _families_for(directories: tuple[str, ...]) -> dict[str, FontFamily]:
    faces = []
    for path in _font_files(directories):
        try:
            faces.append(read_font_face(path))
        except (OSError, ValueError, struct.error) as error:
            logger.info("Police ignorée %s : %s", path, error)
    families = group_faces(faces)
    logger.info("%d familles de polices trouvées dans %s", len(families), ", ".join(directories))
    return families


def available_families() -> dict[str, FontFamily]:
    """Familles TTF/OTF trouvées dans `config.FONT_DIRS` (mis en cache par liste de dossiers)."""
    return dict(_families_for(tuple(config.FONT_DIRS)))


def clear_cache() -> None:
    """Oublie l'inventaire (après installation d'une police ou changement de FONT_DIRS)."""
    _families_for.cache_clear()
    _pymupdf_font.cache_clear()
    natural_line_height.cache_clear()


def _lookup(families: dict[str, FontFamily], family: str) -> str | None:
    wanted = family.strip().lower()
    for name in families:
        if name.lower() == wanted:
            return name
    return None


def resolve_family(family: str) -> tuple[str, bool]:
    """Famille réellement utilisée et indicateur de repli (True si ce n'est pas celle demandée)."""
    families = available_families()
    found = _lookup(families, family)
    if found is not None:
        return found, False
    alias = METRIC_ALIASES.get(family.strip().lower())
    if alias is not None and _lookup(families, alias) is not None:
        return _lookup(families, alias), True
    for candidate in (config.DEFAULT_FONT_FAMILY, config.DEFAULT_SANS_FAMILY, "DejaVu Serif", "DejaVu Sans"):
        found = _lookup(families, candidate)
        if found is not None:
            return found, True
    if families:
        return sorted(families)[0], True
    raise ExportError("Aucune police TrueType n'a été trouvée sur cette machine.",
                      detail=", ".join(config.FONT_DIRS))


def choose_font(family: str, bold: bool, italic: bool) -> FontChoice:
    """Fichier de police à utiliser, avec le détail des replis appliqués."""
    resolved, family_fallback = resolve_family(family)
    font_family = available_families()[resolved]
    path = font_family.nearest_variant(bold, italic)
    variant_fallback = font_family.exact_variant(bold, italic) is None
    return FontChoice(requested_family=family, family=resolved, path=path,
                      family_fallback=family_fallback, variant_fallback=variant_fallback)


def font_file(family: str, bold: bool, italic: bool) -> Path:
    """Fichier TTF/OTF pour la famille et la variante demandées (replis : voir l'en-tête du module)."""
    return choose_font(family, bold, italic).path


DEFAULT_LINE_HEIGHT_RATIO = 1.15


@lru_cache(maxsize=128)
def natural_line_height(path: Path) -> float:
    """Interligne simple de la police, en multiple de la taille (hhea : ascendante + descendante + interligne).

    C'est la référence de « interligne multiple » dans Word et LibreOffice ; le CSS de PyMuPDF
    exprime au contraire `line-height` en multiple de la taille. Liberation Serif : 1,149.
    """
    try:
        data = path.read_bytes()
        tables = _read_tables(data)
        head_offset, _ = tables[b"head"]
        hhea_offset, _ = tables[b"hhea"]
        (units_per_em,) = struct.unpack_from(">H", data, head_offset + 18)
        ascender, descender, line_gap = struct.unpack_from(">hhh", data, hhea_offset + 4)
    except (OSError, KeyError, ValueError, struct.error) as error:
        logger.info("Interligne naturel illisible pour %s : %s", path, error)
        return DEFAULT_LINE_HEIGHT_RATIO
    ratio = (ascender - descender + max(line_gap, 0)) / units_per_em if units_per_em else 0.0
    return ratio if 0.8 <= ratio <= 2.5 else DEFAULT_LINE_HEIGHT_RATIO


@lru_cache(maxsize=64)
def _pymupdf_font(path: str) -> pymupdf.Font:
    return pymupdf.Font(fontfile=path)


def missing_glyphs(family: str, text: str) -> set[str]:
    """Caractères de `text` (hors espaces et contrôles) absents de la police régulière de la famille résolue."""
    font = _pymupdf_font(str(font_file(family, False, False)))
    return {char for char in set(text)
            if not char.isspace() and char.isprintable() and not font.has_glyph(ord(char))}
