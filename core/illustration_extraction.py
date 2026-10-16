"""Illustrations of a page frame: photos and colour pictures, line drawings and ruled tables.

All detection runs on a copy at most `WORK_SIDE` pixels long; boxes are returned normalized (0..1 of
the frame). Rules (explainable, tuned on the synthetic pages of the tests):

- photo: a large non-paper area left after a wide morphological opening (letters, thin rules and
  page edges disappear), with many tones or real colour (a flat black bar is not a picture);
- table: long horizontal and vertical rules crossing at least `TABLE_MIN_CROSSINGS` times;
- drawing: a large, sparse, connected structure of thin strokes (a single ink component spanning a
  big area), which text never forms because letters are separate components.

A region is `uncertain` when confidently read text overlaps it or when its edges fade into the paper
(a loose and a strict threshold give clearly different boxes).
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .coordinates import norm_to_px
from .document_models import Box

WORK_SIDE = 1600
MIN_PICTURE_AREA = 0.01         # share of the frame a photo must cover
MIN_DRAWING_AREA = 0.03
MIN_DRAWING_SIDE = 0.08         # both sides of a drawing, share of the frame side
MAX_DRAWING_FILL = 0.35         # ink pixels / box area (higher is a solid shape, handled as photo)
MIN_TABLE_AREA = 0.02
TABLE_MIN_CROSSINGS = 6
CHROMA_COLOURFUL = 15.0         # Lab chroma above which an area is a colour picture
TONE_SPREAD = 18                # gray levels between the 10th and 90th percentiles of a photo
                                # (an underexposed picture measures ~22, a flat black bar ~0)
PADDING = 0.004                 # margin added around a picture, share of the frame side
FUZZY_EDGE = 0.04               # loose/strict box difference (share of the box side) => uncertain


@dataclass
class DetectedRegion:
    box: Box
    kind: str                   # "photo", "drawing" or "table"
    confidence: float           # 0..100
    uncertain: bool = False
    reason: str = ""


# --------------------------------------------------------------------------- helpers

def _downscale(rgb: np.ndarray) -> tuple[np.ndarray, float]:
    h, w = rgb.shape[:2]
    scale = min(1.0, WORK_SIDE / max(h, w))
    if scale >= 1.0:
        return rgb, 1.0
    return cv2.resize(rgb, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA), scale


def _ink(gray: np.ndarray) -> np.ndarray:
    paper = max(float(np.percentile(gray, 90)), 1.0)
    return (gray < 0.6 * paper).astype(np.uint8)


def _overlap_px(a, b) -> float:
    """Share of pixel box a (l, t, r, b) covered by pixel box b."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max((a[2] - a[0]) * (a[3] - a[1]), 1)
    return ix * iy / area


def _non_paper(lab: np.ndarray, lightness: float) -> np.ndarray:
    L = lab[..., 0]
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    paper = max(float(np.percentile(L, 90)), 1.0)
    return ((L < lightness * paper) | (chroma > 18)).astype(np.uint8)


