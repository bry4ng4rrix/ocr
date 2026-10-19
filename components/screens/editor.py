"""Écran E — Éditeur de page : boîtes sur l'image corrigée (sélection, déplacement, redimensionnement, dessin)
et aperçu reconstruit (= rendu PDF de la composition) côte à côte ; propriétés du bloc sélectionné.

Chaque enregistrement passe par project_service.update_page avec une raison (instantané d'historique).
Les saisies en cours sont des valeurs de widgets : « Enregistrer » les écrit, ou la sauvegarde automatique
après `project.autosave_seconds` (0 = désactivée).
"""
from __future__ import annotations

import hashlib
import time

import streamlit as st

import config
from components import common
from components.box_editor import block_items, box_editor, event_box
from models import (STANDARD_FORMATS, Alignment, BlockKind, Box, ImageBlock, ImageKind, Page, PageCategory,
                    PageFormat, PageStatus, ReconstructionMode, TextBlock, TextStyle)
from models.errors import BookOcrError
from services import (fonts, image_service, layout_service, ocr_service, processing_service,
                      project_service, typography_service)

EDITOR_WIDTH = 620
PREVIEW_WIDTH = 620
STYLE_FIELDS = ["font_family", "font_size_pt", "bold", "italic", "underline", "color", "alignment", "line_spacing",
                "space_before_pt", "space_after_pt", "first_line_indent_pt", "left_indent_pt", "right_indent_pt"]
BOX_FIELDS = ["x", "y", "w", "h"]
NEW_BLOCK_LABELS = {"text": "Bloc de texte", "image": "Image"}

project = common.require_project()
common.require_pages(project)
page: Page = common.page_selector(project, key="editor")
page_locked = common.warn_if_locked(project.id, page.id)
paths = project_service.page_paths(project, page)
tesseract_ready = ocr_service.tesseract_version() is not None and bool(project.languages)

if not paths.corrected.exists():
    st.info("Cette page n'est pas encore corrigée : lancez le traitement (écran Traitement OCR) avant de l'éditer.",
            icon=":material/info:")
    st.page_link(common.SCREEN_PROCESSING, label="Aller au traitement", icon=":material/document_scanner:")
    st.stop()
if page.status != PageStatus.ANALYSED:
    st.info("Cette page n'est pas encore analysée : aucun bloc détecté. Vous pouvez dessiner des blocs à la main "
            "ou lancer l'OCR et la mise en page.", icon=":material/info:")
if page.ocr_outdated:
    st.warning("La correction géométrique a changé depuis l'OCR : les boîtes peuvent être décalées. Relancez "
               "l'OCR et la mise en page.", icon=":material/sync_problem:")


# --------------------------------------------------------------------------- aides


def _labels(page: Page) -> dict[str, str]:
    labels = {block.id: common.BLOCK_KIND_LABELS[block.kind] for block in page.text_blocks}
    labels.update({block.id: common.IMAGE_KIND_LABELS[block.kind] for block in page.image_blocks})
    return labels


def _block_title(page: Page, block_id: str | None, labels: dict[str, str]) -> str:
    if block_id is None:
        return "Aucun bloc"
    ordered = page.blocks_in_order()
    rank = next(index for index, block in enumerate(ordered, start=1) if block.id == block_id)
    block = ordered[rank - 1]
    excerpt = f" — {block.text[:50]}" if isinstance(block, TextBlock) and block.text else ""
    hidden = " (masqué)" if block.hidden else ""
    return f"{rank}. {labels[block_id]}{hidden}{excerpt}"


def _find_block(page: Page, block_id: str | None) -> TextBlock | ImageBlock | None:
    for block in [*page.text_blocks, *page.image_blocks]:
        if block.id == block_id:
            return block
    return None


def _next_order(page: Page) -> int:
    return max((block.order for block in page.blocks_in_order()), default=-1) + 1


def _ocr_box(box: Box) -> tuple[str, float]:
    """Relit une zone de l'image de travail en pleine résolution (Tesseract)."""
    working = processing_service.load_working_image(project, page)
    return ocr_service.ocr_region(image_service.to_gray(working), box, project.languages)


