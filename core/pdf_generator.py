"""PDF generation: reconstruction (mode B, white page, real text) and facsimile (mode A, page image).

Reconstruction: every displayable block is drawn at its place on a white page (geometry from
`coordinates.page_sheet_geometry`). Illustrations first (images given by an `ImageProvider`, never
upsampled, never above `render.image_dpi`), then text blocks as real vector text in embedded
TrueType fonts. A page without any displayable block is exported **blank** (never the photograph)
and reported as not reconstructed.

Facsimile: the page image (`PageImageProvider`) fills the content box, optionally under an
invisible text layer (render mode 3) built from the OCR lines and the corrected text.

The preview (`render_page_png`) is the PDF page itself, rasterised with pypdfium2.
"""
from __future__ import annotations

import io
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Literal

from PIL import Image
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import getAscent, getDescent, stringWidth
from reportlab.pdfgen import canvas as pdfcanvas

from . import fonts
from .coordinates import SheetGeometry, font_scale, mm_to_pt, norm_to_pdf_rect, page_sheet_geometry, sheet_geometry
from .document_models import (
    PAGE_SIZES_MM, Alignment, Box, BlockType, ImageBlock, OverflowPolicy, Page, PageSize, Project, RenderSettings,
    TextBlock,
)
from .errors import LivreOCRError, PDFGenerationError

log = logging.getLogger("livreocr.pdf")

Mode = Literal["reconstruction", "facsimile"]
ImageProvider = Callable[[Page, ImageBlock], Image.Image]
PageImageProvider = Callable[[Page], Image.Image]
ProgressCallback = Callable[[int, int, str], None]

CREATOR = "LivreOCR Streamlit"
FACSIMILE_STATUS = "fac-similé : image de la page originale"
RECONSTRUCTED_STATUS = "reconstruit"
BLANK_STATUS = "page blanche : non reconstruite"
SHRINK_STEP = 0.5          # points
MIN_FONT_PT = 1.0
OVERLAP_SHARE = 0.05       # of the smaller block's area
EXTEND_GAP = 1.0           # points kept free above the next block when a text block is extended

LIST_MARKER = re.compile(
    r"^\s*(?:[•·▪●○◦■□►‣\-–—*]|\(?\d{1,3}[.)°]|\(?[a-zA-Z][.)]|[ivxlcIVXLC]{1,5}[.)])\s+(?=\S)")


# --------------------------------------------------------------------------- reports

@dataclass
class PageReport:
    page_index: int                      # position in project.pages (0-based)
    page_id: str
    warnings: list[str] = field(default_factory=list)
    overflows: list[str] = field(default_factory=list)   # one message per block whose text was cut
    overlaps: int = 0
    reconstructed: bool = False
    text_blocks: int = 0
    image_blocks: int = 0
    mode: str = "reconstruction"
    overflow_blocks: list[str] = field(default_factory=list)   # ids of the blocks whose text was cut
    lost_lines: int = 0
    page_width: float = 0.0              # points
    page_height: float = 0.0

    @property
    def status(self) -> str:
        """What the page of the PDF is, in words for the user."""
        if self.mode == "facsimile":
            return FACSIMILE_STATUS
        return RECONSTRUCTED_STATUS if self.reconstructed else BLANK_STATUS


@dataclass
class RenderReport:
    pages: list[PageReport] = field(default_factory=list)
    mode: str = "reconstruction"
    notes: list[str] = field(default_factory=list)       # document-level warnings

    @property
    def warnings(self) -> list[str]:
        out = list(self.notes)
        for page in self.pages:
            out.extend(page.warnings)
            out.extend(page.overflows)
        return out

    @property
    def blank_pages(self) -> list[int]:
        return [p.page_index for p in self.pages if p.mode == "reconstruction" and not p.reconstructed]


# --------------------------------------------------------------------------- text layout

@dataclass
class _Line:
    text: str
    indent: float       # from the left edge of the box
    width: float        # width available to this line
    stretch: bool       # may be justified (not the last line of a paragraph or hard line)
    gap_before: float   # paragraph spacing added above this line


