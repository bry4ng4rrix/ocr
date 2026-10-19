"""Bords de la page par la couleur du papier : la page est la grande zone de « couleur papier » de la photo.

Complète `orientation_service.detect_page_quad` (feuille claire sur fond sombre) dans les cas où le fond
n'est pas plus sombre partout : marge blanche, tranche du livre, vitre, table claire. Principe :
1. couleur du papier = teinte claire dominante du centre de la photo (Lab) ;
2. masque « papier » (lettres comblées par fermeture), profils par colonne et par ligne → cadre grossier ;
3. sur chaque bord, points de transition fond → papier, droite ajustée de façon robuste (les illustrations
   qui touchent le bord sont écartées comme valeurs aberrantes) ;
4. intersection des quatre droites → quadrilatère, validé (convexe, angles, surface, dans l'image).
Aucune transformation n'est faite ici : seulement des coins proposés, avec un score et un drapeau d'incertitude.
"""
from __future__ import annotations

import cv2
import numpy as np

from models.analysis import QuadResult
from models.geometry import Point

WORK_SIDE = 1000               # copie réduite pour la recherche
PAPER_L_TOLERANCE = 0.15       # écart de clarté admis autour du papier (fraction de sa clarté) ...
PAPER_L_MARGIN = 12.0          # ... plus cette marge (niveaux Lab 0..255)
PAPER_CHROMA_TOLERANCE = 14.0  # écart de teinte admis (a*, b*)
PROFILE_RATIO = 0.45           # une colonne appartient à la page si sa part de papier ≥ 45 % de la médiane
EDGE_BAND = 0.10               # recherche de la transition dans ±10 % autour du bord grossier
MIN_RUN = 4                    # pixels de papier consécutifs pour valider une transition
MIN_SHARE = 0.12               # la page couvre au moins 12 % de la photo
MAX_SHARE = 0.995
MIN_ANGLE, MAX_ANGLE = 70.0, 110.0
MIN_BORDER = 0.015             # au moins un bord de fond de 1,5 % : sinon la photo est déjà recadrée
SIDE_BORDER = 0.008            # un côté « a du fond » s'il en reste au moins 0,8 %
MIN_BORDER_SIDES = 3           # une photo de page montre du fond sur au moins 3 côtés
MIN_INLIERS = 0.5              # au moins la moitié des points d'un bord sur la droite retenue


def _paper_mask(small: np.ndarray) -> tuple[np.ndarray, float]:
    """(masque papier bool, clarté du papier) d'une copie RGB réduite."""
    lab = cv2.cvtColor(small, cv2.COLOR_RGB2Lab).astype(np.float32)
    height, width = lab.shape[:2]
    center = lab[height // 4: 3 * height // 4, width // 4: 3 * width // 4].reshape(-1, 3)
    lightness = center[:, 0]
    light = center[lightness >= np.percentile(lightness, 55)]
    paper = np.median(light, axis=0)
    tolerance = PAPER_L_TOLERANCE * paper[0] + PAPER_L_MARGIN
    delta_l = np.abs(lab[..., 0] - paper[0])
    delta_c = np.hypot(lab[..., 1] - paper[1], lab[..., 2] - paper[2])
    mask = (delta_l <= tolerance) & (delta_c <= PAPER_CHROMA_TOLERANCE)
    if paper[0] < 238:
        mask &= lab[..., 0] < 252          # blanc saturé (marge d'écran, reflet) : pas du papier
    side = max(height, width)
    size = max(3, int(side / 60) | 1)
    closed = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((size, size), np.uint8))
    opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return opened.astype(bool), float(paper[0])


def _longest_run(values: np.ndarray, threshold: float) -> tuple[int, int] | None:
    above = values >= threshold
    best: tuple[int, int] | None = None
    start = None
    for index, flag in enumerate(np.append(above, False)):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            if best is None or index - start > best[1] - best[0]:
                best = (start, index)
            start = None
    return best


