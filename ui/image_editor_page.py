"""Image editor of one page: geometry, processing mode, print colours, OCR image, local corrections,
illustrations. Values stay drafts in session_state until saved; previews are computed on a reduced
copy of the frame (config.PREVIEW_MAX_SIDE) and cached by content."""
from __future__ import annotations

import numpy as np
import streamlit as st
from PIL import Image, ImageDraw

from core.document_models import (AdjustmentTarget, Box, GeometrySettings, ImageAdjustments, LocalAdjustment,
                                  PageStatus, ProcessingMode)

from . import box_editor, state, widgets

SECTIONS = ["Géométrie", "Mode de traitement", "Couleurs pour l'impression", "Image pour l'OCR",
            "Corrections locales", "Illustrations"]
MODE_HELP = {
    ProcessingMode.AUTO.value: ("Auto", "Le mode est choisi d'après le contenu de la page (texte seul, "
                                        "illustrations, ou les deux)."),
    ProcessingMode.TEXT.value: ("A — Texte et OCR", "Priorité à la lisibilité du texte : niveaux de gris, "
                                                    "éclairage aplani. Pour les pages sans illustration."),
    ProcessingMode.ILLUSTRATION.value: ("B — Illustrations et couleurs", "Priorité aux couleurs et aux détails : "
                                        "jamais de binarisation implicite. Pour les planches et photos."),
    ProcessingMode.MIXED.value: ("C — Page mixte", "Traitement OCR sur les zones de texte (fond blanchi), "
                                                   "traitement couleur sur les illustrations."),
}
GEOMETRY_FIELDS = ("auto_orientation", "auto_crop", "manual_quad", "auto_deskew", "deskew_angle", "trim_left",
                   "trim_top", "trim_right", "trim_bottom", "auto_trim_margins", "scale_percent")
TARGET_LABELS = {AdjustmentTarget.PRINT.value: "Impression (PDF, illustrations)",
                 AdjustmentTarget.OCR.value: "Image OCR", AdjustmentTarget.BOTH.value: "Les deux"}


def render(prj) -> None:
    st.header("Éditeur d'image")
    if not prj.pages:
        st.info("Importez d'abord des pages (rubrique « Pages »).")
        return
    top = st.columns([3, 2])
    with top[0]:
        page = state.page_selector(prj, key="img_page")
    with top[1]:
        widgets.history_bar("img")
    section = st.radio("Section", SECTIONS, horizontal=True, key="img_section", label_visibility="collapsed")
    if page.ocr_outdated:
        st.warning("Le cadre de cette page a changé depuis l'OCR : les positions des blocs ne correspondent "
                   "plus à l'image.")
        _ocr_button(prj, page, "ocr_outdated")
    if section == "Mode de traitement":
        _mode(prj, page)
        return
    if not state.frame_ready(prj, page):
        st.info("L'image de travail de cette page (cadre recadré et redressé) n'est pas encore préparée.")
        if st.button("Préparer l'image de la page", type="primary", key=state.k("prepare", page.id)):
            with state.guard("Préparation de la page"), st.spinner("Détection de la feuille, de l'orientation "
                                                                    "et de l'inclinaison…"):
                for w in state.prepare_frame(prj, page):
                    state.flash("warning", w)
                state.bump()
                st.rerun()
        if section != "Géométrie":
            return
    with state.guard(section):
        {"Géométrie": _geometry, "Couleurs pour l'impression": _print_colours, "Image pour l'OCR": _ocr_colours,
         "Corrections locales": _locals, "Illustrations": _illustrations}[section](prj, page)


# --------------------------------------------------------------------------- shared helpers

