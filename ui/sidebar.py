"""Sidebar: projects (open, create, rename, delete, zip backup and restore), sections, undo / redo."""
from __future__ import annotations

import streamlit as st

from . import state, widgets


def render() -> str:
    """Draw the sidebar and return the chosen section."""
    with st.sidebar:
        st.title("LivreOCR")
        st.caption("Photos de pages → OCR → PDF reconstruit")
        _projects()
        prj = state.project()
        if prj is None:
            return "Accueil"
        st.divider()
        section = st.radio("Rubrique", state.SECTIONS, key="nav")
        st.divider()
        st.caption("Historique des modifications (pages)")
        widgets.history_bar("side")
        st.divider()
        _project_tools(prj)
    return section


def _projects() -> None:
    with state.guard("Liste des projets"):
        summaries = state.storage().list_projects()
        ids = [s.id for s in summaries]
        names = {s.id: f"{s.name} ({s.pages} p.)" + (f" — {s.error}" if s.error else "") for s in summaries}
        current = st.session_state.project_id
        options = [None] + ids
        index = options.index(current) if current in options else 0
        choice = st.selectbox("Projet", options, index=index, key=state.k("project_select"),
                              format_func=lambda pid: "— choisir un projet —" if pid is None else names[pid])
        if choice != current:
            state.open_project(choice)
            st.rerun()
    with st.expander("Nouveau projet", expanded=st.session_state.project_id is None):
        with st.form("new_project", clear_on_submit=True):
            name = st.text_input("Nom du livre", key="new_project_name", placeholder="Mon livre")
            if st.form_submit_button("Créer le projet", type="primary"):
                with state.guard("Création du projet"):
                    prj = state.project_service().create_project(name or "Nouveau livre")
                    state.open_project(prj.id)
                    state.flash("success", f"Projet « {prj.name} » créé.")
                    st.rerun()
    with st.expander("Restaurer une sauvegarde (.zip)"):
        upload = st.file_uploader("Archive de projet", type=["zip"], key=state.k("zip_upload"))
        if upload is not None and st.button("Importer l'archive", key="zip_import"):
            with state.guard("Import de l'archive"):
                prj = state.storage().import_zip(upload.getvalue())
                state.open_project(prj.id)
                state.flash("success", f"Projet « {prj.name} » restauré ({len(prj.pages)} pages).")
                st.rerun()


def _project_tools(prj) -> None:
    with st.expander("Projet : renommer, sauvegarder, supprimer"):
        new_name = st.text_input("Nom", value=prj.name, key=state.k("rename", prj.id))
        if st.button("Renommer", key="rename_btn", disabled=not new_name.strip() or new_name == prj.name):
            with state.guard("Renommage"):
                prj.name = new_name.strip()
                state.save(prj)
                state.bump()
                st.rerun()
        st.caption("Sauvegarde complète : project.json, originaux, illustrations et exports.")
        include_work = st.checkbox("Inclure les images de travail (plus lourd)", key="zip_work")
        if st.button("Préparer la sauvegarde .zip", key="zip_prepare"):
            with state.guard("Sauvegarde"):
                with st.spinner("Création de l'archive…"):
                    st.session_state["zip_data"] = (prj.id, state.storage().export_zip(prj, include_work))
        zipped = st.session_state.get("zip_data")
        if zipped and zipped[0] == prj.id:
            st.download_button("Télécharger la sauvegarde", zipped[1], file_name=f"{prj.name}.zip",
                               mime="application/zip", key="zip_download")
        if widgets.confirm_button("Supprimer le projet", "delete_project",
                                  f"Supprimer définitivement « {prj.name} » et tous ses fichiers ?"):
            with state.guard("Suppression du projet"):
                state.storage().delete_project(prj.id)
                st.session_state.histories.pop(prj.id, None)
                state.open_project(None)
                state.flash("success", "Projet supprimé.")
                st.rerun()
