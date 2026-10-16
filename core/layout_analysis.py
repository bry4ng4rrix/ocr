"""Blocks of a page from the OCR words and the detected illustrations: types, styles, reading order.

Pipeline (`analyse_layout`):
1. Tesseract lines are split at wide horizontal gaps (Tesseract sometimes runs a line across two
   columns), then the pieces are grouped into paragraphs: same column, regular line pitch, similar
   size, and no Tesseract paragraph break.
2. Text read inside an illustration: dropped when its confidence is low (picture texture read as
   letters), kept and flagged otherwise. Text inside a ruled table becomes one TABLE block.
3. Styles: font size from the height of each word, corrected by which letters it contains
   (capitals, ascenders, descenders, accented capitals), bold from the relative stroke thickness,
   ink colour, alignment, line spacing.
4. Types by explainable rules (title, subtitle, heading, list, caption, header, footer, page
   number); suggestions the user can correct.
5. Reading order: running heads first, then bands split by blocks spanning the columns, columns
   left to right within a band, footers and page numbers last.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import cv2
import numpy as np

from .coordinates import overlap_ratio, px_to_norm
from .document_models import (Alignment, BlockType, Box, ImageBlock, Margins, OCRLine, OCRWord,
                              ProcessingSettings, TextBlock)
from .illustration_extraction import DetectedRegion
from .ocr_engine import OCRResult, OCRWordPx

# Glyph extents in em (share of the font size), measured on Liberation Serif / Sans and checked by
# tests/test_layout.py::test_font_size_calibration on drawn text of known size.
EM_X_HEIGHT = 0.46       # a, c, e, m, o...
EM_ASCENDER = 0.69       # capitals, digits, b d f h k l t, accented lower case, i j dots
EM_ACCENT_LOWER = 0.73   # é, â, è...: accent above a lower-case letter (a little above the ascenders)
EM_ACCENT_CAP = 0.86     # É, À, Ê...: accent above a capital
EM_DESCENDER = 0.215     # g j p q y ç, comma

LINE_SPLIT_GAP = 1.6     # horizontal gap (in line heights) that splits a Tesseract line in two
PARAGRAPH_PITCH = 1.4    # baseline pitch, relative to the page's usual pitch, that starts a new paragraph
SIZE_JUMP = 1.4          # height ratio between two lines that starts a new paragraph
EDGE_BAND = 0.09         # running heads, footers and page numbers lie in this share of the top / bottom
HEADING_RATIO = 1.3      # font size, relative to the body text, from which a short block is a heading
BOLD_RATIO = 1.4         # stroke / size, relative to the body text, from which a block is bold
BOLD_ABSOLUTE = 0.07     # stroke / size when the page has no body text to compare with
JUSTIFY_GAP_CV = 0.08    # variation of the word spacing between lines above which text is justified
CAPTION_GAP = 0.05       # a caption starts within this share of the page height from its picture
NOISE_CONFIDENCE = 35    # blocks under this confidence with few letters are specks, not text
FULL_WIDTH = 0.6         # share of the content width from which a block separates bands

LIST_MARKER = re.compile(r"^\s*(?:[•·▪●○◦■□\-–—*]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|[ivxlcIVXLC]{1,5}[.)])\s+\S")
CAPTION_START = re.compile(r"^\s*(?:fig(?:ure)?s?\.?|illustrations?|planches?|gravures?|cartes?|photos?|"
                           r"photographies?|pl\.|tableau|table|plate|map)\b", re.IGNORECASE)
# Page numbers: digits, or a plausible roman numeral up to 399 (no D or M, one case): "mix",
# "civil" or "dim" are words, not numbers.
ROMAN = r"(?:c{0,3}(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3}))"
PAGE_NUMBER = re.compile(rf"^[\W_]{{0,3}}(?:[pP](?:age|AGE|\.)\s*)?(\d{{1,4}}|(?=[cxlvi]){ROMAN}|(?=[CXLVI]){ROMAN.upper()})[\W_]{{0,3}}$")

DESCENDERS = set("gjpqyçÇ,;()[]{}|/µ")
ACCENT_CAPS = set("ÀÁÂÃÄÅÈÉÊËÌÍÎÏÒÓÔÕÖÙÚÛÜÝŸÑ")
ACCENT_LOWER = set("àáâãäåèéêëìíîïòóôõöùúûüýÿñ")
TALL = set("bdfhkltijABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789œŒæÆ'\"!?«»#%&@$£€§/\\()[]{}|")
FRENCH_WORDS = {"le", "la", "les", "des", "une", "un", "et", "est", "dans", "du", "de", "que", "qui", "il", "elle",
                "pour", "sur", "au", "aux", "pas", "ne", "se", "son", "sa", "ses", "avec", "par", "à", "où", "était"}
ENGLISH_WORDS = {"the", "and", "of", "to", "in", "is", "was", "for", "with", "that", "it", "on", "as", "by",
                 "from", "at", "a", "an", "they", "them", "this", "be", "or", "were", "their", "his", "her"}


@dataclass
class LayoutResult:
    blocks: list = field(default_factory=list)          # TextBlock and ImageBlock (image_path="")
    columns: int = 1
    margins: Margins = field(default_factory=Margins)
    printed_page_number: str = ""
    warnings: list[str] = field(default_factory=list)
    mean_confidence: float = -1.0


@dataclass
class _Line:
    words: list[OCRWordPx]
    key: tuple | None               # Tesseract (block, par) when trustworthy, else None
    cells: list[str] | None = None  # table rows: text of each cell, left to right

    @property
    def left(self) -> int:
        return min(w.left for w in self.words)

    @property
    def right(self) -> int:
        return max(w.right for w in self.words)

    @property
    def top(self) -> int:
        return min(w.top for w in self.words)

    @property
    def bottom(self) -> int:
        return max(w.bottom for w in self.words)

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def size_px(self) -> float | None:
        return _font_size_px(self.words)

    def baseline(self, size: float) -> float:
        flat = [w.bottom for w in self.words if not (set(w.text) & DESCENDERS)]
        return float(np.median(flat)) if flat else self.bottom - EM_DESCENDER * size


# --------------------------------------------------------------------------- measurements

def _word_em(text: str) -> float | None:
    """Expected height of a word's ink, in em, from the letters it contains."""
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
        return None        # x-height only ("une", "on"): too few pixels to measure reliably
    return top + bottom


