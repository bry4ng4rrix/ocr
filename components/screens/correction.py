"""Écran C — Correction d'image : valeurs détectées, réglages géométriques, réglages d'amélioration avec aperçu
immédiat sur une copie réduite. L'original n'est jamais modifié : tous les réglages sont des paramètres."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import streamlit as st

from components import common
from components.compare_view import Panel, compare_view
from models import GeometryCorrection, ImageEnhancement, Page, PageStatus
from models.errors import BookOcrError
from services import image_service, project_service
from services.processing_service import Step

PREVIEW_SIDE = 900
ROTATION_LABELS = {0: "0°", 1: "90°", 2: "180°", 3: "270°"}
ENHANCE_FIELDS = ["enabled", "white_balance", "brightness", "contrast", "saturation", "sharpness", "denoise",
                  "shadow_correction", "readability"]
GEOMETRY_FIELDS = ["auto_crop", "auto_orientation", "auto_deskew", "auto_dewarp", "turns", "manual_skew_on",
                   "manual_skew"]

project = common.require_project()
common.require_pages(project)
page: Page = common.page_selector(project, key="correction")
page_locked = common.warn_if_locked(project.id, page.id)
paths = project_service.page_paths(project, page)


@st.cache_data(max_entries=16, show_spinner=False)
def _enhanced_preview(path: str, stamp: int, settings_json: str) -> np.ndarray:
    """Aperçu des réglages sur une copie réduite de l'image corrigée (calcul fait par image_service)."""
    base = image_service.downscale(image_service.read_image(Path(path)), PREVIEW_SIDE)[0]
    return image_service.enhance(base, ImageEnhancement.model_validate_json(settings_json))


# --------------------------------------------------------------------------- avant / après

if not paths.corrected.exists():
    st.info("Cette page n'a pas encore été corrigée.", icon=":material/info:")
    if st.button("Calculer la correction automatique", type="primary", icon=":material/auto_fix_high:",
                 disabled=page_locked):
        with common.user_action("Correction calculée.") as result:
            common.process_now(project.id, [page.id], [Step.GEOMETRY], label="Correction de la page…")
        if result.ok:
            st.rerun()

try:
    original = common.original_image(project, page)
    corrected = common.corrected_image(project, page)
except BookOcrError as error:
    st.error(error.user_message)
    original = corrected = None
compare_view([Panel("Avant (original)", original, "Original illisible."),
              Panel("Après (corrigé)", corrected, "Pas encore corrigée.")])

# --------------------------------------------------------------------------- valeurs détectées

geometry = page.geometry
st.subheader("Valeurs détectées")
if not geometry.detection_done:
    st.caption("Détection automatique pas encore faite pour cette page.")
else:
    with st.container(horizontal=True):
        turns = geometry.detected_quarter_turns
        st.metric("Orientation détectée", f"{(turns or 0) * 90}°" if turns is not None else "—", border=True,
                  help="Rotation horaire appliquée pour redresser la page.")
        confidence = geometry.orientation_confidence
        st.metric("Confiance de l'orientation", f"{confidence:.1f}" if confidence is not None else "—", border=True,
                  help=f"Méthode : {geometry.orientation_method or 'aucune'}")
        st.metric("Inclinaison détectée",
                  f"{geometry.detected_skew_deg:.2f}°" if geometry.detected_skew_deg is not None else "—",
                  border=True)
        st.metric("Contour de la feuille", "détecté" if geometry.detected_quad else "non détecté", border=True)
        st.metric("Courbure corrigée", "oui" if geometry.dewarp_applied else "non", border=True)
    st.caption(f"Valeurs appliquées : rotation {geometry.effective_quarter_turns() * 90}°, "
               f"inclinaison {geometry.effective_skew():.2f}°, méthode d'orientation "
               f"« {geometry.orientation_method or 'aucune'} ».")
    if geometry.orientation_uncertain:
        st.warning("Orientation incertaine : vérifiez la page et corrigez la rotation à la main si besoin.",
                   icon=":material/help:")
    if geometry.quad_uncertain:
        st.warning("Contour de la feuille incertain : désactivez le recadrage automatique s'il coupe la page.",
                   icon=":material/help:")
    if geometry.skew_uncertain:
        st.warning("Inclinaison incertaine : un angle manuel peut être plus juste.", icon=":material/help:")
