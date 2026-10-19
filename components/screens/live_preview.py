"""Écran H — Aperçu en direct : vue côte à côte en temps réel.

À gauche l'image de la page (corrigée ou originale), à droite les textes extraits sur une page blanche sans fond,
avec un sélecteur de mode (disposition positionnée / flux de texte).
"""
from __future__ import annotations

import streamlit as st

import config
from components import common
from components.live_preview_render import draw_overlay_boxes, render_flow_html, render_positioned_html
from models import Page, PageStatus, TextBlock
from models.errors import BookOcrError
from services import project_service

project = common.require_project()
common.require_pages(project)
page: Page = common.page_selector(project, key="live_preview")
paths = project_service.page_paths(project, page)

if not paths.corrected.exists() and not paths.original.exists():
    st.info("Cette page n'est pas encore disponible.", icon=":material/info:")
    st.stop()

if page.status in (PageStatus.IMPORTED, PageStatus.CORRECTED) and not page.text_blocks:
    st.info("Le texte de cette page n'a pas encore été extrait. Vous pouvez lancer l'OCR depuis l'écran "
            "Traitement OCR.", icon=":material/info:")

# --------------------------------------------------------------------------- contrôles supérieurs

with st.container(horizontal=True, vertical_alignment="bottom"):
    display_mode = st.segmented_control(
        "Mode du texte",
        ["positioned", "flow"],
        format_func=lambda mode: "Disposition positionnée" if mode == "positioned" else "Flux de texte",
        default="positioned",
        required=True,
        key=f"lp_mode_{page.id}",
        help="« Disposition positionnée » reproduit l'emplacement exact des blocs sur la page blanche. "
             "« Flux de texte » présente les paragraphes et titres dans l'ordre de lecture.",
    )
    has_corrected = paths.corrected.exists()
    image_source = st.segmented_control(
        "Image affichée",
        ["corrected", "original"] if has_corrected else ["original"],
        format_func=lambda src: "Page corrigée" if src == "corrected" else "Original brut",
        default="corrected" if has_corrected else "original",
        required=True,
        key=f"lp_src_{page.id}",
    )
    show_boxes = st.toggle("Surligner les blocs", value=False, key=f"lp_boxes_{page.id}",
                           help="Affiche le contour des blocs de texte (bleu) et des images (vert).")

# --------------------------------------------------------------------------- colonnes gauche / droite

left_col, right_col = st.columns(2, gap="large")

with left_col:
    img_label = "Page corrigée" if image_source == "corrected" else "Original brut"
    st.markdown(f"**Image — {img_label}**")
    try:
        raw_image = (common.corrected_image(project, page)
                     if image_source == "corrected" and has_corrected
                     else common.original_image(project, page))
    except BookOcrError as error:
        st.error(error.user_message)
        raw_image = None

    if raw_image is not None:
        display_img = draw_overlay_boxes(raw_image, page) if show_boxes else raw_image
        st.image(display_img, width="stretch", alt=f"Image de la page {page.id}")
        st.caption(f"Page {project.page_number(page.id)} / {len(project.pages)} · "
                   f"Confiance OCR : {common.confidence_label(page)} · "
                   f"{len(page.text_blocks)} bloc(s) de texte")
    else:
        st.info("Image non disponible.", icon=":material/image_not_supported:")

with right_col:
    mode_label = "Disposition positionnée" if display_mode == "positioned" else "Flux de texte"
    st.markdown(f"**Texte extrait — Page blanche ({mode_label})**")

    uris: dict[str, str] = {}
    for img_block in page.image_blocks:
        if img_block.file:
            img_path = project_service.project_dir(project) / img_block.file
            if img_path.exists():
                try:
                    scaled = common.scaled_image(img_path, 800)
                    if scaled is not None:
                        uris[img_block.id] = common.jpeg_data_uri(scaled)
                except BookOcrError:
                    pass

    aspect = None
    if raw_image is not None and raw_image.shape[0] > 0:
        aspect = raw_image.shape[1] / raw_image.shape[0]

    if display_mode == "positioned":
        sheet_html = render_positioned_html(project, page, aspect_ratio=aspect, image_data_uris=uris)
    else:
        sheet_html = render_flow_html(project, page, image_data_uris=uris)

    st.html(sheet_html)

# --------------------------------------------------------------------------- actions et texte brut

full_text = "\n\n".join(block.text.strip() for block in page.blocks_in_order()
                        if isinstance(block, TextBlock) and not block.hidden and block.text.strip())

with st.container(horizontal=True, vertical_alignment="center"):
    if st.button("Modifier dans l'éditeur de page", icon=":material/edit_document:", type="primary"):
        common.select_page(page.id)
        st.switch_page(common.SCREEN_EDITOR)

if full_text:
    with st.expander("Texte brut complet", icon=":material/description:"):
        st.text_area("Texte extrait", value=full_text, height=180, disabled=True,
                     label_visibility="collapsed")
        st.caption(f"{len(full_text)} caractère(s) · {len(full_text.split())} mot(s)")