def _ocr_button(prj, page, key: str, label: str = "Relancer l'OCR de cette page") -> None:
    if st.button(label, key=state.k(key, page.id)):
        bar = st.progress(0.0, text="OCR…")
        with state.guard("OCR"), st.status("OCR de la page…", expanded=False) as status:
            def progress(index, total, fraction, message):
                bar.progress(min(fraction, 1.0), text=message)
            results = state.processing().process_pages(prj, [page.id], progress=progress)
            if results and results[0].ok:
                status.update(label="OCR terminé", state="complete")
                state.flash("success", f"OCR terminé : {results[0].text_blocks} bloc(s) de texte, "
                                       f"{results[0].image_blocks} illustration(s).")
            else:
                status.update(label="OCR en échec", state="error")
                state.flash("error", results[0].message if results else "OCR en échec.")
            state.bump()
            st.rerun()


def _mode_for_preview(prj, page, small: np.ndarray) -> ProcessingMode:
    key = ("mode", prj.id, state.processing().frame_key(page), page.processing_mode.value,
           page.resolved_mode.value if page.resolved_mode else "")
    return state.cached(key, lambda: state.processing().resolve_mode(page, small))


def _text_boxes(page) -> list[Box]:
    return [b.box for b in page.text_blocks if not b.hidden]


def _illustration_boxes(page) -> list[Box]:
    return [b.source_box or b.box for b in page.image_blocks]


def _render_print(prj, page, adjustments: ImageAdjustments, locals_: list[LocalAdjustment]) -> np.ndarray:
    from core import image_preprocessing
    small = state.small_frame(prj, page)
    mode = _mode_for_preview(prj, page, small)
    boxes = _text_boxes(page)
    key = ("print", prj.id, state.processing().frame_key(page), adjustments.model_dump_json(),
           tuple(l.model_dump_json() for l in locals_), mode.value, state.digest(*[b.model_dump_json() for b in boxes]))
    return state.cached(key, lambda: image_preprocessing.render_print_image(small, adjustments, locals_, mode, boxes))


def _render_ocr(prj, page, adjustments: ImageAdjustments, locals_: list[LocalAdjustment]) -> np.ndarray:
    from core import image_preprocessing
    small = state.small_frame(prj, page)
    mode = _mode_for_preview(prj, page, small)
    boxes = _illustration_boxes(page) if mode != ProcessingMode.TEXT else []
    key = ("ocr", prj.id, state.processing().frame_key(page), adjustments.model_dump_json(),
           tuple(l.model_dump_json() for l in locals_), mode.value, state.digest(*[b.model_dump_json() for b in boxes]))
    return state.cached(key, lambda: image_preprocessing.render_ocr_image(small, adjustments, locals_, mode, boxes))


# --------------------------------------------------------------------------- geometry

def _geo_draft(page) -> dict:
    drafts = st.session_state.geo_drafts
    if page.id not in drafts:
        drafts[page.id] = {f: page.geometry.model_dump(mode="json")[f] for f in GEOMETRY_FIELDS}
    return drafts[page.id]