def _font_size_px(words: list[OCRWordPx]) -> float | None:
    """Median font size (em) in pixels from the word heights."""
    sizes = []
    for w in words:
        if len(re.sub(r"\W", "", w.text)) < 1:
            continue
        em = _word_em(w.text)
        if em:
            sizes.append(w.height / em)
    return float(np.median(sizes)) if sizes else None


def _alignment(lines: list[_Line], column: tuple[float, float]) -> tuple[Alignment, str]:
    """Alignment from the line edges; single lines are compared with the column they sit in."""
    lefts = np.array([l.left for l in lines], float)
    rights = np.array([l.right for l in lines], float)
    width = max(rights.max() - lefts.min(), 1.0)
    col_left, col_right = column
    col_width = max(col_right - col_left, 1.0)
    if len(lines) >= 3:
        body_l, body_r = lefts[:-1], rights[:-1]
        tol = 0.02 * width
        left_even, right_even = body_l.std() <= tol, body_r.std() <= tol
        if left_even and right_even:
            if _gap_variation(lines[:-1]) > JUSTIFY_GAP_CV:
                return Alignment.JUSTIFY, "bords réguliers, espaces entre mots variables"
            return Alignment.LEFT, "bords réguliers, espaces entre mots constants (fer à gauche)"
        if left_even:
            return Alignment.LEFT, "bord gauche régulier"
        if right_even:
            return Alignment.RIGHT, "bord droit régulier"
        if ((body_l + body_r) / 2).std() <= tol:
            return Alignment.CENTER, "lignes centrées"
    # Short block: compare with the column.
    tol = 0.03 * col_width
    centre = (lefts + rights) / 2
    if np.all(np.abs(centre - (col_left + col_right) / 2) <= tol) and np.all(lefts - col_left > 2 * tol):
        return Alignment.CENTER, "centré dans la colonne"
    if np.all(np.abs(lefts - col_left) <= tol):
        if len(lines) >= 2 and np.all(np.abs(rights[:-1] - col_right) <= tol) \
                and _gap_variation(lines[:-1]) > JUSTIFY_GAP_CV:
            return Alignment.JUSTIFY, "aligné sur les deux bords de la colonne"
        return Alignment.LEFT, "aligné sur le bord gauche de la colonne"
    if np.all(np.abs(rights - col_right) <= tol):
        return Alignment.RIGHT, "aligné sur le bord droit de la colonne"
    return Alignment.LEFT, "indéterminé (gauche par défaut)"


