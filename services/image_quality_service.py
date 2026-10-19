"""Mesures objectives d'une page et contrôle avant/après de l'amélioration (aucun appel IA).

Les scores (netteté, contraste) ne prouvent pas une amélioration : `compare` cherche seulement des
dégradations mesurables (netteté effondrée, écrêtage accru, contraste effondré, dérive de couleur,
contenu apparu au bord). Une dégradation fait écarter l'amélioration : la page redressée est gardée.
"""
from __future__ import annotations

import cv2
import numpy as np

from models.geometry import Box
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
MAX_COLOR_SHIFT = 8.0          # dérive chromatique moyenne (a*b*) : au-delà, les couleurs ont dérivé
MAX_COLOR_LOSS = 0.5           # couleur après < 50 % de l'avant sur une page colorée
COLORFUL_PAGE = 15.0           # indice de Hasler au-dessus duquel la page est jugée colorée
LIGHT_PAPER_MIN = 90.0         # sous cette clarté de papier (page sombre), l'« encre au bord » n'a pas de sens
MAX_BORDER_CONTENT = 0.06      # part d'encre dans la bande de bord : contenu peut-être coupé
DARK_STRIP_RELATIVE = 0.45     # rangée de bord plus sombre que 45 % du papier : fond du banc, pas la page
EDGE_LINE_BAND = 0.04         # recherche d'une ligne de bord de page dans les 4 % extérieurs
EDGE_LINE_SHARE = 0.55         # ligne continue sur plus de 55 % de la hauteur (ou largeur)
EDGE_LINE_GRADIENT = 12.0      # gradient de Sobel minimal (niveaux de gris) d'un pixel de ligne
FOREIGN_STRIP_MAX = 0.025      # bande extérieure où une rangée sans papier est du fond (tranche, marge)
FOREIGN_PAPER_SHARE = 0.3      # rangée « sans papier » : moins de 30 % de pixels couleur papier
MAX_DARK_STRIP = 0.08          # une bande plus large n'est plus un simple reste de fond
NOISE_WINDOW = 1024            # recadrage central (pixels) pour l'estimation du bruit
NOISE_FLAT_PERCENTILE = 60     # pixels les moins contrastés retenus (les lettres sont exclues)
INTERIOR_MARGIN = 0.01
CONTENT_INK_RELATIVE = 0.85    # encre ou image : pixel sous 85 % de la clarté du papier
HUE_MIN_CHROMA = 8.0           # pixels assez colorés pour que leur teinte ait un sens
HUE_DEGREES_PER_UNIT = 2.0     # 8 unités (seuil MAX_COLOR_SHIFT) = 16° de teinte
CONTENT_MIN_PIXELS = 50
CONTENT_MIN_SHARE = 0.003      # une rangée est du contenu si au moins 0,3 % de ses pixels sont de l'encre
CONTENT_MIN_RUN = 3            # ... et fait partie d'une suite d'au moins 3 rangées
CONTENT_PADDING = 0.01         # marge gardée autour du bloc de contenu         # marge retirée en plus des bandes sombres pour les mesures tonales


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
    """Bruit estimé (écart-type) par la méthode d'Immerkær, sur un recadrage central en pleine résolution.

    Une réduction moyennerait le bruit : on mesure au pixel près. Les contours (lettres) sont exclus
    par un seuil sur le gradient de Sobel.
    """
    height, width = gray.shape[:2]
    side = NOISE_WINDOW
    y0, x0 = max(0, (height - side) // 2), max(0, (width - side) // 2)
    window = gray[y0:y0 + side, x0:x0 + side].astype(np.float32)
    if min(window.shape) < 8:
        return 0.0
    kernel = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    response = np.abs(cv2.filter2D(window, -1, kernel))[1:-1, 1:-1]
    gradient = cv2.magnitude(cv2.Sobel(window, cv2.CV_32F, 1, 0), cv2.Sobel(window, cv2.CV_32F, 0, 1))[1:-1, 1:-1]
    flat = gradient <= np.percentile(gradient, NOISE_FLAT_PERCENTILE)
    values = response[flat] if flat.sum() > 100 else response.ravel()
    return float(np.sqrt(np.pi / 2.0) / 6.0 * values.mean())


def illumination_unevenness(rgb: np.ndarray) -> float:
    """0..1 : écart relatif du fond papier estimé ; 0 = éclairage uniforme, > 0.25 = ombre marquée."""
    background = downscale(paper_background(rgb), 300)[0]
    high = float(np.percentile(background, 95))
    low = float(np.percentile(background, 5))
    return float(np.clip((high - low) / max(high, 1e-3), 0.0, 1.0))


def dark_edges(gray: np.ndarray, rgb: np.ndarray | None = None) -> tuple[float, float, float, float]:
    """Largeur (fraction du côté) du fond resté sur chaque bord (gauche, haut, droite, bas).

    Deux critères, depuis le bord vers l'intérieur, jusqu'à la première rangée de papier :
    - rangée sombre (médiane < DARK_STRIP_RELATIVE × papier) : fond du banc, ombre, jusqu'à MAX_DARK_STRIP ;
    - rangée presque sans couleur de papier (tranche du livre, marge blanche, table), seulement dans les
      FOREIGN_STRIP_MAX extérieurs (une photo qui déborde au bord n'y perd au plus que cette bande).
    """
    small = downscale(gray, MEASURE_SIDE)[0].astype(np.float32)
    paper = max(float(np.percentile(small, 90)), 1.0)
    dark_columns = np.median(small, axis=0) < DARK_STRIP_RELATIVE * paper
    dark_rows = np.median(small, axis=1) < DARK_STRIP_RELATIVE * paper
    if rgb is not None and rgb.ndim == 3:
        from services import page_bounds

        small_rgb = downscale(rgb, MEASURE_SIDE)[0]
        mask, _ = page_bounds._paper_mask(small_rgb)
        foreign_columns = mask.mean(axis=0) < FOREIGN_PAPER_SHARE
        foreign_rows = mask.mean(axis=1) < FOREIGN_PAPER_SHARE
    else:
        foreign_columns = np.zeros(small.shape[1], bool)
        foreign_rows = np.zeros(small.shape[0], bool)

    def strip(dark: np.ndarray, foreign: np.ndarray) -> float:
        length = max(len(dark), 1)
        limit = int(FOREIGN_STRIP_MAX * length)
        count = 0
        for index in range(len(dark)):
            if dark[index] or (index < limit and foreign[index]):
                count += 1
            else:
                break
        return round(min(MAX_DARK_STRIP, count / length), 4)

    lines = edge_lines(small.astype(np.uint8))
    strips = (strip(dark_columns, foreign_columns), strip(dark_rows, foreign_rows),
              strip(dark_columns[::-1], foreign_columns[::-1]), strip(dark_rows[::-1], foreign_rows[::-1]))
    return tuple(round(max(a, b), 4) for a, b in zip(strips, lines))  # type: ignore[return-value]


def edge_lines(gray: np.ndarray) -> tuple[float, float, float, float]:
    """Vrai bord de la page quand des tranches d'autres pages (couleur papier) restent au-delà.

    Dans la bande extérieure (EDGE_LINE_BAND) de chaque côté, une colonne (ou rangée) dont le gradient est
    fort sur plus de EDGE_LINE_SHARE de la longueur est une ligne droite continue : le texte n'en produit
    pas (interrompu entre les mots et les lignes). La plus intérieure de ces lignes est le bord retenu.
    """
    height, width = gray.shape
    gradient_x = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
    gradient_y = np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
    rows = slice(int(0.1 * height), int(0.9 * height))
    columns = slice(int(0.1 * width), int(0.9 * width))
    vertical = (gradient_x[rows] > EDGE_LINE_GRADIENT).mean(axis=0)
    horizontal = (gradient_y[:, columns] > EDGE_LINE_GRADIENT).mean(axis=1)

    def innermost(profile: np.ndarray) -> float:
        band = max(1, int(EDGE_LINE_BAND * len(profile)))
        hits = np.nonzero(profile[:band] >= EDGE_LINE_SHARE)[0]
        return round(float(hits.max() + 1) / len(profile), 4) if len(hits) else 0.0

    return (innermost(vertical), innermost(horizontal), innermost(vertical[::-1]), innermost(horizontal[::-1]))


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
    rgb3 = rgb if rgb.ndim == 3 else cv2.cvtColor(rgb, cv2.COLOR_GRAY2RGB)
    edges = dark_edges(small, rgb3)
    inner = interior(small, edges)
    skew = orientation_service.estimate_skew(inner)
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
        noise_sigma=round(noise_sigma(interior(gray, edges)), 3),
        residual_skew_deg=None if skew.uncertain else skew.angle_deg,
        skew_reliable=not skew.uncertain,
        border_content=round(border_content(inner), 4),
        dark_edges=edges,
    )


