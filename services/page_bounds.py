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
PAPER_MIN_LIGHTNESS = 110.0    # clarté Lab minimale d'un papier (une couverture sombre n'en est pas un)
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
    mask, paper_lightness = _paper_mask(small)
    if paper_lightness < PAPER_MIN_LIGHTNESS:
        return None                        # page sombre : pas de « papier » ; voir detect_line_quad
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


# --------------------------------------------------------------------------- page sombre : bords droits

LINE_WORK_SIDE = 1000
LINE_MAX_TILT = 10.0           # degrés : un bord « horizontal » ou « vertical » s'écarte au plus de 10°
LINE_MERGE_PX = 6              # segments distants de moins de 6 px : même droite
EXTENT_GAP = 8                # trous tolérés en mesurant l'étendue d'un bord sur la carte des contours
LINE_MAX_GAP = 6              # trous admis dans un bord avant de couper le segment (calibré sur la photo 00019)
LINE_MIN_HEIGHT = 0.25         # la page fait au moins 25 % de la hauteur de la photo
LINE_END_TOLERANCE = 0.05      # extrémités des bords haut et bas alignées à 5 % de la largeur près
LINE_LENGTH_RATIO = 0.7        # bords haut et bas de longueurs comparables
RING_WIDTH = 0.015             # bande examinée autour du contour (fraction du grand côté)
BACKGROUND_MAX_LEVEL = 30.0    # fond de banc : très sombre (clarté Lab) ...
BACKGROUND_MAX_CHROMA = 1.5    # ... et neutre (sans teinte)
MIN_DARK_SIDES = 2             # au moins deux côtés sur fond sombre
RING_MAX_LEVEL = 80.0          # fond de banc : bande extérieure sombre (clarté Lab 0..255)
LINE_SCORE_MARGIN = 0.15      # candidats à moins de 0,15 du meilleur score : on garde le plus petit
SIDE_GRADIENT = 8.0            # gradient de Sobel (clarté Lab) qui marque un vrai bord latéral
LINE_SNAP = 0.04               # un trait vertical à moins de 4 % d'une extrémité devient le bord latéral


def _edges(gray: np.ndarray) -> np.ndarray:
    return cv2.Canny(cv2.GaussianBlur(gray, (0, 0), 1.5), 6, 18)


def _refine_extent(edges: np.ndarray, line: dict) -> None:
    """Étendue réelle d'un bord horizontal mesurée sur la carte des contours le long de la droite ajustée
    (±2 px, trous ≤ EXTENT_GAP) : plus fiable que les extrémités des segments de Hough, souvent morcelés."""
    height, width = edges.shape
    present = np.zeros(width, bool)
    for x in range(width):
        y = int(round(_at(line, x)))
        if 2 <= y < height - 2:
            present[x] = edges[y - 2:y + 3, x].any()
    runs, start, gap = [], None, 0
    for x in range(width + 1):
        on = x < width and present[x]
        if on:
            start = x if start is None else start
            gap = 0
        elif start is not None:
            gap += 1
            if gap > EXTENT_GAP or x == width:
                runs.append((start, x - gap))
                start, gap = None, 0
    overlapping = [run for run in runs if run[1] >= line["start"] and run[0] <= line["end"]]
    line["extents"] = [(line["start"], line["end"])]
    if overlapping:
        refined = (float(min(line["start"], min(run[0] for run in overlapping))),
                   float(max(line["end"], max(run[1] for run in overlapping))))
        if refined != line["extents"][0]:
            line["extents"].append(refined)


def _segments(gray: np.ndarray) -> np.ndarray:
    edges = _edges(gray)
    found = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=50,
                            minLineLength=int(0.12 * min(gray.shape)), maxLineGap=LINE_MAX_GAP)
    return np.zeros((0, 4), np.float64) if found is None else found.reshape(-1, 4).astype(np.float64)


def _merge(segments: np.ndarray, horizontal: bool) -> list[dict]:
    """Regroupe des segments presque alignés en droites : u = a·v + b, étendue [début, fin] le long de v."""
    lines: list[dict] = []
    for x1, y1, x2, y2 in segments:
        angle = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1))) % 180
        is_horizontal = angle < LINE_MAX_TILT or angle > 180 - LINE_MAX_TILT
        is_vertical = abs(angle - 90) < LINE_MAX_TILT
        if (horizontal and not is_horizontal) or (not horizontal and not is_vertical):
            continue
        v1, u1, v2, u2 = (x1, y1, x2, y2) if horizontal else (y1, x1, y2, x2)
        if v2 < v1:
            v1, u1, v2, u2 = v2, u2, v1, u1
        middle = (u1 + u2) / 2
        for line in lines:
            if abs(line["u"] - middle) <= LINE_MERGE_PX:
                line["points"] += [(v1, u1), (v2, u2)]
                line["start"], line["end"] = min(line["start"], v1), max(line["end"], v2)
                line["support"] += float(np.hypot(v2 - v1, u2 - u1))
                line["u"] = float(np.mean([p[1] for p in line["points"]]))
                break
        else:
            lines.append({"points": [(v1, u1), (v2, u2)], "start": v1, "end": v2, "u": middle,
                          "support": float(np.hypot(v2 - v1, u2 - u1))})
    for line in lines:
        points = np.array(line["points"])
        line["a"], line["b"] = np.polyfit(points[:, 0], points[:, 1], 1) if np.ptp(points[:, 0]) > 0 else (0.0, line["u"])
    return lines


