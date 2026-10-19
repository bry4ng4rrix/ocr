"""Écran B — Pages : import, vignettes, ordre (glisser-déposer et repli accessible), rotation, remplacement,
suppression, affichage original / corrigé."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

import config
from components import common
from components.compare_view import Panel, compare_view
from components.sortable_pages import sortable_pages
from models import Page, PageStatus, Severity
from models.errors import BookOcrError
from services import project_service
from services.processing_service import Step

UPLOAD_TYPES = sorted(extension.lstrip(".") for extension in config.ALLOWED_EXTENSIONS)
CARDS_PER_SCREEN = 24
GRID_COLUMNS = 6
ROTATION_LABELS = {0: "0°", 1: "90°", 2: "180°", 3: "270°"}

project = common.require_project()
locked = common.locked_page_ids(project.id)


def _flash_import_report(report) -> None:
    if report.added_page_ids:
        common.flash("success", f"{len(report.added_page_ids)} page(s) importée(s).")
    for issue in report.issues:
        common.flash("error" if issue.severity == Severity.ERROR else "warning", issue.message)
    if not report.added_page_ids and not report.issues:
        common.flash("warning", "Aucune page n'a été ajoutée.")


def _refresh_geometry(page_id: str) -> None:
    common.process_now(project.id, [page_id], [Step.GEOMETRY], label="Correction de la page…")


# --------------------------------------------------------------------------- import

with st.expander("Importer des pages", icon=":material/upload_file:", expanded=not project.pages):
    upload_round = st.session_state.setdefault("_upload_round", 0)
    with st.form(f"import_form_{upload_round}", border=False):
        files = st.file_uploader("Photos ou PDF du livre", type=UPLOAD_TYPES, accept_multiple_files=True,
                                 help="JPG, PNG, TIFF (plusieurs pages possibles) ou PDF (une page par page du PDF).")
        positions = [None] + list(range(len(project.pages)))
        position = st.selectbox(
            "Insérer", positions,
            format_func=lambda index: "À la fin du livre" if index is None
            else f"Avant la page {index + 1} ({project.pages[index].original_name})")
        submitted = st.form_submit_button("Importer", type="primary", icon=":material/upload:")
    if submitted:
        if not files:
            st.error("Choisissez au moins un fichier.")
        else:
            with common.user_action() as result:
                with st.spinner("Import des fichiers…"):
                    with common.editing(project.id) as fresh:
                        report = project_service.import_files(fresh, [(file.name, file.getvalue()) for file in files],
                                                              position=position)
            if result.ok:
                _flash_import_report(report)
                st.session_state["_upload_round"] = upload_round + 1
                st.rerun()

    with st.form("import_folder_form", border=False):
        folder = st.text_input(
            "Dossier local (un fichier à la fois, sans tout charger en mémoire)",
            help="Chemin d'un dossier de photos ou d'un fichier. L'ordre des noms n'est pas forcément l'ordre du livre.")
        recursive = st.checkbox("Inclure les sous-dossiers", value=True)
        folder_submit = st.form_submit_button("Importer le dossier", icon=":material/folder_open:")
    if folder_submit:
        if not folder.strip():
            st.error("Indiquez un chemin de dossier ou de fichier.")
        else:
            try:
                paths = project_service.collect_import_paths(Path(folder.strip()), recursive=recursive)
            except BookOcrError as error:
                st.error(error.user_message)
            else:
                if not paths:
                    st.error("Aucun fichier JPG, PNG, TIFF ou PDF n'a été trouvé.")
                else:
                    with common.user_action() as result:
                        with st.spinner(f"Import de {len(paths)} fichier(s)…"):
                            with common.editing(project.id) as fresh:
                                report = project_service.import_paths(fresh, paths)
                    if result.ok:
                        _flash_import_report(report)
                        st.rerun()

common.require_pages(project)

# --------------------------------------------------------------------------- vignettes

st.subheader("Pages du livre")
with st.container(horizontal=True, vertical_alignment="bottom"):
    view = st.segmented_control("Vignettes", ["original", "corrigé"], default="original", required=True,
                                format_func={"original": "Original", "corrigé": "Corrigé"}.get, key="pages_view")
    screens = max(1, -(-len(project.pages) // CARDS_PER_SCREEN))
    screen = st.number_input(f"Écran (sur {screens})", min_value=1, max_value=screens, value=1, step=1,
                             key="pages_screen", width=160) if screens > 1 else 1

start = (int(screen) - 1) * CARDS_PER_SCREEN
visible = project.pages[start:start + CARDS_PER_SCREEN]
selected_ids: list[str] = []
columns = st.columns(GRID_COLUMNS)
for offset, page in enumerate(visible):
    with columns[offset % GRID_COLUMNS].container(border=True):
        image = None
        if view == "corrigé":
            image = common.corrected_image(project, page, max_side=config.THUMBNAIL_MAX_SIDE)
        if image is None:
            image = project_service.load_thumbnail(project, page)
        if image is not None:
            st.image(image, width="stretch", alt=f"Vignette de la page {project.page_number(page.id)}")
        else:
            st.caption("Vignette indisponible.")
        st.markdown(f"**{common.page_label(project, page)}**")
        st.badge(common.STATUS_LABELS[page.status], color=common.STATUS_COLORS[page.status])
        st.caption(f"Confiance OCR : {common.confidence_label(page)}"
                   + (" · OCR à refaire" if page.ocr_outdated else ""))
        with st.container(horizontal=True):
            if st.checkbox("Choisir", key=f"pick_{page.id}"):
                selected_ids.append(page.id)
            if st.button("Détails", key=f"open_{page.id}", type="tertiary", icon=":material/visibility:"):
                common.select_page(page.id)
                st.rerun()

# --------------------------------------------------------------------------- sélection multiple

if selected_ids:
    with st.container(border=True):
        st.markdown(f"**{len(selected_ids)} page(s) choisie(s)**")
        busy = [page_id for page_id in selected_ids if page_id in locked]
        confirm = st.checkbox("Je confirme la suppression des pages choisies (elles vont dans la corbeille "
                              "du projet, l'original n'est jamais effacé)", key="confirm_delete_selection")
        if st.button("Supprimer les pages choisies", icon=":material/delete:", disabled=not confirm or bool(busy)):
            with common.user_action(f"{len(selected_ids)} page(s) supprimée(s).") as result:
                with common.editing(project.id) as fresh:
                    project_service.delete_pages(fresh, selected_ids)
            if result.ok:
                for page_id in selected_ids:
                    st.session_state.pop(f"pick_{page_id}", None)
                st.rerun()
        if busy:
            st.caption("Certaines pages choisies sont en cours de traitement : suppression impossible pour l'instant.")

# --------------------------------------------------------------------------- ordre

st.subheader("Ordre des pages")
if st.toggle("Réorganiser par glisser-déposer", key="pages_drag_mode"):
    items = []
    for page in project.pages:
        thumb = project_service.load_thumbnail(project, page)
        items.append({"id": page.id, "label": page.original_name,
                      "thumb": common.jpeg_data_uri(thumb, quality=70) if thumb is not None else ""})
    new_order = sortable_pages(items, key=f"sortable_{project.id}")
    if new_order is not None and new_order != [page.id for page in project.pages]:
        with common.user_action("Nouvel ordre enregistré.") as result:
            with common.editing(project.id) as fresh:
                project_service.reorder_pages(fresh, new_order)
        if result.ok:
            st.rerun()

with st.container(horizontal=True, vertical_alignment="bottom"):
    by_id = {page.id: page for page in project.pages}
    moving = st.selectbox("Page à déplacer", list(by_id), key="move_page",
                          format_func=lambda page_id: common.page_label(project, by_id[page_id]))
    target = st.number_input("Nouvelle position", min_value=1, max_value=len(project.pages), step=1,
                             value=project.page_number(moving), key="move_target")
    if st.button("Déplacer", icon=":material/swap_vert:", disabled=int(target) == project.page_number(moving)):
        ordered = [page.id for page in project.pages if page.id != moving]
        ordered.insert(int(target) - 1, moving)
        with common.user_action(f"Page déplacée en position {int(target)}.") as result:
            with common.editing(project.id) as fresh:
                project_service.reorder_pages(fresh, ordered)
        if result.ok:
            st.rerun()

# --------------------------------------------------------------------------- page sélectionnée

st.subheader("Page sélectionnée")
page: Page = common.page_selector(project, key="pages")
page_locked = common.warn_if_locked(project.id, page.id)

paths = project_service.page_paths(project, page)
try:
    original = common.original_image(project, page)
    corrected = common.corrected_image(project, page)
except BookOcrError as error:   # image illisible : message, l'écran reste utilisable
    common.logger.warning("Image illisible pour la page %s : %s", page.id, error)
    st.error("Une image de cette page est illisible ; remplacez la photo ou relancez la correction.")
    original = corrected = None
compare_view([
    Panel("Original", original, "Original illisible."),
    Panel("Corrigé", corrected, "Pas encore corrigée : appliquez une rotation ou lancez le traitement."),
])
st.caption(f"Fichier : {page.original_name} · {page.source_width} × {page.source_height} px · "
           f"statut : {common.STATUS_LABELS[page.status]}" + (f" · erreur : {page.error}" if page.error else ""))

rotation_column, replace_column, delete_column = st.columns(3, border=True)
with rotation_column:
    st.markdown("**Rotation manuelle**")
    st.caption("Quart(s) de tour dans le sens horaire, appliqué(s) après l'orientation automatique.")
    turns = st.segmented_control("Rotation (sens horaire)", list(ROTATION_LABELS), format_func=ROTATION_LABELS.get,
                                 default=page.geometry.manual_quarter_turns, required=True,
                                 key=f"rotation_{page.id}", disabled=page_locked)
    if st.button("Appliquer la rotation", icon=":material/rotate_right:", key="apply_rotation",
                 disabled=page_locked or turns == page.geometry.manual_quarter_turns):
        with common.user_action(f"Rotation de {ROTATION_LABELS[turns]} appliquée.") as result:
            with common.editing(project.id) as fresh:
                project_service.set_manual_rotation(fresh, page.id, int(turns))
            _refresh_geometry(page.id)
        if result.ok:
            st.rerun()

with replace_column:
    st.markdown("**Remplacer la photo**")
    st.caption("Même place dans le livre ; les résultats de cette page seulement sont recalculés.")
    replace_round = st.session_state.setdefault("_replace_round", 0)
    replacement = st.file_uploader("Nouvelle photo", type=UPLOAD_TYPES, key=f"replace_{page.id}_{replace_round}",
                                   disabled=page_locked)
    if st.button("Remplacer", icon=":material/published_with_changes:", disabled=page_locked or replacement is None):
        with common.user_action("Photo remplacée.") as result:
            with common.editing(project.id) as fresh:
                project_service.replace_page_source(fresh, page.id, replacement.name, replacement.getvalue())
        if result.ok:
            st.session_state["_replace_round"] = replace_round + 1
            st.rerun()

with delete_column:
    st.markdown("**Supprimer la page**")
    st.caption("La page va dans la corbeille du projet ; l'original n'est jamais effacé.")
    confirm_one = st.checkbox("Je confirme la suppression", key=f"confirm_delete_{page.id}", disabled=page_locked)
    if st.button("Supprimer cette page", icon=":material/delete:", disabled=page_locked or not confirm_one):
        with common.user_action("Page supprimée.") as result:
            with common.editing(project.id) as fresh:
                project_service.delete_pages(fresh, [page.id])
        if result.ok:
            common.select_page(None)
            st.rerun()

if page.status == PageStatus.IMPORTED and not page_locked:
    if st.button("Calculer la correction de cette page", icon=":material/auto_fix_high:"):
        with common.user_action("Correction calculée.") as result:
            _refresh_geometry(page.id)
        if result.ok:
            st.rerun()