def _join_scan_lines(lines: list[str]) -> str:
    """Reflow lines of a scan. "mot-\\nsuite": the hyphen is removed when the next line starts with a
    lower-case letter; otherwise ("Jean-\\nPierre", "1914-\\n1918") the hyphen is kept, without a space."""
    out = ""
    for raw in lines:
        line = " ".join(raw.split())
        if not line:
            continue
        if not out:
            out = line
        elif re.search(r"\w-$", out):
            out = (out[:-1] if line[0].islower() else out) + line
        else:
            out += " " + line
    return out


def split_paragraphs(text: str, keep_line_breaks: bool = False, is_list: bool = False) -> list[list[str]]:
    """Paragraphs (separated by an empty line), each as a list of hard lines.

    Without keep_line_breaks a single line break is a line end of the scan and the text is
    reflowed; a list starts a new item (hard line) at each line beginning with a bullet or a number.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = []
    for chunk in re.split(r"\n[ \t]*\n", text.strip()):
        lines = [line for line in chunk.split("\n") if line.strip()]
        if not lines:
            continue
        if keep_line_breaks:
            units = [" ".join(line.split()) for line in lines]
        elif is_list:
            groups: list[list[str]] = []
            for line in lines:
                if LIST_MARKER.match(line) or not groups:
                    groups.append([line])
                else:
                    groups[-1].append(line)
            units = [_join_scan_lines(g) for g in groups]
        else:
            units = [_join_scan_lines(lines)]
        paragraphs.append(units)
    return paragraphs


def text_width(text: str, font: str, size: float, spacing: float = 0.0) -> float:
    """Advance of `text` with letter spacing between characters (not after the last one)."""
    return stringWidth(text, font, size) + spacing * max(len(text) - 1, 0)


def _wrap(unit: str, font: str, size: float, spacing: float, width: float, first_indent: float,
          rest_indent: float) -> list[tuple[str, float]]:
    """Greedy line breaking; a word wider than the line is cut. Returns (text, indent) pairs."""
    lines: list[tuple[str, float]] = []
    indent = first_indent
    current = ""
    for word in unit.split(" "):
        candidate = f"{current} {word}" if current else word
        if not current or text_width(candidate, font, size, spacing) <= width - indent:
            current = candidate
        else:
            lines.append((current, indent))
            indent, current = rest_indent, word
        while len(current) > 1 and text_width(current, font, size, spacing) > width - indent:
            cut = len(current) - 1
            while cut > 1 and text_width(current[:cut], font, size, spacing) > width - indent:
                cut -= 1
            lines.append((current[:cut], indent))
            indent, current = rest_indent, current[cut:]
    lines.append((current, indent))
    return lines


def _style_scale(block: TextBlock, size: float) -> float:
    """Point measures of the style (letter spacing, indent, paragraph spacing) follow the font size
    actually used (page scaling and shrinking), so that the block keeps its proportions."""
    return size / block.font_size if block.font_size else 1.0


def typeset(block: TextBlock, font: str, size: float, width: float) -> tuple[list[_Line], float]:
    """Lay out the block's text at this size: (lines, height from the box top to the last descender)."""
    is_list = block.type == BlockType.LIST
    paragraphs = split_paragraphs(block.text, block.keep_line_breaks, is_list)
    k = _style_scale(block, size)
    spacing = block.letter_spacing * k
    max_indent = width * 0.5
    lines: list[_Line] = []
    for p_index, units in enumerate(paragraphs):
        for u_index, unit in enumerate(units):
            first = block.first_line_indent * k if (u_index == 0 or is_list) else 0.0
            first = min(first, max_indent)
            rest = 0.0
            if is_list:
                marker = LIST_MARKER.match(unit)
                hang = text_width(marker.group(0), font, size, spacing) if marker else 0.0
                rest = min(first + hang, max_indent)
            wrapped = _wrap(unit, font, size, spacing, width, first, rest)
            for i, (text, indent) in enumerate(wrapped):
                gap = block.paragraph_spacing * k if (p_index > 0 and u_index == 0 and i == 0) else 0.0
                lines.append(_Line(text, indent, width - indent, i < len(wrapped) - 1, gap))
    return lines, _height(lines, font, size, block.line_spacing)