def _gap_variation(lines: list[_Line]) -> float:
    """Coefficient of variation of the per-line median word space (high in justified text)."""
    gaps = []
    for line in lines:
        ws = sorted(line.words, key=lambda w: w.left)
        g = [b.left - a.right for a, b in zip(ws, ws[1:]) if b.left > a.right]
        if len(g) >= 2:
            gaps.append(float(np.median(g)))
    if len(gaps) < 1:
        return 0.0
    if len(gaps) == 1:
        return 0.0
    return float(np.std(gaps) / max(np.mean(gaps), 1e-6))


def _ink_measures(rgb: np.ndarray, box_px: tuple[int, int, int, int], size_px: float) -> dict:
    """Stroke thickness (share of the font size) and ink colour of a text box of the frame."""
    l, t, r, b = box_px
    patch = rgb[max(0, t):max(t + 1, b), max(0, l):max(l + 1, r)]
    if patch.shape[0] < 4 or patch.shape[1] < 4:
        return {}
    gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
    paper = float(np.percentile(gray, 90))
    darkest = float(np.percentile(gray, 1))
    ink = (gray < (paper + darkest) / 2).astype(np.uint8)
    if ink.sum() < 30 or paper - darkest < 40:
        return {}
    out = {}
    distance = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    ridge = distance[(distance >= cv2.dilate(distance, np.ones((3, 3), np.uint8))) & (distance > 0)]
    if ridge.size and size_px > 0:
        # A stroke w pixels wide has a ridge distance of about w / 2 + 0.5 (pixel centres).
        out["stroke"] = round(max(2.0 * float(np.mean(ridge)) - 1.0, 0.1) / size_px, 4)
    pixels = patch[ink.astype(bool)].astype(np.float32)
    sums = pixels.sum(axis=1)
    dark = pixels[sums <= np.percentile(sums, 40)]
    if len(dark):
        r_, g_, b_ = (int(v) for v in np.median(dark, axis=0))
        lab = cv2.cvtColor(np.uint8([[[r_, g_, b_]]]), cv2.COLOR_RGB2LAB)[0, 0].astype(float)
        chroma = float(np.hypot(lab[1] - 128, lab[2] - 128))
        lightness = lab[0] / 2.55
        out["ink_rgb"] = f"#{r_:02x}{g_:02x}{b_:02x}"
        out["ink_chroma"] = round(chroma, 1)
        if chroma > 18:
            out["color"] = out["ink_rgb"]                 # clearly coloured ink
        elif lightness < 40:
            out["color"] = "#000000"                      # black with a cast or printed grey
        else:
            v = int(round(lightness * 2.55))
            out["color"] = f"#{v:02x}{v:02x}{v:02x}"       # light grey ink
    return out


def _language(text: str, languages: list[str]) -> str:
    if not languages:
        return ""
    if "fra" in languages and "eng" in languages:
        tokens = re.findall(r"[^\W\d_]+", text.lower())
        fr = sum(t in FRENCH_WORDS for t in tokens) + 2 * len(re.findall(r"[éèêàçùœâîôû]", text.lower()))
        en = sum(t in ENGLISH_WORDS for t in tokens)
        if fr or en:
            return "fra" if fr >= en else "eng"
    return languages[0]


# --------------------------------------------------------------------------- lines and paragraphs

