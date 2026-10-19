"""Écran D — Traitement OCR : langues, mode de segmentation, étapes, choix des pages, traitement en arrière-plan
avec progression rafraîchie, annulation, erreurs par page et reprise."""
from __future__ import annotations

import streamlit as st

from components import common
from models import Project
from services import ocr_service, processing_service, project_service
from services.processing_service import ALL_STEPS, STEP_LABELS, Step

PSM_LABELS = {
    3: "Automatique (recommandé)",
    1: "Automatique avec détection d'orientation",
    4: "Une seule colonne de texte",
    6: "Un bloc de texte uniforme",
    11: "Texte épars",
}
SCOPE_LABELS = {"todo": "Pages à faire", "all": "Toutes les pages", "selection": "Pages choisies"}
JOB_STATE_LABELS = {"running": "En cours", "done": "Terminé", "failed": "Échoué", "cancelled": "Annulé",
                    "interrupted": "Interrompu"}

project = common.require_project()
common.require_pages(project)
running = processing_service.is_running(project.id)
by_id = {page.id: page for page in project.pages}


def _label(page_id: str) -> str:
    page = by_id.get(page_id)
    return common.page_label(project, page) if page else f"page supprimée ({page_id})"


def _launch(page_ids: list[str], steps: list[Step], redetect: bool) -> None:
    with common.user_action(f"Traitement lancé en arrière-plan pour {len(page_ids)} page(s).") as result:
        processing_service.start_background_job(project.id, page_ids, steps, redetect=redetect)
    if result.ok:
        st.session_state["_job_was_running"] = True
        st.rerun()


# --------------------------------------------------------------------------- réglages

installed = ocr_service.available_languages()
version = ocr_service.tesseract_version()
st.caption(f"Tesseract {version or 'introuvable'} · langues installées : {', '.join(installed) or 'aucune'}")
if not version:
    st.error("Tesseract est introuvable : installez-le (sudo pacman -S tesseract tesseract-data-fra) pour lancer "
             "l'OCR. La correction géométrique reste possible.", icon=":material/error:")
elif "deu" not in installed:
    st.info("L'allemand (deu) n'est pas installé : sudo pacman -S tesseract-data-deu", icon=":material/translate:")

settings_scope = f"ocr_{project.id}"
common.sync_widgets(settings_scope, {"languages": [lang for lang in project.languages if lang in installed],
                                     "psm": project.ocr_psm if project.ocr_psm in PSM_LABELS else 3},
                    f"{project.languages}{project.ocr_psm}")

with st.container(border=True):
    left, right = st.columns(2)
    languages = left.multiselect("Langues du texte", installed, key=f"{settings_scope}_languages",
                                 disabled=running)
    missing = [lang for lang in project.languages if lang not in installed]
    if missing:
        left.warning(f"Langue(s) du projet non installée(s) : {', '.join(missing)}.", icon=":material/warning:")
    psm = right.selectbox("Segmentation de la page", list(PSM_LABELS), format_func=PSM_LABELS.get,
                          key=f"{settings_scope}_psm", disabled=running)
    steps = st.pills("Étapes", list(ALL_STEPS), selection_mode="multi", default=list(ALL_STEPS),
                     format_func=STEP_LABELS.get, key="ocr_steps", disabled=running)
    steps = [step for step in ALL_STEPS if step in (steps or [])]
    scope = st.segmented_control("Pages", list(SCOPE_LABELS), format_func=SCOPE_LABELS.get, default="todo",
                                 required=True, key="ocr_scope", disabled=running)
    if scope == "selection":
        chosen = st.multiselect("Pages à traiter", list(by_id), format_func=_label, key="ocr_selection",
                                disabled=running)
        page_ids = [page.id for page in project.pages if page.id in set(chosen)]
    elif scope == "all":
        page_ids = [page.id for page in project.pages]
    else:
        needed = {page_id for step in steps for page_id in processing_service.pages_needing(project, step)}
        page_ids = [page.id for page in project.pages if page.id in needed]
    redetect = st.checkbox("Refaire les détections automatiques (orientation, contour, inclinaison)",
                           key="ocr_redetect", disabled=running or Step.GEOMETRY not in steps)
    st.caption(f"{len(page_ids)} page(s) seront traitées.")

    needs_ocr = Step.OCR in steps
    blocked_reason = None
    if running:
        blocked_reason = "Un traitement est déjà en cours."
    elif not steps:
        blocked_reason = "Choisissez au moins une étape."
    elif not page_ids:
        blocked_reason = "Aucune page à traiter avec ces choix."
    elif needs_ocr and not languages:
        blocked_reason = "Choisissez au moins une langue d'OCR."
    elif needs_ocr and not version:
        blocked_reason = "Tesseract est introuvable."
    if st.button("Lancer le traitement", type="primary", icon=":material/play_arrow:",
                 disabled=blocked_reason is not None):
        if list(languages) != project.languages or int(psm) != project.ocr_psm:
            with common.user_action() as saved:
                with common.editing(project.id) as fresh:
                    fresh.languages = list(languages)
                    fresh.ocr_psm = int(psm)
                    project_service.save_project(fresh)
            if not saved.ok:
                st.stop()
        _launch(page_ids, steps, bool(redetect))
    if blocked_reason:
        st.caption(blocked_reason)


# --------------------------------------------------------------------------- progression


def _progress_panel(project: Project) -> None:
    status = processing_service.job_status(project.id)
    is_running = processing_service.is_running(project.id)
    if status is None:
        st.caption("Aucun traitement lancé pour ce projet.")
        return
    st.markdown(f"État : **{JOB_STATE_LABELS.get(status.state, status.state)}**"
                + (f" — {status.message}" if status.message else ""))
    st.progress(min(1.0, status.done / max(1, status.total)), text=f"{status.done}/{status.total} page(s)")
    if is_running:
        current = _label(status.current_page_id) if status.current_page_id else "—"
        step = STEP_LABELS.get(status.current_step, status.current_step or "")
        st.caption(f"Page en cours : {current} · étape : {step}")
        if st.button("Annuler le traitement", icon=":material/stop:", key="cancel_job"):
            if processing_service.cancel_job(project.id):
                st.info("Annulation demandée : la page en cours se termine, puis le traitement s'arrête.")
    for page_id, message in status.errors.items():
        st.error(f"{_label(page_id)} : {message}", icon=":material/error:")
    was_running = st.session_state.get("_job_was_running", False)
    st.session_state["_job_was_running"] = is_running
    if was_running and not is_running:
        st.rerun(scope="app")
    if not is_running and (status.state in ("interrupted", "cancelled", "failed") or status.errors):
        remaining = [page_id for page_id in status.page_ids[status.done:] if page_id in by_id]
        remaining += [page_id for page_id in status.errors if page_id in by_id and page_id not in remaining]
        if remaining:
            steps_to_resume = [Step(step) for step in status.steps]
            if st.button(f"Reprendre ({len(remaining)} page(s))", icon=":material/replay:", key="resume_job"):
                _launch([page.id for page in project.pages if page.id in set(remaining)], steps_to_resume, False)


st.subheader("Progression")
with st.container(border=True):
    st.fragment(run_every=1.5 if running else None)(_progress_panel)(project)
