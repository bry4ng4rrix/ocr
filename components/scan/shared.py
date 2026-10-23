"""Éléments partagés par l'écran « Numérisation intelligente » et ses fenêtres (retouche, toutes les pages).

Libellés, caches d'images, test réel d'Ollama, suivi du traitement, actions courtes. Aucune logique métier :
tout passe par les services.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import streamlit as st

import config
from components import common
from models.errors import AiUnavailableError, BookOcrError
from models.scanning import (MAX_CROP, PAGE_FORMAT_CUSTOM, PAGE_FORMAT_IMAGE, PAGE_FORMAT_MEDIAN,
                             SCAN_PAGE_FORMATS, ScanState)
from services import image_service, ollama_service
from workers import scan_runner

PREVIEW_SIDE = 900
GALLERY_SIDE = 420
GALLERY_PAGE_SIZE = 8
CROP_SIDE = 1400                  # copie de la photo originale affichée dans le sélecteur de recadrage
CROP_DISPLAY_WIDTH = 1100
UPLOAD_TYPES = ["jpg", "jpeg", "png", "tif", "tiff", "pdf"]
USE_AI_KEY = "_scan_use_ai"       # choix « Analyse Gemma » de l'étape 1, repris par la retouche

STATE_LABELS = {ScanState.PENDING: "En attente", ScanState.PROCESSING: "En cours", ScanState.COMPLETED: "Terminée",
                ScanState.REVIEW_REQUIRED: "À vérifier", ScanState.FAILED: "En erreur"}
STATE_COLORS = {ScanState.COMPLETED: "green", ScanState.REVIEW_REQUIRED: "orange", ScanState.FAILED: "red",
                ScanState.PROCESSING: "blue", ScanState.PENDING: "gray"}
SOURCE_LABELS = {"neutral": "Aucune correction", "measured": "Mesures", "gemma": "Gemma + mesures",
                 "manual": "Manuel"}
FILTERS = {"Toutes": None, "À vérifier": [ScanState.REVIEW_REQUIRED], "En erreur": [ScanState.FAILED],
           "En attente": [ScanState.PENDING, ScanState.PROCESSING], "Terminées": [ScanState.COMPLETED]}
SLIDERS = {  # champ: (libellé, min, max, pas)
    "background_removal": ("Suppression du fond (images conservées)", 0.0, 100.0, 5.0),
    "paper_whitening": ("Nettoyage du fond (papier blanc)", 0.0, 100.0, 5.0),
    "exposure": ("Exposition (gain)", -30.0, 40.0, 1.0),
    "brightness": ("Luminosité", -50.0, 50.0, 1.0),
    "contrast": ("Contraste", -50.0, 50.0, 1.0),
    "gamma": ("Gamma", 0.5, 2.0, 0.05),
    "shadow_correction": ("Correction des ombres", 0.0, 80.0, 1.0),
    "local_contrast": ("Contraste local", 0.0, 60.0, 1.0),
    "denoise": ("Débruitage", 0.0, 60.0, 1.0),
    "sharpness": ("Netteté", 0.0, 60.0, 1.0),
}
CROPS = {"crop_left": "Rognage gauche", "crop_top": "Rognage haut", "crop_right": "Rognage droit",
         "crop_bottom": "Rognage bas"}
EXTRA_CROPS = {"extra_left": "Rognage supplémentaire gauche", "extra_top": "Rognage supplémentaire haut",
               "extra_right": "Rognage supplémentaire droit", "extra_bottom": "Rognage supplémentaire bas"}
FORMAT_OPTIONS = [PAGE_FORMAT_IMAGE, PAGE_FORMAT_MEDIAN, *SCAN_PAGE_FORMATS, PAGE_FORMAT_CUSTOM]
FORMAT_LABELS = {PAGE_FORMAT_IMAGE: "Taille de chaque page (photo)", PAGE_FORMAT_MEDIAN: "Format commun du livre",
                 PAGE_FORMAT_CUSTOM: "Personnalisé (mm)",
                 **{name: f"{name} ({fmt.width_mm:g} × {fmt.height_mm:g} mm)" for name, fmt in SCAN_PAGE_FORMATS.items()}}
ORIENTATION_LABELS = {"auto": "Automatique (selon la page)", "portrait": "Portrait (vertical)",
                      "landscape": "Paysage (horizontal)"}
MAX_CROP_PERCENT = MAX_CROP * 100


# --------------------------------------------------------------------------- caches et petites aides


@st.cache_data(ttl=30, show_spinner=False)
def ollama_check(enabled: bool, base_url: str, model: str) -> tuple[str, str]:
    """Test réel (serveur, modèle installé, capacité vision) ; (niveau, message). Rafraîchi toutes les 30 s."""
    if not enabled:
        return "off", "Analyse IA désactivée (OLLAMA_ENABLED=false). Le traitement local fonctionne sans IA."
    current = ollama_service.status()
    if not current.available:
        return "error", f"{current.message} Le traitement continue sans IA."
    try:
        info = ollama_service.model_details(model)
    except AiUnavailableError as error:
        return "error", f"{error.user_message} Le traitement continue sans IA."
    if not info.has_vision:
        return "error", f"Le modèle « {model} » n'a pas la capacité vision. Le traitement continue sans IA."
    return "ok", f"Ollama joignable, modèle « {model} » installé ({info.parameter_size}, {info.quantization})."


def ai_status() -> tuple[str, str]:
    return ollama_check(config.OLLAMA_ENABLED and config.OLLAMA_ANALYSIS_ENABLED, config.OLLAMA_BASE_URL,
                        ollama_service.configured_model())


def use_ai() -> bool:
    """Analyse Gemma demandée (option de traitement de l'étape 1) et réellement disponible."""
    return bool(st.session_state.get(USE_AI_KEY, False)) and ai_status()[0] == "ok"


@st.cache_data(max_entries=64, show_spinner=False)
def _image(path: str, stamp: int, max_side: int, source_index: int = 0) -> np.ndarray:
    return image_service.downscale(image_service.read_image(Path(path), source_index), max_side)[0]


def cached_image(path: Path | None, max_side: int = PREVIEW_SIDE, source_index: int = 0) -> np.ndarray | None:
    if path is None or not path.is_file():
        return None
    try:
        return _image(str(path), path.stat().st_mtime_ns, max_side, source_index)
    except BookOcrError:
        return None


def image_size(path: Path | None) -> str:
    if path is None or not path.is_file():
        return "—"
    try:
        from PIL import Image

        with Image.open(path) as image:
            return f"{image.width} × {image.height} px"
    except OSError:
        return "—"


def run_action(label: str, success: str, action: Callable[[], object]) -> None:
    """Action courte (une page, quelques secondes) avec retour d'erreur en français, puis rafraîchissement."""
    with common.user_action(success) as result, st.spinner(label):
        action()
    if result.ok:
        st.rerun()


# --------------------------------------------------------------------------- suivi du traitement


def runner_caption() -> None:
    """Une ligne sur le traitement en arrière-plan (en cours ou dernier résultat)."""
    runner = scan_runner.status()
    if runner.running:
        speed = f" — {runner.seconds_per_page:.0f} s/page mesurées" if runner.seconds_per_page else ""
        together = f", {runner.workers} pages à la fois" if runner.workers > 1 else ""
        st.caption(f"Traitement en arrière-plan : {runner.processed} page(s) faite(s){speed}{together}.")
    elif runner.message and runner.started_at:
        extra = f" · {runner.export_message}" if runner.export_message else ""
        st.caption(f"Dernier traitement : {runner.message} ({runner.processed} page(s), {runner.failed} en "
                   f"échec){extra}")


def rerun_when_runner_stops() -> None:
    """À appeler dans un fragment rafraîchi : quand le traitement se termine, tout l'écran est rafraîchi."""
    running = scan_runner.is_running()
    was_running = st.session_state.get("_scan_was_running", False)
    st.session_state["_scan_was_running"] = running
    if was_running and not running:
        st.rerun(scope="app")
