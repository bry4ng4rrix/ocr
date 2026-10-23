"""Modes d'amélioration du fac-similé, séparés de la détection géométrique."""
from __future__ import annotations

import cv2
import numpy as np

from models.page import ImageEnhancement
from models.scanning import (AdjustmentPlan, ColorContent, ContrastLevel, ImageMetrics, Level, LightingIssue,
                             OrientationIssue, ScanAction, ScanAdjustments, VisualAnalysis)
from services import image_service


def enhance_page(rgb: np.ndarray, mode: str = "Automatique", strength: int = 35) -> np.ndarray:
    """Améliore une copie de page ; le traitement automatique préserve les pages déjà propres."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    deviation = float(gray.std())
    if mode == "Document noir et blanc":
        # Seuillage adaptatif doux : garde les illustrations en gris en mode global uniquement.
        local = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                      cv2.THRESH_BINARY, 41, 13)
        return cv2.cvtColor(local, cv2.COLOR_GRAY2RGB)
    if mode == "Automatique" and deviation > 52 and float(gray.mean()) > 205:
        return rgb.copy()
    settings = ImageEnhancement(enabled=True, white_balance="auto" if mode != "Document couleur" else "none",
        contrast=min(24, strength * .45), brightness=0, saturation=0,
        sharpness=min(30, strength * .55), denoise=min(20, strength * .35),
        readability=min(18, strength * .3),
        shadow_correction=min(40, strength) if mode in {"Document couleur amélioré", "Automatique"} else 0)
    result = image_service.enhance(rgb, settings)
    if mode in {"Document couleur amélioré", "Automatique"}:
        lab = cv2.cvtColor(result, cv2.COLOR_RGB2LAB)
        l, a, b = cv2.split(lab)
        l = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(l)
        result = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2RGB)
    return result


# --------------------------------------------------------------------------- numérisation fidèle

# Seuils du plan sans analyse IA (stricts : on ne corrige que ce qui est nettement mesuré).
STRICT_UNEVENNESS = 0.30          # éclairage du papier : écart relatif du fond
STRICT_DARK = 90.0                # luminance moyenne : page sous-exposée
STRICT_BRIGHT = 225.0             # luminance moyenne : page surexposée...
STRICT_BRIGHT_CLIP = 0.10         # ... avec beaucoup de blancs brûlés
STRICT_LOW_RANGE = 90.0           # étendue tonale (98e − 2e centile) : contraste faible
STRICT_NOISE = 3.0                # écart-type du bruit de luminance (estimateur d'Immerkær)
# Seuils de confirmation d'une action proposée par Gemma (plus souples, mais toujours mesurés).
CONFIRM_UNEVENNESS = 0.18
CONFIRM_DARK = 120.0
CONFIRM_LOW_RANGE = 120.0
CONFIRM_NOISE = 1.8
SHARPEN_MIN = 40.0                # sous ce Laplacien : flou fort, rien ne restaure les détails absents
SHARPEN_MAX = 150.0               # au-dessus : déjà net, pas de renforcement
COLORFUL_PAGE = 15.0              # indice de Hasler : page colorée, aucune correction qui atténue la couleur

PAPER_MIN = 90.0                  # en dessous, la page n'a pas de papier clair (couverture, aplat sombre)
PAPER_WHITE = 240.0               # clarté visée par le nettoyage du fond (texture du papier gardée)
MAX_WHITENING_GAIN = 2.5
WHITEN_BELOW = 222.0
BG_INK = 0.62                     # clarté relative au papier sous laquelle un pixel est de l'encre (gardé)
BG_PAPER = 0.88                   # au-dessus : papier, mis en blanc (entre les deux : transition douce)
FRAME_MIN_SHARE = 0.03            # rectangle encadré protégé : 3 % à 85 % de la page
FRAME_MAX_SHARE = 0.85
FRAME_RECTANGULARITY = 0.85              # papier plus sombre : nettoyage du fond proposé
COLORFUL_PAPER_MIN = 130.0        # page colorée : gain seulement si un vrai papier clair est mesuré
EDGE_STRIP_MIN = 0.001            # bande de fond à rogner (fraction du côté)
EDGE_STRIP_MAX = 0.08             # bande plus large : contour douteux, vérification plutôt que rognage
EDGE_SAFETY = 0.004               # marge rognée en plus de la bande (transition floue fond / papier)

# Valeurs appliquées (modérées et bornées)
GAMMA_DARK = 1.25
GAMMA_BRIGHT = 0.85
CONTRAST_STEP = 12.0
LOCAL_CONTRAST_STEP = 18.0
DENOISE_STEP = 18.0
SHARPEN_STEP = 20.0

_GEOMETRY_ACTIONS = {ScanAction.ROTATE, ScanAction.DETECT_PAGE, ScanAction.PERSPECTIVE_CORRECTION,
                     ScanAction.MANUAL_REVIEW}


def _crop_fractions(rgb: np.ndarray, left: float, top: float, right: float, bottom: float) -> np.ndarray:
    height, width = rgb.shape[:2]
    x0, x1 = round(width * left), width - round(width * right)
    y0, y1 = round(height * top), height - round(height * bottom)
    if x1 - x0 < 16 or y1 - y0 < 16:
        return rgb
    return rgb[y0:y1, x0:x1]


def crop_edges(rgb: np.ndarray, adjustments: ScanAdjustments) -> np.ndarray:
    """Rogne les bords : d'abord le rognage de la page (bande de fond restée après redressement), puis le
    rognage supplémentaire choisi pour le livre (fractions de l'image déjà rognée) ; copie."""
    out = _crop_fractions(rgb, adjustments.crop_left, adjustments.crop_top, adjustments.crop_right,
                          adjustments.crop_bottom)
    out = _crop_fractions(out, adjustments.extra_left, adjustments.extra_top, adjustments.extra_right,
                          adjustments.extra_bottom)
    return out.copy()


def whiten_paper(rgb: np.ndarray, strength: float) -> np.ndarray:
    """Nettoyage du fond : gain local = (blanc visé / fond du papier estimé)^force, teinte conservée.

    Le papier devient uniforme et clair (PAPER_WHITE) ; l'encre garde son contraste relatif ; sous une
    illustration, le fond est interpolé depuis le papier voisin (image_service.paper_background) : les
    couleurs des photographies ne sont pas délavées. Jamais de seuillage.
    """
    strength = float(np.clip(strength / 100.0, 0.0, 1.0))
    if strength == 0.0:
        return rgb.copy()
    background = image_service.paper_background(rgb)
    gain = np.power((PAPER_WHITE / 255.0) / background, strength, dtype=np.float32)
    np.clip(gain, 1.0 / MAX_WHITENING_GAIN, MAX_WHITENING_GAIN, out=gain)
    out = rgb.astype(np.float32)
    out *= gain[..., None]
    out += 0.5
    np.clip(out, 0, 255, out=out)
    return out.astype(np.uint8)


def _framed_rectangles(small: np.ndarray) -> np.ndarray:
    """Rectangles encadrés (reproductions, manuscrits, tableaux-images) : bords nets, forme rectangulaire.

    Une reproduction de document a souvent la couleur du papier : la détection des photos par la couleur
    ne la voit pas, son cadre droit si. Retourne un masque bool de la taille de `small`.
    """
    height, width = small.shape[:2]
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(gray, 30, 90), np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    mask = np.zeros((height, width), np.uint8)
    page_area = float(height * width)
    for contour in contours:
        area = cv2.contourArea(contour)
        if not FRAME_MIN_SHARE * page_area <= area <= FRAME_MAX_SHARE * page_area:
            continue
        approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        (_, _), (rect_w, rect_h), _ = cv2.minAreaRect(contour)
        if len(approx) == 4 and cv2.isContourConvex(approx) and area >= FRAME_RECTANGULARITY * rect_w * rect_h:
            cv2.drawContours(mask, [approx], -1, 1, thickness=cv2.FILLED)
    return mask.astype(bool)


def image_regions(rgb: np.ndarray, max_side: int = 900) -> np.ndarray:
    """Zones à protéger lors de la suppression du fond (bool, taille de `rgb`) : photos, illustrations, aplats
    colorés (image_service.picture_mask) et rectangles encadrés."""
    small, scale = image_service.downscale(rgb, max_side)
    mask = image_service.picture_mask(small) | _framed_rectangles(small)
    if scale >= 1.0:
        return mask
    return cv2.resize(mask.astype(np.uint8), (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_NEAREST).astype(bool)


def remove_background(rgb: np.ndarray, strength: float = 100.0) -> np.ndarray:
    """Suppression du fond : le papier devient blanc pur, le texte garde sa forme et sa couleur, les images
    (photos, illustrations, encadrés) sont conservées, seulement éclaircies comme le papier voisin.

    1. éclairage égalisé : division par le fond du papier estimé (même calcul que whiten_paper) ;
    2. hors images : rampe douce entre « encre » (clarté relative ≤ BG_INK) et « papier » (≥ BG_PAPER) vers
       le blanc — pas de seuillage, les contours des lettres restent lissés ;
    3. images : version simplement éclaircie, raccord progressif (masque adouci) ;
    4. `strength` mélange avec le simple nettoyage du fond (0 = whiten_paper seul).
    """
    strength = float(np.clip(strength / 100.0, 0.0, 1.0))
    background = image_service.paper_background(rgb)
    gain = np.clip(1.0 / background, 1.0 / MAX_WHITENING_GAIN, MAX_WHITENING_GAIN).astype(np.float32)
    relative = rgb.astype(np.float32) * gain[..., None] / 255.0          # papier ≈ 1.0
    lightness = relative @ np.array([0.299, 0.587, 0.114], np.float32)
    ramp = np.clip((lightness - BG_INK) / (BG_PAPER - BG_INK), 0.0, 1.0)
    ramp = ramp * ramp * (3.0 - 2.0 * ramp)                              # transition douce (smoothstep)
    paper_white = PAPER_WHITE / 255.0
    whitened = np.clip(relative * paper_white, 0.0, 1.0)
    cleaned = np.clip(relative, 0.0, 1.0) * (1.0 - ramp[..., None]) + ramp[..., None]
    protect = image_regions(rgb).astype(np.float32)
    feather = max(1.0, max(rgb.shape[:2]) / 400.0)
    protect = np.clip(cv2.GaussianBlur(cv2.dilate(protect, np.ones((3, 3), np.uint8)), (0, 0), feather) * 1.5, 0, 1)
    mixed = whitened * protect[..., None] + cleaned * (1.0 - protect[..., None])
    out = whitened * (1.0 - strength) + mixed * strength
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _exposure(rgb: np.ndarray, exposure: float) -> np.ndarray:
    """Gain de clarté 2^(exposure/50) appliqué aux trois canaux (teinte conservée), écrêté à 255."""
    gain = 2.0 ** (exposure / 50.0)
    table = np.clip(np.arange(256, dtype=np.float32) * gain + 0.5, 0, 255).astype(np.uint8)
    return cv2.LUT(rgb, table)


def _tone_lut(brightness: float, gamma: float) -> np.ndarray:
    """Table 256 : gamma (> 1 éclaircit les tons moyens, noir et blanc fixes) puis décalage de luminosité."""
    values = np.arange(256, dtype=np.float32) / 255.0
    if gamma != 1.0:
        values = np.power(values, 1.0 / gamma)
    values = values * 255.0 + brightness * 1.27
    return np.clip(values + 0.5, 0, 255).astype(np.uint8)


def apply_adjustments(rgb: np.ndarray, adjustments: ScanAdjustments) -> np.ndarray:
    """Applique les réglages à une copie de la page redressée. Neutre = copie identique.

    Ordre : rognage, nettoyage du fond, ombres, balance des blancs et contraste (image_service.enhance), exposition, gamma et luminosité
    (même table sur les trois canaux : la teinte est conservée), débruitage, contraste local, netteté.
    Jamais de binarisation ni de conversion en gris.
    """
    if adjustments.is_neutral():
        return rgb.copy()
    out = crop_edges(rgb, adjustments) if adjustments.has_crop else rgb
    if adjustments.background_removal:
        out = remove_background(out, adjustments.background_removal)
    elif adjustments.paper_whitening:
        out = whiten_paper(out, adjustments.paper_whitening)
    shadows = ImageEnhancement(enabled=True, white_balance="auto" if adjustments.white_balance else "none",
                               shadow_correction=adjustments.shadow_correction, contrast=adjustments.contrast)
    if not shadows.is_neutral():
        out = image_service.enhance(out, shadows)
    if adjustments.exposure:
        out = _exposure(out, adjustments.exposure)
    if adjustments.gamma != 1.0 or adjustments.brightness:
        out = cv2.LUT(out, _tone_lut(adjustments.brightness, adjustments.gamma))
    finishing = ImageEnhancement(enabled=True, denoise=adjustments.denoise, readability=adjustments.local_contrast,
                                 sharpness=adjustments.sharpness)
    if not finishing.is_neutral():
        out = image_service.enhance(out, finishing)
    return out.copy() if out is rgb else out


def plan_adjustments(metrics: ImageMetrics, analysis: VisualAnalysis | None = None, *,
                     remove_background: bool = False) -> AdjustmentPlan:
    """Plan déterministe à partir des mesures, éventuellement guidé par l'analyse Gemma.

    remove_background : le fond du papier est supprimé (réglage du livre) au lieu d'être seulement éclairci.
    Sans analyse : seuils stricts. Avec analyse : une action proposée n'est retenue que si une mesure la
    confirme (seuils plus souples) ; sinon elle est rejetée avec sa raison. Les actions géométriques ne
    règlent rien ici : elles demandent une vérification si OpenCV ne les a pas déjà traitées.
    """
    values: dict[str, float | bool] = {}
    actions: list[ScanAction] = []
    rejected: list[ScanAction] = []
    reasons: list[str] = []
    review_reasons: list[str] = []
    proposed = set(analysis.recommended_actions) if analysis else set()
    if analysis is not None and ScanAction.ROTATE in proposed and analysis.orientation_issue in (
            OrientationIssue.NONE, OrientationIssue.UNCERTAIN):
        proposed.discard(ScanAction.ROTATE)       # incohérent avec sa propre réponse : ignoré
        rejected.append(ScanAction.ROTATE)
        reasons.append("ROTATE proposé par Gemma sans défaut d'orientation signalé : ignoré.")
    colorful = metrics.colorfulness > COLORFUL_PAGE or (
        analysis is not None and analysis.color_content == ColorContent.FULL_COLOR)

    def keep(action: ScanAction, reason: str, **settings: float | bool) -> None:
        values.update(settings)
        if action not in actions:
            actions.append(action)
        reasons.append(reason)

    def flag(reason: str) -> None:
        reasons.append(reason)
        review_reasons.append(reason)

    def reject(action: ScanAction, reason: str) -> None:
        if action not in rejected:
            rejected.append(action)
        reasons.append(reason)

    # Fond du papier : éclairage irrégulier (ombres) et papier gris corrigés ensemble (nettoyage du fond)
    shadow_proposed = bool(proposed & {ScanAction.SHADOW_CORRECTION, ScanAction.LIGHTING_CORRECTION}) or (
        analysis is not None and (analysis.shadow_level in (Level.MEDIUM, Level.HIGH)
                                  or analysis.lighting_issue in (LightingIssue.UNEVEN, LightingIssue.UNDEREXPOSED)))
    has_paper = metrics.paper_level >= PAPER_MIN and (not colorful or metrics.paper_level >= COLORFUL_PAPER_MIN)
    uneven = metrics.illumination_unevenness > (CONFIRM_UNEVENNESS if shadow_proposed else STRICT_UNEVENNESS)
    gray = metrics.paper_level < WHITEN_BELOW
    dark_proposed = analysis is not None and analysis.lighting_issue == LightingIssue.UNDEREXPOSED
    if has_paper and remove_background:
        if uneven:
            actions.append(ScanAction.SHADOW_CORRECTION)
        keep(ScanAction.LIGHTING_CORRECTION, "Fond supprimé (papier blanc), texte et images conservés.",
             background_removal=100.0)
    elif has_paper and (uneven or gray):
        detail = []
        if uneven:
            detail.append(f"éclairage irrégulier {metrics.illumination_unevenness:.2f}")
            actions.append(ScanAction.SHADOW_CORRECTION)
        if gray:
            detail.append(f"papier gris {metrics.paper_level:.0f}/255")
        keep(ScanAction.LIGHTING_CORRECTION,
             f"Fond nettoyé et éclairci vers {PAPER_WHITE:.0f} ({', '.join(detail)}).", paper_whitening=100.0)
    elif not has_paper and metrics.brightness < STRICT_DARK and dark_proposed:
        keep(ScanAction.LIGHTING_CORRECTION, f"Page sombre (luminance moyenne {metrics.brightness:.0f}).",
             gamma=GAMMA_DARK)
    elif metrics.brightness > STRICT_BRIGHT and metrics.bright_clip > STRICT_BRIGHT_CLIP:
        keep(ScanAction.LIGHTING_CORRECTION, f"Page surexposée (luminance moyenne {metrics.brightness:.0f}).",
             gamma=GAMMA_BRIGHT)
    elif shadow_proposed:
        reject(ScanAction.SHADOW_CORRECTION, "Ombre ou sous-exposition signalée par Gemma mais non mesurée.")
    if not has_paper and metrics.paper_level < PAPER_MIN:
        reasons.append("Pas de papier clair mesuré (couverture ou aplat sombre) : fond laissé tel quel.")

    # Contraste (étendue tonale de l'intérieur de la page)
    contrast_proposed = ScanAction.CONTRAST_ADJUSTMENT in proposed or (
        analysis is not None and analysis.contrast_level == ContrastLevel.LOW)
    whitened = bool(values.get("paper_whitening") or values.get("background_removal"))
    # Après nettoyage du fond, l'étendue tonale grandit avec le gain du papier : on juge celle attendue, et
    # seul le contraste local (qui protège le papier) est ajouté, sinon le papier déjà clair serait brûlé.
    expected_range = metrics.tonal_range * (min(MAX_WHITENING_GAIN, PAPER_WHITE / max(metrics.paper_level, 1.0))
                                            if whitened else 1.0)
    if has_paper and expected_range < (CONFIRM_LOW_RANGE if contrast_proposed else STRICT_LOW_RANGE):
        settings = {"local_contrast": LOCAL_CONTRAST_STEP} if whitened else {
            "contrast": CONTRAST_STEP, "local_contrast": LOCAL_CONTRAST_STEP}
        keep(ScanAction.CONTRAST_ADJUSTMENT, f"Contraste faible mesuré (étendue tonale {metrics.tonal_range:.0f}).",
             **settings)
    elif contrast_proposed:
        reject(ScanAction.CONTRAST_ADJUSTMENT, "Faible contraste signalé par Gemma mais non mesuré.")

    # Bandes de fond restées aux bords après redressement
    crops: dict[str, float] = {}
    # Sans papier clair (couverture, photo pleine page), « bande sombre » et « encre » ne veulent rien dire.
    edge_strips = metrics.dark_edges if metrics.paper_level >= PAPER_MIN else (0.0, 0.0, 0.0, 0.0)
    for side, width in zip(("crop_left", "crop_top", "crop_right", "crop_bottom"), edge_strips):
        if EDGE_STRIP_MIN <= width < EDGE_STRIP_MAX:
            crops[side] = round(min(EDGE_STRIP_MAX, width + EDGE_SAFETY), 4)
        elif width >= EDGE_STRIP_MAX:
            flag("Large bande sombre au bord : contour de page douteux, recadrage à vérifier.")
    if crops:
        keep(ScanAction.DETECT_PAGE, "Fine bande de fond restée au bord après redressement : rognée.", **crops)

    # Bruit
    noise_proposed = ScanAction.DENOISE in proposed or (
        analysis is not None and analysis.noise_level in (Level.MEDIUM, Level.HIGH))
    if metrics.noise_sigma > (CONFIRM_NOISE if noise_proposed else STRICT_NOISE):
        keep(ScanAction.DENOISE, f"Bruit mesuré (σ ≈ {metrics.noise_sigma:.1f}).", denoise=DENOISE_STEP)
    elif noise_proposed:
        reject(ScanAction.DENOISE, "Bruit signalé par Gemma mais non mesuré.")

    # Flou : netteté modérée seulement pour un flou léger, jamais de « restauration »
    blur_proposed = ScanAction.SHARPEN in proposed or (
        analysis is not None and analysis.blur_level in (Level.MEDIUM, Level.HIGH))
    if metrics.sharpness < SHARPEN_MIN:
        flag("Page très floue : la netteté ne peut pas restaurer des détails absents ; "
             "une nouvelle photographie est préférable.")
        if blur_proposed:
            reject(ScanAction.SHARPEN, "Flou trop fort pour un renforcement de netteté.")
    elif blur_proposed and metrics.sharpness < SHARPEN_MAX:
        keep(ScanAction.SHARPEN, f"Flou léger confirmé (netteté {metrics.sharpness:.0f}) : renforcement modéré.",
             sharpness=SHARPEN_STEP)
    elif blur_proposed:
        reject(ScanAction.SHARPEN, "Flou signalé par Gemma mais la page mesurée est nette.")

    # Couleurs : rien qui les atténue (pas de balance des blancs automatique)
    if colorful or ScanAction.PRESERVE_COLOR in proposed:
        if ScanAction.PRESERVE_COLOR not in actions:
            actions.append(ScanAction.PRESERVE_COLOR)
        reasons.append("Page colorée : couleurs conservées, aucune balance des blancs automatique.")

    # Géométrie et vérifications : décidées par OpenCV, jamais réglées par Gemma
    for action in sorted(proposed & _GEOMETRY_ACTIONS):
        flag(f"Gemma signale « {action.value} » : vérification de la géométrie demandée.")
    if analysis is not None:
        if analysis.manual_review_required:
            flag("Gemma demande une vérification humaine.")
        if analysis.curvature_issue == Level.HIGH:
            flag("Courbure forte signalée : une homographie ne la corrige pas entièrement.")

    adjustments = ScanAdjustments(**values)
    corrective = [action for action in actions if action != ScanAction.PRESERVE_COLOR]
    source = "neutral" if not corrective else ("gemma" if analysis is not None else "measured")
    if not corrective:
        reasons.insert(0, "Aucune correction nécessaire : la page est gardée telle quelle.")
    return AdjustmentPlan(adjustments=adjustments, actions=actions, rejected_actions=rejected, reasons=reasons,
                          review_required=bool(review_reasons), review_reasons=review_reasons, source=source)