def color_shift(before: np.ndarray, after: np.ndarray) -> float:
    """Dérive chromatique moyenne (distance a*b* en Lab) entre deux images ramenées à la même petite taille.

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
    # Seule la chromaticité (a*, b*) compte : un changement de clarté voulu (exposition) n'est pas une dérive.
    return float(np.linalg.norm(lab_before[..., 1:] - lab_after[..., 1:], axis=2).mean())


def _content_extent(profile: np.ndarray) -> tuple[int, int] | None:
    """Première et dernière rangée de contenu : au moins CONTENT_MIN_SHARE d'encre, dans une suite d'au
    moins CONTENT_MIN_RUN rangées (une poussière isolée ne compte pas)."""
    active = profile >= CONTENT_MIN_SHARE
    runs = np.convolve(active.astype(np.int32), np.ones(CONTENT_MIN_RUN, np.int32), mode="valid")
    hits = np.nonzero(runs >= CONTENT_MIN_RUN)[0]
    if not len(hits):
        return None
    return int(hits[0]), int(hits[-1] + CONTENT_MIN_RUN)


def content_box(rgb: np.ndarray) -> Box | None:
    """Bloc de contenu (texte, images, numéro de page) d'une page à fond blanc, marge de 1 %.

    Profils d'encre par ligne et par colonne (pixel sous CONTENT_INK_RELATIVE × papier). None si vide.
    """
    small = downscale(_luminance(rgb), MEASURE_SIDE)[0]
    paper = max(float(np.percentile(small, 90)), 1.0)
    ink = cv2.morphologyEx((small < CONTENT_INK_RELATIVE * paper).astype(np.uint8), cv2.MORPH_OPEN,
                           np.ones((2, 2), np.uint8))
    if int(ink.sum()) < CONTENT_MIN_PIXELS:
        return None
    rows, columns = _content_extent(ink.mean(axis=1)), _content_extent(ink.mean(axis=0))
    if rows is None or columns is None:
        return None
    height, width = small.shape
    x0, x1 = columns[0] / width - CONTENT_PADDING, columns[1] / width + CONTENT_PADDING
    y0, y1 = rows[0] / height - CONTENT_PADDING, rows[1] / height + CONTENT_PADDING
    x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1)
    if x1 - x0 <= 0.05 or y1 - y0 <= 0.05:
        return None
    return Box(x=round(float(x0), 5), y=round(float(y0), 5), w=round(float(x1 - x0), 5), h=round(float(y1 - y0), 5))


def image_color_shift(before: np.ndarray, after: np.ndarray) -> float:
    """Dérive de teinte mesurée seulement dans les images de la page (0 s'il n'y en a pas)."""
    from services import image_enhancement

    small_before = downscale(before, COMPARE_SIDE)[0]
    small_after = cv2.resize(after, (small_before.shape[1], small_before.shape[0]), interpolation=cv2.INTER_AREA)
    regions = cv2.erode(image_enhancement.image_regions(small_before, COMPARE_SIDE).astype(np.uint8),
                        np.ones((5, 5), np.uint8)).astype(bool)
    if regions.sum() < 200:
        return 0.0
    lab_before = cv2.cvtColor(small_before.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    lab_after = cv2.cvtColor(small_after.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    # Éclaircir une photo change a*b* sans changer ses couleurs : on mesure l'écart de TEINTE (angle), pondéré
    # par la saturation, ramené à une échelle comparable (MAX_COLOR_SHIFT = 8 ≈ 16° de teinte).
    chroma = np.hypot(lab_before[..., 1], lab_before[..., 2])
    weight = np.where(regions & (chroma > HUE_MIN_CHROMA), chroma, 0.0)
    if weight.sum() <= 0:
        return 0.0
    hue_before = np.arctan2(lab_before[..., 2], lab_before[..., 1])
    hue_after = np.arctan2(lab_after[..., 2], lab_after[..., 1])
    difference = np.degrees(np.abs(np.angle(np.exp(1j * (hue_after - hue_before)))))
    return float((difference * weight).sum() / weight.sum() / HUE_DEGREES_PER_UNIT)


def compare(before: np.ndarray, after: np.ndarray, before_metrics: ImageMetrics | None = None, *,
            background_removed: bool = False) -> QualityCheck:
    """Contrôle de l'amélioration : `before` = page redressée, `after` = page améliorée.

    background_removed : le fond a été volontairement mis en blanc ; le blanc du papier n'est pas un écrêtage
    et la fidélité des couleurs est mesurée sur les images (photos, illustrations) seulement.
    """
    first = before_metrics or measure(before)
    second = first if after is before or (after.shape == before.shape and np.array_equal(after, before)) \
        else measure(after)
    if second is first:
        shift = 0.0
    elif background_removed:
        shift = image_color_shift(before, after)
    else:
        shift = color_shift(before, after)
    degraded: list[str] = []
    warnings: list[str] = []
    if first.sharpness > 1 and second.sharpness < MAX_SHARPNESS_LOSS * first.sharpness:
        degraded.append(f"Netteté fortement réduite ({first.sharpness:.0f} → {second.sharpness:.0f}).")
    if second.dark_clip - first.dark_clip > MAX_CLIP_INCREASE:
        degraded.append("Ombres bouchées : des détails sombres sont perdus.")
    if not background_removed and second.bright_clip - first.bright_clip > MAX_CLIP_INCREASE:
        degraded.append("Hautes lumières brûlées : des détails clairs sont perdus.")
    if first.contrast > 5 and second.contrast < MIN_CONTRAST_RATIO * first.contrast:
        degraded.append("Contraste effondré : page délavée.")
    if shift > MAX_COLOR_SHIFT:
        degraded.append(f"Dérive de couleur excessive (écart chromatique moyen {shift:.1f}).")
    if first.colorfulness > COLORFUL_PAGE and second.colorfulness < MAX_COLOR_LOSS * first.colorfulness:
        degraded.append("Couleurs de la page fortement atténuées.")
    if second.border_content > MAX_BORDER_CONTENT and second.border_content > first.border_content + 0.05:
        degraded.append("Du contenu apparaît au bord après rognage : il a peut-être été coupé.")
    if not degraded and first.border_content > MAX_BORDER_CONTENT and first.paper_level >= LIGHT_PAPER_MIN:
        warnings.append("Contenu dense au bord de la page redressée : vérifiez que rien n'a été coupé.")
    reasons = degraded + warnings
    if not reasons:
        reasons.append("Aucune dégradation mesurée.")
    return QualityCheck(before=first, after=second, color_shift=round(shift, 2),
                        enhancement_degraded=bool(degraded), passed=not degraded and not warnings,
                        reasons=reasons)
