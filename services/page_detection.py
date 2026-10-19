"""Détection prudente des pages physiques dans une photographie."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class PageCandidate:
    """Quadrilatère normalisé dans l'image source, coins TL, TR, BR, BL."""

    corners: tuple[tuple[float, float], ...]
    confidence: float
    warning: str = ""


def _order(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    sums, diffs = points.sum(axis=1), np.diff(points, axis=1).ravel()
    return np.array([points[np.argmin(sums)], points[np.argmin(diffs)],
                     points[np.argmax(sums)], points[np.argmax(diffs)]], dtype=np.float32)


def detect_pages(rgb: np.ndarray, allow_spread: bool = True) -> list[PageCandidate]:
    """Retourne une ou deux détections plausibles ; retourne toute l'image si aucune feuille nette."""
    height, width = rgb.shape[:2]
    scale = min(1.0, 1400 / max(height, width))
    small = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 35, 110)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    quads: list[tuple[float, np.ndarray]] = []
    area_image = small.shape[0] * small.shape[1]
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:30]:
        area = cv2.contourArea(contour)
        if area < area_image * 0.12:
            continue
        approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        corners = _order(approx.reshape(4, 2))
        rect_area = max(1.0, cv2.contourArea(corners))
        rectangularity = min(1.0, area / rect_area)
        score = min(1.0, area / area_image) * (0.5 + rectangularity / 2)
        quads.append((score, corners / scale))
    # Rechercher les contours sur chaque demi-image évite que la reliure fusionne le livre en un seul quad.
    if allow_spread and small.shape[1] / small.shape[0] > 1.18:
        half = small.shape[1] // 2
        split_candidates: list[tuple[float, np.ndarray]] = []
        for offset, roi in ((0, gray[:, :half]), (half, gray[:, half:])):
            roi_edges = cv2.Canny(roi, 30, 100)
            roi_edges = cv2.dilate(roi_edges, np.ones((3, 3), np.uint8), iterations=1)
            roi_contours, _ = cv2.findContours(roi_edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            candidates = []
            for contour in sorted(roi_contours, key=cv2.contourArea, reverse=True)[:16]:
                area = cv2.contourArea(contour)
                if area < roi.shape[0] * roi.shape[1] * 0.15:
                    continue
                approx = cv2.approxPolyDP(contour, 0.025 * cv2.arcLength(contour, True), True)
                if len(approx) == 4 and cv2.isContourConvex(approx):
                    corners = _order(approx.reshape(4, 2))
                    score = min(1.0, area / (roi.shape[0] * roi.shape[1]))
                    candidates.append((score, corners))
            if candidates:
                score, corners = max(candidates, key=lambda item: item[0])
                corners[:, 0] += offset
                split_candidates.append((score, corners / scale))
        if len(split_candidates) == 2 and min(item[0] for item in split_candidates) >= 0.18:
            return [PageCandidate(tuple(map(tuple, corners / [width, height])), float(score),
                                  "Double page proposée automatiquement : vérifiez la reliure et les marges.")
                    for score, corners in split_candidates]
    # Two comparably sized, side-by-side page contours are a spread candidate.
    if allow_spread and len(quads) >= 2:
        quads.sort(key=lambda item: item[1][:, 0].mean())
        left, right = quads[0], quads[-1]
        a, b = cv2.contourArea(left[1]), cv2.contourArea(right[1])
        centers = (left[1][:, 0].mean(), right[1][:, 0].mean())
        if min(a, b) / max(a, b) > 0.45 and centers[1] - centers[0] > width * 0.18:
            return [PageCandidate(tuple(map(tuple, p / [width, height])), float(s),
                                  "Double page proposée : vérifiez la reliure et les marges.")
                    for s, p in (left, right)]
    if quads:
        score, corners = max(quads, key=lambda item: item[0])
        if score >= 0.2:
            return [PageCandidate(tuple(map(tuple, corners / [width, height])), float(score),
                                  "Vérifiez les quatre marges avant validation." if score < 0.55 else "")]
    return [PageCandidate(((0., 0.), (1., 0.), (1., 1.), (0., 1.)), 0.15,
                          "Aucun contour fiable : cadrage manuel recommandé.")]


def warp_page(rgb: np.ndarray, corners: tuple[tuple[float, float], ...], padding: float = 0.012) -> np.ndarray:
    """Rectifie un quadrilatère normalisé, avec une petite marge pour protéger le contenu."""
    h, w = rgb.shape[:2]
    pts = (np.asarray(corners, np.float32) * np.array([w - 1, h - 1], dtype=np.float32)).astype(np.float32)
    tl, tr, br, bl = pts
    out_w = max(32, int(round(max(np.linalg.norm(br-bl), np.linalg.norm(tr-tl)))) + 1)
    out_h = max(32, int(round(max(np.linalg.norm(tr-br), np.linalg.norm(tl-bl)))) + 1)
    dst = np.array([[0, 0], [out_w-1, 0], [out_w-1, out_h-1], [0, out_h-1]], np.float32)
    matrix = cv2.getPerspectiveTransform(pts, dst)
    warped = cv2.warpPerspective(rgb, matrix, (out_w, out_h), borderMode=cv2.BORDER_REPLICATE)
    px, py = round(out_w * padding), round(out_h * padding)
    if px and py and out_w > 2*px and out_h > 2*py:
        warped = cv2.copyMakeBorder(warped, py, py, px, px, cv2.BORDER_REPLICATE)
    return warped


def split_spread(rgb: np.ndarray, split_x: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Sépare une double page à la ligne de reliure indiquée (coordonnée relative)."""
    h, w = rgb.shape[:2]
    x = int(np.clip(split_x, 0.2, 0.8) * w)
    margin = max(1, int(w * 0.012))
    return rgb[:, :min(w, x + margin)].copy(), rgb[:, max(0, x - margin):].copy()
