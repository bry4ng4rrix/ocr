"""Correction géométrique d'une page : feuille et perspective, quart de tour, inclinaison, courbure.

Ordre (contrat, voir models.page.GeometryCorrection) : contour de feuille/perspective -> orientation
automatique (détectée APRÈS le recadrage) -> quart de tour manuel -> inclinaison -> courbure.

Conventions :
- quarts de tour HORAIRES (`OrientationResult.quarter_turns` = rotation à appliquer pour redresser) ;
- coins de feuille normalisés 0..1 sur l'original (après EXIF), ordre tl, tr, br, bl ;
- angle d'inclinaison = inclinaison du contenu, sens antihoraire positif ; la correction tourne de -angle.

Toute détection incertaine produit un `Issue` (code stable, message français) ; une orientation incertaine
n'est jamais appliquée, une courbure peu fiable n'est jamais corrigée. Tout est déterministe : la même
entrée et les mêmes réglages donnent la même image (`detect=False` rejoue les valeurs enregistrées).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import cv2
import numpy as np

import config
from models.analysis import CurvatureResult, OrientationResult, QuadResult, SkewResult
from models.errors import OcrFailedError, OcrUnavailableError
from models.geometry import Point
from models.page import GeometryCorrection
from models.reports import Issue, Severity
from services import ocr_service, page_bounds
from services.image_service import downscale, picture_share, to_gray

logger = logging.getLogger("bookocr.orientation")

QUAD_WORK_SIDE = 1000          # copie réduite pour la recherche de la feuille
MIN_SHEET_SHARE = 0.08         # une feuille couvre au moins 8 % du cadre...
MAX_SHEET_SHARE = 0.95         # ... et au plus 95 % (au-delà : scan déjà recadré)
MIN_OUTPUT_SIDE = 50
OSD_SIDE = 1800                # copie envoyée à l'OSD
OSD_TRUSTED = 2.0              # confiance OSD minimale pour retenir sa réponse
OSD_VERIFIED = 15.0            # sous ce seuil, la réponse OSD est confrontée à son opposé (180°) par lecture
READING_SIDE = 1000            # copie lue lors du contrôle par lecture
READING_MIN_SCORE = 300.0      # score (conf moyenne × nb de mots fiables) minimal d'une page lisible
READING_WORD_MIN_CONF = 60.0   # un mot compte s'il a ≥ 3 lettres et une confiance ≥ 60
OSD_OVERRIDE_MARGIN = 2.0      # pour contredire l'OSD, le sens opposé doit se lire 2 fois mieux
READING_MARGIN = 1.5           # la meilleure orientation doit dépasser la 2e de ce facteur
SKEW_ZERO = 0.15               # |angle| plus petit : considéré comme droit
SKEW_MIN_APPLY = 0.01
QUAD_DECIMALS = 5
SKEW_DECIMALS = 2
CURVE_WORK_SIDE = 1600
CURVE_MIN_LINES = 8
CURVE_MAX_PICTURE_SHARE = 0.15


@dataclass
class GeometryOutcome:
    image: np.ndarray                     # page corrigée, RGB uint8 (nouveau tableau)
    geometry: GeometryCorrection          # copie des réglages, detected_* remplis
    issues: list[Issue] = field(default_factory=list)


def _issue(code: str, message: str, severity: Severity = Severity.WARNING) -> Issue:
    return Issue(code=code, message=message, severity=severity)


def _check_rgb(rgb: np.ndarray) -> None:
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("image RGB uint8 (H, W, 3) attendue")


# --------------------------------------------------------------------------- rotations


def rotate_quarter(image: np.ndarray, quarter_turns_cw: int) -> np.ndarray:
    """Rotation horaire sans perte de `quarter_turns_cw` quarts de tour."""
    turns = quarter_turns_cw % 4
    if turns == 0:
        return image
    return np.ascontiguousarray(np.rot90(image, k=-turns))


def _paper_colour(rgb: np.ndarray) -> tuple[int, int, int]:
    """Couleur claire d'une bande de bord de 2 % (le papier), pour remplir les coins après rotation."""
    height, width = rgb.shape[:2]
    band = max(1, round(0.02 * min(height, width)))
    strip = np.concatenate([rgb[:band].reshape(-1, 3), rgb[-band:].reshape(-1, 3),
                            rgb[:, :band].reshape(-1, 3), rgb[:, -band:].reshape(-1, 3)])
    return tuple(int(value) for value in np.percentile(strip, 90, axis=0))