def _save(updated: Page, reason: str, message: str) -> bool:
    with common.user_action(message) as result:
        common.save_page(project.id, updated, reason)
    if result.ok:
        st.session_state.pop("_editor_dirty_since", None)
    return result.ok


def _create_block(box: Box, kind: str) -> None:
    updated = page.model_copy(deep=True)
    with common.user_action("Bloc ajouté.") as result:
        if kind == "image":
            block = ImageBlock(box=box, kind=ImageKind.PHOTO, user_created=True, user_modified=True,
                               order=_next_order(page))
            updated.image_blocks.append(block)
            processing_service.recrop_image_block(project, updated, block.id)
        else:
            text, confidence = ("", -1.0)
            if tesseract_ready:
                with st.spinner("Lecture du texte de la zone…"):
                    text, confidence = _ocr_box(box)
            block = TextBlock(box=box, ocr_text=text, confidence=confidence, user_created=True, user_modified=True,
                              needs_review=True, order=_next_order(page))
            updated.text_blocks.append(block)
        common.save_page(project.id, updated, "Bloc ajouté")
    if result.ok:
        st.session_state["block_id"] = block.id
        st.rerun()


def _move_block(block_id: str, box: Box) -> None:
    updated = page.model_copy(deep=True)
    with common.user_action() as result:
        target = _find_block(updated, block_id)
        if target is None:
            return
        target.box = box
        target.user_modified = True
        if isinstance(target, ImageBlock):
            processing_service.recrop_image_block(project, updated, block_id)
        common.save_page(project.id, updated, "Bloc déplacé ou redimensionné")
    if result.ok:
        st.session_state["block_id"] = block_id
        st.rerun()


# --------------------------------------------------------------------------- boîtes et aperçu

labels = _labels(page)
selected_id = st.session_state.get("block_id")
if _find_block(page, selected_id) is None:
    selected_id = None
    st.session_state["block_id"] = None

with st.container(horizontal=True, vertical_alignment="bottom"):
    new_kind = st.segmented_control("Nouveau bloc dessiné", list(NEW_BLOCK_LABELS), format_func=NEW_BLOCK_LABELS.get,
                                    default="text", required=True, key="editor_new_kind", disabled=page_locked)
    preview_mode = st.segmented_control("Aperçu", list(ReconstructionMode), format_func=common.MODE_SHORT_LABELS.get,
                                        default=project.export.mode, required=True, key="editor_preview_mode")

editor_column, preview_column = st.columns(2)
with editor_column:
    st.markdown("**Page corrigée et blocs**")
    try:
        image = common.working_image(project, page, max_side=1400)
    except BookOcrError as error:
        st.error(error.user_message)
        st.stop()
    event = box_editor(common.jpeg_data_uri(image), block_items(page, labels, config.LOW_CONFIDENCE),
                       key=f"box_editor_{page.id}", selected=selected_id, width=EDITOR_WIDTH,
                       readonly=page_locked, draw_label=f"Dessiner : {NEW_BLOCK_LABELS[new_kind].lower()}")
    st.caption("Bleu : texte · vert : image · orange : à vérifier · pointillés : masqué.")
    if event is not None and not page_locked:
        box = event_box(event)
        if event.get("action") == "select" and _find_block(page, event.get("id")) is not None:
            selected_id = str(event["id"])
            st.session_state["block_id"] = selected_id
        elif event.get("action") == "update" and box is not None and _find_block(page, event.get("id")):
            _move_block(str(event["id"]), box)
        elif event.get("action") == "create" and box is not None:
            _create_block(box, new_kind)

with preview_column:
    st.markdown(f"**Aperçu reconstruit — {common.MODE_SHORT_LABELS[preview_mode]}**")
    try:
        with st.spinner("Composition de l'aperçu…"):
            preview_image, preview_issues = common.preview_image(project, page, preview_mode, PREVIEW_WIDTH)
        st.image(preview_image, width=PREVIEW_WIDTH, alt="Aperçu de la page reconstruite")
        if preview_issues:
            with st.expander(f"Remarques sur l'aperçu ({len(preview_issues)})", icon=":material/info:"):
                common.show_issues(preview_issues)
    except BookOcrError as error:
        st.error(error.user_message)
    st.page_link(common.SCREEN_LIVE_PREVIEW, label="Aperçu en direct (page blanche)", icon=":material/visibility:")