def _split_lines(words: list[OCRWordPx]) -> list[_Line]:
    """Tesseract lines, split where a horizontal gap is far wider than a word space."""
    by_line: dict[tuple, list[OCRWordPx]] = {}
    for w in words:
        by_line.setdefault((w.block, w.par, w.line), []).append(w)
    pieces: list[tuple[tuple, list[OCRWordPx]]] = []
    split_pars: set[tuple] = set()
    for key, ws in by_line.items():
        ws = sorted(ws, key=lambda w: w.left)
        height = float(np.median([w.height for w in ws]))
        current = [ws[0]]
        parts = []
        for prev, w in zip(ws, ws[1:]):
            if w.left - prev.right > LINE_SPLIT_GAP * max(height, 4.0):
                parts.append(current)
                current = []
            current.append(w)
        parts.append(current)
        if len(parts) > 1:
            split_pars.add(key[:2])
        pieces += [(key[:2], p) for p in parts]
    return [_Line(ws, None if par in split_pars else par) for par, ws in pieces]


def _line_size(line: _Line) -> float:
    return line.size_px() or line.height / (EM_ASCENDER + EM_DESCENDER)


def _pitch(a: _Line, b: _Line) -> float:
    """Baseline distance from line a to line b, in font sizes."""
    size = (_line_size(a) + _line_size(b)) / 2
    return (b.baseline(_line_size(b)) - a.baseline(_line_size(a))) / max(size, 1.0)


def _paragraphs(lines: list[_Line]) -> list[list[_Line]]:
    """Group line pieces into paragraphs (top to bottom, same column, regular pitch)."""
    lines = sorted(lines, key=lambda l: (l.top, l.left))
    # Usual line pitch of the page (in font sizes) between vertically consecutive, overlapping lines.
    pitches = []
    for a in lines:
        below = [b for b in lines if b.top > a.top + 0.5 * a.height and _h_overlap(a, b) > 0.3]
        if below:
            p = _pitch(a, min(below, key=lambda b: b.top))
            if 0 < p < 3.0:
                pitches.append(p)
    usual = float(np.median(pitches)) if pitches else 1.3

    groups: list[list[_Line]] = []
    for line in lines:
        best = None
        for g in groups:
            last = g[-1]
            if line.top <= last.top + 0.5 * last.height:
                continue
            if _h_overlap(last, line) <= 0.3 and _h_overlap_group(g, line) <= 0.3:
                continue
            if _pitch(last, line) > PARAGRAPH_PITCH * usual:
                continue
            sizes = (_line_size(last), _line_size(line))
            if max(sizes) / max(min(sizes), 1.0) > SIZE_JUMP:
                continue
            if last.key is not None and line.key is not None and last.key != line.key:
                continue
            if best is None or last.top > best[-1].top:
                best = g
        if best is None:
            groups.append([line])
        else:
            best.append(line)
    return groups


def _h_overlap(a, b) -> float:
    inter = min(a.right, b.right) - max(a.left, b.left)
    return max(inter, 0) / max(min(a.right - a.left, b.right - b.left), 1)


def _h_overlap_group(group: list[_Line], line: _Line) -> float:
    left, right = min(l.left for l in group), max(l.right for l in group)
    inter = min(right, line.right) - max(left, line.left)
    return max(inter, 0) / max(min(right - left, line.right - line.left), 1)


# --------------------------------------------------------------------------- main entry point

