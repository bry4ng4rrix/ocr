"""Mise en page d'une page : des mots OCR aux blocs typés, ordonnés et stylés.

Chaîne (`analyse_page`) :
1. Lignes : mots regroupés par (bloc, paragraphe, ligne) de Tesseract, puis re-découpés aux grands écarts
   horizontaux (gouttière de colonnes que Tesseract a fusionnée).
2. Illustrations et fonds (`detect_illustrations`, masque Lab) : une zone non-papier grande et contenant du texte
   est un **fond** (`background_regions`) ; les autres sont des images (`image_blocks`, sans fichier). Le texte
   posé dessus est gardé et marqué `on_image` (jamais jeté).
3. Paragraphes : enchaînement de lignes (recouvrement horizontal, interligne régulier, taille comparable) ;
   un retrait de première ligne, une ligne précédente courte, une puce ou un changement de zone coupent le bloc.
   Césure `mot-` + minuscule recollée.
4. Tailles en pixels -> format physique (ratio + corps 8–13 pt) -> tailles en points.
5. Types par règles explicables (titre, sous-titre, liste, légende, en-tête, pied, numéro, note, tableau,
   citation) ; l'utilisateur corrige ensuite dans l'éditeur.
6. Ordre de lecture : en-têtes, bandes pleine largeur et zones en colonnes (colonnes de gauche à droite),
   pieds et numéros en dernier.
7. Styles estimés : taille, gras (épaisseur du trait), italique (inclinaison), couleur d'encre, alignement,
   interligne, retrait de première ligne, espacement avant/après.

Toutes les boîtes sont normalisées (0..1) sur l'image corrigée. Les mesures de pixels se font sur l'image corrigée
fournie ; les tailles se mesurent dans le repère de l'image OCR (`OcrResult.width/height`).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import cv2
import numpy as np

from models import (MARGINAL_KINDS, STANDARD_FORMATS, Alignment, BlockKind, Box, DetectedRegion, ImageBlock,
                    ImageKind, Issue, LayoutResult, LayoutSettings, OcrLine, OcrResult, Page, PageCategory,
                    PageFormat, Severity, TextBlock, TextStyle)
from services.image_service import downscale

logger = logging.getLogger("bookocr.layout")

# --------------------------------------------------------------------------- constantes (réglées sur les pages synthétiques)

# Hauteur de l'encre des glyphes, en em, mesurée sur Liberation Serif / Sans (reprise de l'ancien code testé).
EM_X_HEIGHT = 0.46
EM_ASCENDER = 0.69
EM_ACCENT_LOWER = 0.73
EM_ACCENT_CAP = 0.86
EM_DESCENDER = 0.215
SINGLE_LINE_EM = 1.15          # interligne simple d'une police courante (Liberation Serif : 1,149 em)

LINE_SPLIT_GAP = 1.5           # écart horizontal (en hauteurs de mot) qui coupe une ligne Tesseract en deux
PARAGRAPH_PITCH = 1.35         # interligne (relatif à l'interligne habituel de la page) qui ouvre un paragraphe
SIZE_JUMP = 1.25               # rapport de tailles entre deux lignes qui ouvre un paragraphe (±25 %)
SIZE_JUMP_SINGLE_WORD = 1.45   # même règle quand une ligne n'a qu'un mot mesurable (mesure moins sûre)
MIN_OVERLAP = 0.5              # recouvrement horizontal minimal de deux lignes d'un même paragraphe
SHORT_LINE = 0.7               # ligne plus courte que 70 % de la largeur du bloc : fin de paragraphe
INDENT_EM = 0.8                # retrait (en em) qui marque une première ligne
EDGE_BAND = 0.09               # bandes haute et basse (part de la hauteur) des en-têtes, pieds et numéros
HEADING_1_RATIO = 1.6
HEADING_2_RATIO = 1.25
HEADING_SOFT_RATIO = 1.08      # titre plus petit, mais gras, italique ou centré
NOTE_RATIO = 0.85
BODY_SNAP = 0.08               # écart relatif au corps en deçà duquel un bloc prend la taille du corps
BOLD_RATIO = 1.35              # épaisseur relative du trait (par rapport au corps) à partir de laquelle on est gras
BOLD_ABSOLUTE = 0.115          # épaisseur / taille sans corps de référence
ITALIC_MIN_SLANT = 0.12        # tangente de l'inclinaison des fûts
CAPTION_GAP = 0.04             # écart maximal (part de la hauteur) entre une image et sa légende
JUSTIFY_GAP_CV = 0.08          # variation de l'espace entre mots au-delà de laquelle le texte est justifié
FULL_WIDTH = 0.6               # part de la largeur utile à partir de laquelle un bloc sépare des bandes
BACKGROUND_MIN_AREA = 0.35     # zone non-papier contenant du texte et au moins aussi grande : fond graphique
IMAGE_TEXT_AREA = 0.05         # en dessous de cette surface d'images, la page reste « texte » (cas A)
NOISE_CONFIDENCE = 35
BODY_SIZE_RANGE = (8.0, 13.0)
FORMAT_ASPECT_TOLERANCE = 0.04

# Puces (dont « » », « + » et « © », lectures fréquentes de « • » par Tesseract ; pas « « », qui ouvre un dialogue)
LIST_MARKER = re.compile(r"^\s*(?:[•·▪●○◦■□►‣–—*»+©-]\s*\S|\(?\d{1,3}[.)]\s+\S|\(?[a-z][.)]\s+\S)")
CAPTION_START = re.compile(r"^\s*(?:fig(?:ure)?s?\.?|illustrations?|planches?|gravures?|cartes?|photos?|"
                           r"photographies?|pl\.|tableau|abb\.?|abbildung|plate|map)\b", re.IGNORECASE)
NOTE_START = re.compile(r"^\s*(?:\d{1,3}[.)]?|\(\d{1,3}\)|[*†‡]+|[¹²³⁴⁵⁶⁷⁸⁹⁰]+)\s*\S")
# Numéros : chiffres arabes (≤ 4) ou romains courts plausibles (≤ 6 lettres, une seule casse) ; « mix »,
# « civil » ou « dim » sont des mots.
_ROMAN = r"(?:c{0,3}(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3}))"
PAGE_NUMBER = re.compile(rf"^[\s\-–—·.]{{0,3}}(\d{{1,4}}|(?=[cxlvi]){_ROMAN}|(?=[CXLVI]){_ROMAN.upper()})"
                         rf"[\s\-–—·.]{{0,3}}$")
HYPHENS = ("-", "\u00ac", "\u00ad", "\u2010")          # tiret, signe « non », tiret conditionnel, trait d'union
SOFT_HYPHENS = ("\u00ac", "\u00ad")

DESCENDERS = set("gjpqyçÇ,;()[]{}|/µ")
ACCENT_CAPS = set("ÀÁÂÃÄÅÈÉÊËÌÍÎÏÒÓÔÕÖÙÚÛÜÝŸÑ")
ACCENT_LOWER = set("àáâãäåèéêëìíîïòóôõöùúûüýÿñ")
TALL = set("bdfhkltijABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789œŒæÆßẞ'\"!?«»#%&@$£€§/\\()[]{}|")
BODY_KINDS_FOR_SIZE = {BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.SPECIAL}

# Seuils de la détection des illustrations (reprise de l'ancien code testé)
WORK_SIDE = 1600
MIN_PICTURE_AREA = 0.01
MIN_DRAWING_AREA = 0.03
MIN_DRAWING_SIDE = 0.08
MAX_DRAWING_FILL = 0.35
MIN_TABLE_AREA = 0.02
TABLE_MIN_CROSSINGS = 6
MAX_TABLE_INK = 0.3            # part d'encre maximale d'un tableau réglé (une photo est bien plus dense)
CHROMA_COLOURFUL = 15.0
TONE_SPREAD = 18
PADDING = 0.004
FUZZY_EDGE = 0.04

# --------------------------------------------------------------------------- structures de travail


@dataclass
class _Word:
    text: str
    left: float
    top: float
    right: float
    bottom: float
    confidence: float
    source: object                  # OcrWord d'origine (boîte normalisée)

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass
class _Line:
    words: list[_Word]
    key: tuple[int, int] | None     # (bloc, paragraphe) Tesseract ; None si la ligne a été re-découpée
    row_id: int = -1                # ligne Tesseract d'origine (repère des tableaux sans réglure)
    zone: int = -1                  # région (illustration / fond / tableau) contenant la ligne, -1 sinon
    _size: float | None = None

    @property
    def left(self) -> float:
        return min(word.left for word in self.words)

    @property
    def right(self) -> float:
        return max(word.right for word in self.words)

    @property
    def top(self) -> float:
        return min(word.top for word in self.words)

    @property
    def bottom(self) -> float:
        return max(word.bottom for word in self.words)

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 1.0)

    @property
    def width(self) -> float:
        return max(self.right - self.left, 1.0)

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    @property
    def size(self) -> float:
        """Corps (em) en pixels, estimé d'après la hauteur des mots."""
        if self._size is None:
            self._size = _font_size_px(self.words) or self.height / (EM_ASCENDER + EM_DESCENDER)
        return self._size

    def baseline(self) -> float:
        flat = [word.bottom for word in self.words if not (set(word.text) & DESCENDERS)]
        return float(np.median(flat)) if flat else self.bottom - EM_DESCENDER * self.size


