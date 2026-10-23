"""Fenêtre « Retoucher la page » de l'écran « Numérisation intelligente ». Chaque outil regroupe ses propres
boutons : Aperçu (traiter, valider), Recadrage (tracé, nouvelle détection des bords, contour automatique),
Réglages (amélioration automatique, original, réglages précédents, enregistrement), Orientation et type,
Analyse Gemma (analyser, appliquer la proposition). Ouverte depuis la galerie, le tableau ou l'étape 4, elle
reste ouverte après chaque action (le résultat est affiché à jour)."""
from __future__ import annotations

import streamlit as st

from components import common
from components.crop_selector import crop_selector
from components.scan.shared import (CROP_DISPLAY_WIDTH, CROP_SIDE, CROPS, EXTRA_CROPS, MAX_CROP_PERCENT,
                                    PREVIEW_SIDE, SLIDERS, STATE_COLORS, STATE_LABELS, ai_status, cached_image,
                                    image_size, run_action, use_ai)
from models import Project
from models.errors import BookOcrError
from models.geometry import Point
from models.scanning import MAX_CROP, PAGE_KIND_LABELS, PageKind, ScanAdjustments, ScanState
from repositories import scan_repository
from services import image_analysis_service, image_service, project_service, scanning_service
from workers import scan_runner

DIALOG_KEY = "_scan_dialog_page"
TOOLS = ["Aperçu", "Recadrage", "Réglages", "Orientation et type", "Analyse Gemma"]


def open_retouch(page_id: str) -> None:
    """Ouvre (ou garde ouverte) la fenêtre de retouche sur cette page, au prochain affichage de l'écran."""
    common.select_page(page_id)
    st.session_state[DIALOG_KEY] = page_id
    st.rerun(scope="app")


def close_retouch() -> None:
    st.session_state.pop(DIALOG_KEY, None)


def show_if_open(project: Project) -> None:
    """À appeler en fin d'écran : affiche la fenêtre si une page est en cours de retouche."""
    page_id = st.session_state.get(DIALOG_KEY)
    if page_id in {page.id for page in project.pages}:
        retouch_dialog(project, page_id)


# --------------------------------------------------------------------------- outils


def _preview(project: Project, page, paths, final_path) -> None:
    view = st.segmented_control("Affichage", ["Trois étapes", "Avant / après", "Zoom 100 %"],
                                default="Trois étapes", key="scan_view")
    straightened = cached_image(paths.corrected)
    final = cached_image(final_path)
    if view == "Avant / après":
        side = st.segmented_control("Image", ["Avant (redressée)", "Après (finale)"], default="Après (finale)",
                                    key="scan_side")
        shown = final if side == "Après (finale)" else straightened
        if shown is None:
            st.info("Pas encore disponible.", icon=":material/image_not_supported:")
        else:
            st.image(shown, width="stretch", alt=side or "Page")
    elif view == "Zoom 100 %":
        target = final_path if final_path and final_path.is_file() else paths.corrected
        if target.is_file():
            full = image_service.read_image(target)
            height, width = full.shape[:2]
            zone_x = st.slider("Position horizontale", 0, 100, 50, key="scan_zoom_x")
            zone_y = st.slider("Position verticale", 0, 100, 30, key="scan_zoom_y")
            box = 700
            x0 = int((width - min(box, width)) * zone_x / 100)
            y0 = int((height - min(box, height)) * zone_y / 100)
            st.image(full[y0:y0 + box, x0:x0 + box], alt="Détail à 100 %")
            st.caption(f"Détail 1:1 de {target.name} ({width} × {height} px).")
        else:
            st.info("Pas encore disponible.", icon=":material/image_not_supported:")
    else:
        columns = st.columns(3, border=True)
        panels = [("Photographie originale", common.original_image(project, page, PREVIEW_SIDE), paths.original),
                  ("Page redressée", straightened, paths.corrected),
                  ("Page améliorée (PDF)", final, final_path)]
        for column, (title, image, path) in zip(columns, panels):
            with column:
                st.markdown(f"**{title}**")
                if image is None:
                    st.info("Pas encore disponible.", icon=":material/image_not_supported:")
                else:
                    st.image(image, width="stretch", alt=title)
                    st.caption(image_size(path))