def _quad_overlay(prj, page, draft_quad) -> bytes | None:
    """Thumbnail of the original turned like the frame, with the detected (green) and manual (red) corners."""
    from . import upload_page
    try:
        data = upload_page.original_preview(prj.id, page.original_path, page.source_index, 600)
    except Exception:
        return None
    import io
    image = Image.open(io.BytesIO(data)).convert("RGB")
    turn = page.geometry.effective_rotation()
    if turn:
        image = image.rotate(-turn, expand=True)        # clockwise
    draw = ImageDraw.Draw(image)
    w, h = image.size
    for quad, colour in ((page.geometry.detected_quad, (0, 170, 0)), (draft_quad, (214, 40, 40))):
        if quad:
            pts = [(x * w, y * h) for x, y in quad]
            draw.line(pts + [pts[0]], fill=colour, width=3)
            for px, py in pts:
                draw.ellipse([px - 5, py - 5, px + 5, py + 5], outline=colour, width=2)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _geometry(prj, page) -> None:
    g = page.geometry
    draft = _geo_draft(page)
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Rotation par quart de tour")
        st.caption("Appliquée tout de suite ; les blocs déjà analysés tournent avec la page (pas de nouvel OCR).")
        c = st.columns(3)
        for col, (label, deg) in zip(c, (("↺ 90° à gauche", 270), ("↻ 90° à droite", 90), ("180°", 180))):
            if col.button(label, key=state.k("georot", deg, page.id)):
                state.commit(page, f"Rotation de {deg if deg != 270 else -90}°",
                             lambda d=deg: state.project_service().rotate_page(prj, page, d))
                with st.spinner("Reconstruction du cadre…"):
                    state.processing().ensure_frame(prj, page)
                    state.save(prj)
                st.session_state.geo_drafts.pop(page.id, None)
                st.rerun()
        draft["auto_orientation"] = st.checkbox(
            "Orientation automatique" + (f" (détectée : {g.detected_orientation}°)"
                                         if g.detected_orientation is not None else ""),
            value=bool(draft["auto_orientation"]), key=state.k("geo_orient", page.id))

        st.subheader("Recadrage de la feuille")
        draft["auto_crop"] = st.checkbox("Recadrage automatique de la feuille", value=bool(draft["auto_crop"]),
                                         key=state.k("geo_crop", page.id),
                                         help="Cherche les 4 coins de la feuille sur la photo et la redresse en "
                                              "perspective.")
        if g.detected_quad:
            st.caption("Coins détectés (en vert sur la miniature) : "
                       + ", ".join(f"({x * 100:.1f} %, {y * 100:.1f} %)" for x, y in g.detected_quad))
        elif g.auto_crop:
            st.caption("Aucune feuille nette détectée : la photo entière est utilisée.")
        manual = st.checkbox("Quadrilatère manuel (4 coins en %)", value=draft["manual_quad"] is not None,
                             key=state.k("geo_manual", page.id))
        if manual:
            quad = draft["manual_quad"] or g.detected_quad or [(0, 0), (1, 0), (1, 1), (0, 1)]
            new_quad = []
            names = ("haut gauche", "haut droit", "bas droit", "bas gauche")
            cols = st.columns(4)
            for i, (name, (x, y)) in enumerate(zip(names, quad)):
                with cols[i]:
                    st.caption(name)
                    qx = st.number_input("x %", 0.0, 100.0, float(round(x * 100, 2)), 0.5,
                                         key=state.k("qx", i, page.id))
                    qy = st.number_input("y %", 0.0, 100.0, float(round(y * 100, 2)), 0.5,
                                         key=state.k("qy", i, page.id))
                    new_quad.append((qx / 100, qy / 100))
            draft["manual_quad"] = new_quad
        else:
            draft["manual_quad"] = None

        st.subheader("Redressement")
        options = ["Automatique", "Manuel", "Aucun"]
        current = "Manuel" if draft["deskew_angle"] is not None else ("Automatique" if draft["auto_deskew"] else "Aucun")
        how = st.radio("Angle", options, index=options.index(current), horizontal=True,
                       key=state.k("geo_deskew", page.id))
        if g.detected_skew is not None:
            st.caption(f"Inclinaison détectée : {g.detected_skew:.2f}°")
        if how == "Manuel":
            start = draft["deskew_angle"] if draft["deskew_angle"] is not None else (g.detected_skew or 0.0)
            draft["deskew_angle"] = st.slider("Angle (degrés, sens antihoraire)", -15.0, 15.0,
                                              float(max(-15.0, min(15.0, start))), 0.05,
                                              key=state.k("geo_angle", page.id))
            draft["auto_deskew"] = False
        else:
            draft["deskew_angle"] = None
            draft["auto_deskew"] = how == "Automatique"

        st.subheader("Marges et taille")
        c = st.columns(4)
        for col, (field, label) in zip(c, (("trim_left", "Gauche"), ("trim_top", "Haut"),
                                           ("trim_right", "Droite"), ("trim_bottom", "Bas"))):
            draft[field] = col.slider(f"Rogner {label.lower()} (%)", 0.0, 40.0, float(draft[field]), 0.5,
                                      key=state.k("geo", field, page.id))
        draft["auto_trim_margins"] = st.checkbox("Rognage automatique des marges vides",
                                                 value=bool(draft["auto_trim_margins"]),
                                                 key=state.k("geo_autotrim", page.id))
        draft["scale_percent"] = st.slider("Redimensionnement (%)", 10.0, 400.0, float(draft["scale_percent"]), 5.0,
                                           key=state.k("geo_scale", page.id),
                                           help="Change la résolution du cadre, pas les coordonnées des blocs.")

    merged = GeometrySettings.model_validate({**g.model_dump(mode="json"), **draft})
    dirty = any(getattr(merged, f) != getattr(g, f) for f in GEOMETRY_FIELDS)
    with right:
        overlay = _quad_overlay(prj, page, draft["manual_quad"])
        if overlay:
            st.image(overlay, caption="Original tourné : coins détectés en vert, coins manuels en rouge",
                     width="stretch")
        if state.frame_ready(prj, page):
            st.image(state.small_frame(prj, page), caption=f"Cadre actuel ({page.frame_width} × "
                                                           f"{page.frame_height} px)", width="stretch")
    if dirty:
        st.warning("Modifications de géométrie non appliquées.")
        if page.status == PageStatus.PROCESSED:
            st.warning("Cette page a déjà été lue : un nouveau cadre déplace le repère des coordonnées. "
                       "Les positions des blocs devront être recalculées (OCR obsolète) ; vous pourrez relancer "
                       "l'OCR de la page, vos corrections seront conservées.")
    for warning in page.warnings:
        if "cadre" in warning.lower() or "feuille" in warning.lower():
            st.caption(f"⚠ {warning}")
    c = st.columns(3)
    if c[0].button("Appliquer la géométrie", type="primary", disabled=not dirty, key=state.k("geo_apply", page.id)):
        with st.spinner("Reconstruction du cadre…"):
            state.commit(page, "Géométrie modifiée", lambda: setattr(page, "geometry", merged))
            warnings = state.prepare_frame(prj, page)
        st.session_state.geo_drafts.pop(page.id, None)
        state.flash("success", "Géométrie appliquée.")
        for w in warnings:
            state.flash("warning", w)
        st.rerun()
    if c[1].button("Abandonner les modifications", disabled=not dirty, key=state.k("geo_drop", page.id)):
        st.session_state.geo_drafts.pop(page.id, None)
        state.bump()
        st.rerun()
    if c[2].button("Relancer les détections automatiques", key=state.k("geo_redetect", page.id),
                   help="Orientation, feuille et inclinaison sont recherchées à nouveau."):
        with st.spinner("Détections…"):
            state.history().record(page, "Détections automatiques relancées")
            warnings = state.prepare_frame(prj, page, redetect=True)
        st.session_state.geo_drafts.pop(page.id, None)
        state.bump()
        for w in warnings:
            state.flash("warning", w)
        st.rerun()
    if page.status == PageStatus.PROCESSED and page.ocr_outdated:
        _ocr_button(prj, page, "geo_ocr")


