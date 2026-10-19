"""Écran « Numérisation intelligente » : PDF fidèle par livre (photographies traitées + OCR invisible).

L'écran n'appelle que des services. Gemma (Ollama) est facultatif : son état affiché vient d'un test réel,
ses propositions ne sont appliquées qu'après aperçu et validation (sauf OLLAMA_AUTO_APPLY). Les curseurs
recalculent un aperçu local réduit (OpenCV) sans jamais solliciter Gemma.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import streamlit as st

import config
from components import common
from models.errors import AiUnavailableError, BookOcrError
from models.geometry import Point
from models.scanning import ScanAdjustments, ScanState
from repositories import scan_repository
from services import (image_analysis_service, image_service, ocr_service, ollama_service, project_service,
                      scanning_service)
from workers import scan_runner

PREVIEW_SIDE = 900
STATE_LABELS = {ScanState.PENDING: "En attente", ScanState.PROCESSING: "En cours", ScanState.COMPLETED: "Terminée",
                ScanState.REVIEW_REQUIRED: "À vérifier", ScanState.FAILED: "En erreur"}
SOURCE_LABELS = {"neutral": "Aucune correction", "measured": "Mesures", "gemma": "Gemma + mesures",
                 "manual": "Manuel"}
FILTERS = {"Toutes": None, "À vérifier": [ScanState.REVIEW_REQUIRED], "En erreur": [ScanState.FAILED],
           "En attente": [ScanState.PENDING, ScanState.PROCESSING], "Terminées": [ScanState.COMPLETED]}
SLIDERS = {  # champ: (libellé, min, max, pas)
    "exposure": ("Exposition (gain)", -30.0, 40.0, 1.0),
    "brightness": ("Luminosité", -50.0, 50.0, 1.0),
    "contrast": ("Contraste", -50.0, 50.0, 1.0),
    "gamma": ("Gamma", 0.5, 2.0, 0.05),
    "shadow_correction": ("Correction des ombres", 0.0, 80.0, 1.0),
    "local_contrast": ("Contraste local", 0.0, 60.0, 1.0),
    "denoise": ("Débruitage", 0.0, 60.0, 1.0),
    "sharpness": ("Netteté", 0.0, 60.0, 1.0),
}
CROPS = {"crop_left": "Rognage gauche", "crop_top": "Rognage haut", "crop_right": "Rognage droit",
         "crop_bottom": "Rognage bas"}

project = common.require_project()
common.require_pages(project)
scanning_service.sync_queue(project)


@st.cache_data(ttl=30, show_spinner=False)
def _ollama_check(enabled: bool, base_url: str, model: str) -> tuple[str, str]:
    """Test réel (serveur, modèle installé, capacité vision) ; (niveau, message). Rafraîchi toutes les 30 s."""
    if not enabled:
        return "off", "Analyse IA désactivée (OLLAMA_ENABLED=false). Le traitement local fonctionne sans IA."
    current = ollama_service.status()
    if not current.available:
        return "error", f"{current.message} Le traitement continue sans IA."
    try:
        info = ollama_service.model_details(model)
    except AiUnavailableError as error:
        return "error", f"{error.user_message} Le traitement continue sans IA."
    if not info.has_vision:
        return "error", f"Le modèle « {model} » n'a pas la capacité vision. Le traitement continue sans IA."
    return "ok", f"Ollama joignable, modèle « {model} » installé ({info.parameter_size}, {info.quantization})."


@st.cache_data(max_entries=24, show_spinner=False)
def _image(path: str, stamp: int, max_side: int) -> np.ndarray:
    return image_service.downscale(image_service.read_image(Path(path)), max_side)[0]


def _cached(path: Path, max_side: int = PREVIEW_SIDE) -> np.ndarray | None:
    try:
        return _image(str(path), path.stat().st_mtime_ns, max_side) if path.is_file() else None
    except BookOcrError:
        return None


def _size(path: Path) -> str:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return f"{image.width} × {image.height} px"
    except OSError:
        return "—"


def _run(label: str, success: str, action) -> None:
    """Action bloquante courte (une page) avec retour d'erreur en français."""
    with common.user_action(success) as result, st.spinner(label):
        action()
    if result.ok:
        st.rerun()


