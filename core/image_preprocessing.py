"""Geometry of the page frame and the images derived from it (pure functions, no interface).

`build_frame` turns an original (already upright according to EXIF) into the page frame, in the
order documented on `GeometrySettings`. Everything detected (orientation, sheet, skew) is stored
in the returned geometry, rounded, and the frame is always built from those stored values: so
`build_frame(detect=False)` with the returned geometry gives exactly the same image.

Conventions:
- quarter turns are clockwise;
- quads are tl, tr, br, bl in 0..1 of the image *after* the quarter turns;
- skew angles are the tilt of the content, in degrees, counter-clockwise positive (the value
  `estimate_skew` measures). The frame is straightened by turning it by the opposite angle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import cv2
import numpy as np

from .coordinates import norm_to_px
from .document_models import Box, GeometrySettings, ImageAdjustments, LocalAdjustment, ProcessingMode, AdjustmentTarget
from .image_adjustments import (_check_rgb, _odd, _picture_mask_small, _small, apply_adjustments,
                                apply_local_adjustments, binarize_sauvola, illumination_gain)
from .image_loader import read_image

__all__ = ["FrameResult", "load_oriented", "detect_page_quad", "estimate_skew", "build_frame",
           "render_print_image", "render_ocr_image", "suggest_mode", "binarize_sauvola", "downscale",
           "apply_quad", "rotate_quarter", "rotate_fine", "crop_margins_box", "clean_text_region"]

MIN_FRAME_SIDE = 50          # a cut-out smaller than this is refused (kept whole, with a warning)
SKEW_MIN_APPLY = 0.02        # degrees: below this the fine rotation is skipped
QUAD_DECIMALS = 5
SKEW_DECIMALS = 2


@dataclass
class FrameResult:
    image: np.ndarray                 # RGB uint8 page frame
    geometry: GeometrySettings        # input settings with the detected_* fields filled
    warnings: list[str] = field(default_factory=list)


def load_oriented(path: Path, source_index: int = 0) -> np.ndarray:
    """The stored original as RGB uint8, upright according to EXIF (delegates to image_loader)."""
    return read_image(Path(path), source_index)


def downscale(rgb: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    """(image no larger than max_side, factor new/old ≤ 1). Returns the input when small enough."""
    h, w = rgb.shape[:2]
    if max(h, w) <= max_side:
        return rgb, 1.0
    factor = max_side / max(h, w)
    size = (max(1, round(w * factor)), max(1, round(h * factor)))
    return cv2.resize(rgb, size, interpolation=cv2.INTER_AREA), factor


def _gray(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)


# --------------------------------------------------------------------------- sheet detection

def _order_corners(points: np.ndarray) -> np.ndarray:
    """tl, tr, br, bl: sorted clockwise around the centre, starting at the smallest x + y."""
    centre = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - centre[1], points[:, 0] - centre[0])
    pts = points[np.argsort(angles)]
    start = int(np.argmin(pts.sum(axis=1)))
    return np.roll(pts, -start, axis=0)


def _quad_from_contour(contour: np.ndarray) -> np.ndarray:
    """Four corners of a page outline: polygon simplification, else the extreme points."""
    hull = cv2.convexHull(contour)
    perimeter = cv2.arcLength(hull, True)
    for eps in (0.01, 0.02, 0.03, 0.05, 0.08):
        approx = cv2.approxPolyDP(hull, eps * perimeter, True)
        if len(approx) == 4:
            return _order_corners(approx.reshape(4, 2).astype(np.float32))
    pts = hull.reshape(-1, 2).astype(np.float32)
    s, d = pts.sum(axis=1), pts[:, 1] - pts[:, 0]
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)


def detect_page_quad(rgb: np.ndarray) -> list[tuple[float, float]] | None:
    """Corners (tl, tr, br, bl, 0..1) of a bright sheet photographed on a darker background.

    Returns None when there is no clear sheet: a scan that fills the image, a sheet that is not
    surrounded by darker pixels, or a shape that is not a quadrilateral. Never invents a crop.
    """
    small, _ = downscale(rgb, 1000)
    h, w = small.shape[:2]
    gray = cv2.GaussianBlur(_gray(small), (5, 5), 0)
    otsu, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = (gray > otsu).astype(np.uint8)
    if mask.mean() < 0.1 or mask.mean() > 0.97:
        return None
    # Text and drawings punch holes in the sheet: fill every large outer shape.
    outlines, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    big = [c for c in outlines if cv2.contourArea(c) > 0.03 * h * w]
    if not big:
        return None
    filled = np.zeros_like(mask)
    cv2.drawContours(filled, big, -1, 1, thickness=cv2.FILLED)
    # A small erosion detaches thin bright bridges (reflections, a neighbouring page edge).
    erode = max(2, round(max(h, w) * 0.004))
    kernel = np.ones((2 * erode + 1, 2 * erode + 1), np.uint8)
    core = cv2.erode(filled, kernel)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
    candidates = []
    for i in range(1, count):
        x, y, cw, ch, area = stats[i]
        if area < 0.12 * h * w:
            continue
        touches = x <= 1 or y <= 1 or x + cw >= w - 1 or y + ch >= h - 1
        candidates.append((touches, -area, i))
    if not candidates:
        return None
    _, _, best = min(candidates)
    blob = cv2.bitwise_and(cv2.dilate((labels == best).astype(np.uint8), kernel), filled)
    contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    quad = _quad_from_contour(contour)
    quad_area = cv2.contourArea(quad)
    if quad_area <= 0 or quad_area > 0.92 * h * w or quad_area < 0.12 * h * w:
        return None                        # fills the picture (a scan), or too small to be the page
    if cv2.contourArea(contour) / quad_area < 0.9 or not cv2.isContourConvex(quad.reshape(-1, 1, 2)):
        return None                        # not a quadrilateral sheet
    # A real sheet photo is surrounded by darker pixels; an unevenly lit scan is not.
    polygon = np.zeros((h, w), np.uint8)
    cv2.fillPoly(polygon, [np.round(quad).astype(np.int32)], 1)
    ring_size = max(3, round(max(h, w) * 0.02))
    ring = cv2.dilate(polygon, np.ones((2 * ring_size + 1,) * 2, np.uint8)) - polygon
    if ring.sum() < 0.1 * ring_size * (h + w):
        return None
    inside = float(np.median(gray[polygon > 0]))
    if np.mean(gray[ring > 0] < 0.6 * inside) < 0.6:
        return None
    # Corners of the pixel grid (the contour runs through pixel centres).
    quad = quad + 0.5 * np.sign(quad - quad.mean(axis=0))
    return [(round(float(np.clip(x / w, 0, 1)), QUAD_DECIMALS), round(float(np.clip(y / h, 0, 1)), QUAD_DECIMALS))
            for x, y in quad]


def apply_quad(rgb: np.ndarray, quad: list[tuple[float, float]]) -> np.ndarray | None:
    """Straighten the quadrilateral (tl, tr, br, bl in 0..1) to a rectangle; None if too small."""
    h, w = rgb.shape[:2]
    pts = np.array(quad, np.float32) * np.array([w, h], np.float32)
    tl, tr, br, bl = pts
    out_w = int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))))
    out_h = int(round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))))
    if out_w < MIN_FRAME_SIDE or out_h < MIN_FRAME_SIDE:
        return None
    target = np.array([[0, 0], [out_w, 0], [out_w, out_h], [0, out_h]], np.float32)
    matrix = cv2.getPerspectiveTransform(pts, target)
    return cv2.warpPerspective(rgb, matrix, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


# --------------------------------------------------------------------------- rotations

def rotate_quarter(rgb: np.ndarray, degrees: int) -> np.ndarray:
    """Clockwise quarter turn (0, 90, 180, 270), lossless."""
    degrees %= 360
    if degrees not in (0, 90, 180, 270):
        raise ValueError("rotation par quarts de tour uniquement")
    if degrees == 0:
        return rgb
    return np.ascontiguousarray(np.rot90(rgb, k=-degrees // 90))


def _paper_colour(rgb: np.ndarray) -> tuple[int, int, int]:
    """Bright colour of a 2 % border strip (the paper), used to fill corners after a rotation."""
    h, w = rgb.shape[:2]
    b = max(1, round(0.02 * min(h, w)))
    strip = np.concatenate([rgb[:b].reshape(-1, 3), rgb[-b:].reshape(-1, 3),
                            rgb[:, :b].reshape(-1, 3), rgb[:, -b:].reshape(-1, 3)])
    return tuple(int(v) for v in np.percentile(strip, 90, axis=0))


def rotate_fine(rgb: np.ndarray, angle: float) -> np.ndarray:
    """Turn by `angle` degrees counter-clockwise, same size; corners filled with the paper colour."""
    if abs(angle) < 1e-6:
        return rgb
    h, w = rgb.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(rgb, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                          borderValue=_paper_colour(rgb))


# --------------------------------------------------------------------------- skew

def _profile_score(xs: np.ndarray, ys: np.ndarray, angle: float, height: int) -> float:
    """Sharpness of the row profile once the content is turned back by `angle` (degrees)."""
    a = np.deg2rad(angle)
    rows = np.round(xs * np.sin(a) + ys * np.cos(a)).astype(np.int64)
    rows -= rows.min()
    counts = np.bincount(rows).astype(np.float64)
    return float(counts @ counts)


def estimate_skew(img: np.ndarray, max_angle: float = 10.0) -> float:
    """Tilt of the text lines in degrees (counter-clockwise positive), 0.0 when uncertain.

    Projection profile on letter-sized ink components of a reduced copy: the row histogram is
    sharpest when the lines are level. Coarse search (0.5°) then fine search (0.05°).
    """
    gray = _gray(img)
    small, _ = downscale(gray, 1200)
    h, w = small.shape
    ink = cv2.threshold(small, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    share = float(ink.mean())
    if share < 0.0005 or share > 0.5:
        return 0.0
    # Keep letter-sized shapes only: pictures, rules and page borders would blur the profile.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    ok = (stats[:, 3] <= 0.06 * h) & (stats[:, 2] <= 0.3 * w) & (stats[:, 4] >= 2)
    ok[0] = False
    ys, xs = np.nonzero(ok[labels])
    if len(xs) < 200:
        return 0.0
    if len(xs) > 300_000:
        step = len(xs) // 300_000 + 1
        xs, ys = xs[::step], ys[::step]
    xs = xs.astype(np.float64) - w / 2
    ys = ys.astype(np.float64) - h / 2
    coarse = np.arange(-max_angle, max_angle + 1e-9, 0.5)
    scores = np.array([_profile_score(xs, ys, a, h) for a in coarse])
    best = float(coarse[int(np.argmax(scores))])
    fine = np.arange(best - 0.6, best + 0.6 + 1e-9, 0.05)
    fine_scores = np.array([_profile_score(xs, ys, a, h) for a in fine])
    i = int(np.argmax(fine_scores))
    best = float(fine[i])
    if 0 < i < len(fine) - 1:          # parabola through the peak and its neighbours
        y0, y1, y2 = fine_scores[i - 1:i + 2]
        denom = y0 - 2 * y1 + y2
        if denom < 0:
            best += 0.05 * 0.5 * (y0 - y2) / denom
    # A flat score curve (little text, no lines) gives no reliable angle.
    level = _profile_score(xs, ys, 0.0, h)
    if fine_scores[i] < 1.02 * level and abs(best) > 0.3:
        return 0.0
    if fine_scores[i] < 1.15 * float(np.median(scores)):
        return 0.0
    return round(best, SKEW_DECIMALS)


# --------------------------------------------------------------------------- margins

def crop_margins_box(rgb: np.ndarray, padding: float = 0.02) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) pixels of the content plus a safety padding, or None.

    Ink is anything clearly darker than the local paper (background measured at low resolution,
    so a shadowed side is not taken for content). None when the page is nearly empty or the
    content is sparse (less than 25 % of the width or height): never an aggressive crop.
    `padding` is a fraction of the short side (at least 8 px).
    """
    gray = _gray(rgb)
    h, w = gray.shape
    small, factor = downscale(gray, 600)
    side = max(small.shape)
    background = cv2.morphologyEx(small, cv2.MORPH_CLOSE, np.ones((_odd(side / 40),) * 2, np.uint8))
    background = cv2.GaussianBlur(background, (0, 0), side / 60)
    if factor < 1:
        background = cv2.resize(background, (w, h), interpolation=cv2.INTER_LINEAR)
    threshold = background.astype(np.int16) - np.maximum(25, background // 8).astype(np.int16)
    ink = gray.astype(np.int16) < threshold
    rows, cols = np.flatnonzero(ink.any(axis=1)), np.flatnonzero(ink.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return None
    top, bottom, left, right = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
    if right - left < 0.25 * w or bottom - top < 0.25 * h:
        return None
    pad = max(8, round(padding * min(h, w)))
    box = (max(0, left - pad), max(0, top - pad), min(w, right + pad), min(h, bottom + pad))
    return None if box == (0, 0, w, h) else box


# --------------------------------------------------------------------------- frame

def _normal_turn(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, tuple):         # (turn, confidence) as returned by ocr_engine.detect_orientation
        value = value[0]
        if value is None:
            return None
    turn = int(round(float(value) / 90.0)) * 90 % 360
    return turn


def build_frame(original_rgb: np.ndarray, geometry: GeometrySettings, detect: bool = True,
                orientation_detector: Callable[[np.ndarray], int | None] | None = None) -> FrameResult:
    """Build the page frame from the upright original, following `GeometrySettings`.

    detect=True: orientation (only through `orientation_detector`, called on the image turned by
    `rotation`; without detector the stored value is kept), sheet quad (when `auto_crop` and no
    manual quad) and skew (when `auto_deskew` and no manual angle) are measured and stored in the
    returned geometry. detect=False: the stored detected_* values are reused as they are.
    The margin auto-trim is recomputed each time (deterministic, so still reproducible).
    """
    _check_rgb(original_rgb)
    warnings: list[str] = []
    updates: dict = {}

    # 1. Quarter turns (user turn + detected orientation).
    if detect and geometry.auto_orientation and orientation_detector is not None:
        try:
            updates["detected_orientation"] = _normal_turn(orientation_detector(rotate_quarter(original_rgb, geometry.rotation)))
        except Exception as exc:     # the detector belongs to another module (Tesseract OSD)
            warnings.append(f"Orientation automatique impossible ({exc}).")
    geo = geometry.model_copy(update=updates) if updates else geometry
    img = rotate_quarter(original_rgb, geo.effective_rotation())

    # 2. Sheet cut-out and perspective.
    if detect and geo.auto_crop and not geo.manual_quad:
        updates["detected_quad"] = detect_page_quad(img)
        geo = geometry.model_copy(update=updates)
    quad = geo.effective_quad()
    if quad:
        warped = apply_quad(img, quad)
        if warped is None:
            warnings.append("Zone de page trop petite : image entière conservée.")
        else:
            img = warped

    # 3. Fine rotation.
    if detect and geo.auto_deskew and geo.deskew_angle is None:
        updates["detected_skew"] = estimate_skew(img)
        geo = geometry.model_copy(update=updates)
    skew = geo.effective_skew()
    if abs(skew) >= SKEW_MIN_APPLY:
        img = rotate_fine(img, -skew)

    # 4. Margins: percentages, then the automatic content box.
    h, w = img.shape[:2]
    left, right = round(w * geo.trim_left / 100), w - round(w * geo.trim_right / 100)
    top, bottom = round(h * geo.trim_top / 100), h - round(h * geo.trim_bottom / 100)
    if (left, top, right, bottom) != (0, 0, w, h):
        if right - left < MIN_FRAME_SIDE or bottom - top < MIN_FRAME_SIDE:
            warnings.append("Rognage des marges trop fort : ignoré.")
        else:
            img = img[top:bottom, left:right]
    if geo.auto_trim_margins:
        box = crop_margins_box(img)
        if box is not None:
            img = img[box[1]:box[3], box[0]:box[2]]

    # 5. Resolution.
    if geo.scale_percent != 100.0:
        h, w = img.shape[:2]
        size = (max(1, round(w * geo.scale_percent / 100)), max(1, round(h * geo.scale_percent / 100)))
        interp = cv2.INTER_AREA if geo.scale_percent < 100 else cv2.INTER_CUBIC
        img = cv2.resize(img, size, interpolation=interp)

    if img is original_rgb or not img.flags.c_contiguous or img.base is not None:
        img = np.array(img, copy=True)    # the frame never shares memory with the original
    final = GeometrySettings.model_validate({**geometry.model_dump(), **updates})
    return FrameResult(img, final, warnings)


# --------------------------------------------------------------------------- modes

def clean_text_region(rgb: np.ndarray) -> np.ndarray:
    """Whiter paper and deeper ink in a text region, ink colour kept (luma levels as a ratio)."""
    f = rgb.astype(np.float32) / 255.0
    y = cv2.cvtColor(f, cv2.COLOR_RGB2GRAY)
    paper = max(float(np.percentile(y, 90)), 0.05)
    ink = min(float(np.percentile(y, 1)), 0.5 * paper)
    low, high = ink + 0.15 * (paper - ink), 0.85 * paper
    new = np.clip((y - low) / max(high - low, 1e-3), 0.0, 1.0)
    # Ratio for mid-tones (keeps coloured ink), straight white for paper.
    ratio = new / np.maximum(y, 1e-3)
    out = f * ratio[..., None]
    out[new >= 1.0] = 1.0
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


def render_print_image(frame_rgb: np.ndarray, page_adjust: ImageAdjustments, locals: Iterable[LocalAdjustment],
                       mode: ProcessingMode, text_boxes: Iterable[Box] = ()) -> np.ndarray:
    """Colour image of the page (facsimile, illustrations): global then local corrections.

    Never turns grey or binary by itself: only `page_adjust` can ask for it. In MIXED mode the
    text regions are also cleaned (whiter paper, more contrast) while the rest keeps the colour
    treatment.
    """
    _check_rgb(frame_rgb)
    out = apply_adjustments(frame_rgb, page_adjust)
    if ProcessingMode(mode) == ProcessingMode.MIXED:
        h, w = out.shape[:2]
        for box in text_boxes:
            left, top, right, bottom = norm_to_px(box, w, h)
            if out is frame_rgb:
                out = frame_rgb.copy()
            out[top:bottom, left:right] = clean_text_region(out[top:bottom, left:right])
    return apply_local_adjustments(out, list(locals), AdjustmentTarget.PRINT, reference=frame_rgb)


def render_ocr_image(frame_rgb: np.ndarray, ocr_adjust: ImageAdjustments, locals: Iterable[LocalAdjustment],
                     mode: ProcessingMode, illustration_boxes: Iterable[Box] = ()) -> np.ndarray:
    """Grey uint8 image read by the OCR.

    Lighting is always flattened (unless the whole set is disabled), then `ocr_adjust` and the
    OCR local corrections apply. In MIXED and ILLUSTRATION modes the illustration boxes are
    filled with white so the OCR does not read the pictures.
    """
    _check_rgb(frame_rgb)
    adjust = ocr_adjust
    if ocr_adjust.enabled and not ocr_adjust.illumination_correction:
        adjust = ocr_adjust.model_copy(update={"illumination_correction": True})
    out = apply_adjustments(frame_rgb, adjust)
    out = apply_local_adjustments(out, list(locals), AdjustmentTarget.OCR, reference=frame_rgb)
    gray = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)
    if ProcessingMode(mode) in (ProcessingMode.MIXED, ProcessingMode.ILLUSTRATION):
        h, w = gray.shape
        for box in illustration_boxes:
            left, top, right, bottom = norm_to_px(box, w, h)
            gray[top:bottom, left:right] = 255
    return gray


def suggest_mode(frame_rgb: np.ndarray) -> ProcessingMode:
    """TEXT without large picture, ILLUSTRATION when a picture covers nearly the whole page (or
    most of it with almost no text around), MIXED otherwise. Never AUTO."""
    _check_rgb(frame_rgb)
    small = _small(frame_rgb)
    pictures = _picture_mask_small(small)
    share = float(pictures.mean())
    if share < 0.03:
        return ProcessingMode.TEXT
    if share >= 0.75:
        return ProcessingMode.ILLUSTRATION
    # Letters outside the pictures.
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    ink = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    ink[pictures] = 0
    count, _, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    h = gray.shape[0]
    letters = int(np.sum((stats[1:, 3] >= 3) & (stats[1:, 3] <= 0.05 * h) & (stats[1:, 4] >= 6)))
    if share >= 0.5 and letters < 40:
        return ProcessingMode.ILLUSTRATION
    return ProcessingMode.MIXED