# --------------------------------------------------------------------------- processing mode

def _mode(prj, page) -> None:
    st.subheader("Mode de traitement de la page")
    modes = list(MODE_HELP)
    choice = st.radio("Mode", modes, index=modes.index(page.processing_mode.value),
                      format_func=lambda m: MODE_HELP[m][0], captions=[MODE_HELP[m][1] for m in modes],
                      key=state.k("mode", page.id))
    if page.resolved_mode:
        st.caption(f"Mode retenu au dernier traitement : {MODE_HELP[page.resolved_mode.value][0]}.")
    if choice != page.processing_mode.value:
        state.commit(page, "Mode de traitement", lambda: setattr(page, "processing_mode", ProcessingMode(choice)))
        if choice != ProcessingMode.AUTO.value:
            page.resolved_mode = None
            state.save(prj)
        st.rerun()
    st.info("Le mode change la préparation de l'image OCR et de l'image imprimée. Pour une page déjà lue, "
            "relancez l'OCR afin que le texte en tienne compte.")
    if page.status == PageStatus.PROCESSED:
        _ocr_button(prj, page, "mode_ocr")


# --------------------------------------------------------------------------- global colours

def _colours(prj, page, target: str) -> None:
    is_print = target == "print"
    saved = page.print_adjustments if is_print else page.ocr_adjustments
    key = state.draft_key(page, target)
    if is_print:
        st.caption("Ces réglages produisent l'image imprimée (fac-similé) et s'appliquent aux illustrations "
                   "découpées dans la page. Un réglage de couleur ne déplace aucun bloc et ne nécessite pas "
                   "de nouvel OCR.")
    else:
        st.caption("Ces réglages ne changent que l'image lue par l'OCR, jamais le PDF. Ils ne déplacent aucun "
                   "bloc, mais il faut relancer l'OCR de la page pour qu'ils agissent sur le texte.")
    controls, preview = st.columns([2, 3])
    with controls:
        draft = widgets.adjustment_controls(key, saved, prefix=f"{target}_{page.id}", for_print=is_print)
    with preview:
        dirty = widgets.dirty_badge(draft, saved)
        c = st.columns(2)
        if c[0].button("Enregistrer les réglages", type="primary", disabled=not dirty,
                       key=state.k("save", target, page.id)):
            field = "print_adjustments" if is_print else "ocr_adjustments"
            label = "Réglages couleur (impression)" if is_print else "Réglages de l'image OCR"
            state.commit(page, label, lambda: setattr(page, field, draft))
            state.drop_draft(key)
            state.flash("success", "Réglages enregistrés." + ("" if is_print else
                                                               " Relancez l'OCR de la page pour en tenir compte."))
            st.rerun()
        if c[1].button("Abandonner le brouillon", disabled=not dirty, key=state.k("drop", target, page.id)):
            state.drop_draft(key)
            state.bump()
            st.rerun()
        before = state.small_frame(prj, page)
        after = _render_print(prj, page, draft, page.local_adjustments) if is_print else \
            _render_ocr(prj, page, draft, page.local_adjustments)
        widgets.before_after(before, after, f"pv_{target}", ("Avant : cadre géométrique",
                                                             "Après : image imprimée" if is_print else
                                                             "Après : image lue par l'OCR"))
        widgets.histogram_chart(after if after.ndim == 3 else np.stack([after] * 3, axis=-1), f"hist_{target}")
        if not is_print and page.status == PageStatus.PROCESSED:
            if dirty:
                st.caption("Enregistrez les réglages avant de relancer l'OCR.")
            else:
                _ocr_button(prj, page, "ocr_colours")


