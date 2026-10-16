"""Pages: import, thumbnails grid, order, rotation, deletion, enlarged original."""
from __future__ import annotations

import numpy as np
import streamlit as st
from PIL import Image, ImageDraw

import config
from core.document_models import BLOCK_LABELS, GeometrySettings, ImageBlock, Page, PageStatus, TextBlock

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
    _extraction_preview(prj)
    st.divider()
    _enlarged(prj)


def _import(prj) -> None:
    with st.expander("Ajouter des pages", expanded=not prj.pages):
        files = st.file_uploader("Photos ou PDF des pages", accept_multiple_files=True,
                                 type=["jpg", "jpeg", "png", "tif", "tiff", "pdf"], key=state.k("upload"),
                                 help=f"{config.MAX_FILE_MB} Mo au plus par fichier. Un PDF ou un TIFF "
                                      "multipage donne une page par feuille.")
        if files:
            _upload_preview(prj, files)
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


def _upload_preview(prj, files) -> None:
    from core import image_loader, image_preprocessing

    index = st.selectbox(
        "Aperçu du fichier à ajouter",
        range(len(files)),
        format_func=lambda i: files[i].name,
        key=state.k("upload_preview_file"),
    )
    uploaded = files[index]
    data = uploaded.getvalue()
    with state.guard("Lecture de l’aperçu du fichier"):
        inspected = image_loader.inspect_file(data, uploaded.name)
        source_index = st.selectbox(
            "Page ou image du fichier",
            range(len(inspected.items)),
            format_func=lambda i: f"{i + 1} / {len(inspected.items)}",
            key=state.k("upload_preview_source", index),
            disabled=len(inspected.items) == 1,
        )
        if inspected.kind == "pdf":
            original = image_loader.render_pdf_page(data, source_index)
        else:
            original = image_loader.decode_image(data, source_index)

        geometry = GeometrySettings(
            auto_orientation=prj.processing.auto_orientation,
            auto_crop=prj.processing.auto_crop,
            auto_deskew=prj.processing.auto_deskew,
        )
        framed = image_preprocessing.build_frame(original, geometry, detect=True)
        left, right = st.columns(2)
        with left:
            st.image(image_loader.thumbnail(original, config.PREVIEW_MAX_SIDE),
                     caption="Image ajoutée — originale", width="stretch")
        with right:
            st.image(image_loader.thumbnail(framed.image, config.PREVIEW_MAX_SIDE),
                     caption="Aperçu OCR après préparation — l’original n’est pas modifié",
                     width="stretch")
        for warning in framed.warnings:
            st.warning(warning)


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


def _annotated_frame(frame: np.ndarray, page: Page) -> np.ndarray:
    image = Image.fromarray(frame).convert("RGB")
    draw = ImageDraw.Draw(image)
    line_width = max(2, round(min(image.size) / 500))
    for block in page.ordered_blocks():
        if block.hidden:
            colour = (120, 120, 120)
        elif block.needs_review:
            colour = (230, 126, 0)
        elif isinstance(block, ImageBlock):
            colour = (46, 139, 87)
        else:
            colour = (31, 111, 139)
        x0 = round(block.x * image.width)
        y0 = round(block.y * image.height)
        x1 = round((block.x + block.width) * image.width)
        y1 = round((block.y + block.height) * image.height)
        draw.rectangle((x0, y0, x1, y1), outline=colour, width=line_width)
        label = str(block.reading_order + 1)
        bounds = draw.textbbox((0, 0), label)
        label_width = bounds[2] - bounds[0]
        label_height = bounds[3] - bounds[1]
        tag_bottom = min(image.height, y0 + label_height + 5)
        draw.rectangle((x0, y0, x0 + label_width + 6, tag_bottom), fill=colour)
        draw.text((x0 + 3, y0 + 1), label, fill=(255, 255, 255))
    return np.asarray(image)


