"""Mesures objectives d'une page et contrôle avant/après de l'amélioration (aucun appel IA).

Les scores (netteté, contraste) ne prouvent pas une amélioration : `compare` cherche seulement des
dégradations mesurables (netteté effondrée, écrêtage accru, contraste effondré, dérive de couleur,
contenu apparu au bord). Une dégradation fait écarter l'amélioration : la page redressée est gardée.
"""
from __future__ import annotations

import cv2
import numpy as np

from models.scanning import ImageMetrics, QualityCheck
from services import orientation_service
from services.image_service import downscale, paper_background, to_gray

MEASURE_SIDE = 1200            # copie réduite pour les textures (netteté, bruit)
COMPARE_SIDE = 600             # copies comparées pour la dérive de couleur
BORDER_FRACTION = 0.01         # bande de bord examinée pour un contenu coupé
INK_RELATIVE = 0.6             # « encre » = pixel sous 60 % de la clarté du papier local

# Dégradations (rapport après / avant, ou écart absolu)
MAX_SHARPNESS_LOSS = 0.55      # netteté après < 55 % de l'avant : l'image a été lissée
MAX_CLIP_INCREASE = 0.04       # +4 points de pixels écrêtés (noir ou blanc) : détails perdus
MIN_CONTRAST_RATIO = 0.6       # contraste après < 60 % de l'avant : page délavée
MAX_COLOR_SHIFT = 12.0         # ΔE76 moyen : au-delà, les couleurs de la photographie ont dérivé
MAX_COLOR_LOSS = 0.5           # couleur après < 50 % de l'avant sur une page colorée
COLORFUL_PAGE = 15.0           # indice de Hasler au-dessus duquel la page est jugée colorée
MAX_BORDER_CONTENT = 0.06      # part d'encre dans la bande de bord : contenu peut-être coupé
DARK_STRIP_RELATIVE = 0.45     # rangée de bord plus sombre que 45 % du papier : fond du banc, pas la page
MAX_DARK_STRIP = 0.08          # une bande plus large n'est plus un simple reste de fond
INTERIOR_MARGIN = 0.01         # marge retirée en plus des bandes sombres pour les mesures tonales


def _luminance(rgb: np.ndarray) -> np.ndarray:
    return to_gray(rgb) if rgb.ndim == 3 else rgb


def colorfulness(rgb: np.ndarray) -> float:
    """Indice de Hasler et Süsstrunk (0 = image grise ; > 30 = nettement colorée)."""
    if rgb.ndim != 3:
        return 0.0
    small = downscale(rgb, COMPARE_SIDE)[0].astype(np.float32)
    red, green, blue = small[..., 0], small[..., 1], small[..., 2]
    rg = red - green
    yb = 0.5 * (red + green) - blue
    return float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))


def noise_sigma(gray: np.ndarray) -> float:
    """Bruit estimé (écart-type) : MAD d'un passe-haut, sur les zones plates seulement."""
    image = gray.astype(np.float32)
    residual = image - cv2.medianBlur(gray, 3).astype(np.float32)
    gradient = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    flat = gradient < np.percentile(gradient, 50)
    values = residual[flat] if flat.sum() > 100 else residual.ravel()
    return float(1.4826 * np.median(np.abs(values - np.median(values))))


def illumination_unevenness(rgb: np.ndarray) -> float:
    """0..1 : écart relatif du fond papier estimé ; 0 = éclairage uniforme, > 0.25 = ombre marquée."""
    background = downscale(paper_background(rgb), 300)[0]
    high = float(np.percentile(background, 95))
    low = float(np.percentile(background, 5))
    return float(np.clip((high - low) / max(high, 1e-3), 0.0, 1.0))


def dark_edges(gray: np.ndarray) -> tuple[float, float, float, float]:
    """Largeur (fraction du côté) de la bande de fond sombre restée sur chaque bord (gauche, haut, droite, bas).

    Une rangée de bord compte comme fond si sa médiane est sous DARK_STRIP_RELATIVE × clarté du papier ;
    la bande s'arrête à la première rangée claire. Bornée à MAX_DARK_STRIP.
    """
    small = downscale(gray, MEASURE_SIDE)[0].astype(np.float32)
    height, width = small.shape
    paper = max(float(np.percentile(small, 90)), 1.0)
    limit = DARK_STRIP_RELATIVE * paper
    columns = np.median(small, axis=0)
    rows = np.median(small, axis=1)

    def strip(profile: np.ndarray) -> float:
        bright = np.nonzero(profile >= limit)[0]
        count = int(bright[0]) if len(bright) else len(profile)
        return round(min(MAX_DARK_STRIP, count / max(len(profile), 1)), 4)

    return (strip(columns), strip(rows), strip(columns[::-1]), strip(rows[::-1]))


def interior(image: np.ndarray, edges: tuple[float, float, float, float], margin: float = INTERIOR_MARGIN) -> np.ndarray:
    """Vue de l'intérieur de la page : bandes de fond retirées, puis une petite marge."""
    height, width = image.shape[:2]
    left, top, right, bottom = edges
    x0, x1 = round(width * (left + margin)), round(width * (1 - right - margin))
    y0, y1 = round(height * (top + margin)), round(height * (1 - bottom - margin))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return image
    return image[y0:y1, x0:x1]