def _print_colours(prj, page) -> None:
    _colours(prj, page, "print")


def _ocr_colours(prj, page) -> None:
    _colours(prj, page, "ocr")


# --------------------------------------------------------------------------- local corrections

def _crop_around(image: np.ndarray, box: Box, margin: float = 0.08) -> np.ndarray:
    h, w = image.shape[:2]
    x0 = max(0, int((box.x - margin) * w))
    y0 = max(0, int((box.y - margin) * h))
    x1 = min(w, int((box.x2 + margin) * w) + 1)
    y1 = min(h, int((box.y2 + margin) * h) + 1)
    return image[y0:y1, x0:x1]


def _locals(prj, page) -> None:
    st.caption("Une correction locale ne s'applique qu'à un rectangle de la page. Ses coordonnées sont "
               "relatives au cadre de la page : elles ne dépendent pas du zoom.")
    ids = [loc.id for loc in page.local_adjustments]
    if st.session_state.local_id not in ids:
        st.session_state.local_id = ids[0] if ids else None
    left, right = st.columns([3, 2])
    with left:
        zoom = st.slider("Largeur de l'éditeur (px)", 400, 1400, 700, 50, key="loc_zoom")
        base = state.processing().print_image(prj, page, max_side=1400)
        event = box_editor.box_editor(base, box_editor.local_items(page), key=f"locals_{page.id}",
                                      selected=st.session_state.local_id, width=zoom,
                                      draw_label="Dessiner une zone de correction")
        if box_editor.is_new(event, st.session_state.box_nonces):
            out = box_editor.apply_event(prj, page, event, state.history(), target="locals")
            st.session_state.local_id = out["selected"]
            state.bump()
            st.rerun()
        with st.expander("Ajouter une zone par saisie numérique"):
            with st.form(f"add_local_{page.id}"):
                c = st.columns(4)
                x = c[0].number_input("x %", 0.0, 99.0, 10.0, 0.5)
                y = c[1].number_input("y %", 0.0, 99.0, 10.0, 0.5)
                w = c[2].number_input("largeur %", 0.5, 100.0, 30.0, 0.5)
                h = c[3].number_input("hauteur %", 0.5, 100.0, 20.0, 0.5)
                if st.form_submit_button("Ajouter la zone"):
                    box = Box.clamped(x / 100, y / 100, w / 100, h / 100)
                    local = state.commit(page, "Zone de correction ajoutée",
                                         lambda: state.project_service().add_local_adjustment(page, box))
                    st.session_state.local_id = local.id
                    st.rerun()
    with right:
        if not page.local_adjustments:
            st.info("Aucune correction locale. Cliquez sur « Dessiner une zone de correction » puis tracez un "
                    "rectangle sur l'image, ou utilisez la saisie numérique.")
            return
        chosen = st.selectbox("Zone", ids, index=ids.index(st.session_state.local_id), key=state.k("loc_sel", page.id),
                              format_func=lambda i: next(f"{n + 1}. {l.label}" for n, l in
                                                         enumerate(page.local_adjustments) if l.id == i))
        if chosen != st.session_state.local_id:
            st.session_state.local_id = chosen
            st.rerun()
        local = next(l for l in page.local_adjustments if l.id == chosen)
        _local_panel(prj, page, local)