def _extraction_preview(prj) -> None:
    processed = [page for page in prj.pages if page.status == PageStatus.PROCESSED]
    st.subheader("Aperçu des résultats d’extraction")
    if not processed:
        st.info("Les pages traitées par OCR apparaîtront ici avec leurs blocs de texte et illustrations détectés.")
        return

    page_ids = [page.id for page in processed]
    preview_key = state.k("extraction_preview_page")
    current = st.session_state.get(preview_key)
    selected_id = current if current in page_ids else page_ids[0]
    selected_id = st.selectbox(
        "Page traitée",
        page_ids,
        index=page_ids.index(selected_id),
        format_func=lambda page_id: state.page_label(prj, prj.page(page_id)),
        key=preview_key,
    )
    page = prj.page(selected_id)
    image_column, result_column = st.columns(2)
    from services import export_service

    with image_column:
        st.caption("Image ajoutée")
        with state.guard("Affichage de l’image originale"):
            st.image(original_preview(prj.id, page.original_path, page.source_index, 1200),
                     caption=f"Original — {page.original_filename}", width="stretch")
    with result_column:
        st.caption("Résultat reconstruit à partir des blocs OCR — sans la photo de la page")
        with state.guard("Aperçu du résultat reconstruit"):
            preview, report = export_service.preview_png(prj, page, mode="reconstruction", width_px=900)
            st.image(preview, caption="Page reconstruite en PDF — texte OCR et illustrations extraites",
                     width="stretch")
            if not page.text_blocks:
                st.warning("Aucun texte OCR n’a été reconnu sur cette page. Relancez l’OCR avant d’utiliser "
                           "ce résultat dans le livre.")
            for warning in report.warnings:
                st.warning(warning)
            for overflow in report.overflows:
                st.error(overflow)

    text_blocks = page.text_blocks
    image_blocks = page.image_blocks
    with st.expander("Détails de l’extraction", expanded=False):
        with state.guard("Affichage des zones extraites"):
            frame = state.small_frame(prj, page)
            st.image(_annotated_frame(frame, page),
                     caption="Zones extraites — bleu : texte, vert : illustration, orange : à vérifier",
                     width="stretch")
        details_column, summary_column = st.columns([3, 2])
        with summary_column:
            metrics = st.columns(2)
            metrics[0].metric("Blocs de texte", len(text_blocks))
            metrics[1].metric("Illustrations", len(image_blocks))
            if page.ocr_mean_confidence >= 0:
                st.metric("Confiance OCR moyenne", f"{page.ocr_mean_confidence:.0f} %")
            if page.ocr_languages:
                st.caption("Langues OCR : " + ", ".join(page.ocr_languages))
            if page.ocr_preparation:
                st.caption("Préparation OCR : " + page.ocr_preparation)

        with details_column:
            if not page.blocks:
                st.info("Page traitée, mais aucune zone d’extraction n’a été trouvée.")
        for block in page.ordered_blocks():
            label = BLOCK_LABELS.get(block.type, block.type.value)
            review = " · à vérifier" if block.needs_review else ""
            hidden = " · masqué" if block.hidden else ""
            title = f"{block.reading_order + 1}. {label}{review}{hidden}"
            if isinstance(block, TextBlock):
                with details_column.expander(title):
                    st.text(block.text or "(aucun texte reconnu)")
                    if block.confidence >= 0:
                        st.caption(f"Confiance : {block.confidence:.0f} %")
                    if block.review_reason:
                        st.warning(block.review_reason)
            else:
                with details_column.expander(title):
                    st.caption(f"Type détecté : {block.detection or 'illustration'}")
                    if block.confidence >= 0:
                        st.caption(f"Confiance : {block.confidence:.0f} %")
                    if block.review_reason:
                        st.warning(block.review_reason)
                    with state.guard("Aperçu de l’illustration extraite"):
                        storage = state.storage().ProjectStorage(prj.id)
                        if storage.exists(block.image_path):
                            st.image(storage.path(block.image_path), caption="Illustration extraite",
                                     width="stretch")
                        else:
                            st.warning("Fichier de l’illustration extraite introuvable.")


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