def _height(lines: list[_Line], font: str, size: float, line_spacing: float, count: int | None = None) -> float:
    n = len(lines) if count is None else count
    if n <= 0:
        return 0.0
    leading = size * line_spacing
    ascent = getAscent(font, size)
    descent = -getDescent(font, size)
    return ascent + (n - 1) * leading + sum(line.gap_before for line in lines[1:n]) + descent


def _fitting_count(lines: list[_Line], font: str, size: float, line_spacing: float, height: float) -> int:
    count = 0
    while count < len(lines) and _height(lines, font, size, line_spacing, count + 1) <= height + 0.5:
        count += 1
    return count


def _fit(block: TextBlock, font: str, size: float, floor: float, width: float, height: float,
         shrink: bool) -> tuple[float, list[_Line], float]:
    """Largest size from `size` down to `floor` (by SHRINK_STEP) whose text fits the height."""
    lines, needed = typeset(block, font, size, width)
    while shrink and needed > height + 0.5 and size - SHRINK_STEP >= floor - 1e-9:
        size -= SHRINK_STEP
        lines, needed = typeset(block, font, size, width)
    return size, lines, needed


def _draw_lines(c, lines: list[_Line], block: TextBlock, font: str, size: float, x: float, top: float) -> None:
    color = HexColor(block.color)
    c.setFillColor(color)
    c.setStrokeColor(color)
    leading = size * block.line_spacing
    spacing = block.letter_spacing * _style_scale(block, size)
    y = top - getAscent(font, size)
    for i, line in enumerate(lines):
        if i:
            y -= leading + line.gap_before
        if not line.text:
            continue
        natural = text_width(line.text, font, size, spacing)
        offset, word_space = 0.0, 0.0
        spaces = line.text.count(" ")
        if block.alignment == Alignment.RIGHT:
            offset = line.width - natural
        elif block.alignment == Alignment.CENTER:
            offset = (line.width - natural) / 2
        elif block.alignment == Alignment.JUSTIFY and line.stretch and spaces:
            word_space = max((line.width - natural) / spaces, 0.0)
        start = x + line.indent + max(offset, 0.0)
        obj = c.beginText()
        obj.setFont(font, size)
        obj.setCharSpace(spacing)
        obj.setWordSpace(word_space)
        obj.setTextOrigin(start, y)
        obj.textOut(line.text)
        c.drawText(obj)
        if block.underline:
            c.setLineWidth(max(0.4, size / 18))
            under = y - size * 0.12
            c.line(start, under, start + natural + word_space * spaces, under)


# --------------------------------------------------------------------------- geometry helpers

Rect = tuple[float, float, float, float]   # x, y_bottom, w, h in points


def _sheet(render: RenderSettings, page: Page, fallback_size: tuple[int, int] | None = None) -> SheetGeometry | None:
    """Sheet geometry of the page; None when its size is unknown (page never analysed)."""
    if (page.frame_width or page.original_width) and (page.frame_height or page.original_height):
        return page_sheet_geometry(render, page)
    if fallback_size and fallback_size[0] > 0 and fallback_size[1] > 0:
        return sheet_geometry(render, *fallback_size)
    return None


def _default_sheet(render: RenderSettings) -> SheetGeometry:
    """A4 portrait (or the fixed format), used for a page whose size is unknown."""
    if render.page_size == PageSize.CUSTOM:
        w_mm, h_mm = render.custom_width_mm, render.custom_height_mm
    else:
        w_mm, h_mm = PAGE_SIZES_MM.get(render.page_size, PAGE_SIZES_MM[PageSize.A4])
    w, h = mm_to_pt(w_mm), mm_to_pt(h_mm)
    return SheetGeometry(w, h, 0.0, 0.0, w, h)


def _rotated_bounds(rect: Rect, rotation: float) -> Rect:
    x, y, w, h = rect
    if not rotation % 360:
        return rect
    a = math.radians(rotation)
    bw = abs(w * math.cos(a)) + abs(h * math.sin(a))
    bh = abs(w * math.sin(a)) + abs(h * math.cos(a))
    cx, cy = x + w / 2, y + h / 2
    return cx - bw / 2, cy - bh / 2, bw, bh