# --------------------------------------------------------------------------- choix du bloc

ordered_ids = [block.id for block in page.blocks_in_order()]
with st.container(horizontal=True, vertical_alignment="bottom"):
    chooser_key = f"_block_chooser_{page.id}"
    if st.session_state.get(chooser_key) != selected_id:
        st.session_state[chooser_key] = selected_id

    def _remember_block() -> None:
        st.session_state["block_id"] = st.session_state[chooser_key]

    st.selectbox("Bloc sélectionné", [None, *ordered_ids], key=chooser_key, on_change=_remember_block,
                 format_func=lambda block_id: _block_title(page, block_id, labels))
    if st.button("Ajouter un bloc de texte", icon=":material/add_box:", disabled=page_locked,
                 help="Ajoute une zone au centre de la page ; déplacez-la ensuite sur l'image ou par les champs."):
        _create_block(Box(x=0.3, y=0.45, w=0.4, h=0.06), "text")
    if st.button("Ordre de lecture automatique", icon=":material/format_list_numbered:", disabled=page_locked,
                 help="Recalcule l'ordre de lecture de tous les blocs (colonnes, titres, légendes)."):
        updated = page.model_copy(deep=True)
        rank = {block_id: index for index, block_id in
                enumerate(layout_service.reading_order([*updated.text_blocks, *updated.image_blocks]))}
        for block in [*updated.text_blocks, *updated.image_blocks]:
            block.order = rank.get(block.id, len(rank))
        if _save(updated, "Ordre de lecture automatique", "Ordre de lecture recalculé."):
            st.rerun()

block = _find_block(page, selected_id)


def _reorder(block_id: str, position: int) -> None:
    updated = page.model_copy(deep=True)
    blocks = updated.blocks_in_order()
    moving = next(item for item in blocks if item.id == block_id)
    blocks.remove(moving)
    blocks.insert(max(0, min(position - 1, len(blocks))), moving)
    for index, item in enumerate(blocks):
        item.order = index
    if _save(updated, "Ordre de lecture modifié", f"Bloc placé en position {position}."):
        st.rerun()


def _same(left: object, right: object) -> bool:
    if isinstance(left, float) or isinstance(right, float):
        return left is not None and right is not None and abs(float(left) - float(right)) < 1e-9
    if isinstance(left, str) and isinstance(right, str) and left.startswith("#"):
        return left.upper() == right.upper()
    return left == right


def _box_initial(box: Box) -> dict[str, float]:
    return {"x": round(box.x * 100, 2), "y": round(box.y * 100, 2), "w": round(box.w * 100, 2),
            "h": round(box.h * 100, 2)}


def _box_from(values: dict[str, object]) -> Box:
    x, y, w, h = (float(values[name]) / 100 for name in BOX_FIELDS)
    return Box.from_edges(x, y, x + w, y + h)


def _box_widgets(scope: str) -> None:
    columns = st.columns(4)
    for column, name, label in zip(columns, BOX_FIELDS, ["Gauche (%)", "Haut (%)", "Largeur (%)", "Hauteur (%)"]):
        column.number_input(label, min_value=0.0 if name in "xy" else 0.1, max_value=100.0, step=0.5,
                            format="%.2f", key=f"{scope}_{name}", disabled=page_locked)


