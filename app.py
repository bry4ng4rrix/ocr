"""Point d'entrée Streamlit : navigation entre les écrans, projet ouvert, suivi du traitement en cours.

Lancement : `.venv/bin/streamlit run app.py`
"""
from __future__ import annotations

import streamlit as st

import config
from components import common
from services import processing_service
from services.processing_service import STEP_LABELS

config.setup_logging()
st.set_page_config(page_title="Reconstruction de livres", page_icon=":material/menu_book:", layout="wide")
common.init_state()

page = st.navigation(
    {
        "Projet": [
            st.Page(common.SCREEN_DASHBOARD, title="Tableau de bord", icon=":material/dashboard:",
                    url_path="tableau-de-bord", default=True),
            st.Page(common.SCREEN_PAGES, title="Pages", icon=":material/photo_library:", url_path="pages"),
        ],
        "Traitement": [
            st.Page(common.SCREEN_SMART_SCAN, title="Numérisation intelligente", icon=":material/auto_awesome:",
                    url_path="numerisation-intelligente"),
            st.Page(common.SCREEN_PDF_SCAN, title="Numérisation et amélioration PDF",
                    icon=":material/document_scanner:", url_path="numerisation-pdf"),
            st.Page(common.SCREEN_CORRECTION, title="Correction d'image", icon=":material/crop_rotate:",
                    url_path="correction"),
            st.Page(common.SCREEN_PROCESSING, title="Traitement OCR", icon=":material/document_scanner:",
                    url_path="traitement"),
            st.Page(common.SCREEN_LIVE_PREVIEW, title="Aperçu en direct", icon=":material/visibility:",
                    url_path="apercu-live"),
        ],
        "Mise en page": [
            st.Page(common.SCREEN_EDITOR, title="Éditeur de page", icon=":material/edit_document:",
                    url_path="editeur"),
            st.Page(common.SCREEN_TYPOGRAPHY, title="Typographie", icon=":material/format_size:",
                    url_path="typographie"),
            st.Page(common.SCREEN_REVIEW, title="Contrôle", icon=":material/fact_check:", url_path="controle"),
        ],
        "Livre": [
            st.Page(common.SCREEN_EXPORT, title="Export", icon=":material/download:", url_path="export"),
        ],
    },
    position="sidebar",
    expanded=True,
)


def _job_monitor(project_id: str) -> None:
    """Avancement du traitement en arrière-plan ; quand il se termine, tout l'écran est rafraîchi."""
    running = processing_service.is_running(project_id)
    status = processing_service.job_status(project_id)
    if running and status is not None:
        step = STEP_LABELS.get(status.current_step, status.current_step) if status.current_step else ""
        st.progress(min(1.0, status.done / max(1, status.total)),
                    text=f"Traitement : {status.done}/{status.total} page(s)" + (f" — {step}" if step else ""))
    was_running = st.session_state.get("_job_was_running", False)
    st.session_state["_job_was_running"] = running
    if was_running and not running:
        st.rerun(scope="app")


with st.sidebar:
    project = common.load_project()
    if project is None:
        st.caption("Aucun projet ouvert.")
    else:
        st.markdown(f"**{project.name}**")
        st.caption(f"{len(project.pages)} page(s)")
        running = processing_service.is_running(project.id)
        st.fragment(run_every=2 if running else None)(_job_monitor)(project.id)

st.title(page.title)
common.show_flashes()
page.run()