def _bring_inside(rect: Rect, rotation: float, sheet: SheetGeometry) -> tuple[Rect, bool]:
    """Shift the block so that its (rotated) outline stays on the sheet."""
    bx, by, bw, bh = _rotated_bounds(rect, rotation)
    dx = dy = 0.0
    if bx < 0:
        dx = -bx
    elif bx + bw > sheet.page_width:
        dx = max(sheet.page_width - (bx + bw), -bx)
    if by < 0:
        dy = -by
    elif by + bh > sheet.page_height:
        dy = max(sheet.page_height - (by + bh), -by)
    if abs(dx) < 0.01 and abs(dy) < 0.01:
        return rect, False
    x, y, w, h = rect
    return (x + dx, y + dy, w, h), True


def _rotate_about_centre(c, rect: Rect, rotation: float) -> None:
    if rotation % 360:
        x, y, w, h = rect
        cx, cy = x + w / 2, y + h / 2
        c.translate(cx, cy)
        c.rotate(rotation)
        c.translate(-cx, -cy)


def _intersection(a: Rect, b: Rect) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy


def count_overlaps(rects: list[Rect]) -> int:
    """Pairs of blocks overlapping on more than OVERLAP_SHARE of the smaller one."""
    count = 0
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            smaller = min(a[2] * a[3], b[2] * b[3])
            if smaller > 0 and _intersection(a, b) > OVERLAP_SHARE * smaller:
                count += 1
    return count


# --------------------------------------------------------------------------- images

def _image_reader(image: Image.Image, w_pt: float, h_pt: float, render: RenderSettings) -> tuple[ImageReader, bool]:
    """Image ready to embed: at most render.image_dpi at the printed size, never upsampled,
    lossless (Flate) unless render.jpeg_quality > 0. Returns (reader, has transparency)."""
    if image.mode in ("P", "LA", "PA"):
        image = image.convert("RGBA")
    elif image.mode == "1" or image.mode.startswith("I") or image.mode == "F":
        image = image.convert("L")
    elif image.mode not in ("RGB", "RGBA", "L"):
        image = image.convert("RGB")
    alpha = image.mode == "RGBA" and image.getchannel("A").getextrema()[0] < 255
    if image.mode == "RGBA" and not alpha:
        image = image.convert("RGB")
    max_w = max(1, int(w_pt / 72.0 * render.image_dpi + 1e-6))
    max_h = max(1, int(h_pt / 72.0 * render.image_dpi + 1e-6))
    target = (min(image.width, max_w), min(image.height, max_h))
    if target != image.size:
        image = image.resize(target, Image.LANCZOS)
    if render.jpeg_quality > 0:
        if alpha:
            white = Image.new("RGB", image.size, "white")
            white.paste(image, mask=image.getchannel("A"))
            image, alpha = white, False
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=render.jpeg_quality, optimize=True)
        buffer.seek(0)
        return ImageReader(buffer), False
    return ImageReader(image), alpha


def _fit_rect(rect: Rect, image_size: tuple[int, int], keep_ratio: bool) -> Rect:
    x, y, w, h = rect
    if not keep_ratio or image_size[0] <= 0 or image_size[1] <= 0:
        return rect
    ratio = min(w / image_size[0], h / image_size[1])
    dw, dh = image_size[0] * ratio, image_size[1] * ratio
    return x + (w - dw) / 2, y + (h - dh) / 2, dw, dh


def _draw_image(c, image: Image.Image, rect: Rect, render: RenderSettings, keep_ratio: bool, rotation: float = 0.0) -> Rect:
    """Draw the image in the rectangle (centred when keeping its ratio); returns the drawn rectangle."""
    target = _fit_rect(rect, image.size, keep_ratio)
    reader, alpha = _image_reader(image, target[2], target[3], render)
    c.saveState()
    _rotate_about_centre(c, rect, rotation)
    c.drawImage(reader, target[0], target[1], target[2], target[3], mask="auto" if alpha else None)
    c.restoreState()
    return target