@dataclass
class _Draft:
    """Bloc de texte en construction : lignes en pixels OCR et mesures."""

    lines: list[_Line]
    zone: int = -1
    table: bool = False
    rows: list[list[_Line]] | None = None
    block: TextBlock | None = None
    size_px: float = 0.0
    stroke: float | None = None
    italic: bool = False
    color: str | None = None
    rule_above: bool = False
    measures: dict = field(default_factory=dict)

    @property
    def left(self) -> float:
        return min(line.left for line in self.lines)

    @property
    def right(self) -> float:
        return max(line.right for line in self.lines)

    @property
    def top(self) -> float:
        return min(line.top for line in self.lines)

    @property
    def bottom(self) -> float:
        return max(line.bottom for line in self.lines)

    @property
    def words(self) -> list[_Word]:
        return [word for line in self.lines for word in line.words]


# --------------------------------------------------------------------------- mesures de taille


def _word_em(text: str) -> float | None:
    """Hauteur attendue de l'encre d'un mot, en em, selon les lettres qu'il contient."""
    chars = set(text)
    if not chars:
        return None
    if chars & ACCENT_CAPS:
        top = EM_ACCENT_CAP
    elif chars & ACCENT_LOWER:
        top = EM_ACCENT_LOWER
    elif chars & TALL:
        top = EM_ASCENDER
    else:
        top = EM_X_HEIGHT
    bottom = EM_DESCENDER if chars & DESCENDERS else 0.0
    if top == EM_X_HEIGHT and not bottom:
        return None            # mot en hauteur de x seulement (« une », « on ») : trop peu de pixels
    return top + bottom


def _measurable_words(words: list[_Word]) -> int:
    return sum(1 for word in words if re.sub(r"\W", "", word.text) and _word_em(word.text))


def _font_size_px(words: list[_Word]) -> float | None:
    sizes = []
    for word in words:
        if not re.sub(r"\W", "", word.text):
            continue
        em = _word_em(word.text)
        if em:
            sizes.append(word.height / em)
    return float(np.median(sizes)) if sizes else None


# --------------------------------------------------------------------------- lignes


def _words_px(ocr: OcrResult) -> list[_Word]:
    width, height = max(ocr.width, 1), max(ocr.height, 1)
    words = []
    for word in ocr.words:
        text = word.text.strip()
        if not text:
            continue
        box = word.box
        words.append(_Word(text, box.x * width, box.y * height, box.right * width, box.bottom * height,
                           word.confidence, word))
    return words


def _split_lines(words: list[_Word]) -> list[_Line]:
    """Lignes Tesseract, re-découpées là où un écart horizontal dépasse largement une espace."""
    by_line: dict[tuple[int, int, int], list[_Word]] = {}
    for word in words:
        source = word.source
        by_line.setdefault((source.block_num, source.par_num, source.line_num), []).append(word)
    pieces: list[tuple[tuple[int, int], int, list[_Word]]] = []
    split_paragraphs: set[tuple[int, int]] = set()
    for row_id, (key, line_words) in enumerate(sorted(by_line.items())):
        line_words = sorted(line_words, key=lambda word: word.left)
        height = float(np.median([word.height for word in line_words]))
        parts, current = [], [line_words[0]]
        for previous, word in zip(line_words, line_words[1:]):
            vertical_gap = word.top > previous.bottom or previous.top > word.bottom
            if word.left - previous.right > LINE_SPLIT_GAP * max(height, 4.0) or vertical_gap:
                parts.append(current)
                current = []
            current.append(word)
        parts.append(current)
        if len(parts) > 1:
            split_paragraphs.add(key[:2])
        pieces += [(key[:2], row_id, part) for part in parts]
    return [_Line(part, None if paragraph in split_paragraphs else paragraph, row_id)
            for paragraph, row_id, part in pieces]


def _h_overlap(a_left: float, a_right: float, b_left: float, b_right: float) -> float:
    inter = min(a_right, b_right) - max(a_left, b_left)
    return max(inter, 0.0) / max(min(a_right - a_left, b_right - b_left), 1.0)


def _usual_pitch(lines: list[_Line]) -> float:
    """Interligne habituel de la page (distance des lignes de base, en corps)."""
    pitches = []
    for line in lines:
        below = [other for other in lines if other.top > line.top + 0.5 * line.height
                 and _h_overlap(line.left, line.right, other.left, other.right) > 0.3]
        if below:
            nearest = min(below, key=lambda other: other.top)
            size = (line.size + nearest.size) / 2
            pitch = (nearest.baseline() - line.baseline()) / max(size, 1.0)
            if 0 < pitch < 3.0:
                pitches.append(pitch)
    return float(np.median(pitches)) if pitches else 1.2


# --------------------------------------------------------------------------- paragraphes


def _is_list_start(text: str) -> bool:
    return bool(LIST_MARKER.match(text))


def _can_continue(group: list[_Line], line: _Line, usual_pitch: float) -> bool:
    """`line` prolonge-t-elle le paragraphe `group` (dont la dernière ligne est juste au-dessus) ?"""
    last = group[-1]
    if line.zone != last.zone:
        return False
    if last.key is not None and line.key is not None and last.key != line.key:
        return False
    if _h_overlap(last.left, last.right, line.left, line.right) < MIN_OVERLAP:
        return False
    sizes = (last.size, line.size)
    reliable = min(_measurable_words(last.words), _measurable_words(line.words)) >= 2
    if max(sizes) / max(min(sizes), 1.0) > (SIZE_JUMP if reliable else SIZE_JUMP_SINGLE_WORD):
        return False
    size = (last.size + line.size) / 2
    pitch = (line.baseline() - last.baseline()) / max(size, 1.0)
    if pitch <= 0 or pitch > PARAGRAPH_PITCH * usual_pitch:
        return False
    if _is_list_start(line.text):
        return False
    em = line.size
    group_left = min(member.left for member in group)
    group_right = max(max(member.right for member in group), line.right)
    width = max(group_right - group_left, 1.0)
    first = group[0]
    if _is_list_start(first.text):
        # élément de liste : les lignes suivantes s'alignent sur le texte (retrait suspendu)
        return line.left >= first.left - 0.5 * em and line.left <= first.left + 4 * em
    delta_left = line.left - last.left
    delta_center = abs(line.center_x - last.center_x)
    centered = delta_center <= 0.3 * abs(delta_left) + 0.3 * em and abs(delta_left) > 0.5 * em
    if delta_left >= INDENT_EM * em and not centered:
        return False                                     # retrait de première ligne
    last_left_aligned = abs(last.left - group_left) <= INDENT_EM * em or len(group) == 1
    line_left_aligned = abs(line.left - group_left) <= INDENT_EM * em
    if (last.right < group_left + SHORT_LINE * width and last_left_aligned and line_left_aligned
            and not centered):
        return False                                     # ligne précédente courte : fin de paragraphe
    return True


def _paragraphs(lines: list[_Line], usual_pitch: float) -> list[list[_Line]]:
    groups: list[list[_Line]] = []
    for line in sorted(lines, key=lambda item: (item.top, item.left)):
        best = None
        for group in groups:
            last = group[-1]
            if line.top <= last.top + 0.5 * last.height:
                continue
            if _h_overlap(last.left, last.right, line.left, line.right) < 0.1:
                continue
            if best is None or last.top > best[-1].top:
                best = group
        # seule la ligne la plus proche au-dessus dans la même colonne peut être prolongée
        if best is not None and _can_continue(best, line, usual_pitch):
            best.append(line)
        else:
            groups.append([line])
    return groups


def _join_lines(texts: list[str]) -> str:
    """Texte courant d'un paragraphe : lignes jointes, césures `mot-` + minuscule recollées."""
    out = ""
    for text in texts:
        text = text.strip()
        if not text:
            continue
        if not out:
            out = text
            continue
        stripped = out.rstrip()
        hyphen_end = stripped.endswith(HYPHENS) and len(stripped) >= 2 and stripped[-2].isalpha()
        if hyphen_end and (text[0].islower() or stripped[-1] in SOFT_HYPHENS):
            out = stripped[:-1] + text                 # césure : « voya- / geur » -> « voyageur »
        elif hyphen_end:
            out = stripped + text                      # mot composé coupé au tiret : « Jean- / Pierre »
        else:
            out = f"{stripped} {text}"
    return out