def _signature(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def _autosave_watch(changed: bool, save_draft) -> None:
    """Sauvegarde automatique des saisies en cours après `project.autosave_seconds` (0 = désactivée)."""
    if not changed or page_locked:
        st.session_state.pop("_editor_dirty_since", None)
        return
    since = st.session_state.setdefault("_editor_dirty_since", time.monotonic())
    if project_service.should_autosave(project, since, time.monotonic()):
        save_draft("Sauvegarde automatique")
        return
    if project.autosave_seconds > 0:
        remaining = max(0, int(project.autosave_seconds - (time.monotonic() - since)))
        st.caption(f"Modifications non enregistrées : sauvegarde automatique dans {remaining} s environ.")

        def tick() -> None:
            started = st.session_state.get("_editor_dirty_since")
            if started is not None and project_service.should_autosave(project, started, time.monotonic()):
                st.rerun(scope="app")

        st.fragment(run_every=max(2.0, project.autosave_seconds / 4))(tick)()
    else:
        st.caption("Modifications non enregistrées (sauvegarde automatique désactivée).")


# --------------------------------------------------------------------------- bloc de texte


def _text_block_panel(block: TextBlock) -> None:
    scope = f"blk_{block.id}"
    detached = block.model_copy(update={"style_override": TextStyle()})
    inherited = typography_service.resolve_style(project, page, detached)
    resolved = typography_service.resolve_style(project, page, block)
    initial: dict[str, object] = {"text": block.text, "kind": block.kind, "hidden": block.hidden,
                                  "needs_review": block.needs_review, **_box_initial(block.box)}
    initial.update({name: getattr(resolved, name) for name in STYLE_FIELDS})
    initial["color"] = str(initial["color"]).upper()
    common.sync_widgets(scope, initial, _signature(block.model_dump_json(), project.typography.model_dump_json(),
                                                   str({kind.value: rule.model_dump_json()
                                                        for kind, rule in page.style_rules.items()})))
    families = sorted(fonts.available_families())
    if st.session_state[f"{scope}_font_family"] not in families:
        families = sorted({*families, str(st.session_state[f"{scope}_font_family"])})

    content_column, style_column = st.columns([3, 2])
    with content_column:
        st.text_area("Texte", key=f"{scope}_text", height=220, disabled=page_locked,
                     help="Texte final utilisé dans les exports. Le texte OCR d'origine reste consultable ci-dessous.")
        with st.expander("Texte OCR d'origine", icon=":material/document_scanner:"):
            st.text(block.ocr_text or "(vide)")
            st.caption(f"Confiance : {block.confidence:.0f} %" if block.confidence >= 0 else "Confiance inconnue.")
        weak = block.low_confidence_words(config.LOW_CONFIDENCE)
        if weak:
            st.warning("Mots de faible confiance : " + ", ".join(f"« {word.text} » ({word.confidence:.0f} %)"
                                                                 for word in weak[:40]),
                       icon=":material/spellcheck:")
        st.selectbox("Type de bloc", list(BlockKind), format_func=common.BLOCK_KIND_LABELS.get, key=f"{scope}_kind",
                     disabled=page_locked)
        with st.container(horizontal=True):
            st.toggle("Masqué (exclu des exports)", key=f"{scope}_hidden", disabled=page_locked)
            st.toggle("À vérifier", key=f"{scope}_needs_review", disabled=page_locked)
        st.markdown("Position et taille (en % de la page corrigée)")
        _box_widgets(scope)
    with style_column:
        st.selectbox("Police", families, key=f"{scope}_font_family", disabled=page_locked)
        with st.container(horizontal=True):
            st.number_input("Taille (pt)", min_value=1.0, max_value=400.0, step=0.5, format="%.1f",
                            key=f"{scope}_font_size_pt", disabled=page_locked)
            st.color_picker("Couleur", key=f"{scope}_color", disabled=page_locked)
        with st.container(horizontal=True):
            st.toggle("Gras", key=f"{scope}_bold", disabled=page_locked)
            st.toggle("Italique", key=f"{scope}_italic", disabled=page_locked)
            st.toggle("Souligné", key=f"{scope}_underline", disabled=page_locked)
        st.segmented_control("Alignement", list(Alignment), format_func=common.ALIGNMENT_LABELS.get, required=True,
                             key=f"{scope}_alignment", disabled=page_locked)
        st.number_input("Interligne (multiple)", min_value=0.5, max_value=5.0, step=0.05, format="%.2f",
                        key=f"{scope}_line_spacing", disabled=page_locked)
        with st.container(horizontal=True):
            st.number_input("Espace avant (pt)", min_value=0.0, max_value=500.0, step=1.0, format="%.1f",
                            key=f"{scope}_space_before_pt", disabled=page_locked)
            st.number_input("Espace après (pt)", min_value=0.0, max_value=500.0, step=1.0, format="%.1f",
                            key=f"{scope}_space_after_pt", disabled=page_locked)
        st.number_input("Retrait de première ligne (pt)", min_value=-200.0, max_value=500.0, step=1.0,
                        format="%.1f", key=f"{scope}_first_line_indent_pt", disabled=page_locked)
        with st.container(horizontal=True):
            st.number_input("Retrait gauche (pt)", min_value=0.0, max_value=1000.0, step=1.0, format="%.1f",
                            key=f"{scope}_left_indent_pt", disabled=page_locked)
            st.number_input("Retrait droit (pt)", min_value=0.0, max_value=1000.0, step=1.0, format="%.1f",
                            key=f"{scope}_right_indent_pt", disabled=page_locked)
        overridden = sorted(block.style_override.defined_fields())
        st.caption("Style propre à ce bloc : " + (", ".join(common.STYLE_FIELD_LABELS.get(name, name).lower()
                                                               for name in overridden) if overridden else "aucun (style global)")
                   + f" · police héritée : {inherited.font_family} {inherited.font_size_pt:g} pt")

    values = common.widget_values(scope, list(initial))
    changed = {name for name in initial if not _same(values[name], initial[name])}

    def draft() -> Page:
        updated = page.model_copy(deep=True)
        target = updated.text_block(block.id)
        if "text" in changed:
            target.corrected_text = str(values["text"] or "")
        if "kind" in changed:
            target.kind = BlockKind(values["kind"])
        style_changes = {name: values[name] for name in STYLE_FIELDS if name in changed}
        if style_changes:
            target.style_override = TextStyle(**{**target.style_override.model_dump(), **style_changes})
        if changed & set(BOX_FIELDS):
            target.box = _box_from(values)
        target.hidden = bool(values["hidden"])
        target.needs_review = bool(values["needs_review"])
        target.user_modified = True
        return updated

    def save_draft(reason: str) -> None:
        with common.user_action() as built:
            updated = draft()
        if built.ok and _save(updated, reason, "Bloc enregistré."):
            st.rerun()

    with st.container(horizontal=True):
        if st.button("Enregistrer", type="primary", icon=":material/save:", key="save_text_block",
                     disabled=page_locked or not changed):
            save_draft("Modification du bloc")
        st.button("Annuler les modifications", icon=":material/undo:", key="cancel_text_block",
                  disabled=not changed, on_click=common.forget_widgets, args=(scope,))
        if st.button("Revenir au style global", icon=":material/format_clear:", key="reset_style",
                     disabled=page_locked or not overridden):
            updated = page.model_copy(deep=True)
            updated.text_block(block.id).style_override = TextStyle()
            if _save(updated, "Style du bloc réinitialisé", "Le bloc suit de nouveau le style global."):
                st.rerun()
        if st.button("Relire par OCR", icon=":material/document_scanner:", key="reocr_block",
                     disabled=page_locked or not tesseract_ready,
                     help="Relit la zone du bloc dans l'image (Tesseract). Le texte corrigé est remplacé."
                     if tesseract_ready else "Tesseract ou les langues du projet sont indisponibles."):
            with common.user_action() as read:
                with st.spinner("Lecture de la zone…"):
                    text, confidence = _ocr_box(block.box)
            if read.ok:
                updated = page.model_copy(deep=True)
                target = updated.text_block(block.id)
                target.ocr_text, target.confidence, target.corrected_text, target.lines = text, confidence, None, []
                target.user_modified = True
                if _save(updated, "Relecture OCR du bloc", f"Bloc relu (confiance {confidence:.0f} %)."):
                    st.rerun()
    _autosave_watch(bool(changed), save_draft)

    with st.container(horizontal=True, vertical_alignment="bottom"):
        position = st.number_input("Rang dans l'ordre de lecture", min_value=1, max_value=len(ordered_ids), step=1,
                                   value=ordered_ids.index(block.id) + 1, key=f"order_{block.id}",
                                   disabled=page_locked)
        if st.button("Placer", icon=":material/low_priority:", key="place_block",
                     disabled=page_locked or int(position) == ordered_ids.index(block.id) + 1):
            _reorder(block.id, int(position))
        confirm = st.checkbox("Confirmer la suppression", key=f"confirm_delete_{block.id}", disabled=page_locked)
        if st.button("Supprimer le bloc", icon=":material/delete:", key="delete_text_block",
                     disabled=page_locked or not confirm):
            updated = page.model_copy(deep=True)
            updated.text_blocks = [item for item in updated.text_blocks if item.id != block.id]
            for image_block in updated.image_blocks:
                if image_block.caption_block_id == block.id:
                    image_block.caption_block_id = None
            if _save(updated, "Bloc supprimé", "Bloc supprimé."):
                st.session_state["block_id"] = None
                st.rerun()


# --------------------------------------------------------------------------- image


def _image_block_panel(block: ImageBlock) -> None:
    scope = f"img_{block.id}"
    caption = _find_block(page, block.caption_block_id) if block.caption_block_id else None
    initial: dict[str, object] = {"kind": block.kind, "hidden": block.hidden, "caption_id": block.caption_block_id,
                                  "caption_text": caption.text if isinstance(caption, TextBlock) else "",
                                  **_box_initial(block.box)}
    common.sync_widgets(scope, initial, _signature(block.model_dump_json(),
                                                   caption.model_dump_json() if caption else ""))
    picture_column, fields_column = st.columns([1, 2])
    with picture_column:
        crop_path = project_service.project_dir(project) / block.file if block.file else None
        try:
            crop = common.scaled_image(crop_path, 600) if crop_path else None
        except BookOcrError as error:
            st.error(error.user_message)
            crop = None
        if crop is not None:
            st.image(crop, width="stretch", alt="Image extraite")
        else:
            st.caption("Image pas encore extraite.")
    with fields_column:
        st.selectbox("Type d'image", list(ImageKind), format_func=common.IMAGE_KIND_LABELS.get, key=f"{scope}_kind",
                     disabled=page_locked)
        st.toggle("Masquée (exclue des exports)", key=f"{scope}_hidden", disabled=page_locked)
        text_ids = [item.id for item in page.text_blocks]
        if st.session_state[f"{scope}_caption_id"] not in [None, *text_ids]:
            st.session_state[f"{scope}_caption_id"] = None
        st.selectbox("Légende liée", [None, *text_ids], key=f"{scope}_caption_id", disabled=page_locked,
                     format_func=lambda block_id: "Aucune" if block_id is None else _block_title(page, block_id, labels))
        st.text_area("Texte de la légende", key=f"{scope}_caption_text", height=90,
                     disabled=page_locked or block.caption_block_id is None,
                     help="Modifiable quand une légende est liée (enregistrez d'abord le lien).")
        st.markdown("Position et taille (en % de la page corrigée) — l'image est redécoupée à l'enregistrement")
        _box_widgets(scope)

    values = common.widget_values(scope, list(initial))
    changed = {name for name in initial if not _same(values[name], initial[name])}

    def save_draft(reason: str) -> None:
        updated = page.model_copy(deep=True)
        with common.user_action() as built:
            target = updated.image_block(block.id)
            target.kind = ImageKind(values["kind"])
            target.hidden = bool(values["hidden"])
            target.caption_block_id = values["caption_id"]
            if "caption_text" in changed and block.caption_block_id:
                linked = updated.text_block(block.caption_block_id)
                linked.corrected_text = str(values["caption_text"] or "")
                linked.user_modified = True
            if values["caption_id"] and "caption_id" in changed:
                updated.text_block(str(values["caption_id"])).kind = BlockKind.CAPTION
            target.user_modified = True
            if changed & set(BOX_FIELDS):
                target.box = _box_from(values)
                processing_service.recrop_image_block(project, updated, block.id)
        if built.ok and _save(updated, reason, "Image enregistrée."):
            st.rerun()

    with st.container(horizontal=True, vertical_alignment="bottom"):
        if st.button("Enregistrer", type="primary", icon=":material/save:", key="save_image_block",
                     disabled=page_locked or not changed):
            save_draft("Modification de l'image")
        st.button("Annuler les modifications", icon=":material/undo:", key="cancel_image_block", disabled=not changed,
                  on_click=common.forget_widgets, args=(scope,))
        confirm = st.checkbox("Confirmer la suppression", key=f"confirm_delete_{block.id}", disabled=page_locked)
        if st.button("Supprimer l'image", icon=":material/delete:", key="delete_image_block",
                     disabled=page_locked or not confirm):
            updated = page.model_copy(deep=True)
            updated.image_blocks = [item for item in updated.image_blocks if item.id != block.id]
            if _save(updated, "Image supprimée", "Image supprimée de la page (le fichier reste dans le projet)."):
                st.session_state["block_id"] = None
                st.rerun()
    _autosave_watch(bool(changed), save_draft)


st.subheader("Bloc sélectionné")
if block is None:
    st.caption("Cliquez sur une boîte de l'image ou choisissez un bloc dans la liste ci-dessus.")
elif isinstance(block, TextBlock):
    _text_block_panel(block)
else:
    _image_block_panel(block)

# --------------------------------------------------------------------------- réglages de la page

st.subheader("Réglages de la page")
detected_format = page.detected_format
format_choices = ["detected", *STANDARD_FORMATS, "custom"]
page_scope = f"pagecfg_{page.id}"
override = page.format_override
initial_format = ("detected" if override is None else override.name if override.name in STANDARD_FORMATS
                  else "custom")
common.sync_widgets(page_scope, {
    "category": page.category_override, "format": initial_format,
    "width": float(override.width_mm if override else (detected_format.width_mm if detected_format else 148.0)),
    "height": float(override.height_mm if override else (detected_format.height_mm if detected_format else 210.0)),
}, page.model_dump_json(include={"category_override", "format_override", "detected_format"}))

with st.form(f"page_settings_{page.id}"):
    left, right = st.columns(2)
    left.selectbox("Catégorie de page", [None, *PageCategory], key=f"{page_scope}_category", disabled=page_locked,
                   format_func=lambda category: f"Automatique ({common.CATEGORY_LABELS[page.category]})"
                   if category is None else common.CATEGORY_LABELS[category])
    right.selectbox("Format de cette page", format_choices, key=f"{page_scope}_format", disabled=page_locked,
                    format_func=lambda choice: (f"Détecté ({detected_format.name})" if detected_format else "Détecté")
                    if choice == "detected" else "Personnalisé" if choice == "custom" else choice)
    with right.container(horizontal=True):
        st.number_input("Largeur (mm)", min_value=30.0, max_value=1000.0, step=1.0, key=f"{page_scope}_width",
                        disabled=page_locked)
        st.number_input("Hauteur (mm)", min_value=30.0, max_value=1000.0, step=1.0, key=f"{page_scope}_height",
                        disabled=page_locked)
    st.caption("Le format de la page sert quand l'export utilise le format détecté. Largeur et hauteur ne "
               "comptent que pour « Personnalisé ».")
    submitted = st.form_submit_button("Enregistrer les réglages de la page", icon=":material/save:",
                                      disabled=page_locked)
if submitted:
    values = common.widget_values(page_scope, ["category", "format", "width", "height"])
    updated = page.model_copy(deep=True)
    with common.user_action() as built:
        updated.category_override = values["category"]
        choice = values["format"]
        if choice == "detected":
            updated.format_override = None
        elif choice == "custom":
            updated.format_override = PageFormat(name="Personnalisé", width_mm=float(values["width"]),
                                                 height_mm=float(values["height"]))
        else:
            updated.format_override = STANDARD_FORMATS[choice]
    if built.ok and _save(updated, "Réglages de la page", "Réglages de la page enregistrés."):
        st.rerun()