def analyse_layout(ocr: OCRResult, frame_rgb: np.ndarray, regions: list[DetectedRegion],
                   page_height_pt: float, settings: ProcessingSettings) -> LayoutResult:
    """Typed and ordered blocks of a page. `ocr` coordinates may be at another scale than the frame:
    everything goes through normalized coordinates."""
    result = LayoutResult()
    W, H = max(ocr.width, 1), max(ocr.height, 1)
    if frame_rgb.ndim == 2:
        frame_rgb = cv2.cvtColor(frame_rgb, cv2.COLOR_GRAY2RGB)
    FH, FW = frame_rgb.shape[:2]
    pt_per_px = page_height_pt / H if page_height_pt > 0 else 72.0 / 300.0
    low = settings.low_confidence

    words = [w for w in ocr.words if w.text.strip() and w.width > 0 and w.height > 0]
    paragraphs = _paragraphs(_split_lines(words)) if words else []

    tables = [r for r in regions if r.kind == "table"]
    pictures = [r for r in regions if r.kind != "table"]
    table_text: dict[int, list[list[_Line]]] = {}
    texts: list[TextBlock] = []
    removed = 0

    for group in paragraphs:
        ws = [w for line in group for w in line.words]
        confs = [w.conf for w in ws if w.conf >= 0]
        mean = float(np.mean(confs)) if confs else -1.0
        text = "\n".join(line.text for line in group).strip()
        letters = len(re.findall(r"[^\W\d_]", text))
        if (0 <= mean < NOISE_CONFIDENCE and letters < 6) or not re.search(r"[^\W_]", text):
            removed += 1          # specks, rules and picture texture read as letters or dashes
            continue
        l, t = min(x.left for x in group), min(x.top for x in group)
        r, b = max(x.right for x in group), max(x.bottom for x in group)
        box = px_to_norm(l, t, r, b, W, H)
        in_table = next((i for i, reg in enumerate(tables) if overlap_ratio(box, reg.box) > 0.5), None)
        if in_table is not None:
            table_text.setdefault(in_table, []).append(group)
            continue
        inside = max((overlap_ratio(box, reg.box) for reg in pictures), default=0.0)
        caption_like = bool(CAPTION_START.match(text))
        if inside > 0.5 and (mean < low) and not caption_like:
            removed += 1          # letters read in the texture of a picture
            continue
        block = _text_block(group, box, mean, text, W, H, settings)
        if inside > 0.5 and not caption_like:
            block.needs_review = True
            block.review_reason = _join(block.review_reason, "texte lu dans une illustration : à vérifier")
        texts.append(block)

    if removed:
        result.warnings.append(f"{removed} zone(s) de bruit ou de texte peu fiable lu dans une illustration ignorée(s).")

    # Tables: text read inside -> one TABLE text block; no text -> image of the table.
    images: list[ImageBlock] = []
    for i, reg in enumerate(tables):
        groups = table_text.get(i)
        if groups:
            lines = sorted((line for g in groups for line in g), key=lambda x: (x.top, x.left))
            rows = _table_rows(lines)
            ws = [w for line in lines for w in line.words]
            confs = [w.conf for w in ws if w.conf >= 0]
            mean = float(np.mean(confs)) if confs else -1.0
            block = _text_block(rows, reg.box, mean, "\n".join(r.text for r in rows), W, H, settings)
            block.type = BlockType.TABLE
            block.keep_line_breaks = True
            block.needs_review = True
            block.review_reason = _join(block.review_reason, "tableau : vérifier les cellules")
            texts.append(block)
        else:
            images.append(_image_block(reg, BlockType.TABLE))
    images += [_image_block(reg, BlockType.ILLUSTRATION) for reg in pictures]

    _estimate_styles(texts, frame_rgb, W, H, pt_per_px, FW, FH)
    result.printed_page_number = _classify(texts, images)
    _link_captions(texts, images)
    blocks = texts + images
    result.columns = reading_order(blocks)
    _finish_styles(texts, blocks, W)
    result.blocks = blocks
    if blocks:
        result.margins = Margins(left=round(min(b.x for b in blocks), 4), top=round(min(b.y for b in blocks), 4),
                                 right=round(max(0.0, 1 - max(b.x + b.width for b in blocks)), 4),
                                 bottom=round(max(0.0, 1 - max(b.y + b.height for b in blocks)), 4))
    confs = [w.confidence for b in texts for line in b.lines for w in line.words if w.confidence >= 0]
    result.mean_confidence = round(float(np.mean(confs)), 1) if confs else -1.0
    if not texts:
        result.warnings.append("Aucun texte détecté sur cette page.")
    elif 0 <= result.mean_confidence < low:
        result.warnings.append(f"Confiance OCR moyenne faible ({result.mean_confidence:.0f} %) : relecture conseillée.")
    review = sum(1 for b in texts if b.needs_review)
    if review:
        result.warnings.append(f"{review} bloc(s) de texte à vérifier.")
    return result


def _join(a: str, b: str) -> str:
    return f"{a} ; {b}" if a else b


def _table_rows(lines: list[_Line]) -> list[_Line]:
    """Pieces of a table on the same row joined (left to right) into one line, cells separated by tabs."""
    rows: list[list[_Line]] = []
    for line in lines:
        centre = (line.top + line.bottom) / 2
        row = next((r for r in rows if abs((r[0].top + r[0].bottom) / 2 - centre) < 0.6 * max(r[0].height, line.height)), None)
        if row is None:
            rows.append([line])
        else:
            row.append(line)
    out = []
    for row in rows:
        row.sort(key=lambda x: x.left)
        out.append(_Line([w for piece in row for w in piece.words], None, [piece.text for piece in row]))
    return out