def _table_rows(lines: list[_Line]) -> list[list[_Line]]:
    rows: list[list[_Line]] = []
    for line in sorted(lines, key=lambda item: (item.top, item.left)):
        centre = (line.top + line.bottom) / 2
        row = next((candidate for candidate in rows
                    if abs((candidate[0].top + candidate[0].bottom) / 2 - centre)
                    < 0.6 * max(candidate[0].height, line.height)), None)
        if row is None:
            rows.append([line])
        else:
            row.append(line)
    return [sorted(row, key=lambda item: item.left) for row in rows]


def _aligned_tables(lines: list[_Line], content_width: float) -> list[list[_Line]]:
    """Tableaux sans réglure : ≥ 3 lignes Tesseract consécutives coupées en cellules alignées."""
    by_row: dict[int, list[_Line]] = {}
    for line in lines:
        if line.key is None and line.zone == -1:
            by_row.setdefault(line.row_id, []).append(line)
    rows = sorted((sorted(pieces, key=lambda item: item.left) for pieces in by_row.values() if len(pieces) >= 2),
                  key=lambda pieces: pieces[0].top)
    tables: list[list[list[_Line]]] = []
    current: list[list[_Line]] = []

    def cell_like(row: list[_Line]) -> bool:
        narrow = max(piece.width for piece in row) < 0.3 * content_width
        return len(row) >= 3 or narrow

    def aligned(row_a: list[_Line], row_b: list[_Line]) -> bool:
        em = float(np.median([piece.size for piece in row_a + row_b]))
        lefts_a = [piece.left for piece in row_a]
        hits = sum(any(abs(piece.left - left) <= 1.0 * em for left in lefts_a) for piece in row_b)
        pitch = row_b[0].top - row_a[0].top
        return hits >= 2 and 0 < pitch < 3.0 * em

    for row in rows:
        if not cell_like(row):
            if len(current) >= 3:
                tables.append(current)
            current = []
            continue
        if current and not aligned(current[-1], row):
            if len(current) >= 3:
                tables.append(current)
            current = []
        current.append(row)
    if len(current) >= 3:
        tables.append(current)
    return [[piece for row in table for piece in row] for table in tables]


# --------------------------------------------------------------------------- mesures sur les pixels


def _patch(rgb: np.ndarray, box: Box) -> np.ndarray:
    height, width = rgb.shape[:2]
    left, top, right, bottom = box.to_pixels(width, height)
    return rgb[top:bottom, left:right]


def _ink_mask(patch: np.ndarray) -> tuple[np.ndarray, bool] | None:
    """Masque de l'encre (classe minoritaire d'Otsu) et polarité (True = encre plus claire que le fond)."""
    if patch.shape[0] < 4 or patch.shape[1] < 4:
        return None
    gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY) if patch.ndim == 3 else patch
    threshold, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark = gray <= threshold
    light_ink = dark.mean() > 0.5
    ink = ~dark if light_ink else dark
    if ink.sum() < 30:
        return None
    contrast = abs(float(gray[ink].mean()) - float(gray[~ink].mean())) if (~ink).any() else 0.0
    if contrast < 40:
        return None
    return ink.astype(np.uint8), light_ink


def _stroke_ratio(ink: np.ndarray, size_px: float) -> float | None:
    distance = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    ridge = distance[(distance >= cv2.dilate(distance, np.ones((3, 3), np.uint8))) & (distance > 0)]
    if not ridge.size or size_px <= 0:
        return None
    return max(2.0 * float(np.mean(ridge)) - 1.0, 0.1) / size_px


def _slant(ink: np.ndarray) -> float:
    """Tangente de l'inclinaison des fûts (> 0 : penché vers la droite)."""
    height, width = ink.shape
    if height < 8 or width < 8:
        return 0.0
    source = ink.astype(np.float32)
    best_shear, best_score, base_score = 0.0, -1.0, None
    for shear in np.linspace(-0.15, 0.45, 13):
        matrix = np.float32([[1, shear, -shear * height / 2], [0, 1, 0]])
        sheared = cv2.warpAffine(source, matrix, (width + int(abs(shear) * height) + 2, height),
                                 flags=cv2.INTER_NEAREST)
        profile = sheared.sum(axis=0)
        score = float((profile ** 2).sum())
        if abs(shear) < 1e-6:
            base_score = score
        if score > best_score:
            best_shear, best_score = float(shear), score
    if base_score is None or base_score <= 0 or best_score / base_score < 1.03:
        return 0.0
    return best_shear


def _ink_color(patch: np.ndarray, ink: np.ndarray, light_ink: bool) -> str | None:
    pixels = patch[ink.astype(bool)].reshape(-1, 3).astype(np.float32)
    if not len(pixels):
        return None
    sums = pixels.sum(axis=1)
    core = pixels[sums >= np.percentile(sums, 60)] if light_ink else pixels[sums <= np.percentile(sums, 40)]
    red, green, blue = (int(value) for value in np.median(core, axis=0))
    lab = cv2.cvtColor(np.uint8([[[red, green, blue]]]), cv2.COLOR_RGB2LAB)[0, 0].astype(float)
    chroma = float(np.hypot(lab[1] - 128, lab[2] - 128))
    lightness = lab[0] / 2.55
    if chroma > 18:
        return f"#{red:02X}{green:02X}{blue:02X}"
    if lightness < 40:
        return "#000000"
    if lightness > 88:
        return "#FFFFFF"
    value = int(round(lightness * 2.55))
    return f"#{value:02X}{value:02X}{value:02X}"


def _measure_ink(draft: _Draft, rgb: np.ndarray, ocr_width: int, ocr_height: int) -> None:
    """Épaisseur du trait, inclinaison et couleur de l'encre du bloc."""
    scale_y = rgb.shape[0] / max(ocr_height, 1)
    strokes, slants, colors = [], [], []
    for line in draft.lines[:6]:
        box = Box.from_pixels(line.left, line.top, line.right, line.bottom, ocr_width, ocr_height)
        patch = _patch(rgb, box)
        found = _ink_mask(patch)
        if found is None:
            continue
        ink, light_ink = found
        stroke = _stroke_ratio(ink, line.size * scale_y)
        if stroke is not None:
            strokes.append(stroke)
        slants.append(_slant(ink))
        color = _ink_color(patch, ink, light_ink)
        if color:
            colors.append(color)
    if strokes:
        draft.stroke = float(np.median(strokes))
    if slants:
        draft.italic = float(np.median(slants)) >= ITALIC_MIN_SLANT
        draft.measures["slant"] = round(float(np.median(slants)), 3)
    if colors:
        draft.color = max(set(colors), key=colors.count)


def _rule_above(rgb: np.ndarray, box: Box, limit_top: float) -> bool:
    """Filet horizontal (note de bas de page) entre `limit_top` et le haut de `box`."""
    height, width = rgb.shape[:2]
    top = int(max(limit_top, box.y - 0.05) * height)
    bottom = int(box.y * height)
    if bottom - top < 2:
        return False
    left, right = int(box.x * width), int(box.right * width)
    strip = cv2.cvtColor(rgb[top:bottom, left:right], cv2.COLOR_RGB2GRAY)
    if strip.size == 0:
        return False
    paper = max(float(np.percentile(strip, 90)), 1.0)
    ink = strip < 0.6 * paper
    for row in ink:
        if not row.any():
            continue
        runs = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
        starts, ends = np.flatnonzero(runs == 1), np.flatnonzero(runs == -1)
        if len(starts) and int((ends - starts).max()) >= 0.08 * width:
            return True
    return False


# --------------------------------------------------------------------------- illustrations


def _overlap_px(a: tuple, b: tuple) -> float:
    inter_x = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    inter_y = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return inter_x * inter_y / max((a[2] - a[0]) * (a[3] - a[1]), 1)


def _ink(gray: np.ndarray) -> np.ndarray:
    paper = max(float(np.percentile(gray, 90)), 1.0)
    return (gray < 0.6 * paper).astype(np.uint8)


def _non_paper(lab: np.ndarray, lightness: float) -> np.ndarray:
    lum = lab[..., 0]
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    neutral = lum[chroma < 12]
    paper = max(float(np.percentile(neutral if neutral.size > 100 else lum, 90)), 1.0)
    return ((lum < lightness * paper) | (chroma > 18)).astype(np.uint8)


