"""Edit text and illustration blocks on a processed page."""
from __future__ import annotations

import streamlit as st

from core.document_models import (Alignment, BLOCK_LABELS, BlockType, Box, ImageBlock, PageStatus, TEXT_TYPES,
                                  TextBlock)

from . import box_editor, image_editor_page, state, widgets

ALIGNMENT_LABELS = {
    Alignment.LEFT: "À gauche",
    Alignment.CENTER: "Centré",
    Alignment.RIGHT: "À droite",
    Alignment.JUSTIFY: "Justifié",
}


def render(prj) -> None:
    st.header("Éditeur de blocs")
    if not prj.pages:
        st.info("Importez d’abord des pages (rubrique « Pages »).")
        return

    top = st.columns([3, 2])
    with top[0]:
        page = state.page_selector(prj, key="blocks_page")
    with top[1]:
        widgets.history_bar("blocks")
    if page.ocr_outdated:
        st.warning("Le cadre a changé depuis l’OCR : les coordonnées des blocs peuvent être obsolètes.")
    if page.status != PageStatus.PROCESSED:
        st.info("Cette page n’a pas encore été traitée par OCR. Vous pouvez tout de même créer et modifier des blocs.")

    if not state.frame_ready(prj, page):
        st.info("Préparez d’abord le cadre de la page pour afficher et positionner les blocs.")
        if st.button("Préparer l’image de la page", type="primary", key=state.k("blocks_prepare", page.id)):
            with state.guard("Préparation du cadre"):
                for warning in state.prepare_frame(prj, page):
                    state.flash("warning", warning)
                st.rerun()
        return

    blocks = page.ordered_blocks()
    ids = [block.id for block in blocks]
    if st.session_state.block_id not in ids:
        st.session_state.block_id = ids[0] if ids else None

    actions = st.columns(3)
    if actions[0].button("Ajouter un bloc texte", key=state.k("add_text", page.id)):
        with state.guard("Ajout de bloc"):
            block = state.commit(page, "Bloc texte ajouté",
                                 lambda: state.project_service().add_text_block(page))
            st.session_state.block_id = block.id
            st.rerun()
    with actions[1]:
        upload = st.file_uploader("Ajouter une illustration", type=["png", "jpg", "jpeg", "tif", "tiff"],
                                  key=state.k("add_illustration", page.id))
        if upload is not None and st.button("Importer l’illustration", key=state.k("upload_illustration", page.id)):
            with state.guard("Ajout d’illustration"):
                block = state.commit(
                    page, "Illustration ajoutée",
                    lambda: state.project_service().add_image_block(prj, page, upload.getvalue(), upload.name),
                )
                st.session_state.block_id = block.id
                st.rerun()

    width = st.slider("Largeur de l’éditeur (px)", 400, 1400, 900, 50, key="block_editor_width")
    with state.guard("Affichage du cadre"):
        frame = state.small_frame(prj, page)
        event = box_editor.box_editor(frame, box_editor.block_items(page, st.session_state.block_id),
                                      key=f"blocks_{page.id}", selected=st.session_state.block_id, width=width,
                                      draw_label="Dessiner un bloc texte")
    if box_editor.is_new(event, st.session_state.box_nonces):
        result = box_editor.apply_event(prj, page, event, state.history())
        st.session_state.block_id = result["selected"]
        if result["changed"]:
            state.save(prj)
            state.bump()
        st.rerun()

    if not page.blocks:
        st.info("Aucun bloc sur cette page. Dessinez une zone de texte, importez une illustration ou ajoutez un bloc.")
        return
    ids = [block.id for block in page.ordered_blocks()]
    selected = st.selectbox(
        "Bloc sélectionné",
        ids,
        index=ids.index(st.session_state.block_id),
        format_func=lambda block_id: _block_label(page.block(block_id)),
        key=state.k("selected_block", page.id),
    )
    if selected != st.session_state.block_id:
        st.session_state.block_id = selected
        st.rerun()
    block = page.block(selected)
    _position_editor(prj, page, block)
    if isinstance(block, TextBlock):
        _text_editor(prj, page, block)
    else:
        _image_editor(prj, page, block)
    _order_and_visibility(prj, page, block)
    _live_rebuild_preview(prj, page)


def _block_label(block) -> str:
    kind = "Texte" if isinstance(block, TextBlock) else "Image"
    text = block.text.replace("\n", " ")[:45] if isinstance(block, TextBlock) else block.detection
    status = " — à vérifier" if block.needs_review else ""
    status += " — masqué" if block.hidden else ""
    return f"{block.reading_order + 1}. {kind} · {BLOCK_LABELS[block.type]}{status}" + (f" : {text}" if text else "")