def _text_block(group: list[_Line], box: Box, mean: float, text: str, W: int, H: int,
                settings: ProcessingSettings) -> TextBlock:
    lines = []
    for line in group:
        lb = px_to_norm(line.left, line.top, line.right, line.bottom, W, H)
        ows = []
        for w in line.words:
            wb = px_to_norm(w.left, w.top, w.right, w.bottom, W, H)
            ows.append(OCRWord(text=w.text, x=wb.x, y=wb.y, width=wb.width, height=wb.height,
                               confidence=round(w.conf, 1) if w.conf >= 0 else -1.0))
        lines.append(OCRLine(x=lb.x, y=lb.y, width=lb.width, height=lb.height, words=ows))
    text_lines = ["\t".join(line.cells) if line.cells else line.text for line in group]
    text = "\n".join(text_lines).strip()
    uncertain = [w.text for line in group for w in line.words if 0 <= w.conf < settings.low_confidence]
    block = TextBlock(x=box.x, y=box.y, width=box.width, height=box.height, text=text, ocr_text=text,
                      confidence=round(mean, 1) if mean >= 0 else -1.0, lines=lines,
                      low_confidence_words=uncertain[:100], language=_language(text, settings.languages))
    if 0 <= mean < settings.low_confidence:
        block.needs_review = True
        block.review_reason = f"confiance OCR faible ({mean:.0f} %)"
    block.estimated = {"_group": group}
    return block


def _image_block(region: DetectedRegion, block_type: BlockType) -> ImageBlock:
    b = region.box
    return ImageBlock(x=b.x, y=b.y, width=b.width, height=b.height, image_path="", type=block_type,
                      detection=region.kind, confidence=region.confidence, needs_review=region.uncertain,
                      review_reason=region.reason)


# --------------------------------------------------------------------------- styles and types

def _estimate_styles(texts: list[TextBlock], rgb: np.ndarray, W: int, H: int, pt_per_px: float,
                     FW: int, FH: int) -> None:
    for block in texts:
        group: list[_Line] = block.estimated["_group"]
        words = [w for line in group for w in line.words]
        size_px = _font_size_px(words) or float(np.median([l.height for l in group])) / (EM_ASCENDER + EM_DESCENDER)
        est = {"size_px": round(size_px, 2), "line_height_px": round(float(np.median([l.height for l in group])), 2)}
        block.font_size = float(min(max(round(size_px * pt_per_px, 1), 1.0), 300.0))
        if len(group) >= 2 and block.type != BlockType.TABLE:
            bases = [line.baseline(size_px) for line in group]
            pitch = float(np.median(np.diff(bases)))
            est["baseline_pitch_px"] = round(pitch, 2)
            if pitch > 0:
                block.line_spacing = float(min(max(round(pitch / size_px, 2), 0.5), 4.0))
        fx = FW / W
        fy = FH / H
        box_px = (int(min(l.left for l in group) * fx), int(min(l.top for l in group) * fy),
                  int(max(l.right for l in group) * fx) + 1, int(max(l.bottom for l in group) * fy) + 1)
        est.update(_ink_measures(rgb, box_px, size_px * fy))
        if "color" in est:
            block.color = est["color"]
        est["_group"] = group
        block.estimated = est


def _body_size(texts: list[TextBlock]) -> float:
    """Font size of the running text: the size carrying most characters among ordinary blocks."""
    candidates = [b for b in texts if b.type not in (BlockType.TABLE,) and len(b.text) >= 20] or texts
    if not candidates:
        return 0.0
    sizes = np.array([b.font_size for b in candidates])
    weights = np.array([len(b.text) for b in candidates], float)
    order = np.argsort(sizes)
    cumulative = np.cumsum(weights[order])
    return float(sizes[order][np.searchsorted(cumulative, cumulative[-1] / 2)])