def _local_panel(prj, page, local: LocalAdjustment) -> None:
    pre = f"loc_{local.id}"
    meta_key = f"{page.id}:localmeta:{local.id}"
    meta = st.session_state.drafts.setdefault(meta_key, local.model_dump(mode="json", exclude={"adjustments", "id"}))
    meta["label"] = st.text_input("Nom", meta["label"], key=state.k(pre, "label"))
    targets = list(TARGET_LABELS)
    meta["target"] = st.selectbox("Cible", targets, index=targets.index(meta["target"]), format_func=TARGET_LABELS.get,
                                  key=state.k(pre, "target"))
    c = st.columns(4)
    box = meta["box"]
    box["x"] = c[0].number_input("x %", 0.0, 99.9, round(box["x"] * 100, 2), 0.5, key=state.k(pre, "x")) / 100
    box["y"] = c[1].number_input("y %", 0.0, 99.9, round(box["y"] * 100, 2), 0.5, key=state.k(pre, "y")) / 100
    box["width"] = c[2].number_input("larg. %", 0.1, 100.0, round(box["width"] * 100, 2), 0.5,
                                     key=state.k(pre, "w")) / 100
    box["height"] = c[3].number_input("haut. %", 0.1, 100.0, round(box["height"] * 100, 2), 0.5,
                                      key=state.k(pre, "h")) / 100
    meta["feather"] = st.slider("Fondu des bords", 0.0, 0.2, float(meta["feather"]), 0.01, key=state.k(pre, "feather"))
    meta["restore_original"] = st.checkbox("Revenir à l'original de la zone (aucune correction dans ce rectangle)",
                                           value=bool(meta["restore_original"]), key=state.k(pre, "restore"))
    meta["enabled"] = st.checkbox("Zone active", value=bool(meta["enabled"]), key=state.k(pre, "enabled"))
    adj_key = f"{page.id}:local:{local.id}"
    if not meta["restore_original"]:
        draft_adj = widgets.adjustment_controls(adj_key, local.adjustments, prefix=pre,
                                                for_print=meta["target"] != AdjustmentTarget.OCR.value)
    else:
        draft_adj = ImageAdjustments.model_validate(state.get_draft(adj_key, local.adjustments))
    try:
        b = Box.clamped(box["x"], box["y"], box["width"], box["height"])
        draft_local = LocalAdjustment.model_validate({**meta, "box": b.model_dump(), "id": local.id,
                                                      "adjustments": draft_adj.model_dump(mode="json")})
    except ValueError as exc:
        st.error(f"Zone invalide : {exc}")
        return
    dirty = draft_local != local
    if dirty:
        st.warning("Modifications non enregistrées.")
    c = st.columns(3)
    if c[0].button("Enregistrer la zone", type="primary", disabled=not dirty, key=state.k(pre, "save")):
        def change():
            index = next(i for i, l in enumerate(page.local_adjustments) if l.id == local.id)
            page.local_adjustments[index] = draft_local
        state.commit(page, "Correction locale modifiée", change)
        st.session_state.drafts.pop(meta_key, None)
        state.drop_draft(adj_key)
        st.rerun()
    if c[1].button("Abandonner", disabled=not dirty, key=state.k(pre, "drop")):
        st.session_state.drafts.pop(meta_key, None)
        state.drop_draft(adj_key)
        state.bump()
        st.rerun()
    with c[2]:
        if widgets.confirm_button("Supprimer la zone", f"del_local_{local.id}", "Supprimer cette zone de correction ?"):
            state.commit(page, "Correction locale supprimée",
                         lambda: page.local_adjustments.remove(next(l for l in page.local_adjustments
                                                                    if l.id == local.id)))
            st.session_state.drafts.pop(meta_key, None)
            state.drop_draft(adj_key)
            st.rerun()
    # Preview of the zone: page with the stored zone, then with the draft.
    others = [l for l in page.local_adjustments if l.id != local.id]
    draft_locals = [draft_local if l.id == local.id else l for l in page.local_adjustments]
    use_ocr = draft_local.target == AdjustmentTarget.OCR
    render = _render_ocr if use_ocr else _render_print
    saved_adj = page.ocr_adjustments if use_ocr else page.print_adjustments
    before = render(prj, page, saved_adj, page.local_adjustments if not dirty else others + [local])
    after = render(prj, page, saved_adj, draft_locals)
    widgets.before_after(_crop_around(before, draft_local.box), _crop_around(after, draft_local.box),
                         f"pv_local_{local.id}", ("Zone enregistrée", "Zone avec le brouillon"))


