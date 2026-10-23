"""Fenêtre « Modifier toutes les pages » : recadrage commun à la souris, rognage supplémentaire, rotation, pour
toutes les pages ou les pages à vérifier. Les pages modifiées sont retraitées en arrière-plan (sans Gemma)."""
from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from components import common
from components.crop_selector import crop_selector
from components.scan.shared import CROP_DISPLAY_WIDTH, CROP_SIDE, MAX_CROP_PERCENT
from models import Project
from models.scanning import ScanState
from repositories import scan_repository
from services import scanning_service
from workers import scan_runner

DIALOG_KEY = "_scan_bulk_open"
TOOLS = ["Recadrage commun", "Rognage des bords", "Rotation"]
SCOPES = ["Toutes les pages", "Pages à vérifier"]


def open_bulk() -> None:
    st.session_state[DIALOG_KEY] = True
    st.rerun(scope="app")


def close_bulk() -> None:
    st.session_state.pop(DIALOG_KEY, None)


def show_if_open(project: Project) -> None:
    if st.session_state.get(DIALOG_KEY):
        bulk_dialog(project)


def _apply(project: Project, operation: Callable[[list[str]], list[str]], target_ids: list[str], what: str) -> None:
    """Applique une modification à plusieurs pages puis les retraite en arrière-plan ; ferme la fenêtre."""
    with common.user_action() as result, st.spinner(f"{what}…"):
        changed = operation(target_ids)
        if changed:
            scan_runner.start([project.id], changed, use_ai=False)
    if result.ok:
        common.flash("success" if changed else "info",
                     f"{what} : {len(changed)} page(s) modifiée(s), retraitement en arrière-plan." if changed
                     else f"{what} : aucune page à modifier.")
        close_bulk()
        st.rerun()


@st.dialog("Modifier toutes les pages", width="large", on_dismiss=close_bulk)
def bulk_dialog(project: Project) -> None:
    running = scan_runner.is_running()
    st.caption("Mêmes outils que la retouche, appliqués à plusieurs pages d'un coup. Les pages modifiées sont "
               "retraitées en arrière-plan (géométrie, amélioration, OCR) ; chacune reste retouchable ensuite et "
               "son historique est conservé.")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        scope = st.segmented_control("Pages concernées", SCOPES, default=SCOPES[0], key="bulk_scope")
        tool = st.segmented_control("Outil", TOOLS, default=TOOLS[0], key="bulk_tool")
    review_ids = [task.page_id for task in scan_repository.tasks(project.id, [ScanState.REVIEW_REQUIRED])]
    target_ids = review_ids if scope == "Pages à vérifier" else [page.id for page in project.pages]
    st.caption(f"{len(target_ids)} page(s) concernée(s).")
    if running:
        st.info("Un traitement est en cours : attendez sa fin pour modifier toutes les pages.",
                icon=":material/hourglass_top:")
    disabled = running or not target_ids

    if tool == "Rognage des bords":
        st.caption("Pourcentage retiré en plus sur chaque bord. Il s'ajoute au rognage automatique des bandes de "
                   "fond ; les autres réglages de chaque page sont gardés.")
        columns = st.columns(4)
        trim = {}
        for column, (side, label) in zip(columns, (("left", "Gauche"), ("top", "Haut"), ("right", "Droite"),
                                                   ("bottom", "Bas"))):
            trim[side] = column.number_input(f"{label} (%)", min_value=0.0, max_value=MAX_CROP_PERCENT, step=0.5,
                                             value=0.0, key=f"bulk_trim_{side}") / 100.0
        with st.container(horizontal=True):
            if st.button("Appliquer ce rognage", type="primary", icon=":material/crop:", disabled=disabled,
                         key="bulk_trim_apply"):
                _apply(project, lambda ids: scanning_service.apply_trim_to_pages(project, trim, ids), target_ids,
                       "Rognage des bords")
            if st.button("Retirer le rognage supplémentaire", icon=":material/undo:", disabled=disabled,
                         key="bulk_trim_auto"):
                _apply(project, lambda ids: scanning_service.apply_trim_to_pages(project, None, ids), target_ids,
                       "Rognage supplémentaire retiré")
    elif tool == "Rotation":
        with st.container(horizontal=True, vertical_alignment="bottom"):
            turns = st.segmented_control("Rotation manuelle (après l'orientation automatique)", [0, 1, 2, 3],
                                         default=0, format_func=lambda value: f"{value * 90}°", key="bulk_turns")
            if st.button("Appliquer cette rotation", type="primary", icon=":material/rotate_right:",
                         disabled=disabled or turns is None, key="bulk_rotate"):
                _apply(project, lambda ids: scanning_service.apply_rotation_to_pages(project, int(turns), ids),
                       target_ids, "Rotation")
    else:
        st.caption("Tracez la zone de la page sur une photo de référence : le même contour est appliqué aux autres "
                   "photos. Idéal quand l'appareil et le livre restent à la même place (banc de numérisation).")
        by_page = {page.id: page for page in project.pages}
        with st.container(horizontal=True, vertical_alignment="bottom"):
            reference_id = st.selectbox("Page de référence", list(by_page), key="bulk_reference",
                                        format_func=lambda page_id: f"Page {project.page_number(page_id)} · "
                                                                    f"{by_page[page_id].original_name}")
            if st.button("Revenir au contour automatique", icon=":material/auto_fix_high:", disabled=disabled,
                         key="bulk_auto_quad"):
                _apply(project, lambda ids: scanning_service.apply_quad_to_pages(project, None, ids), target_ids,
                       "Contour automatique")
        reference = by_page[reference_id]
        image = common.original_image(project, reference, CROP_SIDE)
        if image is None:
            st.warning("Photographie de référence illisible.", icon=":material/image_not_supported:")
            return
        corners = crop_selector(common.jpeg_data_uri(image), reference.geometry.effective_quad(),
                                key=f"bulk_crop_{reference_id}", width=CROP_DISPLAY_WIDTH, mode="rect",
                                alt=f"Photo de référence, page {project.page_number(reference_id)}")
        if corners is not None and not disabled:
            _apply(project, lambda ids: scanning_service.apply_quad_to_pages(project, corners, ids), target_ids,
                   "Recadrage commun")