def border_content(gray: np.ndarray) -> float:
    """Part d'encre (pixels nettement plus sombres que le papier) dans la bande de 1 % des quatre bords."""
    small = downscale(gray, MEASURE_SIDE)[0]
    height, width = small.shape
    band = max(2, round(max(height, width) * BORDER_FRACTION))
    paper = max(float(np.percentile(small, 90)), 1.0)
    ink = small < INK_RELATIVE * paper
    strips = [ink[:band, :], ink[-band:, :], ink[:, :band], ink[:, -band:]]
    return float(max(strip.mean() for strip in strips))


def measure(rgb: np.ndarray) -> ImageMetrics:
    """Mesures d'une image RGB (ou grise) uint8. Fonction pure."""
    height, width = rgb.shape[:2]
    gray = _luminance(rgb)
    small = downscale(gray, MEASURE_SIDE)[0]
    edges = dark_edges(small)
    inner = interior(small, edges)
    skew = orientation_service.estimate_skew(inner)
    rgb3 = rgb if rgb.ndim == 3 else cv2.cvtColor(rgb, cv2.COLOR_GRAY2RGB)
    inner_rgb = interior(downscale(rgb3, MEASURE_SIDE)[0], edges)
    low, high = np.percentile(inner, [2, 98])
    return ImageMetrics(
        width=width, height=height,
        sharpness=round(float(cv2.Laplacian(inner, cv2.CV_64F).var()), 2),
        contrast=round(float(inner.std()), 2),
        brightness=round(float(inner.mean()), 2),
        paper_level=round(float(np.percentile(inner, 90)), 2),
        tonal_range=round(float(high - low), 2),
        dark_clip=round(float((inner <= 5).mean()), 4),
        bright_clip=round(float((inner >= 250).mean()), 4),
        colorfulness=round(colorfulness(inner_rgb), 2),
        illumination_unevenness=round(illumination_unevenness(inner_rgb), 4),
        noise_sigma=round(noise_sigma(inner), 3),
        residual_skew_deg=None if skew.uncertain else skew.angle_deg,
        skew_reliable=not skew.uncertain,
        border_content=round(border_content(inner), 4),
        dark_edges=edges,
    )


def color_shift(before: np.ndarray, after: np.ndarray) -> float:
    """ΔE76 moyen (Lab) entre deux images ramenées à la même petite taille.

    Si `after` a été rogné (crop_inset), la comparaison porte sur des copies redimensionnées : le léger
    décalage géométrique reste faible devant une vraie dérive de couleur sur des copies de 600 px.
    """
    small_before = downscale(before, COMPARE_SIDE)[0]
    target = (small_before.shape[1], small_before.shape[0])
    small_after = cv2.resize(after, target, interpolation=cv2.INTER_AREA)
    if small_before.ndim == 2:
        small_before = cv2.cvtColor(small_before, cv2.COLOR_GRAY2RGB)
    if small_after.ndim == 2:
        small_after = cv2.cvtColor(small_after, cv2.COLOR_GRAY2RGB)
    lab_before = cv2.cvtColor(small_before.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    lab_after = cv2.cvtColor(small_after.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    # Flou léger : la netteté accrue ne doit pas compter comme une dérive de couleur.
    lab_before = cv2.GaussianBlur(lab_before, (0, 0), 2.0)
    lab_after = cv2.GaussianBlur(lab_after, (0, 0), 2.0)
    return float(np.linalg.norm(lab_before - lab_after, axis=2).mean())


def compare(before: np.ndarray, after: np.ndarray, before_metrics: ImageMetrics | None = None) -> QualityCheck:
    """Contrôle de l'amélioration : `before` = page redressée, `after` = page améliorée."""
    first = before_metrics or measure(before)
    second = first if after is before or (after.shape == before.shape and np.array_equal(after, before)) \
        else measure(after)
    shift = 0.0 if second is first else color_shift(before, after)
    degraded: list[str] = []
    warnings: list[str] = []
    if first.sharpness > 1 and second.sharpness < MAX_SHARPNESS_LOSS * first.sharpness:
        degraded.append(f"Netteté fortement réduite ({first.sharpness:.0f} → {second.sharpness:.0f}).")
    if second.dark_clip - first.dark_clip > MAX_CLIP_INCREASE:
        degraded.append("Ombres bouchées : des détails sombres sont perdus.")
    if second.bright_clip - first.bright_clip > MAX_CLIP_INCREASE:
        degraded.append("Hautes lumières brûlées : des détails clairs sont perdus.")
    if first.contrast > 5 and second.contrast < MIN_CONTRAST_RATIO * first.contrast:
        degraded.append("Contraste effondré : page délavée.")
    if shift > MAX_COLOR_SHIFT:
        degraded.append(f"Dérive de couleur excessive (ΔE moyen {shift:.1f}).")
    if first.colorfulness > COLORFUL_PAGE and second.colorfulness < MAX_COLOR_LOSS * first.colorfulness:
        degraded.append("Couleurs de la page fortement atténuées.")
    if second.border_content > MAX_BORDER_CONTENT and second.border_content > first.border_content + 0.02:
        degraded.append("Du contenu apparaît au bord après rognage : il a peut-être été coupé.")
    if not degraded and first.border_content > MAX_BORDER_CONTENT:
        warnings.append("Contenu dense au bord de la page redressée : vérifiez que rien n'a été coupé.")
    reasons = degraded + warnings
    if not reasons:
        reasons.append("Aucune dégradation mesurée.")
    return QualityCheck(before=first, after=second, color_shift=round(shift, 2),
                        enhancement_degraded=bool(degraded), passed=not degraded and not warnings,
                        reasons=reasons)
