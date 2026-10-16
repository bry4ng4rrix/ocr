"""Pages: import, thumbnails grid, order, rotation, deletion, enlarged original."""
from __future__ import annotations

import streamlit as st

import config

from . import state, widgets

COLUMNS = 4


@st.cache_data(max_entries=8, show_spinner=False)
def original_preview(project_id: str, relative: str, source_index: int, max_side: int) -> bytes:
    """Reduced copy of an original (pure function of the stored file: cached by path)."""
    from core import image_loader
    from services.storage_service import ProjectStorage
    rgb = image_loader.read_image(ProjectStorage(project_id).path(relative), source_index)
    return image_loader.to_png_bytes(image_loader.thumbnail(rgb, max_side))


def render(prj) -> None:
    st.header("Pages")
    _import(prj)
    if not prj.pages:
        st.info("Aucune page pour l'instant. Ajoutez des photos (JPG, PNG, TIFF) ou des PDF ci-dessus.")
        return
    top = st.columns([3, 2])
    top[0].write(f"**{len(prj.pages)} page(s)** — l'ordre ci-dessous est celui du livre.")
    if top[1].button("Trier par nom de fichier", key="sort_pages"):
        with state.guard("Tri"):
            state.project_service().sort_pages_by_filename(prj)
            state.save(prj)
            state.bump()
            st.rerun()
    st.caption("La rotation tourne aussi les blocs déjà analysés : leurs positions restent justes sans nouvel OCR.")
    _grid(prj)
    st.divider()
    _enlarged(prj)


def _import(prj) -> None:
    with st.expander("Ajouter des pages", expanded=not prj.pages):
        files = st.file_uploader("Photos ou PDF des pages", accept_multiple_files=True,
                                 type=["jpg", "jpeg", "png", "tif", "tiff", "pdf"], key=state.k("upload"),
                                 help=f"{config.MAX_FILE_MB} Mo au plus par fichier. Un PDF ou un TIFF "
                                      "multipage donne une page par feuille.")
        if files and st.button(f"Importer {len(files)} fichier(s)", type="primary", key="do_import"):
            bar = st.progress(0.0, text="Import…")
            with state.guard("Import"), st.status("Import des fichiers…", expanded=True) as status:
                def progress(done, total, message):
                    bar.progress(min(done / max(total, 1), 1.0), text=message)
                    status.write(message)
                report = state.project_service().import_files(prj, [(f.name, f.getvalue()) for f in files], progress)
                status.update(label="Import terminé", state="complete")
                if report.added:
                    state.flash("success", f"{len(report.added)} page(s) ajoutée(s).")
                if report.skipped:
                    state.flash("info", "Déjà présent(s), ignoré(s) : " + ", ".join(report.skipped))
                for err in report.errors:
                    state.flash("error", err)
                state.bump()        # empties the uploader
                st.rerun()


def _grid(prj) -> None:
    ps = state.project_service()
    for start in range(0, len(prj.pages), COLUMNS):
        cols = st.columns(COLUMNS)
        for col, page in zip(cols, prj.pages[start:start + COLUMNS]):
            index = prj.page_index(page.id)
            with col.container(border=True):
                thumb = state.project_file_image(prj, page.thumbnail_path) if page.thumbnail_path else None
                if thumb is not None:
                    st.image(thumb, width="stretch")
                st.markdown(f"**{index + 1}.** {page.original_filename}")
                st.caption(state.status_text(page))
                b = st.columns(5)
                if b[0].button("↑", key=state.k("up", page.id), help="Monter", disabled=index == 0):
                    ps.move_page(prj, page.id, -1)
                    _saved_rerun(prj)
                if b[1].button("↓", key=state.k("down", page.id), help="Descendre",
                               disabled=index == len(prj.pages) - 1):
                    ps.move_page(prj, page.id, 1)
                    _saved_rerun(prj)
                if b[2].button("↺", key=state.k("rotl", page.id), help="Tourner de 90° à gauche"):
                    _rotate(prj, page, 270)
                if b[3].button("↻", key=state.k("rotr", page.id), help="Tourner de 90° à droite"):
                    _rotate(prj, page, 90)
                if b[4].button("✕", key=state.k("del", page.id), help="Supprimer la page"):
                    st.session_state["delete_page"] = page.id
                    st.rerun()
                if st.session_state.get("delete_page") == page.id:
                    st.warning("Supprimer cette page et ses fichiers dérivés ?")
                    yes, no = st.columns(2)
                    if yes.button("Supprimer", key=state.k("delyes", page.id), type="primary"):
                        with state.guard("Suppression"):
                            ps.delete_page(prj, page.id)
                            st.session_state["delete_page"] = None
                            _saved_rerun(prj)
                    if no.button("Annuler", key=state.k("delno", page.id)):
                        st.session_state["delete_page"] = None
                        st.rerun()


def _saved_rerun(prj) -> None:
    state.save(prj)
    state.bump()
    st.rerun()


def _rotate(prj, page, degrees: int) -> None:
    with state.guard("Rotation"):
        def change():
            state.project_service().rotate_page(prj, page, degrees)
        state.commit(page, f"Rotation de {degrees if degrees != 270 else -90}°", change)
        with st.spinner("Reconstruction du cadre de la page…"):
            _turn_thumbnail(prj, page, degrees)
            state.processing().ensure_frame(prj, page)
            state.save(prj)
        st.rerun()


def _turn_thumbnail(prj, page, degrees: int) -> None:
    """The thumbnail follows the rotation (the stored thumbnail itself is turned)."""
    try:
        import numpy as np
        from core import image_loader
        thumb = state.project_file_image(prj, page.thumbnail_path) if page.thumbnail_path else None
        if thumb is None:
            return
        turned = np.ascontiguousarray(np.rot90(thumb, k=-(degrees // 90)))     # clockwise
        state.storage().ProjectStorage(prj.id).write_bytes(page.thumbnail_path, image_loader.to_png_bytes(turned))
    except Exception:
        state.logger.exception("miniature non mise à jour")


def _enlarged(prj) -> None:
    st.subheader("Aperçu de l'original")
    page = state.page_selector(prj, key="upload_preview_page")
    if page is None:
        return
    size = st.select_slider("Taille", options=[600, 900, 1200, 1600], value=900, key="orig_size")
    with state.guard("Aperçu"):
        data = original_preview(prj.id, page.original_path, page.source_index, size)
        st.image(data, caption=f"Original non modifié — {page.original_width} × {page.original_height} px"
                               + (f", feuille {page.source_index + 1} du fichier" if page.source_index else ""))
    widgets.history_bar("pages")
