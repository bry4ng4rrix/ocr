"""Streamlit entry point for the local LivreOCR application."""
from __future__ import annotations

import streamlit as st

from ui import (block_editor_page, image_editor_page, overview_page, processing_page, sidebar, state,
                upload_page)


def main() -> None:
    st.set_page_config(page_title="LivreOCR", page_icon="📖", layout="wide")
    state.init()
    state.show_flash()
    section = sidebar.render()
    project = state.project()
    if project is None:
        st.title("LivreOCR")
        st.write("Importez les pages d’un livre, corrigez leur OCR et exportez un PDF reconstruit.")
        st.info("Créez un projet ou choisissez-en un dans la barre latérale pour commencer.")
        return

    if section == "Vue d'ensemble":
        overview_page.render(project)
    elif section == "Pages":
        upload_page.render(project)
    elif section == "Traitement OCR":
        processing_page.render(project)
    elif section == "Éditeur d'image":
        image_editor_page.render(project)
    elif section == "Éditeur de blocs":
        block_editor_page.render(project)
    elif section == "Export PDF":
        from ui import export_page
        export_page.render(project)


if __name__ == "__main__":
    main()