def _big_areas(mask: np.ndarray) -> np.ndarray:
    """Remove letters, thin rules and small specks: what stays is a large solid area."""
    size = max(15, mask.shape[1] // 40) | 1
    bridged = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    return cv2.morphologyEx(bridged, cv2.MORPH_OPEN, np.ones((size, size), np.uint8))


# --------------------------------------------------------------------------- detectors

def _table_boxes(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """(l, t, r, b) of ruled tables: long horizontal and vertical rules crossing several times."""
    h, w = gray.shape
    ink = _ink(gray)
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 12), 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, h // 25))))
    kernel = np.ones((5, 5), np.uint8)
    grid = cv2.dilate(cv2.bitwise_or(horizontal, vertical), kernel)
    crossings = cv2.bitwise_and(cv2.dilate(horizontal, kernel), cv2.dilate(vertical, kernel))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(grid, connectivity=8)
    boxes = []
    for i in range(1, count):
        x, y, bw, bh, _ = stats[i]
        if bw * bh < MIN_TABLE_AREA * h * w:
            continue
        inside = crossings[y:y + bh, x:x + bw] * (labels[y:y + bh, x:x + bw] == i)
        n, _ = cv2.connectedComponents(inside.astype(np.uint8))
        if n - 1 >= TABLE_MIN_CROSSINGS:
            # the dilation added 2 px on each side
            boxes.append((int(x) + 2, int(y) + 2, int(x + bw) - 2, int(y + bh) - 2))
    return boxes


def _picture_boxes(rgb: np.ndarray) -> list[tuple[tuple[int, int, int, int], float, float]]:
    """[(l, t, r, b), confidence, fuzziness] of photos and colour pictures."""
    lab = cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)
    big = _big_areas(_non_paper(lab, 0.88))
    loose = _big_areas(_non_paper(lab, 0.96))
    L = lab[..., 0]
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    smooth = cv2.medianBlur(np.clip(L * 2.55, 0, 255).astype(np.uint8), 9)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(big, connectivity=8)
    loose_labels = cv2.connectedComponents(loose, connectivity=8)[1]
    out = []
    for i in range(1, count):
        x, y, bw, bh, area = stats[i]
        if area < MIN_PICTURE_AREA * big.size:
            continue
        region = labels == i
        tones = smooth[region]
        spread = float(np.percentile(tones, 90) - np.percentile(tones, 10))
        colour = float(np.median(chroma[region]))
        if colour <= CHROMA_COLOURFUL and spread < TONE_SPREAD:
            continue          # flat dark area (black bar, thick rule, dense bold text)
        touches_edge = x <= 1 or y <= 1 or x + bw >= big.shape[1] - 1 or y + bh >= big.shape[0] - 1
        if touches_edge and colour <= CHROMA_COLOURFUL and max(bw / bh, bh / bw) >= 4:
            continue          # binding shadow or dark border along the frame edge
        box = (int(x), int(y), int(x + bw), int(y + bh))
        confidence = min(99.0, 50.0 + 25.0 * min(colour / CHROMA_COLOURFUL, 1.0) + 25.0 * min(spread / 120.0, 1.0))
        if colour <= CHROMA_COLOURFUL and spread < 55:
            confidence = min(confidence, 55.0)   # dark, almost grey picture: plausible but weak
        # Fuzzy edges: the same area thresholded more loosely grows noticeably.
        ids = np.unique(loose_labels[region])
        ids = ids[ids > 0]
        fuzziness = 0.0
        if ids.size:
            ys, xs = np.nonzero(np.isin(loose_labels[max(0, y - bh):y + 2 * bh, max(0, x - bw):x + 2 * bw], ids))
            if xs.size:
                lx0, ly0 = xs.min() + max(0, x - bw), ys.min() + max(0, y - bh)
                lx1, ly1 = xs.max() + 1 + max(0, x - bw), ys.max() + 1 + max(0, y - bh)
                fuzziness = max((x - lx0) / bw, (lx1 - x - bw) / bw, (y - ly0) / bh, (ly1 - y - bh) / bh, 0.0)
        out.append((box, confidence, fuzziness))
    return out