common.show_issues(page.issues)
if page.ocr_outdated:
    st.warning("La correction a changé depuis l'OCR : relancez l'OCR et la mise en page (écran Traitement OCR).",
               icon=":material/sync_problem:")

# --------------------------------------------------------------------------- réglages géométriques

st.subheader("Correction géométrique")
geo_scope = f"geo_{page.id}"
common.sync_widgets(geo_scope, {
    "auto_crop": geometry.auto_crop, "auto_orientation": geometry.auto_orientation,
    "auto_deskew": geometry.auto_deskew, "auto_dewarp": geometry.auto_dewarp,
    "turns": geometry.manual_quarter_turns, "manual_skew_on": geometry.manual_skew_deg is not None,
    "manual_skew": float(geometry.manual_skew_deg or 0.0)}, geometry.model_dump_json())

with st.form(f"geometry_form_{page.id}"):
    toggles = st.columns(4)
    toggles[0].toggle("Recadrage et perspective auto", key=f"{geo_scope}_auto_crop", disabled=page_locked)
    toggles[1].toggle("Orientation auto", key=f"{geo_scope}_auto_orientation", disabled=page_locked)
    toggles[2].toggle("Redressement auto", key=f"{geo_scope}_auto_deskew", disabled=page_locked)
    toggles[3].toggle("Correction de la courbure", key=f"{geo_scope}_auto_dewarp", disabled=page_locked,
                      help="Appliquée seulement si la mesure est fiable.")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        st.segmented_control("Rotation manuelle (sens horaire)", list(ROTATION_LABELS),
                             format_func=ROTATION_LABELS.get, required=True, key=f"{geo_scope}_turns",
                             disabled=page_locked)
        st.checkbox("Angle manuel", key=f"{geo_scope}_manual_skew_on", disabled=page_locked,
                    help="Remplace l'inclinaison détectée.")
        st.slider("Angle (°, sens antihoraire)", min_value=-45.0, max_value=45.0, step=0.1,
                  key=f"{geo_scope}_manual_skew", disabled=page_locked)
    apply_geometry = st.form_submit_button("Appliquer la correction", type="primary", icon=":material/check:",
                                           disabled=page_locked)

if apply_geometry:
    values = common.widget_values(geo_scope, GEOMETRY_FIELDS)
    updated = page.model_copy(deep=True)
    updated.geometry.auto_crop = bool(values["auto_crop"])
    updated.geometry.auto_orientation = bool(values["auto_orientation"])
    updated.geometry.auto_deskew = bool(values["auto_deskew"])
    updated.geometry.auto_dewarp = bool(values["auto_dewarp"])
    updated.geometry.manual_quarter_turns = int(values["turns"] or 0)
    updated.geometry.manual_skew_deg = float(values["manual_skew"]) if values["manual_skew_on"] else None
    with common.user_action("Correction géométrique appliquée.") as result:
        common.save_page(project.id, updated, "Correction géométrique")
        common.process_now(project.id, [page.id], [Step.GEOMETRY], label="Correction de la page…")
    if result.ok:
        st.rerun()

with st.container(horizontal=True):
    if st.button("Relancer la détection automatique", icon=":material/refresh:", disabled=page_locked,
                 help="Recalcule orientation, contour et inclinaison depuis l'original."):
        with common.user_action("Détection relancée.") as result:
            common.process_now(project.id, [page.id], [Step.GEOMETRY], redetect=True, label="Détection…")
        if result.ok:
            st.rerun()
    if st.button("Réinitialiser la correction", icon=":material/restart_alt:", disabled=page_locked,
                 help="Revient aux réglages automatiques par défaut (rotation et angle manuels retirés)."):
        updated = page.model_copy(deep=True)
        updated.geometry = GeometryCorrection()
        with common.user_action("Correction réinitialisée.") as result:
            common.save_page(project.id, updated, "Réinitialisation de la correction")
            common.process_now(project.id, [page.id], [Step.GEOMETRY], redetect=True, label="Détection…")
        if result.ok:
            st.rerun()