# --------------------------------------------------------------------------- illustrations

def _small(image: np.ndarray, side: int = 900) -> np.ndarray:
    from core import image_preprocessing
    return image_preprocessing.downscale(image, side)[0]


def _illustrations(prj, page) -> None:
    blocks = page.image_blocks
    if not blocks:
        st.info("Aucune illustration sur cette page. Elles sont détectées par l'OCR ; vous pouvez aussi en "
                "ajouter une dans l'éditeur de blocs (zone dessinée ou image importée).")
        return
    ids = [b.id for b in blocks]
    if st.session_state.illus_id not in ids:
        st.session_state.illus_id = ids[0]
    ps = state.processing()
    cols = st.columns(min(len(blocks), 6))
    for i, block in enumerate(blocks):
        with cols[i % len(cols)]:
            try:
                st.image(_small(ps.illustration_array(prj, page, block), 220), width="stretch")
            except Exception as exc:
                st.error(f"Illustration illisible ({exc}).")
            badge = " — non validé" if block.has_unvalidated_edits else ""
            if st.button(f"Illustration {i + 1}{badge}", key=state.k("illus_pick", block.id),
                         type="primary" if block.id == st.session_state.illus_id else "secondary"):
                st.session_state.illus_id = block.id
                st.rerun()
    block = page.block(st.session_state.illus_id)
    st.divider()
    _illustration_panel(prj, page, block)