def rotate_fine(rgb: np.ndarray, angle_deg: float) -> np.ndarray:
    """Tourne de `angle_deg` degrés (antihoraire), même taille ; coins remplis avec la couleur du papier."""
    if abs(angle_deg) < 1e-6:
        return rgb
    height, width = rgb.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle_deg, 1.0)
    return cv2.warpAffine(rgb, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                          borderValue=_paper_colour(rgb))


# --------------------------------------------------------------------------- feuille et perspective


def _order_corners(points: np.ndarray) -> np.ndarray:
    """tl, tr, br, bl : tri horaire autour du centre, en partant du plus petit x + y."""
    centre = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - centre[1], points[:, 0] - centre[0])
    ordered = points[np.argsort(angles)]
    start = int(np.argmin(ordered.sum(axis=1)))
    return np.roll(ordered, -start, axis=0)


def _approx_quad(contour: np.ndarray) -> np.ndarray:
    hull = cv2.convexHull(contour)
    perimeter = cv2.arcLength(hull, True)
    for epsilon in (0.01, 0.02, 0.03, 0.05, 0.08):
        approx = cv2.approxPolyDP(hull, epsilon * perimeter, True)
        if len(approx) == 4:
            return _order_corners(approx.reshape(4, 2).astype(np.float32))
    points = hull.reshape(-1, 2).astype(np.float32)
    sums, diffs = points.sum(axis=1), points[:, 1] - points[:, 0]
    return np.array([points[np.argmin(sums)], points[np.argmin(diffs)], points[np.argmax(sums)],
                     points[np.argmax(diffs)]], np.float32)


def _segment_distance(points: np.ndarray, start: np.ndarray, end: np.ndarray) -> np.ndarray:
    direction = end - start
    length2 = max(float(direction @ direction), 1e-9)
    t = np.clip(((points - start) @ direction) / length2, 0.0, 1.0)
    nearest = start + t[:, None] * direction
    return np.linalg.norm(points - nearest, axis=1)


def _refine_quad(contour: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """Coins plus précis : droite ajustée (Huber) sur les points de chaque côté, puis intersections."""
    points = contour.reshape(-1, 2).astype(np.float32)
    distances = np.stack([_segment_distance(points, quad[i], quad[(i + 1) % 4]) for i in range(4)], axis=1)
    side_of = np.argmin(distances, axis=1)
    lines = []
    for side in range(4):
        start, end = quad[side], quad[(side + 1) % 4]
        length = float(np.linalg.norm(end - start))
        selected = points[(side_of == side) & (distances[:, side] < max(3.0, 0.03 * length))]
        if len(selected) >= 2:
            projection = (selected - start) @ ((end - start) / max(length, 1e-6))
            selected = selected[(projection > 0.08 * length) & (projection < 0.92 * length)]
        if len(selected) < 10:
            return quad
        vx, vy, x0, y0 = cv2.fitLine(selected, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append((np.array([x0, y0]), np.array([vx, vy])))
    corners = []
    for index in range(4):
        (p1, d1), (p2, d2) = lines[index - 1], lines[index]
        matrix = np.array([d1, -d2]).T
        if abs(np.linalg.det(matrix)) < 1e-6:
            return quad
        t = np.linalg.solve(matrix, p2 - p1)
        corners.append(p1 + t[0] * d1)
    refined = np.array(corners, np.float32)
    return refined if np.abs(refined - quad).max() < 0.05 * max(np.ptp(quad[:, 0]), np.ptp(quad[:, 1])) else quad


def _interior_angles(quad: np.ndarray) -> np.ndarray:
    angles = []
    for index in range(4):
        previous, corner, following = quad[index - 1], quad[index], quad[(index + 1) % 4]
        a, b = previous - corner, following - corner
        cosine = float(a @ b) / max(float(np.linalg.norm(a) * np.linalg.norm(b)), 1e-9)
        angles.append(np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0))))
    return np.array(angles)