def _at(line: dict, v: float) -> float:
    return line["a"] * v + line["b"]


def _side_line(top: dict, bottom: dict, side: str, verticals: list[dict], small_w: int) -> tuple[float, float]:
    """Bord latéral x = a·y + b : trait vertical proche de l'extrémité, sinon droite joignant les extrémités."""
    top_x, bottom_x = (top["start"], bottom["start"]) if side == "left" else (top["end"], bottom["end"])
    middle_y = (top["u"] + bottom["u"]) / 2
    # Un bord partiellement détecté n'est que raccourci : l'extrémité la plus extérieure est la bonne.
    guess = min(top_x, bottom_x) if side == "left" else max(top_x, bottom_x)
    span = bottom["u"] - top["u"]
    near = [line for line in verticals
            if abs(_at(line, middle_y) - guess) <= LINE_SNAP * small_w
            and min(line["end"], bottom["u"]) - max(line["start"], top["u"]) > 0.2 * span]
    if near:
        chosen = max(near, key=lambda line: line["support"])
        return chosen["a"], chosen["b"]
    y_top, y_bottom = _at(top, top_x), _at(bottom, bottom_x)      # bord invisible sur fond noir
    slope = (bottom_x - top_x) / max(1e-6, y_bottom - y_top)
    return slope, top_x - slope * y_top


def _pair_candidate(top: dict, bottom: dict, verticals: list[dict], gray: np.ndarray, lab: np.ndarray,
                    gradient_x: np.ndarray) -> tuple[float, float, np.ndarray] | None:
    """(score, surface, coins) d'une paire de bords haut/bas, ou None si elle ne décrit pas une page."""
    small_h, small_w = gray.shape
    if bottom["u"] - top["u"] < LINE_MIN_HEIGHT * small_h:
        return None
    top_length, bottom_length = top["end"] - top["start"], bottom["end"] - bottom["start"]
    if min(top_length, bottom_length) < LINE_LENGTH_RATIO * max(top_length, bottom_length):
        return None
    if abs(top["start"] - bottom["start"]) > LINE_END_TOLERANCE * small_w or \
            abs(top["end"] - bottom["end"]) > LINE_END_TOLERANCE * small_w:
        return None
    left = _side_line(top, bottom, "left", verticals, small_w)
    right = _side_line(top, bottom, "right", verticals, small_w)
    top_line, bottom_line = (top["a"], top["b"]), (bottom["a"], bottom["b"])
    corners = np.array([_intersect(left, top_line), _intersect(right, top_line),
                        _intersect(right, bottom_line), _intersect(left, bottom_line)])
    area = cv2.contourArea(corners.astype(np.float32)) / float(small_w * small_h)
    angles = _angles(corners)
    if not (MIN_SHARE <= area <= 0.9) or angles.min() < MIN_ANGLE or angles.max() > MAX_ANGLE:
        return None
    center = (small_w / 2.0, small_h / 2.0)
    if cv2.pointPolygonTest(corners.astype(np.float32).reshape(-1, 1, 2), center, False) < 0:
        return None
    if not _on_dark_surround(gray, corners) or _includes_background(lab, corners):
        return None
    alignment = (abs(top["start"] - bottom["start"]) + abs(top["end"] - bottom["end"])) / (
        2 * LINE_END_TOLERANCE * small_w)
    coverage = _side_coverage(gradient_x, corners[0], corners[3]) + _side_coverage(gradient_x, corners[1], corners[2])
    return coverage + (1.0 - alignment), area, corners