def _position_editor(prj, page, block) -> None:
    with st.form(f"block_box_{page.id}_{block.id}"):
        st.subheader("Position et taille")
        cols = st.columns(4)
        x = cols[0].number_input("x %", 0.0, 100.0, block.x * 100, 0.5)
        y = cols[1].number_input("y %", 0.0, 100.0, block.y * 100, 0.5)
        width = cols[2].number_input("Largeur %", 0.1, 100.0, block.width * 100, 0.5)
        height = cols[3].number_input("Hauteur %", 0.1, 100.0, block.height * 100, 0.5)
        if st.form_submit_button("Enregistrer la position"):
            box = Box.clamped(x / 100, y / 100, width / 100, height / 100)
            with state.guard("Modification de la position"):
                warnings = state.commit(
                    page, "Position du bloc modifiée",
                    lambda: state.project_service().set_block_box(block, box.x, box.y, box.width, box.height),
                )
                for warning in warnings:
                    state.flash("warning", warning)
                st.rerun()


def _text_editor(prj, page, block: TextBlock) -> None:
    with st.form(f"block_text_{page.id}_{block.id}"):
        st.subheader("Texte et style")
        text = st.text_area("Texte imprimé", value=block.text, height=180)
        st.caption(f"Texte OCR d’origine : {block.ocr_text or '—'}")
        columns = st.columns(3)
        types = [block_type for block_type in BlockType if block_type in TEXT_TYPES]
        block_type = columns[0].selectbox("Type", types, index=types.index(block.type),
                                         format_func=BLOCK_LABELS.get)
        alignment = columns[1].selectbox("Alignement", list(Alignment), index=list(Alignment).index(block.alignment),
                                         format_func=ALIGNMENT_LABELS.get)
        font_size = columns[2].number_input("Taille (pt)", 1.0, 300.0, float(block.font_size), 0.5)
        columns = st.columns(3)
        font_family = columns[0].text_input("Famille de police", value=block.font_family)
        font_weight = columns[1].selectbox("Graisse", ["normal", "bold"],
                                           index=["normal", "bold"].index(block.font_weight))
        color = columns[2].color_picker("Couleur", value=block.color)
        columns = st.columns(3)
        italic = columns[0].checkbox("Italique", value=block.italic)
        underline = columns[1].checkbox("Souligné", value=block.underline)
        keep_line_breaks = columns[2].checkbox("Conserver les retours à la ligne", value=block.keep_line_breaks)
        line_spacing = st.slider("Interligne", 0.5, 4.0, float(block.line_spacing), 0.1)
        if st.form_submit_button("Enregistrer le texte et le style", type="primary"):
            with state.guard("Modification du bloc texte"):
                state.commit(page, "Texte ou style modifié", lambda: _update_text(
                    block, text, block_type, alignment, font_size, font_family, font_weight, color, italic,
                    underline, keep_line_breaks, line_spacing))
                st.rerun()
    _text_zone_actions(prj, page, block)


def _text_zone_actions(prj, page, block: TextBlock) -> None:
    st.subheader("Actions OCR de la zone")
    ordered = page.ordered_blocks()
    index = ordered.index(block)
    next_block = ordered[index + 1] if index + 1 < len(ordered) else None
    columns = st.columns(3)
    if columns[0].button("Relire cette zone", key=state.k("reocr_block", block.id),
                         disabled=page.ocr_outdated):
        with state.guard("Relecture OCR de la zone"):
            from core import ocr_engine
            from services import processing_service

            languages = page.ocr_languages or prj.processing.languages
            ocr_engine.check_languages(languages)
            frame = processing_service.ensure_frame(prj, page)
            mode = processing_service.resolve_mode(page, frame)
            gray = processing_service.ocr_image(prj, page, frame, mode)
            text, confidence = ocr_engine.ocr_region(gray, block.box, languages)
            was_corrected = block.is_corrected

            def update_ocr():
                block.ocr_text = text
                block.confidence = confidence
                if not was_corrected:
                    block.text = text

            state.commit(page, "Zone OCR relue", update_ocr)
            st.session_state.block_id = block.id
            st.rerun()
    if columns[1].button("Restaurer le texte OCR", key=state.k("restore_ocr", block.id),
                         disabled=not block.is_corrected):
        with state.guard("Restauration du texte OCR"):
            state.commit(page, "Texte OCR restauré",
                         lambda: state.project_service().restore_ocr_text(block))
            st.session_state.block_id = block.id
            st.rerun()
    if columns[2].button("Fusionner avec la suivante", key=state.k("merge_block", block.id),
                         disabled=not isinstance(next_block, TextBlock)):
        with state.guard("Fusion des zones OCR"):
            state.commit(page, "Zones OCR fusionnées",
                         lambda: state.project_service().merge_text_blocks(page, block.id))
            st.session_state.block_id = block.id
            st.rerun()

    text_lines = block.text.split("\n")
    if len(text_lines) > 1:
        cut_after = st.selectbox("Couper après la ligne", range(1, len(text_lines)),
                                 format_func=lambda line: str(line), key=state.k("split_line", block.id))
        if st.button("Séparer cette zone", key=state.k("split_block", block.id)):
            with state.guard("Séparation de la zone OCR"):
                second = state.commit(page, "Zone OCR séparée",
                                      lambda: state.project_service().split_text_block(page, block, cut_after))
                st.session_state.block_id = second.id
                st.rerun()
    if page.ocr_outdated:
        st.caption("Relancez d’abord l’OCR : le cadre a changé et les coordonnées de cette zone sont obsolètes.")