def _commands(project: Project, page, record, busy: bool) -> None:
    """Traitement complet de la page et validation, les deux décisions sur la page entière."""
    with st.container(horizontal=True, vertical_alignment="center"):
        if st.button("Traiter cette page", icon=":material/play_arrow:", type="primary", disabled=busy,
                     key="retouch_process"):
            run_action("Traitement de la page…", "Page traitée.",
                       lambda: scanning_service.process_page(project, page.id, use_ai=use_ai()))
        if st.button("Dévalider" if record.validated else "Valider la page", icon=":material/task_alt:",
                     disabled=busy or not scanning_service.is_current(page, record), key="retouch_validate"):
            run_action("Enregistrement…", "Validation enregistrée.",
                       lambda: scanning_service.validate_page(project, page.id, not record.validated))


def _crop(project: Project, page, busy: bool) -> None:
    """Recadrage manuel à la souris : rectangle ou 4 coins (perspective) sur la photographie originale."""
    st.caption("Tracez la zone de la page sur la photo d'origine, ou passez en « 4 coins » pour suivre une page "
               "photographiée en biais, puis cliquez sur « Appliquer le recadrage ». La perspective, l'orientation "
               "et l'amélioration sont ensuite recalculées pour cette page.")
    original = common.original_image(project, page, CROP_SIDE)
    if original is None:
        st.warning("Photographie originale illisible.", icon=":material/image_not_supported:")
        return
    current = page.geometry.effective_quad()
    rectangular = current is None or (
        abs(current[0].y - current[1].y) < 0.002 and abs(current[0].x - current[3].x) < 0.002
        and abs(current[2].y - current[3].y) < 0.002 and abs(current[1].x - current[2].x) < 0.002)
    corners = crop_selector(common.jpeg_data_uri(original), current, key=f"crop_{page.id}",
                            width=CROP_DISPLAY_WIDTH, mode="rect" if rectangular else "quad",
                            alt=f"Photographie originale de la page {project.page_number(page.id)}")
    if corners is not None and not busy:
        run_action("Recadrage et nouveau traitement de la page…", "Recadrage manuel appliqué.",
                   lambda: scanning_service.set_manual_quad(project, page.id, corners))
    origin = "manuel" if page.geometry.manual_quad is not None else (
        "automatique" if page.geometry.detected_quad is not None else "aucun (photo entière)")
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(f"Contour actuel : {origin}.")
        if st.button("Détecter les bords à nouveau", icon=":material/crop_free:", disabled=busy,
                     key="retouch_redetect"):
            run_action("Détection géométrique…", "Géométrie recalculée.",
                       lambda: scanning_service.redetect_geometry(project, page.id))
        if page.geometry.manual_quad is not None and st.button("Revenir au contour automatique", disabled=busy,
                                                               icon=":material/auto_fix_high:",
                                                               key="retouch_auto_quad"):
            run_action("Recadrage…", "Contour automatique rétabli.",
                       lambda: scanning_service.set_manual_quad(project, page.id, None))
    with st.expander("Saisie numérique des coins (alternative au clavier)"):
        quad = current or [Point(x=0, y=0), Point(x=1, y=0), Point(x=1, y=1), Point(x=0, y=1)]
        with st.form(f"scan_quad_{page.id}", border=False):
            values = []
            for index, (corner, label) in enumerate(zip(quad, ["Haut-gauche", "Haut-droit", "Bas-droit",
                                                               "Bas-gauche"])):
                x_column, y_column = st.columns(2)
                x = x_column.number_input(f"{label} x", 0.0, 1.0, float(corner.x), 0.005, format="%.3f",
                                          key=f"q{index}x_{page.id}")
                y = y_column.number_input(f"{label} y", 0.0, 1.0, float(corner.y), 0.005, format="%.3f",
                                          key=f"q{index}y_{page.id}")
                values.append(Point(x=x, y=y))
            apply_quad = st.form_submit_button("Appliquer ces coins", disabled=busy)
        if apply_quad:
            run_action("Recadrage…", "Recadrage manuel appliqué.",
                       lambda: scanning_service.set_manual_quad(project, page.id, values))