# --------------------------------------------------------------------------- A. état du système

ai_level, ai_message = _ollama_check(config.OLLAMA_ENABLED and config.OLLAMA_ANALYSIS_ENABLED,
                                     config.OLLAMA_BASE_URL, ollama_service.configured_model())
ai_ready = ai_level == "ok"
tesseract = ocr_service.tesseract_version()
missing_languages = [lang for lang in project.languages if lang not in ocr_service.available_languages()]


@st.fragment(run_every=3 if scan_runner.is_running() else None)
def system_status() -> None:
    progress = scan_repository.progress(project.id)
    library = scan_repository.progress()
    runner = scan_runner.status()
    with st.container(horizontal=True):
        st.metric("Ollama", "Disponible" if ai_ready else ("Désactivé" if ai_level == "off" else "Indisponible"),
                  border=True)
        st.metric("Modèle", ollama_service.configured_model() or "—", border=True)
        st.metric("OCR", f"Tesseract {tesseract}" if tesseract else "Absent", border=True)
        st.metric("Pages du livre", len(project.pages), border=True)
        st.metric("À vérifier", progress.counts.get(ScanState.REVIEW_REQUIRED, 0), border=True)
        st.metric("En erreur", progress.counts.get(ScanState.FAILED, 0), border=True)
    st.progress(progress.done / max(1, progress.total),
                text=f"Livre : {progress.done}/{progress.total} page(s) traitée(s)")
    if library.total > progress.total:
        st.progress(library.done / max(1, library.total),
                    text=f"Bibliothèque : {library.done}/{library.total} page(s) traitée(s)")
    if runner.running:
        speed = f" — {runner.seconds_per_page:.0f} s/page mesurées" if runner.seconds_per_page else ""
        st.caption(f"Traitement en arrière-plan : {runner.processed} page(s) faite(s){speed}.")
    elif runner.message and runner.started_at:
        st.caption(f"Dernier traitement : {runner.message} ({runner.processed} page(s), "
                   f"{runner.failed} en échec).")
    was_running = st.session_state.get("_scan_was_running", False)
    st.session_state["_scan_was_running"] = runner.running
    if was_running and not runner.running:
        st.rerun(scope="app")


system_status()
if ai_level == "ok":
    st.success(ai_message, icon=":material/smart_toy:")
elif ai_level == "error":
    st.warning(ai_message, icon=":material/cloud_off:")
else:
    st.info(ai_message, icon=":material/info:")
if not tesseract:
    st.error("Tesseract est introuvable : la couche OCR du PDF ne peut pas être produite.", icon=":material/error:")
elif missing_languages:
    st.warning(f"Langue(s) OCR absente(s) : {', '.join(missing_languages)}.", icon=":material/translate:")

# --------------------------------------------------------------------------- B. sélection des pages

overview = scanning_service.book_overview(project)
st.subheader("Pages")
filter_label = st.segmented_control("Afficher", list(FILTERS), default="Toutes", key="scan_filter")
wanted = FILTERS.get(filter_label or "Toutes")
rows = [row for row in overview if wanted is None or ScanState(row["état"]) in wanted]
table = st.dataframe(
    [{**row, "état": STATE_LABELS[ScanState(row["état"])], "réglages": SOURCE_LABELS.get(row["réglages"], row["réglages"])}
     for row in rows],
    column_order=["numéro", "fichier", "état", "à jour", "validée", "réglages", "analyse", "proposition",
                  "OCR (mots)", "motifs"],
    hide_index=True, on_select="rerun", selection_mode="multi-row", key="scan_table", height=260,
    alt="Pages du livre et état de la numérisation")
selected_ids = [rows[index]["page_id"] for index in table.selection.rows] if table.selection else []
st.caption(f"{len(selected_ids)} page(s) sélectionnée(s) dans le tableau." if selected_ids
           else "Sélectionnez des lignes du tableau pour traiter seulement ces pages.")