def _update_text(block, text, block_type, alignment, font_size, font_family, font_weight, color,
                 italic, underline, keep_line_breaks, line_spacing) -> None:
    block.text = text
    block.type = block_type
    block.alignment = alignment
    block.font_size = font_size
    block.font_family = font_family.strip() or "Serif"
    block.font_weight = font_weight
    block.color = color
    block.italic = italic
    block.underline = underline
    block.keep_line_breaks = keep_line_breaks
    block.line_spacing = line_spacing
    block.manual = True
    block.needs_review = False
    block.review_reason = ""


def _image_editor(prj, page, block: ImageBlock) -> None:
    st.subheader("Illustration")
    storage = state.storage().ProjectStorage(prj.id)
    with state.guard("Affichage de l’illustration"):
        if storage.exists(block.image_path):
            st.image(storage.read_bytes(block.image_path), caption=f"Source : {block.detection or 'image'}")
        else:
            st.warning("Fichier source de l’illustration introuvable.")
    image_editor_page.image_block_actions(prj, page, block, prefix="blocks")


def _order_and_visibility(prj, page, block) -> None:
    st.subheader("Ordre de lecture et visibilité")
    ordered = page.ordered_blocks()
    index = ordered.index(block)
    columns = st.columns(4)
    if columns[0].button("Monter", disabled=index == 0, key=state.k("block_up", block.id)):
        with state.guard("Ordre de lecture"):
            state.commit(page, "Ordre de lecture modifié",
                         lambda: state.project_service().move_in_reading_order(page, block.id, -1))
            state.bump()
            st.rerun()
    if columns[1].button("Descendre", disabled=index == len(ordered) - 1, key=state.k("block_down", block.id)):
        with state.guard("Ordre de lecture"):
            state.commit(page, "Ordre de lecture modifié",
                         lambda: state.project_service().move_in_reading_order(page, block.id, 1))
            state.bump()
            st.rerun()
    if columns[2].button("Masquer" if not block.hidden else "Afficher", key=state.k("block_hide", block.id)):
        with state.guard("Visibilité du bloc"):
            state.commit(page, "Visibilité du bloc modifiée", lambda: setattr(block, "hidden", not block.hidden))
            st.rerun()
    with columns[3]:
        if widgets.confirm_button("Supprimer", f"delete_block_{block.id}", "Supprimer ce bloc de la page ?"):
            with state.guard("Suppression du bloc"):
                state.commit(page, "Bloc supprimé",
                             lambda: state.project_service().delete_block(prj, page, block.id))
                st.session_state.block_id = None
                st.rerun()


def _live_rebuild_preview(prj, page) -> None:
    st.divider()
    st.subheader("Aperçu de la page reconstruite")
    st.caption("Comme dans l’éditeur Django : la photo source à gauche, puis le PDF reconstruit "
               "à droite avec le texte OCR et les illustrations extraites.")
    original, rebuilt = st.columns(2)
    with original:
        st.caption("Image originale")
        from . import upload_page
        st.image(upload_page.original_preview(prj.id, page.original_path, page.source_index, 1000),
                 caption=page.original_filename, width="stretch")
    with rebuilt:
        st.caption("Résultat reconstruit — fond blanc")
        if not page.text_blocks:
            st.warning("Aucun texte OCR sur cette page. Relancez l’OCR pour reconstruire le texte.")
        with state.guard("Aperçu PDF reconstruit"):
            from services import export_service
            image, report = export_service.preview_png(prj, page, mode="reconstruction", width_px=900)
            st.image(image, caption="Aperçu identique à la page exportée dans le PDF",
                     width="stretch")
            for warning in report.warnings:
                st.warning(warning)
            for overflow in report.overflows:
                st.error(overflow)
