"""Reusable widgets: the photometric sliders (ImageAdjustments), undo/redo bar, before/after view."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import streamlit as st

from core.document_models import Binarization, ImageAdjustments, WhiteBalance

from . import state


@dataclass(frozen=True)
class Slider:
    field: str
    label: str
    lo: float
    hi: float
    step: float
    help: str = ""


GROUPS: dict[str, list[Slider]] = {
    "Lumière": [
        Slider("exposure", "Exposition (EV)", -3.0, 3.0, 0.05, "Gain en lumière linéaire : +1 EV double la lumière."),
        Slider("brightness", "Luminosité", -100.0, 100.0, 1.0),
        Slider("contrast", "Contraste", -100.0, 100.0, 1.0),
        Slider("gamma", "Gamma", 0.2, 5.0, 0.05, "Supérieur à 1 : éclaircit les tons moyens."),
        Slider("shadows", "Ombres", -100.0, 100.0, 1.0, "Positif : débouche les tons sombres."),
        Slider("highlights", "Hautes lumières", -100.0, 100.0, 1.0, "Négatif : récupère les tons clairs."),
    ],
    "Couleur": [
        Slider("temperature", "Température", -100.0, 100.0, 1.0, "Positif : plus chaud (plus de rouge, moins de bleu)."),
        Slider("tint", "Teinte vert-magenta", -100.0, 100.0, 1.0, "Positif : magenta ; négatif : vert."),
        Slider("saturation", "Saturation", -100.0, 100.0, 1.0, "-100 : niveaux de gris."),
        Slider("hue", "Teinte (degrés)", -180.0, 180.0, 1.0),
    ],
    "Détail": [
        Slider("sharpness", "Netteté (%)", 0.0, 300.0, 5.0, "Masque flou : force de l'accentuation."),
        Slider("sharpen_radius", "Rayon de netteté (px)", 0.3, 10.0, 0.1),
        Slider("denoise", "Débruitage", 0.0, 30.0, 0.5),
        Slider("local_contrast", "Contraste local (CLAHE)", 0.0, 8.0, 0.1, "0 : désactivé."),
    ],
}
WB_LABELS = {WhiteBalance.NONE.value: "Aucune", WhiteBalance.GRAY_WORLD.value: "Monde gris (moyenne neutre)",
             WhiteBalance.WHITE_PATCH.value: "Papier blanc (pixels les plus clairs)"}
BIN_LABELS = {Binarization.NONE.value: "Aucune", Binarization.OTSU.value: "Otsu (seuil global)",
              Binarization.SAUVOLA.value: "Sauvola (seuil local)"}
NEUTRAL = ImageAdjustments().model_dump(mode="json")
OPTION_FIELDS = ("white_balance", "illumination_correction", "grayscale", "binarization")


def _reset(draft_key: str, fields: list[str]) -> None:
    draft = st.session_state.drafts.get(draft_key)
    if draft is not None:
        for name in fields:
            draft[name] = NEUTRAL[name]
    state.bump()


def adjustment_controls(draft_key: str, saved: ImageAdjustments, prefix: str, for_print: bool = True,
                        draft_source: ImageAdjustments | None = None) -> ImageAdjustments:
    """Every ImageAdjustments field as a widget, kept as a draft in session_state.

    `saved` is the stored value (the draft starts from `draft_source` or `saved`). Returns the draft.
    """
    draft = state.get_draft(draft_key, draft_source or saved)
    top = st.columns([2, 2, 1])
    draft["enabled"] = top[0].checkbox("Activer ces réglages", value=bool(draft["enabled"]),
                                       key=state.k(prefix, "enabled"),
                                       help="Décoché : les réglages sont conservés mais ne s'appliquent pas.")
    if top[2].button("Tout réinitialiser", key=state.k(prefix, "reset_all"),
                     on_click=_reset, args=(draft_key, [f for f in NEUTRAL if f != "enabled"])):
        pass
    for group, sliders in GROUPS.items():
        with st.expander(group, expanded=group == "Lumière"):
            if st.button(f"↺ Neutre ({group.lower()})", key=state.k(prefix, "reset", group),
                         on_click=_reset, args=(draft_key, [s.field for s in sliders])):
                pass
            for s in sliders:
                left, right = st.columns([8, 1], vertical_alignment="bottom")
                value = left.slider(s.label, min_value=s.lo, max_value=s.hi, value=float(draft[s.field]),
                                    step=s.step, key=state.k(prefix, s.field), help=s.help or None)
                draft[s.field] = float(value)
                right.button("↺", key=state.k(prefix, "r", s.field), help=f"{s.label} : revenir au neutre",
                             on_click=_reset, args=(draft_key, [s.field]))
    with st.expander("Balance des blancs, éclairage, gris, binarisation", expanded=False):
        if st.button("↺ Neutre (options)", key=state.k(prefix, "reset", "options"),
                     on_click=_reset, args=(draft_key, list(OPTION_FIELDS))):
            pass
        wb = list(WB_LABELS)
        draft["white_balance"] = st.selectbox("Balance des blancs", wb, index=wb.index(draft["white_balance"]),
                                              format_func=WB_LABELS.get, key=state.k(prefix, "white_balance"))
        draft["illumination_correction"] = st.checkbox(
            "Correction d'éclairage (aplanit les ombres et l'éclairage inégal)",
            value=bool(draft["illumination_correction"]), key=state.k(prefix, "illumination_correction"))
        draft["grayscale"] = st.checkbox("Niveaux de gris", value=bool(draft["grayscale"]),
                                         key=state.k(prefix, "grayscale"))
        bins = list(BIN_LABELS)
        draft["binarization"] = st.selectbox("Binarisation (noir et blanc)", bins,
                                             index=bins.index(draft["binarization"]),
                                             format_func=BIN_LABELS.get, key=state.k(prefix, "binarization"))
        if for_print and draft["binarization"] != Binarization.NONE.value:
            st.warning("La binarisation transforme l'image imprimée en noir et blanc pur : les illustrations "
                       "et les nuances de couleur seront perdues dans le PDF. Elle est d'ordinaire réservée "
                       "à l'image OCR.")
    try:
        value = ImageAdjustments.model_validate(draft)
    except ValueError as exc:
        st.error(f"Réglage invalide : {exc}")
        value = saved
    return value


def dirty_badge(draft: ImageAdjustments, saved: ImageAdjustments) -> bool:
    dirty = draft != saved
    if dirty:
        st.warning("Modifications non enregistrées : l'aperçu montre le brouillon, le PDF utilise encore "
                   "les réglages enregistrés.")
    else:
        st.caption("Réglages enregistrés : l'aperçu correspond à ce qui sera utilisé.")
    return dirty


def history_bar(prefix: str) -> None:
    """Undo / redo buttons with the label of the action."""
    hist = state.history()
    left, right = st.columns(2)
    if left.button("↶ Annuler" + (f" : {hist.undo_label()}" if hist.can_undo() else ""), key=state.k(prefix, "undo"),
                   disabled=not hist.can_undo(), width="stretch"):
        label = state.undo()
        if label:
            state.flash("success", f"Annulé : {label}")
        st.rerun()
    if right.button("↷ Rétablir" + (f" : {hist.redo_label()}" if hist.can_redo() else ""), key=state.k(prefix, "redo"),
                    disabled=not hist.can_redo(), width="stretch"):
        label = state.redo()
        if label:
            state.flash("success", f"Rétabli : {label}")
        st.rerun()


def before_after(before: np.ndarray, after: np.ndarray, prefix: str, captions=("Avant", "Après")) -> None:
    zoom = st.slider("Zoom de l'aperçu (%)", 25, 200, 100, 5, key=f"{prefix}_zoom",
                     help="Ne change que l'affichage : l'aperçu est calculé sur une copie réduite de la page.")
    width = max(120, int(480 * zoom / 100))
    cols = st.columns(2)
    cols[0].image(before, caption=captions[0], width=width, clamp=True)
    cols[1].image(after, caption=captions[1], width=width, clamp=True)


def histogram_chart(image: np.ndarray, key: str) -> None:
    if not st.checkbox("Afficher l'histogramme", key=key):
        return
    try:
        from core import image_adjustments
        hist = image_adjustments.histogram(image)
        import pandas as pd
        frame = pd.DataFrame({name: np.asarray(values, dtype=float) for name, values in hist.items()})
        st.line_chart(frame, height=180)
    except Exception as exc:
        st.caption(f"Histogramme indisponible ({exc}).")


def confirm_button(label: str, key: str, message: str = "Confirmer ?") -> bool:
    """Two-step destructive button: returns True once the user confirmed."""
    flag = f"confirm_{key}"
    if not st.session_state.get(flag):
        if st.button(label, key=f"{key}_ask"):
            st.session_state[flag] = True
            st.rerun()
        return False
    st.warning(message)
    yes, no = st.columns(2)
    if yes.button("Oui, confirmer", key=f"{key}_yes", type="primary"):
        st.session_state[flag] = False
        return True
    if no.button("Non", key=f"{key}_no"):
        st.session_state[flag] = False
        st.rerun()
    return False