def detect_line_quad(rgb: np.ndarray) -> QuadResult | None:
    """Page sombre ou colorée (couverture, photo pleine page) sur fond sombre : la couleur ne la sépare pas
    du fond, ses bords droits si. On cherche une paire de bords haut/bas parallèles dont les extrémités sont
    alignées (même largeur) ; les bords latéraux sont les traits verticaux proches de ces extrémités ou, à
    défaut, la droite qui joint les extrémités (bord latéral invisible sur fond noir). Un contour qui englobe
    du fond de banc (bande intérieure très sombre et neutre) est refusé.
    """
    height, width = rgb.shape[:2]
    scale = min(1.0, LINE_WORK_SIDE / max(height, width))
    small = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    small_h, small_w = small.shape[:2]
    lab = cv2.cvtColor(cv2.GaussianBlur(small, (0, 0), 2), cv2.COLOR_RGB2Lab).astype(np.float32)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2Lab)[..., 0]
    segments = _segments(gray)
    gradient_x = np.abs(cv2.Sobel(cv2.GaussianBlur(gray, (0, 0), 1.5).astype(np.float32), cv2.CV_32F, 1, 0, ksize=3))
    horizontals = _merge(segments, horizontal=True)
    edge_map = _edges(gray)
    for line in horizontals:
        _refine_extent(edge_map, line)
    verticals = _merge(segments, horizontal=False)
    candidates = []
    for top in horizontals:
        for bottom in horizontals:
            for t0, t1 in top["extents"]:
                for b0, b1 in bottom["extents"]:
                    found = _pair_candidate(dict(top, start=t0, end=t1), dict(bottom, start=b0, end=b1),
                                            verticals, gray, lab, gradient_x)
                    if found is not None:
                        candidates.append(found)
    if not candidates:
        return None
    # Scores voisins (panneau, cadre du banc et page sont emboîtés) : la page est le plus petit d'entre eux.
    top_score = max(candidate[0] for candidate in candidates)
    close = [candidate for candidate in candidates if candidate[0] >= top_score - LINE_SCORE_MARGIN]
    best = min(close, key=lambda candidate: candidate[1])
    corners = best[2]
    corners[:, 0] = np.clip(corners[:, 0], 0, small_w - 1)
    corners[:, 1] = np.clip(corners[:, 1], 0, small_h - 1)
    xs, ys = corners[:, 0] / small_w, corners[:, 1] / small_h
    margins = (xs.min(), ys.min(), 1 - xs.max(), 1 - ys.max())
    if sum(margin >= SIDE_BORDER for margin in margins) < MIN_BORDER_SIDES:
        return None
    normalized = [Point(x=round(float(np.clip((x + 0.5) / small_w, 0, 1)), 5),
                        y=round(float(np.clip((y + 0.5) / small_h, 0, 1)), 5)) for x, y in corners]
    return QuadResult(corners=normalized, score=round(float(np.clip(best[0] / 3.0, 0.0, 1.0)), 3), uncertain=False)


def _includes_background(lab: np.ndarray, corners: np.ndarray) -> bool:
    """Vrai si, le long d'un côté, l'intérieur du quadrilatère est du fond de banc (très sombre ET neutre) :
    le contour englobe alors du fond. Même la partie la plus sombre d'une page imprimée garde une teinte."""
    height, width = lab.shape[:2]
    offset = max(4.0, 2 * RING_WIDTH * max(height, width))
    center = corners.mean(axis=0)
    for index in range(4):
        start, end = corners[index], corners[(index + 1) % 4]
        direction = (end - start) / max(1e-6, float(np.linalg.norm(end - start)))
        normal = np.array([direction[1], -direction[0]])
        if normal @ ((start + end) / 2 - center) > 0:
            normal = -normal                                       # normale vers l'intérieur
        values = []
        for t in np.linspace(0.15, 0.85, 30):
            x, y = (start + t * (end - start) + offset * normal).round().astype(int)
            if 0 <= x < width and 0 <= y < height:
                values.append(lab[y, x])
        if len(values) < 10:
            continue
        values = np.array(values)
        lightness = float(np.median(values[:, 0]))
        chroma = float(np.median(np.hypot(values[:, 1] - 128.0, values[:, 2] - 128.0)))
        if lightness <= BACKGROUND_MAX_LEVEL and chroma <= BACKGROUND_MAX_CHROMA:
            return True
    return False


def _on_dark_surround(gray: np.ndarray, corners: np.ndarray) -> bool:
    """Vrai si au moins deux côtés du quadrilatère touchent un fond sombre, plus sombre que l'intérieur
    (page posée sur le banc) ; faux pour un rectangle entouré de papier clair (photo dans une page)."""
    height, width = gray.shape
    offset = max(3.0, RING_WIDTH * max(height, width))
    center = corners.mean(axis=0)
    dark_sides = 0
    for index in range(4):
        start, end = corners[index], corners[(index + 1) % 4]
        direction = (end - start) / max(1e-6, float(np.linalg.norm(end - start)))
        normal = np.array([direction[1], -direction[0]])
        if normal @ ((start + end) / 2 - center) < 0:
            normal = -normal                                       # normale vers l'extérieur
        outside, inside = [], []
        for t in np.linspace(0.1, 0.9, 40):
            point = start + t * (end - start)
            for target, sign in ((outside, 1.0), (inside, -1.0)):
                x, y = (point + sign * offset * normal).round().astype(int)
                if 0 <= x < width and 0 <= y < height:
                    target.append(gray[y, x])
        if len(outside) >= 10 and len(inside) >= 10:
            outside_level, inside_level = float(np.median(outside)), float(np.median(inside))
            dark_sides += int(outside_level <= RING_MAX_LEVEL and outside_level < inside_level)
    return dark_sides >= MIN_DARK_SIDES


def _side_coverage(gradient_x: np.ndarray, start: np.ndarray, end: np.ndarray, samples: int = 60) -> float:
    """Part d'un bord latéral (de `start` à `end`) le long de laquelle la clarté change vraiment (±2 px)."""
    height, width = gradient_x.shape
    hits = 0
    for t in np.linspace(0.05, 0.95, samples):
        x, y = start + t * (end - start)
        row, column = int(round(y)), int(round(x))
        if not (0 <= row < height and 2 <= column < width - 2):
            continue
        hits += int(gradient_x[row, column - 2:column + 3].max() >= SIDE_GRADIENT)
    return hits / samples