def _extent(fractions: np.ndarray) -> tuple[int, int] | None:
    smooth = np.convolve(fractions, np.ones(5) / 5, mode="same")
    length = len(smooth)
    reference = float(np.median(smooth[length // 4: 3 * length // 4]))
    if reference <= 0.05:
        return None
    return _longest_run(smooth, PROFILE_RATIO * reference)


def _first_run(line: np.ndarray) -> int | None:
    """Indice du début de la première suite de MIN_RUN pixels de papier."""
    if len(line) < MIN_RUN:
        return None
    window = np.convolve(line.astype(np.int32), np.ones(MIN_RUN, np.int32), mode="valid")
    hits = np.nonzero(window >= MIN_RUN)[0]
    return int(hits[0]) if len(hits) else None


def _fit_line(points: np.ndarray) -> tuple[float, float, float] | None:
    """Ajuste u = a·v + b (u : coordonnée du bord, v : position le long du bord) ; (a, b, part d'inliers)."""
    if len(points) < 8:
        return None
    v, u = points[:, 0].astype(np.float64), points[:, 1].astype(np.float64)
    keep = np.ones(len(u), bool)
    for _ in range(5):
        a, b = np.polyfit(v[keep], u[keep], 1)
        residual = np.abs(u - (a * v + b))
        mad = float(np.median(residual[keep])) + 0.5
        new_keep = residual <= 3.0 * mad + 1.0
        if new_keep.sum() < 8 or np.array_equal(new_keep, keep):
            break
        keep = new_keep
    a, b = np.polyfit(v[keep], u[keep], 1)
    return float(a), float(b), float(keep.mean())


def _edge_points(mask: np.ndarray, box: tuple[int, int, int, int], side: str) -> np.ndarray:
    """Points (position le long du bord, coordonnée de la transition) d'un bord de la page."""
    x0, x1, y0, y1 = box
    height, width = mask.shape
    band_x, band_y = int(EDGE_BAND * width), int(EDGE_BAND * height)
    points = []
    if side in ("left", "right"):
        rows = np.linspace(y0 + 0.08 * (y1 - y0), y1 - 0.08 * (y1 - y0), 60).astype(int)
        for row in rows:
            if side == "left":
                start = max(0, x0 - band_x)
                found = _first_run(mask[row, start:min(width, x0 + band_x)])
                if found is not None:
                    points.append((row, start + found))
            else:
                end = min(width, x1 + band_x)
                segment = mask[row, max(0, x1 - band_x):end][::-1]
                found = _first_run(segment)
                if found is not None:
                    points.append((row, end - 1 - found))
    else:
        columns = np.linspace(x0 + 0.08 * (x1 - x0), x1 - 0.08 * (x1 - x0), 60).astype(int)
        for column in columns:
            if side == "top":
                start = max(0, y0 - band_y)
                found = _first_run(mask[start:min(height, y0 + band_y), column])
                if found is not None:
                    points.append((column, start + found))
            else:
                end = min(height, y1 + band_y)
                segment = mask[max(0, y1 - band_y):end, column][::-1]
                found = _first_run(segment)
                if found is not None:
                    points.append((column, end - 1 - found))
    return np.array(points, np.float64).reshape(-1, 2)


def _intersect(vertical: tuple[float, float], horizontal: tuple[float, float]) -> tuple[float, float]:
    """Intersection de x = a·y + b (bord vertical) et y = c·x + d (bord horizontal)."""
    a, b = vertical
    c, d = horizontal
    y = (c * b + d) / max(1e-9, 1.0 - a * c)
    return a * y + b, y


def _angles(quad: np.ndarray) -> np.ndarray:
    result = []
    for index in range(4):
        before, here, after = quad[index - 1], quad[index], quad[(index + 1) % 4]
        first, second = before - here, after - here
        cosine = first @ second / max(1e-9, float(np.linalg.norm(first) * np.linalg.norm(second)))
        result.append(np.degrees(np.arccos(np.clip(cosine, -1, 1))))
    return np.array(result)


def detect_paper_quad(rgb: np.ndarray) -> QuadResult | None:
    """Coins de la page (tl, tr, br, bl normalisés) d'après la couleur du papier ; None si non fiable."""
    height, width = rgb.shape[:2]
    scale = min(1.0, WORK_SIDE / max(height, width))
    small = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    mask, _ = _paper_mask(small)
    columns = _extent(mask.mean(axis=0))
    rows = _extent(mask.mean(axis=1))
    if columns is None or rows is None:
        return None
    box = (columns[0], columns[1] - 1, rows[0], rows[1] - 1)
    small_h, small_w = mask.shape
    fits = {side: _fit_line(_edge_points(mask, box, side)) for side in ("left", "right", "top", "bottom")}
    if any(fit is None for fit in fits.values()):
        return None
    left, right, top, bottom = (fits[side] for side in ("left", "right", "top", "bottom"))
    corners = np.array([
        _intersect(left[:2], top[:2]), _intersect(right[:2], top[:2]),
        _intersect(right[:2], bottom[:2]), _intersect(left[:2], bottom[:2]),
    ], np.float64)
    corners[:, 0] = np.clip(corners[:, 0], 0, small_w - 1)
    corners[:, 1] = np.clip(corners[:, 1], 0, small_h - 1)
    area = cv2.contourArea(corners.astype(np.float32)) / float(small_w * small_h)
    angles = _angles(corners)
    if not (MIN_SHARE <= area <= MAX_SHARE) or not cv2.isContourConvex(corners.astype(np.float32).reshape(-1, 1, 2)):
        return None
    if angles.min() < MIN_ANGLE or angles.max() > MAX_ANGLE:
        return None
    xs, ys = corners[:, 0] / small_w, corners[:, 1] / small_h
    margins = (xs.min(), ys.min(), 1 - xs.max(), 1 - ys.max())
    if max(margins) < MIN_BORDER:
        return None                        # papier jusqu'aux bords : scan déjà recadré, rien à retirer
    if sum(margin >= SIDE_BORDER for margin in margins) < MIN_BORDER_SIDES:
        return None                        # fond d'un seul côté : plus probablement une illustration au bord
    inliers = min(fit[2] for fit in fits.values())
    inside = cv2.fillConvexPoly(np.zeros(mask.shape, np.uint8), corners.astype(np.int32), 1).astype(bool)
    paper_share = float(mask[inside].mean()) if inside.any() else 0.0
    score = round(float(np.clip(0.5 * inliers + 0.5 * paper_share, 0.0, 1.0)), 3)
    uncertain = inliers < MIN_INLIERS or paper_share < 0.35
    normalized = [Point(x=round(float(np.clip((x + 0.5) / small_w, 0, 1)), 5),
                        y=round(float(np.clip((y + 0.5) / small_h, 0, 1)), 5)) for x, y in corners]
    return QuadResult(corners=normalized, score=score, uncertain=uncertain)


def quad_overlap(first: list[Point], second: list[Point], side: int = 400) -> float:
    """Recouvrement (intersection / union) de deux quadrilatères normalisés, sur une grille de `side` px."""
    masks = []
    for corners in (first, second):
        mask = np.zeros((side, side), np.uint8)
        cv2.fillConvexPoly(mask, np.array([[p.x * side, p.y * side] for p in corners], np.int32), 1)
        masks.append(mask.astype(bool))
    union = np.logical_or(*masks).sum()
    return float(np.logical_and(*masks).sum() / union) if union else 0.0