# --------------------------------------------------------------------------- amélioration

st.subheader("Amélioration de l'image")
if not paths.corrected.exists():
    st.caption("Les réglages d'amélioration seront disponibles une fois la page corrigée.")
    st.stop()

enhancement = page.enhancement
enh_scope = f"enh_{page.id}"
common.sync_widgets(enh_scope, {**enhancement.model_dump(), "white_balance": enhancement.white_balance == "auto"},
                    enhancement.model_dump_json())


def _settings_from_widgets() -> ImageEnhancement:
    values = common.widget_values(enh_scope, ENHANCE_FIELDS)
    values["white_balance"] = "auto" if values["white_balance"] else "none"
    return ImageEnhancement(**values)


def _reset_enhancement() -> None:
    """Rappel du bouton « Revenir à l'original » : réglages neutres enregistrés et image améliorée retirée."""
    updated = page.model_copy(deep=True)
    updated.enhancement = ImageEnhancement()
    try:
        common.save_page(project.id, updated, "Réglages d'amélioration neutres")
        common.process_now(project.id, [page.id], [Step.ENHANCE], label="Mise à jour de l'image…")
    except BookOcrError as error:
        common.flash("error", error.user_message)
        return
    common.forget_widgets(enh_scope)
    common.flash("success", "Réglages neutres : l'image de travail est de nouveau l'image corrigée.")


settings_column, preview_column = st.columns([1, 2])
with settings_column:
    st.toggle("Réglages actifs", key=f"{enh_scope}_enabled", disabled=page_locked)
    st.toggle("Balance des blancs automatique", key=f"{enh_scope}_white_balance", disabled=page_locked)
    st.slider("Luminosité", -100.0, 100.0, step=1.0, key=f"{enh_scope}_brightness", disabled=page_locked)
    st.slider("Contraste", -100.0, 100.0, step=1.0, key=f"{enh_scope}_contrast", disabled=page_locked)
    st.slider("Saturation", -100.0, 100.0, step=1.0, key=f"{enh_scope}_saturation", disabled=page_locked)
    st.slider("Netteté", 0.0, 100.0, step=1.0, key=f"{enh_scope}_sharpness", disabled=page_locked)
    st.slider("Débruitage", 0.0, 100.0, step=1.0, key=f"{enh_scope}_denoise", disabled=page_locked)
    st.slider("Correction des ombres", 0.0, 100.0, step=1.0, key=f"{enh_scope}_shadow_correction",
              disabled=page_locked)
    st.slider("Lisibilité (contraste local)", 0.0, 100.0, step=1.0, key=f"{enh_scope}_readability",
              disabled=page_locked)

settings = _settings_from_widgets()
with preview_column:
    try:
        preview = _enhanced_preview(str(paths.corrected), paths.corrected.stat().st_mtime_ns,
                                    settings.model_dump_json())
        base = common.corrected_image(project, page, max_side=PREVIEW_SIDE)
    except BookOcrError as error:
        st.error(error.user_message)
        preview = base = None
    compare_view([Panel("Image corrigée", base), Panel("Aperçu des réglages", preview)], border=False)
    st.caption("Aperçu calculé sur une copie réduite ; « Appliquer » recalcule l'image en pleine résolution.")

with st.container(horizontal=True):
    if st.button("Appliquer les réglages", type="primary", icon=":material/check:",
                 disabled=page_locked or settings == enhancement):
        updated = page.model_copy(deep=True)
        updated.enhancement = settings
        with common.user_action("Réglages d'amélioration appliqués.") as result:
            common.save_page(project.id, updated, "Réglages d'amélioration")
            common.process_now(project.id, [page.id], [Step.ENHANCE], label="Amélioration de l'image…")
        if result.ok:
            if page.status in (PageStatus.OCR_DONE, PageStatus.ANALYSED):
                common.flash("info", "L'OCR de cette page a été fait avec les anciens réglages : relancez-le "
                                     "(écran Traitement OCR) pour en tenir compte.")
            st.rerun()
    st.button("Revenir à l'original (réglages neutres)", icon=":material/undo:", on_click=_reset_enhancement,
              disabled=page_locked or enhancement.is_neutral())