running = scan_runner.is_running()
with st.container(horizontal=True, vertical_alignment="bottom"):
    use_ai = st.toggle("Analyse Gemma pendant le traitement", value=ai_ready, disabled=not ai_ready,
                       help="Une requête à la fois ; environ une minute par page sur ce poste (mesuré).")
    if st.button("Traiter la sélection", icon=":material/play_arrow:", disabled=running or not selected_ids):
        with common.user_action("Traitement lancé en arrière-plan."):
            scan_runner.start([project.id], selected_ids, use_ai=bool(use_ai))
            st.rerun()
    if st.button("Traiter les pages en attente", icon=":material/playlist_play:", type="primary", disabled=running):
        with common.user_action("Traitement lancé en arrière-plan."):
            scan_runner.start([project.id], use_ai=bool(use_ai))
            st.rerun()
    if st.button("Relancer les erreurs", icon=":material/replay:", disabled=running):
        with common.user_action("Relance des pages en erreur lancée."):
            scan_runner.start([project.id], retry_failed=True, use_ai=bool(use_ai))
            st.rerun()
    if st.button("Arrêter", icon=":material/stop:", disabled=not running):
        scan_runner.cancel()
        common.flash("info", "Arrêt demandé : la page en cours se termine.")
        st.rerun()

# --------------------------------------------------------------------------- C. aperçu de la page

st.subheader("Page")
candidates = selected_ids or [row["page_id"] for row in rows] or [page.id for page in project.pages]
page = common.page_selector(project, key="smart_scan", page_ids=candidates)
record = scanning_service.load_record(project, page)
paths = project_service.page_paths(project, page)
final_path = project_service.project_dir(project) / record.final_file if record.final_file else None
busy = running and scan_runner.status().current_page == page.id
task = scan_repository.get_task(project.id, page.id)
state = task.state if task else ScanState.PENDING

with st.container(horizontal=True):
    st.badge(STATE_LABELS[state], color={"COMPLETED": "green", "REVIEW_REQUIRED": "orange", "FAILED": "red"}
             .get(state.value, "gray"))
    st.badge("Validée" if record.validated else "Non validée", color="green" if record.validated else "gray")
    st.badge(f"Réglages : {SOURCE_LABELS.get(record.adjustments_source, record.adjustments_source)}", color="blue")
    if record.final_file and not scanning_service.is_current(page, record):
        st.badge("Image finale obsolète", color="orange")
if record.error:
    st.error(record.error, icon=":material/error:")
for reason in record.review_reasons:
    st.warning(reason, icon=":material/rule:")

view = st.segmented_control("Affichage", ["Trois étapes", "Avant / après", "Zoom 100 %"], default="Trois étapes",
                            key="scan_view")
original = common.original_image(project, page, PREVIEW_SIDE)
straightened = _cached(paths.corrected)
final = _cached(final_path) if final_path else None
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
    panels = [("Photographie originale", original, paths.original),
              ("Page redressée", straightened, paths.corrected),
              ("Page améliorée (PDF)", final, final_path)]
    for column, (title, image, path) in zip(columns, panels):
        with column:
            st.markdown(f"**{title}**")
            if image is None:
                st.info("Pas encore disponible.", icon=":material/image_not_supported:")
            else:
                st.image(image, width="stretch", alt=title)
                st.caption(_size(path) if path is not None else "")

# --------------------------------------------------------------------------- D. commandes

st.subheader("Commandes")
has_straightened = paths.corrected.is_file()
with st.container(horizontal=True):
    if st.button("Traiter cette page", icon=":material/play_arrow:", type="primary", disabled=busy):
        _run("Traitement de la page…", "Page traitée.",
             lambda: scanning_service.process_page(project, page.id, use_ai=ai_ready and use_ai))
    if st.button("Analyser avec Gemma", icon=":material/smart_toy:", disabled=busy or not ai_ready or not has_straightened,
                 help="Analyse mise en cache : relancée seulement si l'image ou le modèle change."):
        _run("Analyse par Gemma (environ une minute)…", "Analyse terminée.",
             lambda: scanning_service.analyse(project, page.id))
    if st.button("Détecter les bords et corriger la perspective", icon=":material/crop_free:", disabled=busy):
        _run("Détection géométrique…", "Géométrie recalculée.",
             lambda: scanning_service.redetect_geometry(project, page.id))
    if st.button("Améliorer automatiquement", icon=":material/auto_fix_high:", disabled=busy or not has_straightened):
        _run("Amélioration mesurée…", "Amélioration appliquée.", lambda: scanning_service.auto_enhance(project, page.id))
    if st.button("Restaurer l'original", icon=":material/restore:", disabled=busy or not has_straightened,
                 help="Aucune amélioration : la page redressée telle quelle."):
        _run("Restauration…", "Réglages neutres appliqués.",
             lambda: scanning_service.reset_to_original(project, page.id))
    if st.button("Réglages précédents", icon=":material/undo:", disabled=busy or not record.history):
        _run("Retour aux réglages précédents…", "Réglages précédents restaurés.",
             lambda: scanning_service.restore_previous(project, page.id))
    if st.button("Dévalider" if record.validated else "Valider la page", icon=":material/task_alt:",
                 disabled=busy or not scanning_service.is_current(page, record)):
        _run("Enregistrement…", "Validation enregistrée.",
             lambda: scanning_service.validate_page(project, page.id, not record.validated))