# --------------------------------------------------------------------------- pages

def _label(index: int) -> str:
    return f"Page {index + 1}"


def _block_label(block) -> str:
    return f"bloc {block.reading_order + 1} ({block.id})"


def _displayed(page: Page, render: RenderSettings) -> list[TextBlock | ImageBlock]:
    blocks = [b for b in page.ordered_blocks() if render.include_hidden_blocks or not b.hidden]
    return [b for b in blocks if isinstance(b, ImageBlock) or b.text.strip()]


def _start_page(c, sheet: SheetGeometry, report: PageReport) -> None:
    c.setPageSize((sheet.page_width, sheet.page_height))
    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, sheet.page_width, sheet.page_height, stroke=0, fill=1)
    report.page_width, report.page_height = sheet.page_width, sheet.page_height


def _choose_font(block: TextBlock, render: RenderSettings, label: str, report: PageReport) -> str:
    family = block.font_family or render.default_font_family
    bold, italic = block.font_weight == "bold", block.italic
    font = fonts.resolve(family, bold, italic)
    if not fonts.is_embedded(font):
        report.warnings.append(f"{label} : aucune police TrueType disponible, Times (non intégrée) utilisée.")
    missing = fonts.missing_characters(font, block.text)
    if not missing:
        return font
    replacement = fonts.fallback_font(block.text, bold, italic, exclude=font)
    shown = "".join(sorted(missing))[:20]
    if replacement and len(replacement[1]) < len(missing):
        font, still = replacement
        report.warnings.append(f"{label} : caractères absents de « {family} » ({shown}), police de remplacement utilisée.")
        if still:
            report.warnings.append(f"{label} : caractères impossibles à afficher ({''.join(sorted(still))[:20]}).")
    else:
        report.warnings.append(f"{label} : caractères impossibles à afficher ({shown}).")
    return font


def _free_space_bottom(rect: Rect, others: list[Rect], sheet: SheetGeometry) -> float:
    """Lowest y the block may extend to: the top of the next block below it, or the content bottom."""
    x, y, w, _ = rect
    limit = sheet.content_y
    for ox, oy, ow, oh in others:
        top = oy + oh
        if top <= y + 0.5 and ox < x + w and ox + ow > x:
            limit = max(limit, top + EXTEND_GAP)
    return limit


def _draw_text_block(c, page: Page, block: TextBlock, rect: Rect, others: list[Rect], sheet: SheetGeometry,
                     render: RenderSettings, scale: float, label: str, report: PageReport) -> Rect:
    font = _choose_font(block, render, label, report)
    x, y, w, h = rect
    top = y + h
    wanted = max(block.font_size * scale, MIN_FONT_PT)
    floor = max(wanted * render.min_font_scale, MIN_FONT_PT)
    policy = render.overflow_policy
    shrink = policy in (OverflowPolicy.SHRINK, OverflowPolicy.SHRINK_THEN_EXTEND)
    size, lines, needed = _fit(block, font, wanted, floor, w, h, shrink)
    height = h
    if needed > h + 0.5 and policy == OverflowPolicy.SHRINK_THEN_EXTEND and not block.rotation % 360:
        bottom = _free_space_bottom(rect, others, sheet)
        if top - bottom > h + 0.5:
            height = top - bottom
            size, lines, needed = _fit(block, font, wanted, floor, w, height, True)
            report.warnings.append(
                f"{label}, {_block_label(block)} : texte prolongé de {height - h:.0f} pt dans l'espace libre sous le bloc.")
    if size < wanted - 0.01:
        report.warnings.append(
            f"{label}, {_block_label(block)} : taille réduite de {wanted:.1f} à {size:.1f} pt pour tenir dans le bloc.")
    if needed > height + 0.5:
        kept = _fitting_count(lines, font, size, block.line_spacing, height)
        lost = len([line for line in lines[kept:] if line.text])
        lines = lines[:kept]
        report.overflows.append(
            f"{label}, {_block_label(block)} : texte trop long pour son bloc, {lost} ligne(s) perdue(s) "
            "— agrandissez le bloc ou réduisez la taille.")
        report.overflow_blocks.append(block.id)
        report.lost_lines += lost
    c.saveState()
    _rotate_about_centre(c, rect, block.rotation)
    _draw_lines(c, lines, block, font, size, x, top)
    c.restoreState()
    return x, top - height, w, height