def _illustration_panel(prj, page, block) -> None:
    ps = state.processing()
    pre = f"ill_{block.id}"
    key = f"{page.id}:illus:{block.id}"
    controls, preview = st.columns([2, 3])
    with controls:
        st.caption("Les curseurs modifient le brouillon de l'illustration ; le PDF n'utilise que la version "
                   "validée.")
        draft = widgets.adjustment_controls(key, block.adjustments, prefix=pre, for_print=True,
                                            draft_source=block.draft_adjustments)
    wanted = None if draft == block.adjustments else draft
    if wanted != block.draft_adjustments:
        block.draft_adjustments = wanted         # persisted draft, used by the draft preview; not in the PDF
        state.save(prj)
    with preview:
        if block.has_unvalidated_edits:
            st.warning("Non validé : le PDF utilise encore les réglages précédents.")
        else:
            st.caption("Validé : l'aperçu correspond au PDF.")
        c = st.columns(2)
        if c[0].button("Valider pour le PDF", type="primary", disabled=not block.has_unvalidated_edits,
                       key=state.k(pre, "validate")):
            def change():
                block.adjustments = draft
                block.draft_adjustments = None
            state.commit(page, "Illustration validée", change)
            state.drop_draft(key)
            st.rerun()
        if c[1].button("Abandonner", disabled=block.draft_adjustments is None, key=state.k(pre, "discard")):
            block.draft_adjustments = None
            state.save(prj)
            state.drop_draft(key)
            state.bump()
            st.rerun()
        before = _small(ps.illustration_array(prj, page, block, draft=False))
        after = _small(ps.illustration_array(prj, page, block, draft=True))
        widgets.before_after(before, after, f"pv_{pre}", ("Version validée (PDF)", "Brouillon"))

    st.subheader("Cadrage, rotation, source")
    with st.form(f"illus_geom_{block.id}"):
        c = st.columns(4)
        crop = block.crop
        cx = c[0].number_input("Recadrage x %", 0.0, 99.0, round(crop.x * 100, 2), 0.5)
        cy = c[1].number_input("Recadrage y %", 0.0, 99.0, round(crop.y * 100, 2), 0.5)
        cw = c[2].number_input("Largeur gardée %", 1.0, 100.0, round(crop.width * 100, 2), 0.5)
        ch = c[3].number_input("Hauteur gardée %", 1.0, 100.0, round(crop.height * 100, 2), 0.5)
        c = st.columns(2)
        rotation = c[0].number_input("Rotation (degrés, sens antihoraire)", -360.0, 360.0, float(block.rotation), 1.0)
        keep = c[1].checkbox("Conserver les proportions", value=block.preserve_aspect_ratio)
        if st.form_submit_button("Enregistrer le cadrage"):
            new_crop = Box.clamped(cx / 100, cy / 100, cw / 100, ch / 100)

            def change():
                block.crop = new_crop
                block.rotation = rotation
                block.preserve_aspect_ratio = keep
            state.commit(page, "Cadrage de l'illustration", change)
            st.rerun()
    image_block_actions(prj, page, block, prefix="img")


def image_block_actions(prj, page, block, prefix: str) -> None:
    """Replace, revert, delete an illustration (shared with the block editor)."""
    service = state.project_service()
    c = st.columns(3)
    with c[0]:
        upload = st.file_uploader("Remplacer par une image", type=["png", "jpg", "jpeg", "tif", "tiff"],
                                  key=state.k(prefix, "replace", block.id))
        if upload is not None and st.button("Remplacer", key=state.k(prefix, "replace_btn", block.id)):
            with state.guard("Remplacement"):
                state.commit(page, "Illustration remplacée",
                             lambda: service.replace_image(prj, block, upload.getvalue(), upload.name))
                st.rerun()
    with c[1]:
        can_revert = bool(block.original_image_path) and block.image_path != block.original_image_path
        if st.button("Revenir à l'extraction d'origine", disabled=not can_revert,
                     key=state.k(prefix, "revert", block.id)):
            with state.guard("Retour à l'original"):
                state.commit(page, "Illustration d'origine rétablie", lambda: service.revert_image(prj, block))
                st.rerun()
    with c[2]:
        if widgets.confirm_button("Supprimer l'illustration", f"{prefix}_del_{block.id}",
                                  "Supprimer cette illustration de la page ?"):
            with state.guard("Suppression"):
                state.commit(page, "Illustration supprimée", lambda: service.delete_block(prj, page, block.id))
                st.rerun()
