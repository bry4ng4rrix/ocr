"""Coordinate conversions between the normalized frame, image pixels and PDF points.

- Normalized frame: 0..1, origin top-left, y downwards (document_models).
- Pixels: same orientation, scaled by the image size.
- PDF (ReportLab): points (1/72 inch), origin **bottom-left**, y upwards. `content_box` is where the
  frame is drawn on the sheet; `norm_to_pdf_rect` flips the y axis.
"""
from __future__ import annotations

from dataclasses import dataclass

from .document_models import PAGE_SIZES_MM, Box, Page, PageSize, RenderSettings

PT_PER_INCH = 72.0
MM_PER_INCH = 25.4
PT_PER_MM = PT_PER_INCH / MM_PER_INCH


def mm_to_pt(mm: float) -> float:
    return mm * PT_PER_MM


def pt_to_mm(pt: float) -> float:
    return pt / PT_PER_MM


def norm_to_px(box: Box, width: int, height: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) integer pixel box, at least 1 px, inside the image."""
    left = min(max(int(round(box.x * width)), 0), max(width - 1, 0))
    top = min(max(int(round(box.y * height)), 0), max(height - 1, 0))
    right = min(max(int(round(box.x2 * width)), left + 1), width)
    bottom = min(max(int(round(box.y2 * height)), top + 1), height)
    return left, top, right, bottom


def px_to_norm(left: float, top: float, right: float, bottom: float, width: int, height: int) -> Box:
    return Box.clamped(left / width, top / height, (right - left) / width, (bottom - top) / height)


@dataclass(frozen=True)
class SheetGeometry:
    """Page size in points and the rectangle (bottom-left origin) where the frame is drawn."""
    page_width: float
    page_height: float
    content_x: float
    content_y: float        # bottom of the content box
    content_width: float
    content_height: float

    @property
    def content_top(self) -> float:
        return self.content_y + self.content_height


def sheet_geometry(render: RenderSettings, frame_width: int, frame_height: int) -> SheetGeometry:
    """Where a frame of this pixel size goes on the PDF page.

    AUTO: the page is the frame at `auto_dpi` (no margin). Fixed formats: the frame is scaled to fit
    inside the margins, keeping its proportions, centred; the sheet turns landscape for a landscape
    frame when `orientation_follows_page`.
    """
    if frame_width <= 0 or frame_height <= 0:
        raise ValueError("taille de cadre invalide")
    if render.page_size == PageSize.AUTO:
        w = frame_width / render.auto_dpi * PT_PER_INCH
        h = frame_height / render.auto_dpi * PT_PER_INCH
        return SheetGeometry(w, h, 0.0, 0.0, w, h)
    if render.page_size == PageSize.CUSTOM:
        w_mm, h_mm = render.custom_width_mm, render.custom_height_mm
    else:
        w_mm, h_mm = PAGE_SIZES_MM[render.page_size]
    if render.orientation_follows_page and (frame_width > frame_height) != (w_mm > h_mm):
        w_mm, h_mm = h_mm, w_mm
    page_w, page_h = mm_to_pt(w_mm), mm_to_pt(h_mm)
    margin = mm_to_pt(render.margin_mm)
    avail_w, avail_h = max(page_w - 2 * margin, 1.0), max(page_h - 2 * margin, 1.0)
    scale = min(avail_w / frame_width, avail_h / frame_height)
    cw, ch = frame_width * scale, frame_height * scale
    return SheetGeometry(page_w, page_h, (page_w - cw) / 2, (page_h - ch) / 2, cw, ch)


def page_sheet_geometry(render: RenderSettings, page: Page) -> SheetGeometry:
    return sheet_geometry(render, page.frame_width or page.original_width, page.frame_height or page.original_height)


def norm_to_pdf_rect(box: Box, sheet: SheetGeometry) -> tuple[float, float, float, float]:
    """(x, y_bottom, width, height) in PDF points, bottom-left origin."""
    x = sheet.content_x + box.x * sheet.content_width
    w = box.width * sheet.content_width
    h = box.height * sheet.content_height
    y_bottom = sheet.content_top - (box.y + box.height) * sheet.content_height
    return x, y_bottom, w, h


def pdf_rect_to_norm(x: float, y_bottom: float, w: float, h: float, sheet: SheetGeometry) -> Box:
    """Inverse of norm_to_pdf_rect."""
    nx = (x - sheet.content_x) / sheet.content_width
    ny = (sheet.content_top - (y_bottom + h)) / sheet.content_height
    return Box.clamped(nx, ny, w / sheet.content_width, h / sheet.content_height)


def font_scale(render: RenderSettings, page: Page, sheet: SheetGeometry) -> float:
    """Factor applied to stored font sizes (which refer to page.font_reference_height_pt)."""
    if not render.scale_fonts_with_page or page.font_reference_height_pt <= 0:
        return 1.0
    return sheet.content_height / page.font_reference_height_pt


def rotate_box_quarter(box: Box, degrees: int) -> Box:
    """The same region after the frame is turned clockwise by 0/90/180/270 degrees."""
    degrees %= 360
    x, y, w, h = box.x, box.y, box.width, box.height
    if degrees == 0:
        return box
    if degrees == 90:      # (x, y) -> (1 - y - h, x)
        return Box.clamped(1.0 - y - h, x, h, w)
    if degrees == 180:
        return Box.clamped(1.0 - x - w, 1.0 - y - h, w, h)
    if degrees == 270:     # (x, y) -> (y, 1 - x - w)
        return Box.clamped(y, 1.0 - x - w, h, w)
    raise ValueError("rotation par quarts de tour uniquement")


def iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a.x2, b.x2) - max(a.x, b.x))
    iy = max(0.0, min(a.y2, b.y2) - max(a.y, b.y))
    inter = ix * iy
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def overlap_ratio(a: Box, b: Box) -> float:
    """Share of `a` covered by `b`."""
    ix = max(0.0, min(a.x2, b.x2) - max(a.x, b.x))
    iy = max(0.0, min(a.y2, b.y2) - max(a.y, b.y))
    return ix * iy / a.area if a.area > 0 else 0.0