with st.expander("Orientation et recadrage manuels", icon=":material/crop_rotate:"):
    st.caption(f"Orientation automatique : {page.geometry.orientation_method or '—'}"
               f"{' (incertaine)' if page.geometry.orientation_uncertain else ''} ; quarts de tour appliqués : "
               f"{page.geometry.effective_quarter_turns()}.")
    turns = st.segmented_control("Rotation manuelle (après l'automatique)", [0, 1, 2, 3],
                                 default=page.geometry.manual_quarter_turns,
                                 format_func=lambda value: f"{value * 90}°", key=f"scan_turns_{page.id}")
    if st.button("Confirmer la rotation", disabled=busy or turns is None or turns == page.geometry.manual_quarter_turns):
        _run("Rotation…", "Rotation appliquée.", lambda: scanning_service.set_rotation(project, page.id, int(turns)))
    quad = page.geometry.effective_quad() or [Point(x=0, y=0), Point(x=1, y=0), Point(x=1, y=1), Point(x=0, y=1)]
    with st.form(f"scan_quad_{page.id}"):
        st.caption("Coins de la page sur la photographie originale (0 à 1 ; haut-gauche, haut-droit, "
                   "bas-droit, bas-gauche). Le contour est contrôlé (convexe, assez grand, angles plausibles).")
        values = []
        for index, (corner, label) in enumerate(zip(quad, ["Haut-gauche", "Haut-droit", "Bas-droit", "Bas-gauche"])):
            cx, cy = st.columns(2)
            x = cx.number_input(f"{label} x", 0.0, 1.0, float(corner.x), 0.005, format="%.3f", key=f"q{index}x_{page.id}")
            y = cy.number_input(f"{label} y", 0.0, 1.0, float(corner.y), 0.005, format="%.3f", key=f"q{index}y_{page.id}")
            values.append(Point(x=x, y=y))
        apply_quad = st.form_submit_button("Appliquer le recadrage", disabled=busy)
    if apply_quad:
        _run("Recadrage…", "Recadrage manuel appliqué.", lambda: scanning_service.set_manual_quad(project, page.id, values))
    if page.geometry.manual_quad is not None and st.button("Revenir au contour automatique", disabled=busy):
        _run("Recadrage…", "Contour automatique rétabli.", lambda: scanning_service.set_manual_quad(project, page.id, None))

# --------------------------------------------------------------------------- analyse Gemma et proposition

if record.analysis is not None:
    with st.expander("Analyse Gemma", icon=":material/smart_toy:", expanded=record.proposed_plan is not None):
        summary = image_analysis_service.summary(record.analysis)
        st.table(summary)
        st.caption(f"Modèle {record.analysis.model} · consigne {record.analysis.prompt_version} · "
                   f"{record.analysis.created_at:%d/%m/%Y %H:%M} · "
                   f"{record.analysis.duration_s or 0:.0f} s. Gemma propose, les mesures décident.")
        if record.proposed_plan is not None:
            st.markdown("**Proposition (après confirmation par les mesures)**")
            for reason in record.proposed_plan.reasons:
                st.caption(f"• {reason}")
            st.json(record.proposed_plan.adjustments.model_dump(exclude_defaults=True))
            if st.button("Appliquer la proposition", icon=":material/check:", disabled=busy):
                _run("Application…", "Proposition appliquée.", lambda: scanning_service.apply_proposal(project, page.id))