def _draw_reconstruction(c, project: Project, page: Page, index: int, image_provider: ImageProvider) -> PageReport:
    render = project.render
    label = _label(index)
    report = PageReport(index, page.id, mode="reconstruction")
    sheet = _sheet(render, page)
    if sheet is None:
        _start_page(c, _default_sheet(render), report)
        report.warnings.append(f"{label} : page non analysée (taille inconnue) ; page blanche exportée sans "
                               "réutiliser la photographie.")
        c.showPage()
        return report
    _start_page(c, sheet, report)
    blocks = _displayed(page, render)
    if not blocks:
        report.warnings.append(f"{label} : aucun bloc affichable ; page blanche exportée sans réutiliser la "
                               "photographie. Lancez l'analyse (OCR) ou ajoutez des blocs.")
        c.showPage()
        return report

    rects: dict[str, Rect] = {}
    for block in blocks:
        rect, moved = _bring_inside(norm_to_pdf_rect(block.box, sheet), block.rotation, sheet)
        if moved:
            report.warnings.append(f"{label}, {_block_label(block)} : le bloc sortait de la page, il a été ramené dedans.")
        rects[block.id] = rect
    drawn: list[Rect] = []
    for block in blocks:
        if not isinstance(block, ImageBlock):
            continue
        try:
            image = image_provider(page, block)
        except Exception as exc:   # one missing illustration must not stop the book
            log.exception("illustration %s", block.id)
            report.warnings.append(f"{label}, {_block_label(block)} : illustration indisponible ({exc}).")
            continue
        if image is None or image.width <= 0 or image.height <= 0:
            report.warnings.append(f"{label}, {_block_label(block)} : illustration vide, non insérée.")
            continue
        _draw_image(c, image, rects[block.id], render, block.preserve_aspect_ratio, block.rotation)
        drawn.append(rects[block.id])
        report.image_blocks += 1
    scale = font_scale(render, page, sheet)
    for block in blocks:
        if not isinstance(block, TextBlock):
            continue
        others = [r for bid, r in rects.items() if bid != block.id]
        drawn.append(_draw_text_block(c, page, block, rects[block.id], others, sheet, render, scale, label, report))
        report.text_blocks += 1
    report.overlaps = count_overlaps(drawn)
    if report.overlaps:
        report.warnings.append(f"{label} : {report.overlaps} chevauchement(s) entre blocs, à vérifier.")
    report.reconstructed = bool(report.text_blocks or report.image_blocks)
    if not report.reconstructed:
        report.warnings.append(f"{label} : aucun bloc n'a pu être dessiné ; page blanche.")
    c.showPage()
    return report


# --------------------------------------------------------------------------- facsimile

def _layer_items(block: TextBlock) -> list[tuple[str, float, float, float, float]]:
    """(text, x, y, w, h) in frame coordinates for the invisible layer.

    Uncorrected text with OCR words: one item per word. Corrected text: its words are spread over
    the OCR lines in proportion to the line widths. No OCR lines: the block is cut into as many
    bands as the text has lines.
    """
    lines = [line for line in block.lines if line.width > 0 and line.height > 0]
    if not lines:
        hard = [" ".join(t.split()) for t in block.text.split("\n") if t.strip()]
        step = block.height / max(len(hard), 1)
        return [(t, block.x, block.y + i * step, block.width, step) for i, t in enumerate(hard)]
    if not block.is_corrected and all(line.words for line in lines):
        return [(w.text, w.x, w.y, w.width, w.height) for line in lines for w in line.words
                if w.text.strip() and w.width > 0 and w.height > 0]
    words = block.text.split()
    total = sum(line.width for line in lines)
    items, start, acc = [], 0, 0.0
    for i, line in enumerate(lines):
        acc += line.width
        end = len(words) if i == len(lines) - 1 else min(len(words), round(len(words) * acc / total))
        if end > start:
            items.append((" ".join(words[start:end]), line.x, line.y, line.width, line.height))
        start = max(start, end)
    return items