def _adjustments(project: Project, page, record, busy: bool, has_straightened: bool) -> None:
    if not has_straightened:
        st.info("Traitez la page pour régler son amélioration.", icon=":material/info:")
        return
    with st.container(horizontal=True, vertical_alignment="center"):               # réglages tout faits
        if st.button("Améliorer automatiquement", icon=":material/auto_fix_high:", disabled=busy,
                     key="retouch_auto_enhance"):
            run_action("Amélioration mesurée…", "Amélioration appliquée.",
                       lambda: scanning_service.auto_enhance(project, page.id))
        if st.button("Restaurer l'original", icon=":material/restore:", disabled=busy, key="retouch_reset",
                     help="Aucune amélioration : la page redressée telle quelle."):
            run_action("Restauration…", "Réglages neutres appliqués.",
                       lambda: scanning_service.reset_to_original(project, page.id))
        if st.button("Réglages précédents", icon=":material/undo:", disabled=busy or not record.history,
                     key="retouch_previous"):
            run_action("Retour aux réglages précédents…", "Réglages précédents restaurés.",
                       lambda: scanning_service.restore_previous(project, page.id))
    scope = f"scanadj_{page.id}"
    common.sync_widgets(scope, record.adjustments.model_dump(), record.final_signature or "neutre")
    left, right = st.columns([1, 2])
    with left:
        with st.expander("Image", icon=":material/tune:", expanded=True):
            for field, (label, low, high, step) in SLIDERS.items():
                st.slider(label, low, high, step=step, key=f"{scope}_{field}")
            st.toggle("Balance des blancs automatique", key=f"{scope}_white_balance",
                      help="Déconseillée sur les pages colorées : elle peut altérer les couleurs d'origine.")
        with st.expander("Rognage des bords", icon=":material/crop:"):
            st.caption("Rognage automatique (bandes de fond) puis rognage supplémentaire, en fraction de chaque bord "
                       f"(0 à {MAX_CROP_PERCENT:g} %).")
            for field, label in {**CROPS, **EXTRA_CROPS}.items():
                st.slider(label, 0.0, MAX_CROP, step=0.001, format="%.3f", key=f"{scope}_{field}")
    values = common.widget_values(scope, list(ScanAdjustments.model_fields))
    try:
        adjustments = ScanAdjustments(**values)
    except ValueError as error:
        st.error(f"Réglage refusé : {error}")
        return
    with right:
        try:
            before, after, check = scanning_service.preview(project, page.id, adjustments, PREVIEW_SIDE)
        except BookOcrError as error:
            st.error(error.user_message)
            return
        columns = st.columns(2)
        columns[0].image(before, width="stretch", caption="Redressée", alt="Page redressée")
        columns[1].image(after, width="stretch", caption="Aperçu des réglages", alt="Aperçu des réglages")
        (st.success if check.passed else st.warning)(" ".join(check.reasons))
        st.caption(f"Netteté {check.before.sharpness:.0f} → {check.after.sharpness:.0f} · clarté du papier "
                   f"{check.before.paper_level:.0f} → {check.after.paper_level:.0f} · dérive chromatique "
                   f"{check.color_shift:.1f} (aperçu réduit ; les scores seuls ne prouvent pas une amélioration).")
        if st.button("Enregistrer ces réglages (pleine résolution + OCR)", icon=":material/save:", type="primary",
                     disabled=busy, key="retouch_save_adjustments"):
            run_action("Application…", "Réglages enregistrés et appliqués.",
                       lambda: scanning_service.render_final(project, page.id, adjustments, source="manual"))


def _orientation_and_kind(project: Project, page, record, busy: bool) -> None:
    st.caption(f"Orientation automatique : {page.geometry.orientation_method or '—'}"
               f"{' (incertaine)' if page.geometry.orientation_uncertain else ''} ; quarts de tour appliqués : "
               f"{page.geometry.effective_quarter_turns()}.")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        turns = st.segmented_control("Rotation manuelle (après l'automatique)", [0, 1, 2, 3],
                                     default=page.geometry.manual_quarter_turns,
                                     format_func=lambda value: f"{value * 90}°", key=f"scan_turns_{page.id}")
        if st.button("Confirmer la rotation", key="retouch_rotate",
                     disabled=busy or turns is None or turns == page.geometry.manual_quarter_turns):
            run_action("Rotation…", "Rotation appliquée.",
                       lambda: scanning_service.set_rotation(project, page.id, int(turns)))
    kind_options = [None, *PageKind]
    kind_choice = st.selectbox(
        "Type de page (statistiques et prix)", kind_options,
        index=kind_options.index(record.page_kind_override),
        format_func=lambda kind: (f"Automatique : {PAGE_KIND_LABELS[record.page_kind]}" if record.page_kind
                                  else "Automatique (après traitement)") if kind is None else PAGE_KIND_LABELS[kind],
        key=f"scan_kind_{page.id}", disabled=busy)
    if kind_choice != record.page_kind_override:
        run_action("Enregistrement…", "Type de page enregistré.",
                   lambda: scanning_service.set_page_kind(project, page.id, kind_choice))


