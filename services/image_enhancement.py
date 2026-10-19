"""Modes d'amélioration du fac-similé, séparés de la détection géométrique."""
from __future__ import annotations

import cv2
import numpy as np

from models.page import ImageEnhancement
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