def _drawing_boxes(gray: np.ndarray, exclude: list, text_boxes: list) -> list[tuple[int, int, int, int]]:
    """Large line drawings: one connected ink structure spanning a big area with little ink."""
    h, w = gray.shape
    ink = cv2.morphologyEx(_ink(gray), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    boxes = []
    for i in range(1, count):
        x, y, bw, bh, area = stats[i]
        if bw * bh < MIN_DRAWING_AREA * h * w or bw < MIN_DRAWING_SIDE * w or bh < MIN_DRAWING_SIDE * h:
            continue
        if area / float(bw * bh) >= MAX_DRAWING_FILL:
            continue
        box = (int(x), int(y), int(x + bw), int(y + bh))
        if any(_overlap_px(box, e) > 0.5 for e in exclude):
            continue
        if text_boxes and sum(_overlap_px(t, box) > 0.5 for t in text_boxes) >= 3 \
                and sum((t[2] - t[0]) * (t[3] - t[1]) for t in text_boxes if _overlap_px(t, box) > 0.5) > 0.5 * bw * bh:
            continue      # mostly text (a framed paragraph)
        boxes.append(box)
    return boxes


def _pad(box, padding_px: int, shape, text_boxes: list) -> tuple[int, int, int, int]:
    """Grow a box by a few pixels on each side, stopping before neighbouring text."""
    h, w = shape
    l, t, r, b = box
    nl, nt, nr, nb = max(0, l - padding_px), max(0, t - padding_px), min(w, r + padding_px), min(h, b + padding_px)
    for tl, tt, tr, tb in text_boxes:
        if _overlap_px((tl, tt, tr, tb), box) > 0.3:
            continue       # text inside the picture: not a neighbour
        if tr > nl and tl < nr:          # horizontally in front: limit vertical growth
            if tb <= t and tb > nt:
                nt = tb
            if tt >= b and tt < nb:
                nb = tt
        if tb > nt and tt < nb:          # vertically in front: limit horizontal growth
            if tr <= l and tr > nl:
                nl = tr
            if tl >= r and tl < nr:
                nr = tl
    return nl, nt, nr, nb


# --------------------------------------------------------------------------- public API

def detect_illustrations(frame_rgb: np.ndarray, text_boxes_px=()) -> list[DetectedRegion]:
    """Photos, drawings and ruled tables of a frame, as normalized boxes.

    `text_boxes_px`: (left, top, right, bottom) pixel boxes, in the frame, of text read with good
    confidence; they mark overlapping regions as uncertain and stop the padding.
    """
    if frame_rgb.ndim == 2:
        frame_rgb = cv2.cvtColor(frame_rgb, cv2.COLOR_GRAY2RGB)
    H, W = frame_rgb.shape[:2]
    if H < 16 or W < 16:
        return []
    small, scale = _downscale(frame_rgb)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    texts = [tuple(int(round(v * scale)) for v in t) for t in text_boxes_px]

    tables = _table_boxes(gray)
    pictures = [p for p in _picture_boxes(small) if not any(_overlap_px(p[0], t) > 0.5 for t in tables)]
    drawings = _drawing_boxes(gray, [p[0] for p in pictures] + tables, texts)

    pad = max(1, int(round(PADDING * max(small.shape[:2]))))
    found: list[tuple[str, tuple, float, float]] = []
    found += [("table", t, 80.0, 0.0) for t in tables]
    found += [("photo", b, c, f) for b, c, f in pictures]
    found += [("drawing", d, 60.0, 0.0) for d in drawings]

    regions = []
    for kind, box, confidence, fuzziness in found:
        reasons = []
        overlapping = [t for t in texts if _overlap_px(t, box) > 0.5]
        if kind != "table" and overlapping:
            reasons.append(f"{len(overlapping)} zone(s) de texte lisible dans l'illustration")
        if fuzziness > FUZZY_EDGE:
            reasons.append("bords flous : vérifier le cadrage")
        padded = box if kind == "table" else _pad(box, pad, small.shape[:2], texts)
        l, t, r, b = (v / scale for v in padded)
        norm = Box.clamped(l / W, t / H, (r - l) / W, (b - t) / H)
        regions.append(DetectedRegion(norm, kind, round(confidence, 1), bool(reasons), " ; ".join(reasons)))
    regions.sort(key=lambda reg: (reg.box.y, reg.box.x))
    return regions


def crop_region(frame_rgb: np.ndarray, box: Box, padding: float = 0.0) -> np.ndarray:
    """Exact pixels of a region at full resolution (a copy; no resampling, no colour change).

    `padding` grows the box by this share of the frame side on each side (clipped to the frame).
    """
    h, w = frame_rgb.shape[:2]
    if padding:
        box = Box.clamped(box.x - padding, box.y - padding, box.width + 2 * padding, box.height + 2 * padding)
    left, top, right, bottom = norm_to_px(box, w, h)
    return frame_rgb[top:bottom, left:right].copy()