def _analysis(project: Project, page, record, busy: bool, has_straightened: bool) -> None:
    level, message = ai_status()
    if st.button("Analyser avec Gemma" if record.analysis is None else "Analyser à nouveau",
                 icon=":material/smart_toy:", disabled=busy or level != "ok" or not has_straightened,
                 key="retouch_analyse", help="Analyse mise en cache : relancée seulement si l'image ou le modèle "
                                             "change. Environ 2 minutes par page sur ce poste."):
        run_action("Analyse par Gemma (environ 2 minutes)…", "Analyse terminée.",
                   lambda: scanning_service.analyse(project, page.id))
    if level != "ok":
        st.caption(message)
    if record.analysis is None:
        st.info("Pas encore d'analyse Gemma pour cette page.", icon=":material/smart_toy:")
        return
    st.table(image_analysis_service.summary(record.analysis))
    st.caption(f"Modèle {record.analysis.model} · consigne {record.analysis.prompt_version} · "
               f"{record.analysis.created_at:%d/%m/%Y %H:%M} · "
               f"{record.analysis.duration_s or 0:.0f} s. Gemma propose, les mesures décident.")
    if record.proposed_plan is not None:
        st.markdown("**Proposition (après confirmation par les mesures)**")
        for reason in record.proposed_plan.reasons:
            st.caption(f"• {reason}")
        st.json(record.proposed_plan.adjustments.model_dump(exclude_defaults=True))
        if st.button("Appliquer la proposition", icon=":material/check:", disabled=busy, key="retouch_proposal"):
            run_action("Application…", "Proposition appliquée.",
                       lambda: scanning_service.apply_proposal(project, page.id))


# --------------------------------------------------------------------------- fenêtre


@st.dialog("Retoucher la page", width="large", on_dismiss=close_retouch)
def retouch_dialog(project: Project, page_id: str) -> None:
    page = project.page(page_id)
    record = scanning_service.load_record(project, page)
    paths = project_service.page_paths(project, page)
    final_path = project_service.project_dir(project) / record.final_file if record.final_file else None
    busy = scan_runner.is_page_busy(page.id)
    task = scan_repository.get_task(project.id, page.id)
    state = task.state if task else ScanState.PENDING
    has_straightened = paths.corrected.is_file()
    ids = [item.id for item in project.pages]
    position = ids.index(page.id)

    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown(f"**Page {position + 1} / {len(ids)}** · {page.original_name}")
        st.badge(STATE_LABELS[state], color=STATE_COLORS[state])
        st.badge("Validée" if record.validated else "Non validée", color="green" if record.validated else "gray")
        if record.effective_kind is not None:
            st.badge(PAGE_KIND_LABELS[record.effective_kind], color="violet")
        if record.final_file and not scanning_service.is_current(page, record):
            st.badge("Image finale obsolète", color="orange")
    with st.container(horizontal=True):
        if st.button("Page précédente", icon=":material/chevron_left:", disabled=position == 0, key="retouch_prev"):
            open_retouch(ids[position - 1])
        if st.button("Page suivante", icon=":material/chevron_right:", disabled=position == len(ids) - 1,
                     key="retouch_next"):
            open_retouch(ids[position + 1])
        if st.button("Fermer", icon=":material/close:", key="retouch_close"):
            close_retouch()
            st.rerun()
    if busy:
        st.info("Cette page est en cours de traitement en arrière-plan : les commandes sont désactivées.",
                icon=":material/hourglass_top:")
    for reason in record.review_reasons:
        st.warning(reason, icon=":material/rule:")
    if record.error:
        st.error(record.error, icon=":material/error:")

    tool = st.segmented_control("Outil", TOOLS, default=TOOLS[0], key="retouch_tool")
    if tool == "Recadrage":
        _crop(project, page, busy)
    elif tool == "Réglages":
        _adjustments(project, page, record, busy, has_straightened)
    elif tool == "Orientation et type":
        _orientation_and_kind(project, page, record, busy)
    elif tool == "Analyse Gemma":
        _analysis(project, page, record, busy, has_straightened)
    else:
        _commands(project, page, record, busy)
        _preview(project, page, paths, final_path)