def _big_areas(mask: np.ndarray) -> np.ndarray:
    """Retire lettres, filets fins et taches : il reste les grandes zones pleines."""
    size = max(15, mask.shape[1] // 40) | 1
    bridged = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    return cv2.morphologyEx(bridged, cv2.MORPH_OPEN, np.ones((size, size), np.uint8))


def _table_boxes(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Tableaux réglés : longues réglures horizontales et verticales qui se croisent plusieurs fois."""
    height, width = gray.shape
    ink = _ink(gray)
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, width // 12), 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, height // 25))))
    kernel = np.ones((5, 5), np.uint8)
    grid = cv2.dilate(cv2.bitwise_or(horizontal, vertical), kernel)
    crossings = cv2.bitwise_and(cv2.dilate(horizontal, kernel), cv2.dilate(vertical, kernel))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(grid, connectivity=8)
    boxes = []
    for index in range(1, count):
        x, y, box_w, box_h, _ = stats[index]
        if box_w * box_h < MIN_TABLE_AREA * height * width:
            continue
        inside = crossings[y:y + box_h, x:x + box_w] * (labels[y:y + box_h, x:x + box_w] == index)
        components, _ = cv2.connectedComponents(inside.astype(np.uint8))
        density = float(ink[y:y + box_h, x:x + box_w].mean())
        if components - 1 >= TABLE_MIN_CROSSINGS and density < MAX_TABLE_INK:
            boxes.append((int(x) + 2, int(y) + 2, int(x + box_w) - 2, int(y + box_h) - 2))
    return boxes


def _picture_boxes(rgb: np.ndarray, texts: list[tuple]) -> list[tuple[tuple[int, int, int, int], float, float, bool]]:
    """[(boîte, confiance 0..1, flou des bords, aplat)] des photos, images en couleur et aplats contenant du texte."""
    lab = cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)
    big = _big_areas(_non_paper(lab, 0.88))
    loose = _big_areas(_non_paper(lab, 0.96))
    lum = lab[..., 0]
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    smooth = cv2.medianBlur(np.clip(lum * 2.55, 0, 255).astype(np.uint8), 9)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(big, connectivity=8)
    loose_labels = cv2.connectedComponents(loose, connectivity=8)[1]
    found = []
    for index in range(1, count):
        x, y, box_w, box_h, area = stats[index]
        if area < MIN_PICTURE_AREA * big.size:
            continue
        box = (int(x), int(y), int(x + box_w), int(y + box_h))
        region = labels == index
        tones = smooth[region]
        spread = float(np.percentile(tones, 90) - np.percentile(tones, 10))
        colour = float(np.median(chroma[region]))
        holds_text = any(_overlap_px(text, box) > 0.5 for text in texts)
        flat = colour <= CHROMA_COLOURFUL and spread < TONE_SPREAD
        if flat and not holds_text:
            continue           # aplat sombre sans texte : barre noire, filet épais, texte gras dense
        touches_edge = x <= 1 or y <= 1 or x + box_w >= big.shape[1] - 1 or y + box_h >= big.shape[0] - 1
        if touches_edge and colour <= CHROMA_COLOURFUL and max(box_w / box_h, box_h / box_w) >= 4 and not holds_text:
            continue           # ombre de reliure ou bord sombre le long du cadre
        confidence = min(0.99, 0.5 + 0.25 * min(colour / CHROMA_COLOURFUL, 1.0) + 0.25 * min(spread / 120.0, 1.0))
        if colour <= CHROMA_COLOURFUL and spread < 55:
            confidence = min(confidence, 0.55)
        ids = np.unique(loose_labels[region])
        ids = ids[ids > 0]
        fuzziness = 0.0
        if ids.size:
            y0, x0 = max(0, y - box_h), max(0, x - box_w)
            ys, xs = np.nonzero(np.isin(loose_labels[y0:y + 2 * box_h, x0:x + 2 * box_w], ids))
            if xs.size:
                lx0, ly0, lx1, ly1 = xs.min() + x0, ys.min() + y0, xs.max() + 1 + x0, ys.max() + 1 + y0
                fuzziness = max((x - lx0) / box_w, (lx1 - x - box_w) / box_w, (y - ly0) / box_h,
                                (ly1 - y - box_h) / box_h, 0.0)
        found.append((box, confidence, fuzziness, colour > CHROMA_COLOURFUL and spread < TONE_SPREAD))
    return found


def _drawing_boxes(gray: np.ndarray, exclude: list, texts: list) -> list[tuple[int, int, int, int]]:
    """Grands dessins au trait : une structure d'encre connexe, étendue et peu dense."""
    height, width = gray.shape
    ink = cv2.morphologyEx(_ink(gray), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    boxes = []
    for index in range(1, count):
        x, y, box_w, box_h, area = stats[index]
        if (box_w * box_h < MIN_DRAWING_AREA * height * width or box_w < MIN_DRAWING_SIDE * width
                or box_h < MIN_DRAWING_SIDE * height):
            continue
        if area / float(box_w * box_h) >= MAX_DRAWING_FILL:
            continue
        box = (int(x), int(y), int(x + box_w), int(y + box_h))
        if any(_overlap_px(box, other) > 0.5 for other in exclude):
            continue
        inside = [text for text in texts if _overlap_px(text, box) > 0.5]
        if len(inside) >= 3 and sum((t[2] - t[0]) * (t[3] - t[1]) for t in inside) > 0.5 * box_w * box_h:
            continue           # surtout du texte (paragraphe encadré)
        boxes.append(box)
    return boxes


def _pad(box: tuple, padding_px: int, shape: tuple, texts: list) -> tuple[int, int, int, int]:
    """Agrandit une boîte de quelques pixels, sans empiéter sur le texte voisin."""
    height, width = shape
    left, top, right, bottom = box
    new_left, new_top = max(0, left - padding_px), max(0, top - padding_px)
    new_right, new_bottom = min(width, right + padding_px), min(height, bottom + padding_px)
    for t_left, t_top, t_right, t_bottom in texts:
        if _overlap_px((t_left, t_top, t_right, t_bottom), box) > 0.3:
            continue
        if t_right > new_left and t_left < new_right:
            if top >= t_bottom > new_top:
                new_top = t_bottom
            if bottom <= t_top < new_bottom:
                new_bottom = t_top
        if t_bottom > new_top and t_top < new_bottom:
            if left >= t_right > new_left:
                new_left = t_right
            if right <= t_left < new_right:
                new_right = t_left
    return new_left, new_top, new_right, new_bottom


def detect_illustrations(corrected_rgb: np.ndarray, text_boxes: list[Box]) -> list[DetectedRegion]:
    """Photos, images en couleur, aplats contenant du texte, dessins au trait et tableaux réglés.

    `text_boxes` : boîtes normalisées du texte lu avec une bonne confiance ; elles rendent une région incertaine
    quand elles la recouvrent, arrêtent l'agrandissement des boîtes et permettent de garder un aplat qui porte du
    texte (fond graphique). Le classement fond / image est fait par `analyse_page`.
    """
    rgb = corrected_rgb if corrected_rgb.ndim == 3 else cv2.cvtColor(corrected_rgb, cv2.COLOR_GRAY2RGB)
    full_h, full_w = rgb.shape[:2]
    if full_h < 16 or full_w < 16:
        return []
    small, scale = downscale(rgb, WORK_SIDE)
    small_h, small_w = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    texts = [box.to_pixels(small_w, small_h) for box in text_boxes]

    pictures = _picture_boxes(small, texts)
    tables = [table for table in _table_boxes(gray)
              if not any(_overlap_px(table, item[0]) > 0.5 for item in pictures)]
    drawings = _drawing_boxes(gray, [item[0] for item in pictures] + tables, texts)

    pad = max(1, int(round(PADDING * max(small_h, small_w))))
    found: list[tuple[ImageKind, tuple, float, float]] = []
    found += [(ImageKind.TABLE_IMAGE, box, 0.8, 0.0) for box in tables]
    found += [(ImageKind.ILLUSTRATION if flat_colour else ImageKind.PHOTO, box, confidence, fuzziness)
              for box, confidence, fuzziness, flat_colour in pictures]
    found += [(ImageKind.DRAWING, box, 0.6, 0.0) for box in drawings]

    regions = []
    for kind, box, confidence, fuzziness in found:
        reasons = []
        overlapping = [text for text in texts if _overlap_px(text, box) > 0.5]
        if kind != ImageKind.TABLE_IMAGE and overlapping:
            reasons.append(f"{len(overlapping)} zone(s) de texte dans l'illustration")
        if fuzziness > FUZZY_EDGE:
            reasons.append("bords flous : vérifier le cadrage")
        padded = box if kind == ImageKind.TABLE_IMAGE else _pad(box, pad, (small_h, small_w), texts)
        left, top, right, bottom = (value / scale for value in padded)
        regions.append(DetectedRegion(box=Box.from_pixels(left, top, right, bottom, full_w, full_h), kind=kind,
                                      confidence=round(confidence, 3), uncertain=bool(reasons),
                                      reason=" ; ".join(reasons)))
    regions.sort(key=lambda region: (region.box.y, region.box.x))
    return regions


# --------------------------------------------------------------------------- format de page


# Formats par fréquence décroissante dans l'édition : à proportions égales (A5, B5 et 17 × 24 ont presque le même
# ratio), le plus courant dont le corps estimé est plausible l'emporte.
FORMAT_PRIORITY = ["A5", "A4", "15,5 × 24 cm", "13 × 20 cm", "11 × 17,8 cm", "17 × 24 cm", "B5", "Letter",
                   "21 × 28 cm", "23 × 30 cm"]
SAME_ASPECT = 0.015


def _candidate_formats() -> list[PageFormat]:
    names = FORMAT_PRIORITY + [name for name in STANDARD_FORMATS if name not in FORMAT_PRIORITY]
    formats = [STANDARD_FORMATS[name] for name in names if name in STANDARD_FORMATS]
    formats += [PageFormat(name=f"{fmt.name} (paysage)", width_mm=fmt.height_mm, height_mm=fmt.width_mm)
                for fmt in STANDARD_FORMATS.values()]
    return formats


def detect_page_format(width_px: int, height_px: int, body_px: float | None) -> tuple[PageFormat, list[Issue]]:
    """Format courant le plus proche : ratio largeur/hauteur à ±4 % et corps estimé entre 8 et 13 pt.

    Sinon A5 avec un avertissement (le format reste modifiable par page).
    """
    aspect = width_px / max(height_px, 1)
    candidates = [fmt for fmt in _candidate_formats()
                  if abs(fmt.aspect - aspect) / fmt.aspect <= FORMAT_ASPECT_TOLERANCE]
    fallback = STANDARD_FORMATS["A5"]
    if not candidates:
        return fallback, [Issue(code="layout_format_guess", message=(
            f"Format de page non reconnu (rapport largeur/hauteur {aspect:.3f}) : A5 proposé, à vérifier."))]
    def rank(fmt: PageFormat) -> tuple[bool, int]:
        return abs(fmt.aspect - aspect) / fmt.aspect > SAME_ASPECT, candidates.index(fmt)

    if not body_px:
        best = min(candidates, key=rank)
        return best, [Issue(code="layout_format_ratio_only", severity=Severity.INFO, message=(
            f"Format {best.name} estimé d'après les proportions seules (pas de texte courant) : à vérifier."))]
    plausible = [fmt for fmt in candidates
                 if BODY_SIZE_RANGE[0] <= body_px / (width_px / fmt.width_pt) <= BODY_SIZE_RANGE[1]]
    if not plausible:
        body_pt = body_px / (width_px / fallback.width_pt)
        return fallback, [Issue(code="layout_format_guess", message=(
            f"Aucun format courant ne donne un corps de texte plausible (8–13 pt) : A5 proposé "
            f"(corps estimé {body_pt:.1f} pt), à vérifier."))]
    return min(plausible, key=rank), []


# --------------------------------------------------------------------------- types de blocs


def _weighted_median(values: list[float], weights: list[float]) -> float:
    order = np.argsort(values)
    sorted_values = np.asarray(values, float)[order]
    cumulative = np.cumsum(np.asarray(weights, float)[order])
    return float(sorted_values[np.searchsorted(cumulative, cumulative[-1] / 2)])


def _body_size_px(drafts: list[_Draft]) -> float | None:
    """Corps du texte courant : la taille qui porte le plus de caractères (hors texte sur image si possible)."""
    def chars(draft: _Draft) -> int:
        return len(draft.block.ocr_text)

    pools = [
        [d for d in drafts if not d.table and d.block.on_image is False and chars(d) >= 20 and len(d.lines) >= 2],
        [d for d in drafts if not d.table and d.block.on_image is False and chars(d) >= 20],
        [d for d in drafts if not d.table and chars(d) >= 20],
        [d for d in drafts if not d.table],
    ]
    for pool in pools:
        if pool:
            return _weighted_median([d.size_px for d in pool], [max(chars(d), 1) for d in pool])
    return None


def _page_number_value(text: str) -> str | None:
    match = PAGE_NUMBER.match(text.strip())
    return match.group(1) if match else None


def _centered_on_page(box: Box, content: Box) -> bool:
    centre = box.x + box.w / 2
    content_centre = content.x + content.w / 2
    return abs(centre - content_centre) <= 0.03 * content.w and box.x - content.x >= 0.05 * content.w


def _classify(drafts: list[_Draft], body_pt: float | None, content: Box) -> str | None:
    """Types de blocs (en place) ; renvoie le numéro de page imprimé trouvé."""
    printed = None
    for draft in drafts:
        block = draft.block
        if draft.table:
            block.kind = BlockKind.TABLE
            continue
        size = block.estimated_style.font_size_pt or 0.0
        ratio = size / body_pt if body_pt else 1.0
        text = block.ocr_text.strip()
        n_lines = len(draft.lines)
        letters = len(re.findall(r"[^\W\d_]", text))
        in_top = block.box.bottom <= EDGE_BAND
        in_bottom = block.box.y >= 1 - EDGE_BAND
        if (in_top or in_bottom) and n_lines <= 2:
            number = _page_number_value(text) if n_lines == 1 else None
            if number is not None:
                block.kind = BlockKind.PAGE_NUMBER
                printed = printed or number
                continue
            if ratio < 1.3:
                block.kind = BlockKind.HEADER if in_top else BlockKind.FOOTER
                continue
        if CAPTION_START.match(text) and n_lines <= 4:
            block.kind = BlockKind.CAPTION
            continue
        if _is_list_start(text):
            block.kind = BlockKind.LIST_ITEM
            continue
        if (not block.on_image and block.box.y > 0.55 and body_pt and ratio < NOTE_RATIO + 0.1
                and (NOTE_START.match(text) or draft.rule_above) and ratio < (0.95 if draft.rule_above else NOTE_RATIO)):
            block.kind = BlockKind.NOTE
            continue
        bold = bool(block.estimated_style.bold)
        italic = bool(block.estimated_style.italic)
        centered = _centered_on_page(block.box, content)
        short = n_lines <= 3 and len(text) <= 160 and letters >= 2
        no_period = not text.endswith((".", ";", ","))
        if short and ratio >= HEADING_1_RATIO:
            block.kind = BlockKind.HEADING_1
        elif short and ratio >= HEADING_2_RATIO and (no_period or bold or centered):
            block.kind = BlockKind.HEADING_2
        elif (short and n_lines <= 2 and no_period and ratio >= HEADING_SOFT_RATIO
              and (bold or italic or centered)):
            block.kind = BlockKind.HEADING_2
        elif short and n_lines <= 2 and len(text) <= 80 and no_period and bold and ratio >= 0.95:
            block.kind = BlockKind.HEADING_2
        else:
            block.kind = BlockKind.PARAGRAPH
    return printed


def _link_captions(drafts: list[_Draft], images: list[ImageBlock], body_pt: float | None) -> None:
    """Chaque image reçoit au plus une légende : bloc court juste dessous (sinon dessus), aussi large au plus."""
    used: set[str] = set()
    allowed = {BlockKind.PARAGRAPH, BlockKind.CAPTION, BlockKind.SPECIAL, BlockKind.HEADING_2}
    for image in sorted(images, key=lambda item: (item.box.y, item.box.x)):
        best, best_score = None, None
        for draft in drafts:
            block = draft.block
            if block.id in used or block.kind not in allowed:
                continue
            keyword = bool(CAPTION_START.match(block.ocr_text))
            inside = block.box.fraction_inside(image.box) > 0.5
            if inside and not keyword:
                continue
            inter = min(block.box.right, image.box.right) - max(block.box.x, image.box.x)
            if inter <= 0.3 * min(block.box.w, image.box.w):
                continue
            if block.box.w > image.box.w * 1.1 + 0.01 and not keyword:
                continue
            size = block.estimated_style.font_size_pt or 0.0
            if body_pt and size > body_pt * 1.05 and not keyword:
                continue
            if len(draft.lines) > 4:
                continue
            # un paragraphe ordinaire qui suit l'image n'est pas une légende : il faut un mot-clé, un corps plus
            # petit, de l'italique ou un texte court centré, et pas de retrait de première ligne
            smaller = bool(body_pt) and size <= 0.95 * body_pt
            centered_short = len(draft.lines) <= 2 and abs(block.box.x + block.box.w / 2 - (image.box.x + image.box.w / 2)) \
                <= 0.03 * image.box.w and block.box.w < 0.9 * image.box.w
            indented = len(draft.lines) >= 2 and draft.lines[0].left - draft.lines[1].left > INDENT_EM * draft.size_px
            if not keyword and (indented or not (smaller or block.estimated_style.italic or centered_short)):
                continue
            below = block.box.y - image.box.bottom
            above = image.box.y - block.box.bottom
            tolerance = -0.5 * block.box.h
            gap = below if below >= tolerance else above
            if inside:
                gap = 0.0
            elif gap < tolerance or gap > CAPTION_GAP:
                continue
            score = (0 if keyword else 1, 0 if below >= tolerance else 1, abs(gap))
            if best_score is None or score < best_score:
                best, best_score = block, score
        if best is not None:
            best.kind = BlockKind.CAPTION
            image.caption_block_id = best.id
            used.add(best.id)


# --------------------------------------------------------------------------- colonnes et ordre de lecture


def _is_marginal(block: TextBlock | ImageBlock) -> bool:
    return isinstance(block, TextBlock) and block.kind in MARGINAL_KINDS


def _column_layout(body: list[TextBlock | ImageBlock]) -> tuple[list[list[float]], dict[str, int | None]]:
    """Colonnes (étendues horizontales des blocs qui ont un voisin côte à côte) et colonne de chaque bloc
    (None : bloc pleine largeur qui sépare des bandes)."""
    columns: list[list[float]] = []
    if not body:
        return columns, {}
    content_left = min(block.box.x for block in body)
    content_width = max(max(block.box.right for block in body) - content_left, 1e-6)

    def side_by_side(a: Box, b: Box) -> bool:
        vertical = min(a.bottom, b.bottom) - max(a.y, b.y)
        disjoint = a.right <= b.x or b.right <= a.x
        return disjoint and vertical > 0.3 * min(a.h, b.h)

    columnar = [block for block in body if block.box.w < FULL_WIDTH * content_width
                and any(side_by_side(block.box, other.box) for other in body if other is not block)]
    for block in sorted(columnar, key=lambda item: item.box.x):
        if columns and block.box.x < columns[-1][1]:
            columns[-1][1] = max(columns[-1][1], block.box.right)
        else:
            columns.append([block.box.x, block.box.right])

    assignment: dict[str, int | None] = {}
    for block in body:
        box = block.box
        if not columns:
            assignment[block.id] = 0
            continue
        if box.w >= FULL_WIDTH * content_width:
            assignment[block.id] = None
            continue
        hits = [index for index, (left, right) in enumerate(columns)
                if min(right, box.right) - max(left, box.x) > 0.15 * min(right - left, box.w)]
        if len(hits) > 1:
            assignment[block.id] = None
        elif hits:
            assignment[block.id] = hits[0]
        else:
            centre = box.x + box.w / 2
            assignment[block.id] = int(np.argmin([abs(centre - (left + right) / 2) for left, right in columns]))
    return columns, assignment


def _order(blocks: list[TextBlock | ImageBlock]) -> tuple[list[str], int, list[list[float]], dict[str, int | None]]:
    edges = [block for block in blocks if _is_marginal(block)]
    body = [block for block in blocks if not _is_marginal(block)]
    head = sorted((b for b in edges if b.box.y + b.box.h / 2 < 0.5), key=lambda b: (b.box.y, b.box.x))
    foot = sorted((b for b in edges if b.box.y + b.box.h / 2 >= 0.5), key=lambda b: (b.box.y, b.box.x))
    columns, assignment = _column_layout(body)
    ordered = [block.id for block in head]
    band: list[TextBlock | ImageBlock] = []

    def flush() -> None:
        ordered.extend(block.id for block in sorted(band, key=lambda b: (assignment[b.id], b.box.y, b.box.x)))
        band.clear()

    for block in sorted(body, key=lambda b: (b.box.y, b.box.x, b.id)):
        if assignment.get(block.id) is None:
            flush()
            ordered.append(block.id)
        else:
            band.append(block)
    flush()
    ordered += [block.id for block in foot]
    return ordered, max(1, len(columns)), columns, assignment


def reading_order(blocks: list[TextBlock | ImageBlock]) -> list[str]:
    """Identifiants dans l'ordre de lecture : en-têtes, puis bandes pleine largeur et zones en colonnes
    (colonnes de gauche à droite, blocs de haut en bas), pieds et numéros en dernier."""
    return _order(blocks)[0]


# --------------------------------------------------------------------------- styles


def _alignment(lines: list[_Line], column: tuple[float, float]) -> Alignment:
    lefts = np.array([line.left for line in lines], float)
    rights = np.array([line.right for line in lines], float)
    col_left, col_right = column
    col_width = max(col_right - col_left, 1.0)
    em = float(np.median([line.size for line in lines]))
    if len(lines) >= 3:
        body_left, body_right = lefts[1:-1], rights[:-1]
        tolerance = max(0.02 * col_width, 0.3 * em)
        left_even = body_left.std() <= tolerance if len(body_left) else True
        right_even = body_right.std() <= tolerance
        centres = (lefts + rights) / 2
        if centres[1:].std() <= tolerance and lefts[1:].std() > max(2 * centres[1:].std(), 0.2 * em):
            return Alignment.CENTER                       # bords irréguliers des deux côtés, centres alignés
        if left_even and right_even:
            return Alignment.JUSTIFY if _gap_variation(lines[:-1]) > JUSTIFY_GAP_CV else Alignment.LEFT
        if left_even:
            return Alignment.LEFT
        if right_even:
            return Alignment.RIGHT
    tolerance = max(0.03 * col_width, 0.5 * em)
    centres = (lefts + rights) / 2
    if np.all(np.abs(centres - (col_left + col_right) / 2) <= tolerance) and np.all(lefts - col_left > 2 * tolerance):
        return Alignment.CENTER
    if len(lines) == 2 and np.all(np.abs(lefts[1:] - col_left) <= tolerance) \
            and abs(rights[0] - col_right) <= tolerance and abs(rights[1] - col_right) <= tolerance \
            and _gap_variation(lines) > JUSTIFY_GAP_CV:
        return Alignment.JUSTIFY
    if np.all(np.abs(rights - col_right) <= tolerance) and np.all(lefts - col_left > 2 * tolerance):
        return Alignment.RIGHT
    return Alignment.LEFT


def _gap_variation(lines: list[_Line]) -> float:
    """Coefficient de variation de l'espace médian entre mots, ligne à ligne (élevé dans un texte justifié)."""
    gaps = []
    for line in lines:
        ordered = sorted(line.words, key=lambda word: word.left)
        spaces = [b.left - a.right for a, b in zip(ordered, ordered[1:]) if b.left > a.right]
        if len(spaces) >= 2:
            gaps.append(float(np.median(spaces)))
    if len(gaps) < 2:
        return 0.0
    return float(np.std(gaps) / max(np.mean(gaps), 1e-6))


def _line_spacing(draft: _Draft) -> float | None:
    if len(draft.lines) < 2 or draft.table:
        return None
    bases = [line.baseline() for line in draft.lines]
    pitch = float(np.median(np.diff(bases)))
    if pitch <= 0:
        return None
    return float(min(max(round(pitch / draft.size_px / SINGLE_LINE_EM, 2), 0.6), 4.0))


def _first_line_indent(draft: _Draft, px_per_pt: float, alignment: Alignment) -> float | None:
    if len(draft.lines) < 2 or alignment in (Alignment.CENTER, Alignment.RIGHT) or draft.table:
        return None
    others = float(np.median([line.left for line in draft.lines[1:]]))
    indent_px = draft.lines[0].left - others
    if abs(indent_px) < 0.3 * draft.size_px:
        return 0.0
    return float(min(max(round(indent_px / px_per_pt, 1), -200.0), 500.0))


def _apply_spacing(ordered: list[TextBlock | ImageBlock], drafts_by_id: dict[str, _Draft], px_per_pt: float,
                   assignment: dict[str, int | None]) -> None:
    """Espacement supplémentaire entre deux blocs successifs d'une même colonne (au-delà de l'interligne)."""
    body = [block for block in ordered if not _is_marginal(block)]
    for previous, current in zip(body, body[1:]):
        if not (isinstance(previous, TextBlock) and isinstance(current, TextBlock)):
            continue
        if previous.on_image != current.on_image or current.box.y <= previous.box.bottom - 1e-4:
            continue
        if assignment.get(previous.id) != assignment.get(current.id) and \
                None not in (assignment.get(previous.id), assignment.get(current.id)):
            continue
        before, after = drafts_by_id[previous.id], drafts_by_id[current.id]
        pitch_em = (current.estimated_style.line_spacing or 1.05) * SINGLE_LINE_EM
        expected = pitch_em * after.size_px
        extra_px = (after.lines[0].baseline() - before.lines[-1].baseline()) - expected
        extra_pt = max(0.0, round(extra_px / px_per_pt, 1))
        extra_pt = min(extra_pt, 500.0)
        if current.kind in (BlockKind.HEADING_1, BlockKind.HEADING_2):
            current.estimated_style = current.estimated_style.model_copy(update={"space_before_pt": extra_pt})
        else:
            previous.estimated_style = previous.estimated_style.model_copy(update={"space_after_pt": extra_pt})


# --------------------------------------------------------------------------- analyse complète


def _region_of(box: Box, regions: list[Box]) -> int:
    best, best_fraction = -1, 0.5
    for index, region in enumerate(regions):
        fraction = box.fraction_inside(region)
        if fraction >= best_fraction:
            best, best_fraction = index, fraction
    return best


def _line_box(line: _Line, width: int, height: int) -> Box:
    return Box.from_pixels(line.left, line.top, line.right, line.bottom, width, height)


def _build_block(draft: _Draft, width: int, height: int, low: float, language: str | None) -> TextBlock:
    ocr_lines = []
    for line in draft.lines:
        known = [word.confidence for word in line.words if word.confidence >= 0]
        ocr_lines.append(OcrLine(text=line.text, box=_line_box(line, width, height),
                                 confidence=round(float(np.mean(known)), 1) if known else -1,
                                 words=[word.source for word in line.words]))
    if draft.rows:
        text = "\n".join("\t".join(piece.text for piece in row) for row in draft.rows)
    else:
        text = _join_lines([line.text for line in draft.lines])
    known = [word.confidence for word in draft.words if word.confidence >= 0]
    confidence = round(float(np.mean(known)), 1) if known else -1
    weak = any(0 <= word.confidence < low for word in draft.words)
    box = Box.from_pixels(draft.left, draft.top, draft.right, draft.bottom, width, height)
    return TextBlock(box=box, ocr_text=text, lines=ocr_lines, confidence=confidence,
                     needs_review=(0 <= confidence < low) or weak or draft.table, language=language)


def _language(text: str, languages: list[str]) -> str | None:
    if not languages:
        return None
    if len(languages) == 1:
        return languages[0]
    lowered = text.lower()
    scores = {
        "fra": len(re.findall(r"\b(?:le|la|les|des|une|et|est|dans|du|que|qui|pour|sur|avec)\b", lowered))
        + 2 * len(re.findall(r"[éèêàçùœâîôû]", lowered)),
        "eng": len(re.findall(r"\b(?:the|and|of|to|in|is|was|for|with|that|this)\b", lowered)),
        "deu": len(re.findall(r"\b(?:der|die|das|und|ist|nicht|mit|von|den|ein|eine)\b", lowered))
        + 2 * len(re.findall(r"[äöüß]", lowered)),
    }
    candidates = [(scores.get(lang, 0), -index, lang) for index, lang in enumerate(languages)]
    best = max(candidates)
    return best[2] if best[0] > 0 else languages[0]


def analyse_page(ocr: OcrResult, corrected_rgb: np.ndarray, settings: LayoutSettings) -> LayoutResult:
    """Blocs typés, ordonnés et stylés d'une page ; images (sans fichier), fonds, catégorie, format."""
    rgb = corrected_rgb if corrected_rgb.ndim == 3 else cv2.cvtColor(corrected_rgb, cv2.COLOR_GRAY2RGB)
    width, height = max(ocr.width, 1), max(ocr.height, 1)
    issues: list[Issue] = []
    low = settings.low_confidence

    words = _words_px(ocr)
    lines = _split_lines(words) if words else []
    reliable = [_line_box(line, width, height) for line in lines
                if not any(0 <= word.confidence < low for word in line.words) or len(line.text) >= 4]
    regions = detect_illustrations(rgb, reliable)

    # Fonds : grandes zones non-papier qui portent du texte. Les autres régions sont des images.
    region_boxes = [region.box for region in regions]
    for line in lines:
        line.zone = _region_of(_line_box(line, width, height), region_boxes)
    holds_text = {line.zone for line in lines if line.zone >= 0}
    background_ids = {index for index, region in enumerate(regions)
                      if region.kind != ImageKind.TABLE_IMAGE and index in holds_text
                      and region.box.area >= BACKGROUND_MIN_AREA}
    table_ids = {index for index, region in enumerate(regions) if region.kind == ImageKind.TABLE_IMAGE}

    # Tableaux réglés : tout le texte d'une région « tableau » forme un bloc TABLE.
    drafts: list[_Draft] = []
    table_lines = {index: [line for line in lines if line.zone == index] for index in table_ids}
    for index, members in table_lines.items():
        if members:
            drafts.append(_Draft(lines=sorted(members, key=lambda l: (l.top, l.left)), zone=index, table=True,
                                 rows=_table_rows(members)))
    free = [line for line in lines if line.zone not in table_ids]
    content_width = (max((l.right for l in free), default=1.0) - min((l.left for l in free), default=0.0)) or 1.0
    for members in _aligned_tables(free, content_width):
        drafts.append(_Draft(lines=sorted(members, key=lambda l: (l.top, l.left)), table=True,
                             rows=_table_rows(members)))
    in_tables = {id(line) for draft in drafts for line in draft.lines}
    free = [line for line in free if id(line) not in in_tables]
    usual = _usual_pitch(free)
    drafts += [_Draft(lines=group, zone=group[0].zone) for group in _paragraphs(free, usual)]

    # Construction des blocs ; les taches sans caractère alphanumérique hors image sont écartées.
    kept: list[_Draft] = []
    removed = 0
    for draft in drafts:
        draft.block = _build_block(draft, width, height, low, None)
        on_image = draft.zone >= 0 and draft.zone not in table_ids
        draft.block.on_image = on_image
        text = draft.block.ocr_text
        if not on_image and not re.search(r"[^\W_]", text) and 0 <= draft.block.confidence < NOISE_CONFIDENCE:
            removed += 1
            continue
        draft.block.language = _language(text, settings.languages)
        draft.size_px = float(np.median([line.size for line in draft.lines]))
        if draft.block.on_image and 0 <= draft.block.confidence < low:
            draft.block.needs_review = True
        kept.append(draft)
    drafts = kept
    if removed:
        issues.append(Issue(code="layout_noise_removed", severity=Severity.INFO,
                            message=f"{removed} tache(s) sans texte lisible ignorée(s)."))

    # Format et tailles en points ; signes isolés minuscules sans lettre ni chiffre (taches lues) écartés
    body_px = _body_size_px(drafts)
    if body_px:
        specks = [d for d in drafts if not d.block.on_image and not re.search(r"[^\W_]", d.block.ocr_text)
                  and len(d.block.ocr_text) <= 2 and d.size_px < 0.5 * body_px]
        if specks:
            drafts = [d for d in drafts if d not in specks]
            issues.append(Issue(code="layout_noise_removed", severity=Severity.INFO,
                                message=f"{len(specks)} signe(s) isolé(s) trop petit(s) ignoré(s)."))
    page_format, format_issues = detect_page_format(rgb.shape[1], rgb.shape[0],
                                                    body_px * rgb.shape[1] / width if body_px else None)
    issues += format_issues
    px_per_pt = width / page_format.width_pt
    body_pt = round(body_px / px_per_pt * 2) / 2 if body_px else None      # corps : au demi-point près

    for draft in drafts:
        _measure_ink(draft, rgb, width, height)
    body_strokes = [d.stroke for d in drafts if d.stroke and not d.table and len(d.block.ocr_text) >= 20
                    and abs(d.size_px - (body_px or d.size_px)) <= 0.25 * d.size_px]
    body_stroke = float(np.median(body_strokes)) if body_strokes else None
    for draft in drafts:
        bold = None
        if draft.stroke is not None and len(re.findall(r"[^\W\d_]", draft.block.ocr_text)) >= 3:
            bold = draft.stroke >= BOLD_RATIO * body_stroke if body_stroke else draft.stroke >= BOLD_ABSOLUTE
            draft.measures["stroke"] = round(draft.stroke, 4)
        size_pt = float(min(max(round(draft.size_px / px_per_pt, 1), 1.0), 400.0))
        if body_pt and abs(size_pt - body_pt) <= BODY_SNAP * body_pt:
            size_pt = body_pt              # bruit de mesure (±5 %) : le texte courant d'un livre a un seul corps
        draft.block.estimated_style = TextStyle(font_size_pt=size_pt, bold=bold, italic=draft.italic,
                                                color=draft.color, line_spacing=_line_spacing(draft))

    # Images (hors fonds)
    image_blocks = [ImageBlock(kind=region.kind, box=region.box, confidence=region.confidence,
                               uncertain=region.uncertain)
                    for index, region in enumerate(regions)
                    if index not in background_ids and not (index in table_ids and table_lines.get(index))]
    background_regions = [regions[index].box for index in sorted(background_ids)]

    # Zone utile provisoire, filets de notes, types et légendes
    body_boxes = [d.block.box for d in drafts if not d.block.on_image] + [image.box for image in image_blocks]
    content = _union(body_boxes) or Box(x=0, y=0, w=1, h=1)
    for draft in drafts:
        above = [other.block.box.bottom for other in drafts
                 if other is not draft and other.block.box.bottom <= draft.block.box.y]
        draft.rule_above = (not draft.block.on_image and draft.block.box.y > 0.55
                            and _rule_above(rgb, draft.block.box, max(above, default=0.0)))
    printed = _classify(drafts, body_pt, content)
    _link_captions(drafts, image_blocks, body_pt)

    # Ordre de lecture, colonnes, styles dépendant de la colonne
    texts = [draft.block for draft in drafts]
    all_blocks: list[TextBlock | ImageBlock] = [*texts, *image_blocks]
    ordered_ids, columns, column_extents, assignment = _order(all_blocks)
    rank = {block_id: index for index, block_id in enumerate(ordered_ids)}
    for block in all_blocks:
        block.order = rank[block.id]
    drafts_by_id = {draft.block.id: draft for draft in drafts}
    body_blocks = [b for b in all_blocks if not _is_marginal(b)]
    page_extent = ((min(b.box.x for b in body_blocks), max(b.box.right for b in body_blocks))
                   if body_blocks else (0.0, 1.0))
    for draft in drafts:
        block = draft.block
        column_index = assignment.get(block.id)
        left, right = (column_extents[column_index] if column_index is not None and column_extents
                       and column_index < len(column_extents) else page_extent)
        if block.on_image or _is_marginal(block):
            left, right = (min(left, block.box.x), max(right, block.box.right))
        alignment = _alignment(draft.lines, (left * width, right * width))
        indent = _first_line_indent(draft, px_per_pt, alignment)
        block.estimated_style = block.estimated_style.model_copy(
            update={"alignment": alignment, "first_line_indent_pt": indent})
        if (block.kind == BlockKind.PARAGRAPH and not block.on_image and len(draft.lines) >= 2
                and alignment != Alignment.CENTER):
            span = max(right - left, 1e-6)
            if block.box.x - left >= 0.08 * span and right - block.box.right >= 0.08 * span:
                block.kind = BlockKind.SPECIAL            # citation ou encadré en retrait des deux côtés
    ordered_blocks = sorted(all_blocks, key=lambda b: b.order)
    _apply_spacing(ordered_blocks, drafts_by_id, px_per_pt, assignment)

    category = _category(texts, image_blocks, background_regions)
    known = [word.confidence for d in drafts for word in d.words if word.confidence >= 0]
    mean_confidence = round(float(np.mean(known)), 1) if known else 0.0
    issues += _final_issues(texts, mean_confidence, low, image_blocks)
    content_box = _union([b.box for b in all_blocks if not _is_marginal(b)]) or _union([b.box for b in all_blocks])

    text_blocks = sorted(texts, key=lambda b: b.order)
    image_blocks = sorted(image_blocks, key=lambda b: b.order)
    return LayoutResult(text_blocks=text_blocks, image_blocks=image_blocks, category=category, columns=columns,
                        content_box=content_box, printed_page_number=printed,
                        background_regions=background_regions, detected_format=page_format,
                        body_font_size_pt=body_pt, mean_confidence=mean_confidence, issues=issues)


def _union(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    return Box.from_edges(min(b.x for b in boxes), min(b.y for b in boxes),
                          max(b.right for b in boxes), max(b.bottom for b in boxes))


def _category(texts: list[TextBlock], images: list[ImageBlock], backgrounds: list[Box]) -> PageCategory:
    """A texte, B fond graphique, C photo + texte, texte sur image, ou mixte si plusieurs cas se combinent."""
    pictures = [image for image in images if image.kind != ImageKind.TABLE_IMAGE]
    picture_area = sum(image.box.area for image in pictures)
    on_picture = any(block.on_image and any(block.box.fraction_inside(image.box) >= 0.5 for image in pictures)
                     for block in texts)
    outside = [block for block in texts if not block.on_image and block.kind not in MARGINAL_KINDS]
    flags = []
    if backgrounds:
        flags.append(PageCategory.GRAPHIC_BACKGROUND)
    if picture_area >= IMAGE_TEXT_AREA and outside:
        flags.append(PageCategory.PHOTO_TEXT)
    if on_picture:
        flags.append(PageCategory.TEXT_ON_IMAGE)
    if not flags:
        return PageCategory.PHOTO_TEXT if picture_area >= IMAGE_TEXT_AREA else PageCategory.TEXT
    return flags[0] if len(flags) == 1 else PageCategory.MIXED


def _final_issues(texts: list[TextBlock], mean_confidence: float, low: float, images: list[ImageBlock]) -> list[Issue]:
    issues = []
    if not texts:
        issues.append(Issue(code="layout_no_text", message="Aucun texte détecté sur cette page."))
        return issues
    body = [block for block in texts if block.kind not in MARGINAL_KINDS]
    line_count = sum(len(block.lines) for block in body)
    if len(body) == 1 and line_count >= 8:
        issues.append(Issue(code="layout_single_block", block_id=body[0].id, message=(
            "Toute la page a été regroupée en un seul bloc : vérifiez le découpage en paragraphes.")))
    if mean_confidence < low:
        issues.append(Issue(code="layout_low_confidence", message=(
            f"Confiance OCR moyenne faible ({mean_confidence:.0f} %) : relecture conseillée.")))
    review = sum(1 for block in texts if block.needs_review)
    if review:
        issues.append(Issue(code="layout_review_blocks", severity=Severity.INFO,
                            message=f"{review} bloc(s) de texte à vérifier (mots peu sûrs)."))
    for block in texts:
        if block.kind == BlockKind.TABLE:
            issues.append(Issue(code="layout_table", block_id=block.id, message=(
                "Tableau détecté : vérifiez le découpage des cellules.")))
    for image in images:
        if image.uncertain:
            issues.append(Issue(code="layout_image_uncertain", severity=Severity.INFO, block_id=image.id,
                                message="Image détectée avec incertitude : vérifiez son cadrage."))
    return issues


# --------------------------------------------------------------------------- fusion avec les corrections de l'utilisateur


def _user_touched(block: TextBlock | ImageBlock) -> bool:
    if block.user_created or block.user_modified:
        return True
    if isinstance(block, TextBlock):
        return block.corrected_text is not None or bool(block.style_override.defined_fields()) or block.hidden
    return block.hidden


def _overlaps(a: Box, b: Box) -> bool:
    return a.fraction_inside(b) >= 0.5 or b.fraction_inside(a) >= 0.5


def merge_layout(previous: Page, fresh: LayoutResult) -> Page:
    """Copie de `previous` avec la nouvelle analyse ; les blocs créés ou modifiés par l'utilisateur sont gardés.

    Un bloc est conservé s'il est `user_created`, `user_modified`, corrigé (`corrected_text`), masqué ou porteur
    d'une surcharge de style. Les blocs frais qui recouvrent un bloc conservé sont écartés (pas de doublon) ;
    une légende écartée est remplacée par le bloc conservé correspondant. L'ordre de lecture est recalculé.
    `category_override`, `format_override`, `style_rules` et `issues` ne sont pas modifiés.
    """
    page = previous.model_copy(deep=True)
    kept_texts = [block for block in page.text_blocks if _user_touched(block)]
    kept_images = [block for block in page.image_blocks if _user_touched(block)]
    remap: dict[str, str] = {}
    new_texts = []
    for block in fresh.text_blocks:
        twin = next((kept for kept in kept_texts if _overlaps(block.box, kept.box)), None)
        if twin is not None:
            remap[block.id] = twin.id
            continue
        new_texts.append(block.model_copy(deep=True))
    new_images = [image.model_copy(deep=True) for image in fresh.image_blocks
                  if not any(_overlaps(image.box, kept.box) for kept in kept_images)]
    for image in new_images:
        if image.caption_block_id in remap:
            image.caption_block_id = remap[image.caption_block_id]
    page.text_blocks = [*kept_texts, *new_texts]
    page.image_blocks = [*kept_images, *new_images]
    rank = {block_id: index for index, block_id in enumerate(reading_order([*page.text_blocks, *page.image_blocks]))}
    for block in [*page.text_blocks, *page.image_blocks]:
        block.order = rank[block.id]
    page.text_blocks.sort(key=lambda b: b.order)
    page.image_blocks.sort(key=lambda b: b.order)
    page.category = fresh.category
    page.columns = fresh.columns
    page.content_box = fresh.content_box
    page.printed_page_number = fresh.printed_page_number
    if page.background_regions != fresh.background_regions:
        page.background_files = []           # fichiers de fond à régénérer (processing_service)
    page.background_regions = list(fresh.background_regions)
    page.detected_format = fresh.detected_format
    page.body_font_size_pt = fresh.body_font_size_pt
    page.mean_confidence = fresh.mean_confidence
    return page