def _text_layer(c, page: Page, sheet: SheetGeometry, render: RenderSettings, label: str, report: PageReport) -> int:
    blocks = [b for b in page.ordered_blocks() if isinstance(b, TextBlock) and b.text.strip()
              and (render.include_hidden_blocks or not b.hidden)]
    if not blocks:
        return 0
    text = " ".join(b.text for b in blocks)
    font = fonts.resolve("Sans")
    if fonts.missing_characters(font, text):
        replacement = fonts.fallback_font(text, exclude=font)
        if replacement:
            font = replacement[0]
    count = 0
    for block in blocks:
        for word, nx, ny, nw, nh in _layer_items(block):
            box = Box.clamped(nx, ny, nw, nh)
            x, y, w, h = norm_to_pdf_rect(box, sheet)
            size = max(h * 0.85, 1.0)
            natural = stringWidth(word, font, size) or 1.0
            obj = c.beginText()
            obj.setTextRenderMode(3)
            obj.setFont(font, size)
            obj.setHorizScale(100.0 * w / natural)
            obj.setTextOrigin(x, y + h * 0.2)
            obj.textOut(word + " ")
            c.drawText(obj)
            count += 1
    return count


def _draw_facsimile(c, project: Project, page: Page, index: int, page_image_provider: PageImageProvider) -> PageReport:
    render = project.render
    label = _label(index)
    report = PageReport(index, page.id, mode="facsimile")
    image = None
    try:
        image = page_image_provider(page)
    except Exception as exc:
        log.exception("image de page %s", page.id)
        report.warnings.append(f"{label} : image de la page indisponible ({exc}).")
    sheet = _sheet(render, page, image.size if image is not None else None)
    if sheet is None:
        sheet = _default_sheet(render)
    _start_page(c, sheet, report)
    if image is not None:
        rect = (sheet.content_x, sheet.content_y, sheet.content_width, sheet.content_height)
        _draw_image(c, image.convert("RGB") if image.mode not in ("RGB", "L") else image, rect, render, keep_ratio=False)
        report.image_blocks = 1
    else:
        report.warnings.append(f"{label} : page blanche (pas d'image de page).")
    if render.facsimile_text_layer:
        report.text_blocks = sum(1 for b in page.text_blocks if b.text.strip()
                                 and (render.include_hidden_blocks or not b.hidden))
        if not _text_layer(c, page, sheet, render, label, report):
            report.warnings.append(f"{label} : aucun texte OCR, la page ne sera pas cherchable.")
    c.showPage()
    return report


# --------------------------------------------------------------------------- public API

def _select_pages(project: Project, page_ids: list[str] | None) -> tuple[list[tuple[int, Page]], list[str]]:
    if page_ids is None:
        return list(enumerate(project.pages)), []
    wanted = set(page_ids)
    known = {p.id for p in project.pages}
    unknown = [pid for pid in page_ids if pid not in known]
    return [(i, p) for i, p in enumerate(project.pages) if p.id in wanted], unknown


def _new_canvas(buffer: BinaryIO, project: Project) -> pdfcanvas.Canvas:
    c = pdfcanvas.Canvas(buffer, pageCompression=1)
    c.setTitle(project.render.title or project.name)
    if project.render.author:
        c.setAuthor(project.render.author)
    c.setCreator(CREATOR)
    c.setProducer(CREATOR + " (ReportLab)")
    return c


def _draw_page(c, project: Project, page: Page, index: int, mode: str, image_provider: ImageProvider,
               page_image_provider: PageImageProvider | None) -> PageReport:
    if mode == "facsimile":
        return _draw_facsimile(c, project, page, index, page_image_provider)
    return _draw_reconstruction(c, project, page, index, image_provider)


def _check_mode(mode: str, page_image_provider) -> None:
    if mode not in ("reconstruction", "facsimile"):
        raise PDFGenerationError(f"Mode de PDF inconnu : « {mode} » (attendu : reconstruction ou facsimile).")
    if mode == "facsimile" and page_image_provider is None:
        raise PDFGenerationError("Le mode fac-similé a besoin des images de page : aucune n'a été fournie.")