# --------------------------------------------------------------------------- E. réglages manuels (aperçu local)


@st.fragment
def manual_adjustments() -> None:
    if not has_straightened:
        st.info("Traitez la page pour régler son amélioration.", icon=":material/info:")
        return
    scope = f"scanadj_{page.id}"
    common.sync_widgets(scope, record.adjustments.model_dump(), record.final_signature or "neutre")
    left, right = st.columns([1, 2])
    with left:
        for field, (label, low, high, step) in SLIDERS.items():
            st.slider(label, low, high, step=step, key=f"{scope}_{field}")
        for field, label in CROPS.items():
            st.slider(label, 0.0, 0.08, step=0.001, format="%.3f", key=f"{scope}_{field}")
        st.toggle("Balance des blancs automatique", key=f"{scope}_white_balance",
                  help="Déconseillée sur les pages colorées : elle peut altérer les couleurs d'origine.")
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
        cols = st.columns(2)
        cols[0].image(before, width="stretch", caption="Redressée", alt="Page redressée")
        cols[1].image(after, width="stretch", caption="Aperçu des réglages", alt="Aperçu des réglages")
        (st.success if check.passed else st.warning)(" ".join(check.reasons))
        st.caption(f"Netteté {check.before.sharpness:.0f} → {check.after.sharpness:.0f} · clarté du papier "
                   f"{check.before.paper_level:.0f} → {check.after.paper_level:.0f} · dérive chromatique "
                   f"{check.color_shift:.1f} (aperçu réduit ; les scores seuls ne prouvent pas une amélioration).")
        if st.button("Enregistrer ces réglages (pleine résolution + OCR)", icon=":material/save:", type="primary",
                     disabled=busy):
            with common.user_action("Réglages enregistrés et appliqués.") as result, st.spinner("Application…"):
                scanning_service.render_final(project, page.id, adjustments, source="manual")
            if result.ok:
                st.rerun(scope="app")


with st.expander("Ajuster manuellement", icon=":material/tune:"):
    manual_adjustments()

# --------------------------------------------------------------------------- export

st.subheader("PDF du livre")
st.caption("Un PDF par livre : photographies finales en couleur, couche OCR invisible, ordre du livre. "
           "Toutes les pages doivent être traitées et à jour.")
include_unvalidated = st.toggle("Inclure les pages à vérifier non validées", value=True, key="scan_include")
if st.button("Exporter le PDF du livre", icon=":material/picture_as_pdf:", type="primary", disabled=running):
    with common.user_action() as result, st.spinner("Création et contrôle du PDF…"):
        report, validation = scanning_service.export_book_pdf(project, include_unvalidated=include_unvalidated)
    if result.ok:
        st.session_state["scan_last_export"] = (report.model_dump_json(), validation.model_dump_json())
last = st.session_state.get("scan_last_export")
if last:
    from models.scanning import SearchablePdfReport, SearchablePdfValidation

    report = SearchablePdfReport.model_validate_json(last[0])
    validation = SearchablePdfValidation.model_validate_json(last[1])
    (st.success if validation.passed else st.error)(
        f"{report.page_count} page(s), {report.words_written} mots dans la couche invisible, "
        f"{report.file_size / 1_048_576:.1f} Mo. Validation : {'réussie' if validation.passed else 'échouée'}.",
        title="PDF créé")
    st.table({"Contrôle": ["Nombre de pages", "Pages avec texte", "Texte invisible uniquement",
                           "Images non recompressées", "Écart de couleur max", "Mots témoins trouvés"],
              "Résultat": [f"{validation.page_count}/{validation.expected_pages}", str(validation.pages_with_text),
                           "oui" if validation.invisible_text_only else "non",
                           "oui" if validation.images_unrecompressed else "non",
                           f"{validation.max_color_delta}",
                           f"{sum(p.found for p in validation.probes)}/{len(validation.probes)}"]})
    common.show_issues(validation.issues + report.issues)
    output = Path(report.output)
    if output.is_file():
        st.download_button("Télécharger le PDF", data=output.read_bytes(), file_name=output.name,
                           mime="application/pdf", icon=":material/download:")
