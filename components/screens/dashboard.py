"""Écran A — Tableau de bord : créer ou ouvrir un projet, état du livre, sauvegarde."""
from __future__ import annotations

import streamlit as st

from components import common
from models import PageStatus, Severity
from services import ocr_service, processing_service, project_service

JOB_STATE_LABELS = {"running": "En cours", "done": "Terminé", "failed": "Échoué", "cancelled": "Annulé",
                    "interrupted": "Interrompu"}

project = common.load_project()

# --------------------------------------------------------------------------- projet ouvert

if project is not None:
    pages = project.pages
    analysed = sum(page.status == PageStatus.ANALYSED for page in pages)
    corrected = sum(page.status != PageStatus.IMPORTED and page.status != PageStatus.ERROR for page in pages)
    in_error = [page for page in pages if page.status == PageStatus.ERROR]
    to_review = sum(1 for page in pages for block in page.text_blocks if block.needs_review and not block.hidden)
    outdated = sum(page.ocr_outdated for page in pages)

    st.subheader(project.name)
    st.caption(f"Langues OCR : {', '.join(project.languages) or 'aucune'} · dernière modification : "
               f"{project.updated_at.astimezone():%d/%m/%Y %H:%M}")
    with st.container(horizontal=True):
        st.metric("Pages", len(pages), border=True)
        st.metric("Pages traitées", corrected, border=True, help="Correction géométrique faite (au moins).")
        st.metric("Pages analysées", analysed, border=True, help="OCR et mise en page terminés.")
        st.metric("Pages en erreur", len(in_error), border=True)
        st.metric("Blocs à vérifier", to_review, border=True)

    status = processing_service.job_status(project.id)
    with st.container(border=True):
        st.markdown("**Dernier traitement**")
        if status is None:
            st.caption("Aucun traitement lancé pour ce projet.")
        else:
            label = JOB_STATE_LABELS.get(status.state, status.state)
            st.markdown(f"État : **{label}** — {status.done}/{status.total} page(s)"
                        + (f" · {status.message}" if status.message else ""))
            if status.errors:
                st.warning(f"{len(status.errors)} page(s) en erreur pendant ce traitement : voir ci-dessous.",
                           icon=":material/warning:")
            if status.state == "interrupted":
                st.page_link(common.SCREEN_PROCESSING, label="Reprendre le traitement", icon=":material/replay:")
        if outdated:
            st.warning(f"{outdated} page(s) ont une correction géométrique modifiée après l'OCR : relancez l'OCR "
                       "et la mise en page pour ces pages (écran Contrôle).", icon=":material/sync_problem:")

    errors_tab, warnings_tab = st.columns(2)
    with errors_tab:
        st.markdown("**Erreurs**")
        if not in_error:
            st.caption("Aucune page en erreur.")
        for page in in_error:
            st.error(f"{common.page_label(project, page)} : {page.error or 'erreur inconnue'}",
                     icon=":material/error:")
    with warnings_tab:
        st.markdown("**Avertissements**")
        warnings = [(page, issue) for page in pages for issue in page.issues if issue.severity != Severity.INFO]
        if not warnings:
            st.caption("Aucun avertissement.")
        for page, issue in warnings[:50]:
            st.warning(f"{common.page_label(project, page)} : {issue.message}", icon=":material/warning:")
        if len(warnings) > 50:
            st.caption(f"… et {len(warnings) - 50} autre(s) avertissement(s) (voir l'écran Contrôle).")

    with st.container(border=True):
        st.markdown("**Enregistrement**")
        st.caption("Chaque action (import, rotation, réglage appliqué, bloc enregistré) est enregistrée aussitôt. "
                   "Dans l'éditeur, les modifications en cours sont enregistrées par le bouton « Enregistrer » "
                   "ou automatiquement après le délai ci-dessous.")
        with st.container(horizontal=True, vertical_alignment="bottom"):
            delay = st.number_input("Sauvegarde automatique (secondes, 0 = désactivée)", min_value=0,
                                    max_value=3600, step=5, value=int(project.autosave_seconds),
                                    key=f"autosave_{project.id}")
            if st.button("Appliquer le délai", icon=":material/timer:", disabled=delay == project.autosave_seconds):
                with common.user_action("Délai de sauvegarde automatique enregistré.") as result:
                    with common.editing(project.id) as fresh:
                        fresh.autosave_seconds = int(delay)
                        project_service.save_project(fresh)
                if result.ok:
                    st.rerun()
            if st.button("Enregistrer le projet", type="primary", icon=":material/save:"):
                with common.user_action("Projet enregistré (la version précédente est gardée en .bak).") as result:
                    with common.editing(project.id) as fresh:
                        project_service.save_project(fresh)
                if result.ok:
                    st.rerun()
            if st.button("Fermer le projet", icon=":material/close:"):
                st.session_state["project_id"] = None
                common.select_page(None)
                st.rerun()
    st.divider()

# --------------------------------------------------------------------------- créer / ouvrir

create_column, open_column = st.columns(2)

with create_column:
    st.subheader("Nouveau projet")
    installed = ocr_service.available_languages()
    if not installed:
        st.warning("Tesseract est introuvable : l'OCR sera impossible tant qu'il n'est pas installé "
                   "(sudo pacman -S tesseract tesseract-data-fra).", icon=":material/warning:")
    elif "deu" not in installed:
        st.info("L'allemand (deu) n'est pas installé pour l'OCR. Pour l'ajouter : "
                "sudo pacman -S tesseract-data-deu", icon=":material/translate:")
    defaults = [lang for lang in ("fra", "eng") if lang in installed]
    with st.form("create_project", border=True):
        name = st.text_input("Nom du livre", placeholder="Par exemple : Histoire de la vallée")
        languages = st.multiselect("Langues du texte (OCR)", installed, default=defaults,
                                   help="Choisissez toutes les langues présentes dans le livre.")
        submitted = st.form_submit_button("Créer le projet", type="primary", icon=":material/add:")
    if submitted:
        if not name.strip():
            st.error("Donnez un nom au projet.")
        elif not languages:
            st.error("Choisissez au moins une langue d'OCR.")
        else:
            with common.user_action(f"Projet « {name.strip()} » créé.") as result:
                created = project_service.create_project(name.strip(), languages)
            if result.ok:
                st.session_state["project_id"] = created.id
                common.select_page(None)
                st.switch_page(common.SCREEN_PAGES)

with open_column:
    st.subheader("Projets existants")
    summaries = project_service.list_projects()
    if not summaries:
        st.caption("Aucun projet pour l'instant.")
    else:
        st.dataframe(
            [{"Nom": summary.name, "Pages": summary.page_count, "Analysées": summary.analysed_count,
              "En erreur": summary.error_count, "Modifié le": summary.updated_at.astimezone()}
             for summary in summaries],
            hide_index=True,
            column_config={"Modifié le": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm")},
            alt="Liste des projets",
        )
        by_id = {summary.id: summary for summary in summaries}
        with st.container(horizontal=True, vertical_alignment="bottom"):
            chosen = st.selectbox("Projet", list(by_id), format_func=lambda project_id: by_id[project_id].name,
                                  key="open_project_choice")
            if st.button("Ouvrir", icon=":material/folder_open:",
                         disabled=project is not None and chosen == project.id):
                st.session_state["project_id"] = chosen
                common.select_page(None)
                common.flash("success", f"Projet « {by_id[chosen].name} » ouvert.")
                st.rerun()