def _sheet_masks(brightness: np.ndarray) -> list[np.ndarray]:
    """Masques « feuille claire » pour plusieurs seuils, coupés le long des fines vallées sombres
    (pli entre la feuille et la page suivante, bord de vitre)."""
    height, width = brightness.shape
    side = max(height, width)
    valley_size = _odd(side / 50)
    closed = cv2.morphologyEx(brightness, cv2.MORPH_CLOSE, np.ones((valley_size, valley_size), np.uint8))
    valleys = (closed.astype(np.int16) - brightness) > np.maximum(15, closed // 6)
    otsu = float(cv2.threshold(brightness, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0])
    bright = float(np.percentile(brightness, 99))
    thresholds = sorted({int(t) for t in (otsu, otsu + (bright - otsu) / 3, 0.7 * otsu, 0.45 * otsu)
                         if 8 <= t < bright - 5})
    masks = []
    for threshold in thresholds:
        mask = ((brightness > threshold) & ~valleys).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        masks.append(mask)
    return masks


def _odd(value: float, minimum: int = 3) -> int:
    value = max(minimum, int(round(value)))
    return value if value % 2 else value + 1


def _evaluate_sheet(contour: np.ndarray, brightness: np.ndarray) -> tuple[float, np.ndarray, bool] | None:
    """(score, coins, incertain) d'un contour candidat, ou None s'il ne ressemble pas à une feuille."""
    height, width = brightness.shape
    area = float(cv2.contourArea(contour))
    if area < MIN_SHEET_SHARE * height * width:
        return None
    quad = _refine_quad(contour, _approx_quad(contour))
    quad_area = float(cv2.contourArea(quad))
    if quad_area <= 0 or not MIN_SHEET_SHARE <= quad_area / (height * width) <= MAX_SHEET_SHARE:
        return None
    fill = area / quad_area
    hull_fill = float(cv2.contourArea(cv2.convexHull(contour))) / quad_area
    # Une illustration sombre qui touche le bord de la feuille laisse une encoche : acceptée (incertaine)
    # si l'enveloppe convexe reste un quadrilatère net.
    notched = fill < 0.88 and fill >= 0.6 and 0.95 <= hull_fill <= 1.05
    if (fill < 0.88 and not notched) or fill > 1.08 or \
            not cv2.isContourConvex(np.round(quad).astype(np.int32).reshape(-1, 1, 2)):
        return None
    angles = _interior_angles(quad)
    if np.abs(angles - 90).max() > 35:
        return None
    polygon = np.zeros((height, width), np.uint8)
    cv2.fillPoly(polygon, [np.round(quad).astype(np.int32)], 1)
    ring_size = max(3, round(max(height, width) * 0.015))
    ring = cv2.dilate(polygon, np.ones((2 * ring_size + 1,) * 2, np.uint8)) - polygon
    if ring.sum() < 0.1 * ring_size * (height + width):
        return None                          # la feuille remplit l'image : rien à recadrer
    inside = float(np.median(brightness[polygon > 0]))
    darker = float(np.mean(brightness[ring > 0] < 0.6 * inside))
    if darker < 0.45:
        return None                          # pas de fond plus sombre autour : pas une feuille photographiée
    xs, ys = quad[:, 0], quad[:, 1]
    if xs.min() <= 2 or ys.min() <= 2 or xs.max() >= width - 3 or ys.max() >= height - 3:
        return None                          # un bord de la feuille est hors cadre : perspective non mesurable
    uncertain = notched or darker < 0.65 or np.abs(angles - 90).max() > 15 or fill < 0.93
    score = (quad_area / (height * width)) * min(max(fill, hull_fill if notched else fill), 1.0) * darker * \
        (0.8 if notched else 1.0)
    return score, quad, bool(uncertain)


def _best_component(filled: np.ndarray, kernel: np.ndarray,
                    brightness: np.ndarray) -> tuple[float, np.ndarray, bool] | None:
    """Meilleure feuille parmi les composantes d'un masque, après une érosion qui détache les ponts clairs
    (reflets, bord de la page voisine) ; chaque composante est ensuite regonflée dans le masque."""
    height, width = filled.shape
    count, labels, stats, _ = cv2.connectedComponentsWithStats(cv2.erode(filled, kernel), connectivity=8)
    best = None
    for index in range(1, count):
        if stats[index][4] < MIN_SHEET_SHARE * 0.6 * height * width:
            continue
        blob = cv2.bitwise_and(cv2.dilate((labels == index).astype(np.uint8), kernel), filled)
        contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            continue
        evaluated = _evaluate_sheet(max(contours, key=cv2.contourArea), brightness)
        if evaluated is not None and (best is None or evaluated[0] > best[0]):
            best = evaluated
    return best


def detect_page_quad(rgb: np.ndarray) -> QuadResult | None:
    """Coins (tl, tr, br, bl normalisés) de la plus grande feuille claire et cohérente sur un fond plus
    sombre. None si aucune feuille nette : scan qui remplit l'image, forme non quadrilatère, pas de fond
    sombre autour. N'invente jamais un recadrage.
    """
    _check_rgb(rgb)
    small, _ = downscale(rgb, QUAD_WORK_SIDE)
    height, width = small.shape[:2]
    # Luminosité = max(R, G, B) : une feuille colorée (verte, brune) ressort mieux que par le gris.
    brightness = cv2.GaussianBlur(small.max(axis=2), (5, 5), 0)
    best: tuple[float, np.ndarray, bool] | None = None
    kernels = [np.ones((2 * size + 1, 2 * size + 1), np.uint8)
               for size in (max(2, round(max(height, width) * fraction)) for fraction in (0.004, 0.012))]
    for mask in _sheet_masks(brightness):
        outlines, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        large = [c for c in outlines if cv2.contourArea(c) > 0.03 * height * width]
        if not large:
            continue
        filled = np.zeros_like(mask)
        cv2.drawContours(filled, large, -1, 1, thickness=cv2.FILLED)
        for kernel in kernels:
            candidate = _best_component(filled, kernel, brightness)
            if candidate is not None and (best is None or candidate[0] > best[0]):
                best = candidate
    if best is None:
        return None
    score, quad, uncertain = best
    # Le contour passe par le centre des pixels : on rejoint le bord de la grille.
    quad = quad + 0.5 * np.sign(quad - quad.mean(axis=0))
    corners = [Point(x=round(float(np.clip(x / width, 0, 1)), QUAD_DECIMALS),
                     y=round(float(np.clip(y / height, 0, 1)), QUAD_DECIMALS)) for x, y in quad]
    return QuadResult(corners=corners, score=round(min(1.0, score * 2), 3), uncertain=uncertain)


def warp_quad(rgb: np.ndarray, corners: list[Point]) -> np.ndarray | None:
    """Redresse le quadrilatère (coins normalisés) en rectangle ; None s'il est trop petit."""
    height, width = rgb.shape[:2]
    points = np.array([[p.x * width, p.y * height] for p in corners], np.float32)
    tl, tr, br, bl = points
    out_w = int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))))
    out_h = int(round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))))
    if out_w < MIN_OUTPUT_SIDE or out_h < MIN_OUTPUT_SIDE:
        return None
    target = np.array([[0, 0], [out_w, 0], [out_w, out_h], [0, out_h]], np.float32)
    matrix = cv2.getPerspectiveTransform(points, target)
    return cv2.warpPerspective(rgb, matrix, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


# --------------------------------------------------------------------------- orientation


def _reading_languages(languages: list[str] | None) -> list[str]:
    installed = ocr_service.available_languages()
    wanted = [lang for lang in (languages or config.DEFAULT_LANGUAGES) if lang in installed]
    if not wanted:
        wanted = [lang for lang in config.DEFAULT_LANGUAGES if lang in installed]
    return wanted


def _reading_score(gray: np.ndarray, languages: list[str]) -> float:
    """Conf. moyenne × nombre de mots fiables (≥ 3 lettres, confiance ≥ 60) lus en psm 6.

    Le filtre est indispensable : un tableau de chiffres lu à l'envers donne beaucoup de « mots » courts
    et peu sûrs, qui suffiraient à faire gagner la mauvaise orientation."""
    try:
        result = ocr_service.TesseractEngine(timeout=min(config.OCR_TIMEOUT_S, 90)).recognize(gray, languages, psm=6)
    except OcrFailedError as error:
        logger.info("Contrôle par lecture impossible : %s", error)
        return 0.0
    confidences = [word.confidence for word in result.words
                   if word.confidence >= READING_WORD_MIN_CONF and len(re.findall(r"[^\W\d_]", word.text)) >= 3]
    return float(np.mean(confidences) * len(confidences)) if confidences else 0.0


def _verify_half_turn(gray: np.ndarray, turns: int, languages: list[str], details: str) -> tuple[int, str]:
    """L'OSD confond parfois une page et son retournement (180°) : on lit les deux sens et on ne change
    la réponse que si l'opposé se lit nettement mieux."""
    small, _ = downscale(gray, READING_SIDE)
    try:
        guess = _reading_score(rotate_quarter(small, turns), languages)
        opposite = _reading_score(rotate_quarter(small, turns + 2), languages)
    except OcrUnavailableError as error:
        return turns, f"{details} ; contrôle 180° impossible ({error})"
    details = f"{details} ; lecture {turns * 90}°/{(turns + 2) % 4 * 90}° : {guess:.0f}/{opposite:.0f}"
    if opposite >= READING_MIN_SCORE and opposite >= OSD_OVERRIDE_MARGIN * guess:
        return (turns + 2) % 4, details + " -> retournement corrigé"
    return turns, details


def detect_orientation(rgb: np.ndarray, languages: list[str] | None = None) -> OrientationResult:
    """Quart de tour horaire qui redresse le texte.

    OSD Tesseract (psm 0) sur une copie réduite ; si l'OSD est peu sûr (< 2) ou échoue : contrôle par
    lecture (psm 6) aux 4 orientations. Toujours incertain : quarter_turns = 0, uncertain = True.
    """
    gray, _ = downscale(to_gray(rgb), OSD_SIDE)
    if ocr_service.tesseract_version() is None:
        return OrientationResult(quarter_turns=0, method="none", uncertain=True, details="Tesseract absent")
    osd = ocr_service.run_osd(gray)
    languages = _reading_languages(languages)
    if osd is not None and osd.orientation_confidence >= OSD_TRUSTED:
        turns = (osd.rotate // 90) % 4
        details = f"OSD : rotation {osd.rotate}°, confiance {osd.orientation_confidence:.1f}"
        if osd.orientation_confidence < OSD_VERIFIED and languages:
            turns, details = _verify_half_turn(gray, turns, languages, details)
        method = "osd" if turns == (osd.rotate // 90) % 4 else "reading"
        return OrientationResult(quarter_turns=turns, confidence=osd.orientation_confidence, method=method,
                                 uncertain=False, details=details)
    osd_text = (f"OSD : rotation {osd.rotate}°, confiance {osd.orientation_confidence:.1f}" if osd
                else "OSD sans réponse")
    if not languages:
        return OrientationResult(quarter_turns=0, method="none", uncertain=True,
                                 details=f"{osd_text} ; aucune langue pour le contrôle par lecture")
    small, _ = downscale(gray, READING_SIDE)
    try:
        scores = [_reading_score(rotate_quarter(small, turns), languages) for turns in range(4)]
    except OcrUnavailableError as error:
        return OrientationResult(quarter_turns=0, method="none", uncertain=True, details=f"{osd_text} ; {error}")
    order = np.argsort(scores)[::-1]
    best, second = scores[order[0]], scores[order[1]]
    details = f"{osd_text} ; lecture 0/90/180/270 : " + "/".join(f"{score:.0f}" for score in scores)
    if best >= READING_MIN_SCORE and best >= READING_MARGIN * second:
        confidence = round(min(10.0, best / max(second, 1.0)), 2)
        return OrientationResult(quarter_turns=int(order[0]), confidence=confidence, method="reading",
                                 uncertain=False, details=details)
    return OrientationResult(quarter_turns=0, confidence=0.0, method="none", uncertain=True, details=details)


# --------------------------------------------------------------------------- inclinaison


def _profile_score(xs: np.ndarray, ys: np.ndarray, angle_deg: float) -> float:
    """Netteté du profil des lignes une fois le contenu tourné de -angle (somme des carrés des comptes)."""
    radians = np.deg2rad(angle_deg)
    rows = np.round(xs * np.sin(radians) + ys * np.cos(radians)).astype(np.int64)
    rows -= rows.min()
    counts = np.bincount(rows).astype(np.float64)
    return float(counts @ counts)


def estimate_skew(gray: np.ndarray, max_angle: float = 10.0) -> SkewResult:
    """Inclinaison des lignes de texte (°, antihoraire positif) par profil de projection.

    Composantes de la taille d'une lettre sur une copie réduite ; recherche grossière (0,5°) puis fine
    (0,05°). Profil plat (peu de texte, pas de lignes) : uncertain. |angle| < 0,15° : 0.
    """
    gray = to_gray(gray)
    small, _ = downscale(gray, 1200)
    height, width = small.shape
    ink = cv2.threshold(small, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    share = float(ink.mean())
    if share < 0.0005 or share > 0.5:
        return SkewResult(angle_deg=0.0, confidence=0.0, uncertain=True)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    letters = (stats[:, 3] <= 0.06 * height) & (stats[:, 2] <= 0.3 * width) & (stats[:, 4] >= 2)
    letters[0] = False
    ys, xs = np.nonzero(letters[labels])
    if len(xs) < 200:
        return SkewResult(angle_deg=0.0, confidence=0.0, uncertain=True)
    if len(xs) > 300_000:
        step = len(xs) // 300_000 + 1
        xs, ys = xs[::step], ys[::step]
    xs = xs.astype(np.float64) - width / 2
    ys = ys.astype(np.float64) - height / 2
    coarse = np.arange(-max_angle, max_angle + 1e-9, 0.5)
    coarse_scores = np.array([_profile_score(xs, ys, angle) for angle in coarse])
    best = float(coarse[int(np.argmax(coarse_scores))])
    fine = np.arange(best - 0.6, best + 0.6 + 1e-9, 0.05)
    fine_scores = np.array([_profile_score(xs, ys, angle) for angle in fine])
    index = int(np.argmax(fine_scores))
    best = float(fine[index])
    if 0 < index < len(fine) - 1:          # parabole par le sommet et ses voisins
        y0, y1, y2 = fine_scores[index - 1:index + 2]
        denominator = y0 - 2 * y1 + y2
        if denominator < 0:
            best += 0.05 * 0.5 * (y0 - y2) / denominator
    sharpness = fine_scores[index] / max(float(np.median(coarse_scores)), 1.0)
    confidence = round(float(np.clip((sharpness - 1.0) / 0.5, 0.0, 1.0)), 3)
    level = _profile_score(xs, ys, 0.0)
    if sharpness < 1.15 or (fine_scores[index] < 1.02 * level and abs(best) > 0.3):
        return SkewResult(angle_deg=0.0, confidence=confidence, uncertain=True)
    if abs(best) < SKEW_ZERO:
        best = 0.0
    return SkewResult(angle_deg=round(best, SKEW_DECIMALS), confidence=confidence, uncertain=False)


# --------------------------------------------------------------------------- courbure (D-11)


@dataclass
class _CurveModel:
    result: CurvatureResult
    line_y: np.ndarray | None = None      # ordonnées (pixels de la copie) des lignes, croissantes
    linear: np.ndarray | None = None      # coefficient de u par ligne (u = x normalisé -1..1)
    quadratic: np.ndarray | None = None   # coefficient de u² par ligne
    scale: float = 1.0                    # copie / image


def _text_line_points(gray: np.ndarray) -> tuple[list[tuple[np.ndarray, np.ndarray]], float]:
    """Points (x, y) du centre de l'encre le long des lignes de texte longues, et hauteur médiane des lettres.

    Les lettres sont reliées horizontalement (pas verticalement) ; chaque ligne est découpée en tranches de
    ~4 hauteurs de lettre et une tranche n'est gardée que si elle ne contient qu'une ligne.
    """
    height, width = gray.shape
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, np.ones((_odd(max(height, width) / 60),) * 2, np.uint8))
    ink = ((background.astype(np.int16) - gray) > np.maximum(25, background // 5)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    keep = (stats[:, 3] >= 3) & (stats[:, 3] <= 0.04 * height) & (stats[:, 2] <= 0.06 * width) & (stats[:, 4] >= 4)
    keep[0] = False
    if keep.sum() < 50:
        return [], 0.0
    letters = keep[labels].astype(np.uint8)
    letter_height = float(np.median(stats[keep, 3]))
    smear = cv2.dilate(letters, np.ones((1, _odd(letter_height * 2.5)), np.uint8))
    count, line_labels, line_stats, _ = cv2.connectedComponentsWithStats(smear, connectivity=8)
    text_columns = np.flatnonzero(letters.any(axis=0))
    text_width = float(text_columns[-1] - text_columns[0]) if len(text_columns) else float(width)
    bin_width = max(8, int(letter_height * 4))
    lines = []
    for index in range(1, count):
        left, top, line_w, line_h, _ = line_stats[index]
        if line_w < 0.5 * text_width:
            continue
        region = (line_labels[top:top + line_h, left:left + line_w] == index) & \
                 (letters[top:top + line_h, left:left + line_w] > 0)
        xs_points, ys_points, bins = [], [], 0
        for start in range(0, line_w - bin_width // 2, bin_width):
            bins += 1
            rows, cols = np.nonzero(region[:, start:start + bin_width])
            if len(rows) < 6 or rows.max() - rows.min() > 2.2 * letter_height:
                continue
            ys_points.append(top + float(rows.mean()))
            xs_points.append(left + start + float(cols.mean()))
        if len(xs_points) >= 5 and len(xs_points) >= 0.7 * bins:
            lines.append((np.array(xs_points), np.array(ys_points)))
    return lines, letter_height


def _curve_model(gray: np.ndarray, check_pictures: np.ndarray | None = None) -> _CurveModel:
    small, scale = downscale(to_gray(gray), CURVE_WORK_SIDE)
    height, width = small.shape
    if check_pictures is not None and picture_share(check_pictures) > CURVE_MAX_PICTURE_SHARE:
        return _CurveModel(CurvatureResult(applicable=False, reason="grande illustration sur la page"))
    lines, letter_height = _text_line_points(small)
    if len(lines) < CURVE_MIN_LINES:
        return _CurveModel(CurvatureResult(applicable=False, lines_used=len(lines),
                                           reason=f"pas assez de lignes de texte longues ({len(lines)})"))
    centres, linear, quadratic, residual_flat, residual_model = [], [], [], [], []
    for xs, ys in lines:
        u = xs / (width / 2.0) - 1.0
        c2, c1, c0 = np.polyfit(u, ys, 2)
        centres.append(c0)
        linear.append(c1)
        quadratic.append(c2)
        residual_flat.append(float(np.mean(np.abs(ys - np.median(ys)))))
        residual_model.append(float(np.mean(np.abs(ys - (c0 + c1 * u + c2 * u * u)))))
    order = np.argsort(centres)
    centres = np.array(centres)[order]
    linear, quadratic = np.array(linear)[order], np.array(quadratic)[order]
    used = len(centres)
    # Valeurs lissées (médiane glissante sur 5 lignes) : une ligne mal mesurée ne déforme pas la page.
    linear_smooth = _running_median(linear, 5)
    quadratic_smooth = _running_median(quadratic, 5)
    offsets = np.abs(quadratic_smooth)            # écart bord/centre dû à la courbure seule
    max_offset = float(offsets.max()) / scale
    flat = float(np.mean(residual_flat))
    fitted = float(np.mean(residual_model))
    base = CurvatureResult(applicable=False, lines_used=used, max_offset_px=round(max_offset, 2))
    if float(offsets.max()) < max(2.0, 0.2 * letter_height):
        base.reason = "page plate"
        return _CurveModel(base)
    signs = np.sign(quadratic)
    coherent = float(np.mean(signs == np.sign(np.median(quadratic)))) if np.median(quadratic) != 0 else 0.0
    if coherent < 0.75:
        base.reason = f"courbure incohérente entre les lignes ({coherent:.0%})"
        return _CurveModel(base)
    if fitted > 0.6 * flat:
        base.reason = "le modèle de courbure n'explique pas les lignes"
        return _CurveModel(base)
    base.applicable = True
    base.reason = f"{used} lignes cohérentes"
    return _CurveModel(base, centres, linear_smooth, quadratic_smooth, scale)


def _running_median(values: np.ndarray, size: int) -> np.ndarray:
    half = size // 2
    return np.array([float(np.median(values[max(0, i - half):i + half + 1])) for i in range(len(values))])


def estimate_curvature(gray: np.ndarray) -> CurvatureResult:
    """Mesure de la courbure des lignes de texte (D-11) : applicable seulement si au moins 8 lignes longues
    sont courbées de façon cohérente et que le modèle explique nettement leur forme."""
    image = gray if gray.ndim == 3 else None
    return _curve_model(to_gray(gray), image).result


def dewarp(rgb: np.ndarray) -> tuple[np.ndarray, CurvatureResult]:
    """Redresse les lignes courbées si la mesure est fiable ; sinon renvoie l'image inchangée.

    Déplacement vertical Δ(x, y) = a1(y)·u + a2(y)·u² (u = x normalisé -1..1), interpolé entre les lignes
    mesurées ; image(x, y) <- source(x, y + Δ). Déterministe.
    """
    _check_rgb(rgb)
    model = _curve_model(to_gray(rgb), rgb)
    if not model.result.applicable:
        return rgb, model.result
    height, width = rgb.shape[:2]
    rows = (np.arange(height, dtype=np.float64) + 0.5) * model.scale - 0.5
    linear = np.interp(rows, model.line_y, model.linear) / model.scale
    quadratic = np.interp(rows, model.line_y, model.quadratic) / model.scale
    u = np.arange(width, dtype=np.float64) / (width / 2.0) - 1.0
    map_y = (np.arange(height, dtype=np.float64)[:, None] + linear[:, None] * u[None, :]
             + quadratic[:, None] * (u * u)[None, :]).astype(np.float32)
    map_x = np.broadcast_to(np.arange(width, dtype=np.float32)[None, :], (height, width)).copy()
    out = cv2.remap(rgb, map_x, map_y, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return out, model.result


# --------------------------------------------------------------------------- page complète


def _detect_sheet(image: np.ndarray, geometry: GeometryCorrection, issues: list[Issue]) -> None:
    found = detect_page_quad(image)
    if found is None or found.uncertain:
        # Fond pas plus sombre partout (marge claire, tranche du livre, table) : bords par la couleur du papier.
        by_paper = page_bounds.detect_paper_quad(image)
        # Un contour incertain mais présent n'est remplacé que par un contour qui couvre la même zone.
        consistent = found is None or (by_paper is not None and
                                       page_bounds.quad_overlap(found.corners, by_paper.corners) >= 0.8)
        if by_paper is not None and not by_paper.uncertain and consistent:
            found = by_paper
            issues.append(_issue("quad_from_paper", "Contour de la page trouvé par la couleur du papier "
                                 "(fond irrégulier autour de la page).", Severity.INFO))
        elif found is None:
            # Page sombre ou colorée (couverture, photo pleine page) sur fond sombre : ses bords droits.
            by_lines = page_bounds.detect_line_quad(image)
            if by_lines is not None:
                found = by_lines
                issues.append(_issue("quad_from_lines", "Contour de la page trouvé par ses bords droits "
                                     "(page sombre ou colorée) : vérifiez le recadrage.", Severity.INFO))
    geometry.detected_quad = found.corners if found else None
    geometry.quad_uncertain = bool(found and found.uncertain)
    if found is None:
        issues.append(_issue("quad_not_found", "Aucun contour de feuille net : l'image entière est conservée "
                             "(contour réglable à la main).", Severity.INFO))
    elif found.uncertain:
        issues.append(_issue("quad_uncertain", "Contour de la feuille incertain : vérifiez le recadrage "
                             "(réglable à la main)."))


def _detect_orientation(image: np.ndarray, geometry: GeometryCorrection, languages: list[str] | None,
                        issues: list[Issue]) -> None:
    result = detect_orientation(image, languages)
    geometry.detected_quarter_turns = result.quarter_turns
    geometry.orientation_confidence = round(result.confidence, 2)
    geometry.orientation_method = result.method
    geometry.orientation_uncertain = result.uncertain
    logger.info("Orientation : %s", result.details)
    if result.uncertain:
        issues.append(_issue("orientation_uncertain", "Orientation incertaine : la page n'a pas été tournée "
                             "automatiquement. Vérifiez-la et utilisez la rotation manuelle si besoin."))


def _detect_skew(image: np.ndarray, geometry: GeometryCorrection, issues: list[Issue]) -> None:
    result = estimate_skew(to_gray(image))
    geometry.detected_skew_deg = result.angle_deg
    geometry.skew_uncertain = result.uncertain
    if result.uncertain:
        issues.append(_issue("skew_uncertain", "Inclinaison non mesurable de façon fiable : elle n'a pas été "
                             "corrigée (angle réglable à la main).", Severity.INFO))


def _apply_dewarp(image: np.ndarray, geometry: GeometryCorrection, detect: bool,
                  issues: list[Issue]) -> np.ndarray:
    if not geometry.auto_dewarp:
        geometry.dewarp_applied = False
        return image
    if not detect and not geometry.dewarp_applied:
        return image
    corrected, curvature = dewarp(image)
    if curvature.applicable:
        geometry.dewarp_applied = True
        issues.append(_issue("dewarp_applied", f"Courbure des lignes corrigée ({curvature.reason}, "
                             f"écart maximal {curvature.max_offset_px:.0f} px).", Severity.INFO))
        return corrected
    if geometry.dewarp_applied and not detect:
        issues.append(_issue("dewarp_not_reproduced", "La correction de courbure enregistrée n'a pas pu être "
                             f"rejouée ({curvature.reason}) : page laissée sans correction de courbure."))
    elif curvature.lines_used >= CURVE_MIN_LINES and curvature.reason != "page plate":
        issues.append(_issue("dewarp_skipped", f"Courbure non corrigée : mesure peu fiable ({curvature.reason})."))
    geometry.dewarp_applied = False
    return image


def correct_page(original_rgb: np.ndarray, settings: GeometryCorrection, detect: bool = True,
                 languages: list[str] | None = None) -> GeometryOutcome:
    """Construit l'image corrigée depuis l'original (après EXIF), sans jamais le modifier.

    detect=True : feuille (si auto_crop et pas de contour manuel), orientation (si auto_orientation, après
    recadrage), inclinaison (si auto_deskew et pas d'angle manuel) et courbure (si auto_dewarp) sont mesurées
    et enregistrées dans la copie des réglages. detect=False : les valeurs detected_* enregistrées sont
    rejouées telles quelles ; la courbure n'est réestimée (déterministe) que si dewarp_applied.
    """
    _check_rgb(original_rgb)
    geometry = settings.model_copy(deep=True)
    issues: list[Issue] = []
    image = original_rgb

    if detect and geometry.auto_crop and geometry.manual_quad is None:
        _detect_sheet(original_rgb, geometry, issues)
    quad = geometry.effective_quad()
    if quad is not None:
        warped = warp_quad(image, quad)
        if warped is None:
            issues.append(_issue("quad_too_small", "Contour de feuille trop petit : l'image entière est conservée."))
        else:
            image = warped

    if detect and geometry.auto_orientation:
        _detect_orientation(image, geometry, languages, issues)
    image = rotate_quarter(image, geometry.effective_quarter_turns())

    if detect and geometry.auto_deskew and geometry.manual_skew_deg is None:
        _detect_skew(image, geometry, issues)
    angle = geometry.effective_skew()
    if abs(angle) >= SKEW_MIN_APPLY:
        image = rotate_fine(image, -angle)

    image = _apply_dewarp(image, geometry, detect, issues)

    if detect:
        geometry.detection_done = True
    if image is original_rgb or np.shares_memory(image, original_rgb):
        image = np.array(image, copy=True)
    return GeometryOutcome(image=np.ascontiguousarray(image), geometry=geometry, issues=issues)