def _write(data: bytes, output: Path | str | BinaryIO) -> None:
    if hasattr(output, "write"):
        try:
            output.write(data)
        except (OSError, ValueError) as exc:
            raise PDFGenerationError(f"Impossible d'écrire le PDF : {exc}") from exc
        return
    path = Path(output)
    if not path.parent.is_dir():
        raise PDFGenerationError(f"Impossible d'écrire le PDF : le dossier « {path.parent} » n'existe pas.")
    tmp = path.with_name(path.name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise PDFGenerationError(f"Impossible d'écrire le PDF « {path.name} » : {exc.strerror or exc}.") from exc


def generate_pdf(project: Project, output: Path | str | BinaryIO, mode: Mode, image_provider: ImageProvider,
                 page_image_provider: PageImageProvider | None = None, page_ids: list[str] | None = None,
                 progress: ProgressCallback | None = None) -> RenderReport:
    """Write the PDF of the project's pages (in project order, filtered by page_ids) and report on it.

    Raises PDFGenerationError (French message) when the PDF cannot be built or written.
    """
    _check_mode(mode, page_image_provider)
    selected, unknown = _select_pages(project, page_ids)
    if not selected:
        raise PDFGenerationError("Aucune page à exporter.")
    report = RenderReport(mode=mode)
    if mode == "facsimile":
        report.notes.append("PDF fac-similé : chaque page est l'image de la page originale, non reconstruite.")
    if unknown:
        report.notes.append(f"{len(unknown)} page(s) demandée(s) introuvable(s) dans le projet, ignorée(s).")
    buffer = io.BytesIO()
    try:
        c = _new_canvas(buffer, project)
        for n, (index, page) in enumerate(selected):
            if progress:
                progress(n, len(selected), f"Page {n + 1}/{len(selected)}")
            report.pages.append(_draw_page(c, project, page, index, mode, image_provider, page_image_provider))
        c.save()
    except LivreOCRError:
        raise
    except Exception as exc:
        log.exception("génération du PDF")
        raise PDFGenerationError(f"La génération du PDF a échoué : {exc}") from exc
    _write(buffer.getvalue(), output)
    if progress:
        progress(len(selected), len(selected), "PDF terminé")
    return report


def render_page_bytes(project: Project, page: Page, image_provider: ImageProvider, mode: Mode = "reconstruction",
                      page_image_provider: PageImageProvider | None = None) -> tuple[bytes, PageReport]:
    """One page as a one-page PDF, in memory."""
    _check_mode(mode, page_image_provider)
    try:
        index = project.page_index(page.id)
    except KeyError:
        index = 0
    buffer = io.BytesIO()
    try:
        c = _new_canvas(buffer, project)
        report = _draw_page(c, project, page, index, mode, image_provider, page_image_provider)
        c.save()
    except LivreOCRError:
        raise
    except Exception as exc:
        raise PDFGenerationError(f"La génération de l'aperçu a échoué : {exc}") from exc
    return buffer.getvalue(), report


def render_page_png(project: Project, page: Page, image_provider: ImageProvider, mode: Mode = "reconstruction",
                    width_px: int = 900, page_image_provider: PageImageProvider | None = None
                    ) -> tuple[Image.Image, PageReport]:
    """Preview of one page: the PDF page rasterised by pypdfium2 at `width_px` pixels wide (RGB)."""
    import pypdfium2 as pdfium

    data, report = render_page_bytes(project, page, image_provider, mode, page_image_provider)
    pdf = pdfium.PdfDocument(data)
    try:
        first = pdf[0]
        width_pt, height_pt = first.get_size()
        scale = max(width_px, 1) / width_pt
        image = first.render(scale=scale).to_pil().convert("RGB")
        first.close()
    finally:
        pdf.close()
    expected = (max(width_px, 1), max(1, round(height_pt * scale)))
    if image.size != expected:
        image = image.resize(expected, Image.LANCZOS)
    return image, report
