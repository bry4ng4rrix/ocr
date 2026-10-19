"""Écran G — Contrôle : comparaison original | corrigé | reconstruit, confiance par page, blocs à vérifier dans
tout le livre (lien vers l'éditeur), pages dont l'OCR est à refaire, restauration d'une version précédente."""
from __future__ import annotations

import streamlit as st

import config
from components import common
from components.compare_view import Panel, compare_view
from models import Page, TextBlock
from models.errors import BookOcrError
from services import processing_service, project_service
from services.processing_service import Step

project = common.require_project()
common.require_pages(project)
running = processing_service.is_running(project.id)
low = config.LOW_CONFIDENCE
by_id = {page.id: page for page in project.pages}


def _needs_check(block: TextBlock) -> bool:
    """Signalé par l'analyse, ou de confiance faible et pas encore revu par l'utilisateur."""
    return not block.hidden and (block.needs_review or (0 <= block.confidence < low and not block.user_modified))


# --------------------------------------------------------------------------- tableau des pages

st.subheader("Confiance par page")
st.dataframe(
    [{"N°": project.page_number(page.id), "Page": page.original_name, "Statut": common.STATUS_LABELS[page.status],
      "Confiance": page.mean_confidence or 0.0,
      "Blocs à vérifier": sum(_needs_check(block) for block in page.text_blocks),
      "OCR à refaire": "oui" if page.ocr_outdated else "", "Erreur": page.error or ""}
     for page in project.pages],
    hide_index=True,
    column_config={"Confiance": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f %%")},
    alt="Confiance OCR et état de chaque page",
)

# --------------------------------------------------------------------------- comparaison

st.subheader("Comparaison")
page: Page = common.page_selector(project, key="review")
try:
    original = common.original_image(project, page)
    corrected = common.corrected_image(project, page)
except BookOcrError as error:
    st.error(error.user_message)
    original = corrected = None
reconstructed, remarks = None, []
if corrected is not None:
    try:
        with st.spinner("Composition de l'aperçu…"):
            reconstructed, remarks = common.preview_image(project, page)
    except BookOcrError as error:
        st.error(error.user_message)
compare_view([
    Panel("Original", original, "Original illisible."),
    Panel("Corrigé", corrected, "Pas encore corrigée."),
    Panel(f"Reconstruit ({common.MODE_SHORT_LABELS[project.export.mode]})", reconstructed,
          "Pas encore reconstruite : lancez le traitement."),
])
st.caption(f"Confiance OCR : {common.confidence_label(page)} · catégorie : "
           f"{common.CATEGORY_LABELS[page.effective_category]}")
if remarks:
    with st.expander(f"Remarques sur la reconstruction ({len(remarks)})", icon=":material/info:"):
        common.show_issues(remarks)
common.show_issues(page.issues)

# --------------------------------------------------------------------------- blocs à vérifier

st.subheader("Blocs à vérifier dans tout le livre")
to_check = [(page_item, block) for page_item in project.pages for block in page_item.text_blocks
            if _needs_check(block)]
if not to_check:
    st.caption("Aucun bloc à vérifier.")
else:
    st.dataframe(
        [{"Page": project.page_number(page_item.id), "Type": common.BLOCK_KIND_LABELS[block.kind],
          "Confiance": f"{block.confidence:.0f} %" if block.confidence >= 0 else "—",
          "Raison": "signalé par l'analyse" if block.needs_review else "confiance faible", "Texte": block.text[:80]}
         for page_item, block in to_check],
        hide_index=True, alt="Blocs à vérifier",
    )
    choices = {f"{page_item.id}:{block.id}": (page_item, block) for page_item, block in to_check}
    with st.container(horizontal=True, vertical_alignment="bottom"):
        choice = st.selectbox("Bloc", list(choices), key="review_block_choice",
                              format_func=lambda value: f"p. {project.page_number(choices[value][0].id)} — "
                                                        f"{common.BLOCK_KIND_LABELS[choices[value][1].kind]} : "
                                                        f"{choices[value][1].text[:60]}")
        page_item, block = choices[choice]
        if st.button("Ouvrir dans l'éditeur", type="primary", icon=":material/edit_document:"):
            common.select_page(page_item.id, block.id)
            st.switch_page(common.SCREEN_EDITOR)
        if st.button("Marquer comme vérifié", icon=":material/task_alt:",
                     disabled=page_item.id in common.locked_page_ids(project.id)):
            updated = page_item.model_copy(deep=True)
            target = updated.text_block(block.id)
            target.needs_review = False
            target.user_modified = True
            with common.user_action("Bloc marqué comme vérifié.") as result:
                common.save_page(project.id, updated, "Bloc vérifié")
            if result.ok:
                st.rerun()

# --------------------------------------------------------------------------- OCR à refaire

st.subheader("Pages dont l'OCR est à refaire")
outdated = [page_item for page_item in project.pages if page_item.ocr_outdated]
if not outdated:
    st.caption("Aucune : l'OCR de chaque page correspond à sa correction géométrique actuelle.")
else:
    st.warning("La correction géométrique de ces pages a changé après l'OCR : " + ", ".join(
        common.page_label(project, page_item) for page_item in outdated), icon=":material/sync_problem:")
    if st.button(f"Relancer l'OCR et la mise en page ({len(outdated)} page(s))", icon=":material/replay:",
                 disabled=running):
        with common.user_action("Traitement lancé en arrière-plan.") as result:
            processing_service.start_background_job(project.id, [page_item.id for page_item in outdated],
                                                    [Step.OCR, Step.LAYOUT])
        if result.ok:
            st.session_state["_job_was_running"] = True
            st.rerun()

# --------------------------------------------------------------------------- versions

st.subheader(f"Versions précédentes — {common.page_label(project, page)}")
versions = project_service.list_page_versions(project, page.id)
if not versions:
    st.caption("Aucune version enregistrée pour cette page.")
else:
    labels = {version.version_id: f"{version.created_at.astimezone():%d/%m/%Y %H:%M:%S} — {version.reason}"
              for version in versions}
    with st.container(horizontal=True, vertical_alignment="bottom"):
        version_id = st.selectbox("Version", list(labels), format_func=labels.get, key=f"version_{page.id}")
        confirm = st.checkbox("Confirmer la restauration", key=f"confirm_restore_{page.id}",
                              help="L'état actuel est d'abord gardé comme version « Avant restauration ».")
        if st.button("Restaurer cette version", icon=":material/history:",
                     disabled=not confirm or page.id in common.locked_page_ids(project.id)):
            with common.user_action("Version restaurée.") as result:
                with common.editing(project.id) as fresh:
                    project_service.restore_page_version(fresh, page.id, version_id)
            if result.ok:
                st.rerun()