def _classify(texts: list[TextBlock], images: list[ImageBlock]) -> str:
    """Set block types (in place); return the printed page number found ("" if none)."""
    page_number = ""
    body = _body_size([b for b in texts if b.type == BlockType.PARAGRAPH])
    strokes = [b.estimated.get("stroke") for b in texts if b.type == BlockType.PARAGRAPH and len(b.text) >= 20]
    body_stroke = float(np.median([s for s in strokes if s])) if any(strokes) else 0.0
    for b in texts:
        stroke = b.estimated.get("stroke")
        if stroke:
            ratio = stroke / body_stroke if body_stroke else 0.0
            bold = ratio >= BOLD_RATIO if body_stroke else stroke >= BOLD_ABSOLUTE
            b.estimated["bold_ratio"] = round(ratio, 2)
            b.font_weight = "bold" if bold else "normal"

    headings = []
    for b in texts:
        if b.type == BlockType.TABLE:
            continue
        n_lines = len(b.lines)
        stripped = b.text.strip()
        in_band = b.y + b.height <= EDGE_BAND or b.y >= 1 - EDGE_BAND
        if in_band and n_lines <= 2:
            match = PAGE_NUMBER.match(stripped)
            if match and n_lines == 1:
                b.type = BlockType.PAGE_NUMBER
                page_number = page_number or match.group(1)
                continue
            if not body or b.font_size < HEADING_RATIO * body:
                b.type = BlockType.HEADER if b.y < 0.5 else BlockType.FOOTER
                continue
        if CAPTION_START.match(stripped) and n_lines <= 4:
            b.type = BlockType.CAPTION
            continue
        lines = [line for line in stripped.split("\n") if line.strip()]
        if len(lines) >= 2 and sum(bool(LIST_MARKER.match(line)) for line in lines) >= max(2, len(lines) // 2):
            b.type = BlockType.LIST
            continue
        short = n_lines <= 3 and len(stripped) <= 160
        bigger = body > 0 and b.font_size >= HEADING_RATIO * body
        bold_heading = b.font_weight == "bold" and n_lines <= 2 and len(stripped) <= 80 and not stripped.endswith(".")
        if short and (bigger or bold_heading) and len(re.findall(r"[^\W\d_]", stripped)) >= 2:
            b.type = BlockType.HEADING
            headings.append(b)

    # Title: the largest heading in the upper part of the page; a smaller heading right under it is
    # its subtitle.
    if headings:
        top = [h for h in headings if h.y < 0.4 and h.font_size >= HEADING_RATIO * max(body, 1e-6)]
        if top:
            largest = max(h.font_size for h in top)
            title = min((h for h in top if h.font_size >= 0.9 * largest), key=lambda h: h.y)
            title.type = BlockType.TITLE
            below = [h for h in headings if h is not title and h.y > title.y and h.y - (title.y + title.height) < 0.08
                     and h.font_size < title.font_size]
            if below:
                min(below, key=lambda h: h.y).type = BlockType.SUBTITLE
    return page_number


def _link_captions(texts: list[TextBlock], images: list[ImageBlock]) -> None:
    """Each picture gets at most one caption: a "Figure..." block or a short block right below
    (else right above) it, horizontally overlapping."""
    used: set[str] = set()
    for img in sorted(images, key=lambda i: (i.y, i.x)):
        best, best_score = None, None
        for b in texts:
            if b.id in used or b.type in (BlockType.TITLE, BlockType.SUBTITLE, BlockType.HEADER, BlockType.FOOTER,
                                          BlockType.PAGE_NUMBER, BlockType.TABLE):
                continue
            inter = min(b.x + b.width, img.x + img.width) - max(b.x, img.x)
            if inter <= 0.3 * min(b.width, img.width):
                continue
            below = b.y - (img.y + img.height)
            above = img.y - (b.y + b.height)
            gap = below if below >= -0.5 * b.height else above
            if gap < -0.5 * b.height or gap > CAPTION_GAP:
                if not (b.type == BlockType.CAPTION and overlap_ratio(b.box, img.box) > 0.5):
                    continue
                gap = 0.0
            keyword = b.type == BlockType.CAPTION
            if not keyword and (len(b.lines) > 2 or b.type not in (BlockType.PARAGRAPH, BlockType.HEADING)):
                continue
            score = (0 if keyword else 1, 0 if below >= -0.5 * b.height else 1, abs(gap))
            if best_score is None or score < best_score:
                best, best_score = b, score
        if best is not None:
            best.type = BlockType.CAPTION
            img.caption_id = best.id
            used.add(best.id)


def _finish_styles(texts: list[TextBlock], blocks: list, W: int) -> None:
    """Alignment (compared with the block's column) and raw measures cleaned for JSON."""
    if not texts:
        return
    body = [b for b in blocks if getattr(b, "type", None) not in EDGE_TYPES]
    columns, column_of = _column_layout(body)
    ref = body or texts
    page_col = (min(o.x for o in ref), max(o.x + o.width for o in ref))
    for b in texts:
        group: list[_Line] = b.estimated.pop("_group")
        idx = column_of(b) if b in body else None
        left, right = columns[idx] if (idx is not None and columns) else page_col
        align, why = _alignment(group, (left * W, right * W))
        b.alignment = align
        b.estimated["alignment"] = why
        b.estimated["font_size_pt"] = b.font_size


# --------------------------------------------------------------------------- reading order

EDGE_TYPES = {BlockType.HEADER, BlockType.FOOTER, BlockType.PAGE_NUMBER}


def _column_layout(body: list):
    """(column extents [(left, right)...], column_of(block) -> index or None for a band separator).

    Columns are the merged horizontal extents of the blocks that have a neighbour side by side.
    """
    columns: list[list[float]] = []
    if not body:
        return columns, lambda b: 0
    content_left = min(b.x for b in body)
    content_w = max(max(b.x + b.width for b in body) - content_left, 1e-6)

    def side_by_side(a, b) -> bool:
        v = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
        disjoint = a.x + a.width <= b.x or b.x + b.width <= a.x
        return disjoint and v > 0.3 * min(a.height, b.height)

    columnar = [b for b in body if b.width < FULL_WIDTH * content_w and any(side_by_side(b, o) for o in body if o is not b)]
    for b in sorted(columnar, key=lambda b: b.x):
        if columns and b.x < columns[-1][1]:
            columns[-1][1] = max(columns[-1][1], b.x + b.width)
        else:
            columns.append([b.x, b.x + b.width])

    def column_of(b) -> int | None:
        if not columns:
            return 0
        if b.width >= FULL_WIDTH * content_w:
            return None
        hits = [i for i, (l, r) in enumerate(columns) if min(r, b.x + b.width) - max(l, b.x) > 0.15 * min(r - l, b.width)]
        if len(hits) > 1:
            return None
        if hits:
            return hits[0]
        centre = b.x + b.width / 2
        return int(np.argmin([abs(centre - (l + r) / 2) for l, r in columns]))

    return columns, column_of


def reading_order(blocks: list) -> int:
    """Sort `blocks` in place in reading order, set `reading_order` (0..n-1) and `column`; return
    the number of columns.

    Columns are the horizontal extents of blocks that have a neighbour side by side. A block that
    crosses two columns (or spans most of the content width) separates bands; inside a band,
    columns are read left to right, each from top to bottom. Running heads come first, footers and
    page numbers last.
    """
    edges = [b for b in blocks if getattr(b, "type", None) in EDGE_TYPES]
    body = [b for b in blocks if b not in edges]
    head = sorted((b for b in edges if b.y + b.height / 2 < 0.5), key=lambda b: (b.y, b.x))
    foot = sorted((b for b in edges if b.y + b.height / 2 >= 0.5), key=lambda b: (b.y, b.x))
    columns, column_of = _column_layout(body)

    ordered = list(head)
    band: list = []

    def flush():
        ordered.extend(sorted(band, key=lambda b: (b.column, b.y, b.x)))
        band.clear()

    for b in sorted(body, key=lambda b: (b.y, b.x)):
        col = column_of(b)
        if col is None:
            flush()
            b.column = 0
            ordered.append(b)
        else:
            b.column = col
            band.append(b)
    flush()
    for b in head + foot:
        b.column = 0
    ordered += foot
    for i, b in enumerate(ordered):
        b.reading_order = i
    blocks[:] = ordered
    return max(1, len(columns))
