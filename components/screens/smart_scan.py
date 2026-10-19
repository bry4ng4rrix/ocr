"""Écran « Numérisation intelligente » : un livre = des photos → un PDF fidèle (photos traitées + OCR invisible).

Déroulé : 1. choisir ou créer le livre et donner toutes ses photos ; 2. un bouton lance le traitement
(arrière-plan) ; 3. la galerie montre chaque page avant/après et la liste des modifications faites ;
4. retouche éventuelle d'une page ; 5. export du PDF du livre ; 6. statistiques des pages terminées
(texte seul, avec image, couverture) et montants selon les prix de l'onglet « Prix ».

L'écran n'appelle que des services. Gemma (Ollama) est facultatif : son état affiché vient d'un test réel,
ses propositions ne sont appliquées qu'après aperçu (sauf OLLAMA_AUTO_APPLY). Les curseurs recalculent un
aperçu local réduit (OpenCV) sans jamais solliciter Gemma.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import streamlit as st

import config
from components import common
from models.errors import AiUnavailableError, BookOcrError
from models.geometry import Point
from models.scanning import (PAGE_KIND_LABELS, PageKind, ScanAdjustments, ScanBookSettings, ScanState,
                             SearchablePdfReport, SearchablePdfValidation)
from components.crop_selector import crop_selector
from components.price_summary import price_summary
from repositories import scan_repository
from services import (image_analysis_service, image_service, ocr_service, ollama_service, pricing_service,
                      project_service, scanning_service)
from workers import scan_runner

PREVIEW_SIDE = 900
GALLERY_SIDE = 420
GALLERY_PAGE_SIZE = 8
CROP_SIDE = 1400                  # copie de la photo originale affichée dans le sélecteur de recadrage
CROP_DISPLAY_WIDTH = 1100
RETOUCH_TOOLS = ["Aperçu et commandes", "Recadrage", "Réglages", "Orientation et type", "Analyse Gemma"]
NEW_BOOK = "__nouveau__"
UPLOAD_TYPES = ["jpg", "jpeg", "png", "tif", "tiff", "pdf"]
STATE_LABELS = {ScanState.PENDING: "En attente", ScanState.PROCESSING: "En cours", ScanState.COMPLETED: "Terminée",
                ScanState.REVIEW_REQUIRED: "À vérifier", ScanState.FAILED: "En erreur"}
STATE_COLORS = {ScanState.COMPLETED: "green", ScanState.REVIEW_REQUIRED: "orange", ScanState.FAILED: "red",
                ScanState.PROCESSING: "blue", ScanState.PENDING: "gray"}
SOURCE_LABELS = {"neutral": "Aucune correction", "measured": "Mesures", "gemma": "Gemma + mesures",
                 "manual": "Manuel"}
FILTERS = {"Toutes": None, "À vérifier": [ScanState.REVIEW_REQUIRED], "En erreur": [ScanState.FAILED],
           "En attente": [ScanState.PENDING, ScanState.PROCESSING], "Terminées": [ScanState.COMPLETED]}
SLIDERS = {  # champ: (libellé, min, max, pas)
    "background_removal": ("Suppression du fond (images conservées)", 0.0, 100.0, 5.0),
    "paper_whitening": ("Nettoyage du fond (papier blanc)", 0.0, 100.0, 5.0),
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


@st.cache_data(max_entries=64, show_spinner=False)
def _image(path: str, stamp: int, max_side: int, source_index: int = 0) -> np.ndarray:
    return image_service.downscale(image_service.read_image(Path(path), source_index), max_side)[0]


def _cached(path: Path | None, max_side: int = PREVIEW_SIDE, source_index: int = 0) -> np.ndarray | None:
    if path is None or not path.is_file():
        return None
    try:
        return _image(str(path), path.stat().st_mtime_ns, max_side, source_index)
    except BookOcrError:
        return None


def _size(path: Path | None) -> str:
    if path is None or not path.is_file():
        return "—"
    try:
        from PIL import Image

        with Image.open(path) as image:
            return f"{image.width} × {image.height} px"
    except OSError:
        return "—"


def open_retouch(page_id: str) -> None:
    """Ouvre (ou garde ouverte) la fenêtre de retouche sur cette page, au prochain affichage de l'écran."""
    common.select_page(page_id)
    st.session_state["_scan_dialog_page"] = page_id
    st.rerun(scope="app")


def _run(label: str, success: str, action) -> None:
    """Action bloquante courte (une page) avec retour d'erreur en français."""
    with common.user_action(success) as result, st.spinner(label):
        action()
    if result.ok:
        st.rerun()


ai_level, ai_message = _ollama_check(config.OLLAMA_ENABLED and config.OLLAMA_ANALYSIS_ENABLED,
                                     config.OLLAMA_BASE_URL, ollama_service.configured_model())
ai_ready = ai_level == "ok"
tesseract = ocr_service.tesseract_version()
running = scan_runner.is_running()

# --------------------------------------------------------------------------- 1. livre et photos

st.subheader("1. Livre et photos")
books = {summary.id: summary for summary in project_service.list_projects()}
current_id = st.session_state.get("project_id")
options = [NEW_BOOK] + list(books)
pending = st.session_state.pop("_scan_select_book", None)       # livre créé à l'exécution précédente
if pending in books:
    st.session_state["scan_book_choice"] = pending
elif st.session_state.get("scan_book_choice") not in options:
    st.session_state["scan_book_choice"] = current_id if current_id in books else (options[1] if books else NEW_BOOK)
choice = st.selectbox(
    "Livre", options,
    format_func=lambda value: "Nouveau livre…" if value == NEW_BOOK
    else f"{books[value].name} — {books[value].page_count} page(s)",
    key="scan_book_choice")
if choice != NEW_BOOK and choice != current_id:
    common.select_page(None)
    st.session_state["project_id"] = choice
    st.rerun()

with st.container(border=True):
    if choice == NEW_BOOK:
        name_col, lang_col = st.columns([2, 1])
        new_name = name_col.text_input("Nom du nouveau livre", key="scan_new_name", placeholder="Ex. Blickpunkt Geschichte")
        installed = ocr_service.available_languages() or ["fra", "deu", "eng"]
        languages = lang_col.multiselect("Langues du texte", [lang for lang in installed if lang != "osd"],
                                         default=[lang for lang in ("fra", "deu") if lang in installed],
                                         key="scan_new_languages")
    source = st.segmented_control("Photos du livre", ["Téléverser les photos", "Dossier sur ce PC"],
                                  default="Téléverser les photos", key="scan_source")
    uploads, folder = [], ""
    if source == "Dossier sur ce PC":
        folder = st.text_input("Dossier contenant toutes les photos du livre",
                               placeholder="/home/…/photos/livre-01", key="scan_folder",
                               help="Les photos sont lues une à une (recommandé pour les gros livres).")
    else:
        uploads = st.file_uploader("Toutes les photos du livre (ordre des noms de fichiers)", type=UPLOAD_TYPES,
                                   accept_multiple_files=True,
                                   key=f"scan_uploads_{st.session_state.get('_scan_upload_round', 0)}")
    existing = project_service.open_project(choice) if choice != NEW_BOOK else None
    settings = scanning_service.load_book_settings(existing) if existing else ScanBookSettings()
    with st.container(horizontal=True):
        remove_background = st.toggle("Supprimer le fond (papier blanc), sauf les images", value=settings.remove_background,
                                      key=f"scan_remove_bg_{choice}",
                                      help="Le papier devient blanc ; texte, photos, illustrations et encadrés "
                                           "colorés sont conservés.")
        center_content = st.toggle("Centrer le contenu dans le PDF", value=settings.center_content,
                                   key=f"scan_center_{choice}",
                                   help="Sur les pages à fond supprimé, le bloc de texte et d'images est centré "
                                        "sur une page PDF de format commun.")
    wanted_settings = ScanBookSettings(remove_background=remove_background, center_content=center_content)
    if existing is not None and wanted_settings != settings:
        scanning_service.save_book_settings(existing, wanted_settings)
        if wanted_settings.remove_background != settings.remove_background and existing.pages:
            st.session_state["_scan_settings_changed"] = existing.id
    if st.session_state.get("_scan_settings_changed") == choice:
        st.info("Réglage du fond modifié : retraitez le livre pour l'appliquer aux pages déjà traitées "
                "(les réglages faits à la main sont conservés).", icon=":material/info:")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        use_ai = st.toggle("Analyse Gemma pendant le traitement", value=False, disabled=not ai_ready,
                           help="Facultatif. Environ 2 minutes par page sur ce poste (mesuré) ; sans IA : ≈ 17 s.")
        has_input = bool(uploads) or bool(folder.strip())
        label = "Importer et traiter le livre" if has_input else "Traiter le livre"
        if existing is not None and not has_input and st.session_state.get("_scan_settings_changed") == choice:
            label = "Retraiter tout le livre"
        go = st.button(label, type="primary", icon=":material/play_arrow:", disabled=running or (
            choice == NEW_BOOK and not has_input))

if go:
    with common.user_action() as result, st.spinner("Import des photos…"):
        project = scanning_service.create_book(new_name, languages) if choice == NEW_BOOK \
            else project_service.open_project(choice)
        scanning_service.save_book_settings(project, wanted_settings)
        if label == "Retraiter tout le livre":
            scanning_service.requeue_book(project)
            st.session_state.pop("_scan_settings_changed", None)
        report = None
        if uploads:
            report = scanning_service.import_book_files(project, [(file.name, file.getvalue()) for file in uploads])
        elif folder.strip():
            report = scanning_service.import_book_folder(project, folder.strip())
        else:
            scanning_service.sync_queue(project)
        scan_runner.start([project.id], use_ai=bool(use_ai))
    if result.ok:
        st.session_state["project_id"] = project.id
        st.session_state["_scan_select_book"] = project.id
        st.session_state["_scan_upload_round"] = st.session_state.get("_scan_upload_round", 0) + 1
        if report is not None:
            common.flash("success", f"{len(report.added_page_ids)} photo(s) importée(s) ; traitement lancé en arrière-plan."
                         + (f" {len(report.issues)} remarque(s) d'import." if report.issues else ""))
        else:
            common.flash("success", "Traitement lancé en arrière-plan.")
        st.rerun()

project = common.load_project() if choice != NEW_BOOK else None
if project is None:
    st.info("Choisissez un livre ou créez-en un, ajoutez ses photos puis lancez le traitement.",
            icon=":material/menu_book:")
    st.stop()
if not project.pages:
    st.info("Ce livre n'a encore aucune photo : ajoutez-les ci-dessus.", icon=":material/add_photo_alternate:")
    st.stop()
scanning_service.sync_queue(project)

# --------------------------------------------------------------------------- 2. traitement


@st.fragment(run_every=3 if running else None)
def processing_status() -> None:
    progress = scan_repository.progress(project.id)
    library = scan_repository.progress()
    runner = scan_runner.status()
    with st.container(horizontal=True):
        st.metric("Pages", len(project.pages), border=True)
        st.metric("Terminées", progress.counts.get(ScanState.COMPLETED, 0), border=True)
        st.metric("À vérifier", progress.counts.get(ScanState.REVIEW_REQUIRED, 0), border=True)
        st.metric("En erreur", progress.counts.get(ScanState.FAILED, 0), border=True)
        st.metric("En attente", progress.counts.get(ScanState.PENDING, 0) + progress.counts.get(ScanState.PROCESSING, 0),
                  border=True)
    st.progress(progress.done / max(1, progress.total), text=f"Livre : {progress.done}/{progress.total} page(s)")
    if library.total > progress.total:
        st.progress(library.done / max(1, library.total),
                    text=f"Bibliothèque : {library.done}/{library.total} page(s)")
    if runner.running:
        speed = f" — {runner.seconds_per_page:.0f} s/page mesurées" if runner.seconds_per_page else ""
        st.caption(f"Traitement en arrière-plan : {runner.processed} page(s) faite(s){speed}. "
                   "La galerie se met à jour toute seule.")
    elif runner.message and runner.started_at:
        st.caption(f"Dernier traitement : {runner.message} ({runner.processed} page(s), {runner.failed} en échec).")
    was_running = st.session_state.get("_scan_was_running", False)
    st.session_state["_scan_was_running"] = runner.running
    if was_running and not runner.running:
        st.rerun(scope="app")


st.subheader("2. Traitement")
processing_status()
with st.container(horizontal=True):
    if st.button("Relancer les pages en erreur", icon=":material/replay:", disabled=running):
        with common.user_action("Relance des pages en erreur lancée."):
            scan_runner.start([project.id], retry_failed=True, use_ai=bool(use_ai))
            st.rerun()
    if st.button("Arrêter", icon=":material/stop:", disabled=not running):
        scan_runner.cancel()
        common.flash("info", "Arrêt demandé : la page en cours se termine.")
        st.rerun()
with st.expander("État du système", icon=":material/monitor_heart:"):
    (st.success if ai_ready else st.warning if ai_level == "error" else st.info)(ai_message)
    if tesseract:
        missing = [lang for lang in project.languages if lang not in ocr_service.available_languages()]
        st.caption(f"OCR : Tesseract {tesseract} ; langues du livre : {', '.join(project.languages)}"
                   + (f" (absentes : {', '.join(missing)})" if missing else "") + ".")
    else:
        st.error("Tesseract est introuvable : la couche OCR du PDF ne peut pas être produite.")

# --------------------------------------------------------------------------- 3. résultats (galerie)

overview = scanning_service.book_overview(project)
by_id = {page.id: page for page in project.pages}


@st.fragment(run_every=5 if running else None)
def gallery() -> None:
    filter_label = st.segmented_control("Afficher", list(FILTERS), default="Toutes", key="scan_filter")
    wanted = FILTERS.get(filter_label or "Toutes")
    rows = [row for row in scanning_service.book_overview(project)
            if wanted is None or ScanState(row["état"]) in wanted]
    if not rows:
        st.info("Aucune page dans ce filtre.")
        return
    pages_count = (len(rows) - 1) // GALLERY_PAGE_SIZE + 1
    view = st.number_input(f"Vue (sur {pages_count})", 1, pages_count, 1, key="scan_gallery_view") \
        if pages_count > 1 else 1
    for row in rows[(view - 1) * GALLERY_PAGE_SIZE: view * GALLERY_PAGE_SIZE]:
        page = by_id.get(row["page_id"])
        if page is None:
            continue
        record = scanning_service.load_record(project, page)
        state = ScanState(row["état"])
        root = project_service.project_dir(project)
        final_path = root / record.final_file if record.final_file else None
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"**Page {row['numéro']}** · {page.original_name}")
                st.badge(STATE_LABELS[state], color=STATE_COLORS[state])
                if record.validated:
                    st.badge("Validée", color="green", icon=":material/check:")
                if record.effective_kind is not None:
                    st.badge(PAGE_KIND_LABELS[record.effective_kind], color="violet",
                             icon=":material/edit:" if record.page_kind_override else None)
            before_col, after_col, changes_col = st.columns([1, 1, 1.2])
            page_paths = project_service.page_paths(project, page)
            original = (_cached(page_paths.thumbnail, GALLERY_SIDE) if page_paths.thumbnail.is_file()
                        else _cached(page_paths.original, GALLERY_SIDE, page.source_index))
            final = _cached(final_path, GALLERY_SIDE)
            with before_col:
                if original is not None:
                    st.image(original, width="stretch", caption="Photo d'origine", alt=f"Page {row['numéro']} avant")
            with after_col:
                if final is not None:
                    st.image(final, width="stretch", caption=f"Page du PDF · {_size(final_path)}",
                             alt=f"Page {row['numéro']} après")
                elif state in (ScanState.PENDING, ScanState.PROCESSING):
                    st.info("Traitement à venir…" if state == ScanState.PENDING else "Traitement en cours…",
                            icon=":material/hourglass_top:")
                else:
                    st.warning("Pas d'image finale.", icon=":material/image_not_supported:")
            with changes_col:
                if record.final_file:
                    st.markdown("**Modifications**")
                    for change in scanning_service.describe_changes(project, page, record):
                        st.markdown(f"- {change}")
                for reason in record.review_reasons:
                    st.warning(reason, icon=":material/rule:")
                if record.error:
                    st.error(record.error, icon=":material/error:")
                if st.button("Retoucher cette page", key=f"retouch_{page.id}", icon=":material/tune:"):
                    open_retouch(page.id)


st.subheader("3. Résultats")
gallery()

# --------------------------------------------------------------------------- 4. retouche d'une page (fenêtre)

st.subheader("4. Retouche d'une page")
st.caption("Choisissez une page ici, ou cliquez sur « Retoucher cette page » dans les résultats : la retouche "
           "s'ouvre dans une fenêtre (aperçu, recadrage à la souris, réglages, orientation et type).")
with st.container(horizontal=True, vertical_alignment="bottom"):
    chosen_page = common.page_selector(project, key="smart_scan")
    if st.button("Ouvrir la retouche", icon=":material/tune:", type="primary"):
        open_retouch(chosen_page.id)


def _retouch_preview(page, record, paths, final_path) -> None:
    view = st.segmented_control("Affichage", ["Trois étapes", "Avant / après", "Zoom 100 %"],
                                default="Trois étapes", key="scan_view")
    straightened = _cached(paths.corrected)
    final = _cached(final_path)
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
                    st.caption(_size(path))


def _retouch_commands(page, record, busy: bool, has_straightened: bool) -> None:
    with st.container(horizontal=True):
        if st.button("Traiter cette page", icon=":material/play_arrow:", type="primary", disabled=busy):
            _run("Traitement de la page…", "Page traitée.",
                 lambda: scanning_service.process_page(project, page.id, use_ai=ai_ready and bool(use_ai)))
        if st.button("Analyser avec Gemma", icon=":material/smart_toy:",
                     disabled=busy or not ai_ready or not has_straightened,
                     help="Analyse mise en cache : relancée seulement si l'image ou le modèle change."):
            _run("Analyse par Gemma (environ 2 minutes)…", "Analyse terminée.",
                 lambda: scanning_service.analyse(project, page.id))
        if st.button("Détecter les bords à nouveau", icon=":material/crop_free:", disabled=busy):
            _run("Détection géométrique…", "Géométrie recalculée.",
                 lambda: scanning_service.redetect_geometry(project, page.id))
        if st.button("Améliorer automatiquement", icon=":material/auto_fix_high:", disabled=busy or not has_straightened):
            _run("Amélioration mesurée…", "Amélioration appliquée.",
                 lambda: scanning_service.auto_enhance(project, page.id))
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


def _retouch_crop(page, busy: bool) -> None:
    """Recadrage manuel à la souris : rectangle ou 4 coins (perspective) sur la photographie originale."""
    st.caption("Tracez la zone de la page sur la photo d'origine, ou passez en « 4 coins » pour suivre une page "
               "photographiée en biais, puis cliquez sur « Appliquer le recadrage ». La perspective, l'orientation "
               "et l'amélioration sont ensuite recalculées pour cette page.")
    original = common.original_image(project, page, CROP_SIDE)
    if original is None:
        st.warning("Photographie originale illisible.", icon=":material/image_not_supported:")
        return
    current = page.geometry.effective_quad()
    rectangular = current is None or (abs(current[0].y - current[1].y) < 0.002 and abs(current[0].x - current[3].x) < 0.002
                                      and abs(current[2].y - current[3].y) < 0.002 and abs(current[1].x - current[2].x) < 0.002)
    corners = crop_selector(common.jpeg_data_uri(original), current, key=f"crop_{page.id}",
                            width=CROP_DISPLAY_WIDTH, mode="rect" if rectangular else "quad",
                            alt=f"Photographie originale de la page {project.page_number(page.id)}")
    if corners is not None and not busy:
        _run("Recadrage et nouveau traitement de la page…", "Recadrage manuel appliqué.",
             lambda: scanning_service.set_manual_quad(project, page.id, corners))
    origin = "manuel" if page.geometry.manual_quad is not None else (
        "automatique" if page.geometry.detected_quad is not None else "aucun (photo entière)")
    st.caption(f"Contour actuel : {origin}.")
    if page.geometry.manual_quad is not None and st.button("Revenir au contour automatique", disabled=busy,
                                                           icon=":material/auto_fix_high:"):
        _run("Recadrage…", "Contour automatique rétabli.",
             lambda: scanning_service.set_manual_quad(project, page.id, None))
    with st.expander("Saisie numérique des coins (alternative au clavier)"):
        quad = current or [Point(x=0, y=0), Point(x=1, y=0), Point(x=1, y=1), Point(x=0, y=1)]
        with st.form(f"scan_quad_{page.id}", border=False):
            values = []
            for index, (corner, label) in enumerate(zip(quad, ["Haut-gauche", "Haut-droit", "Bas-droit",
                                                               "Bas-gauche"])):
                cx, cy = st.columns(2)
                x = cx.number_input(f"{label} x", 0.0, 1.0, float(corner.x), 0.005, format="%.3f",
                                    key=f"q{index}x_{page.id}")
                y = cy.number_input(f"{label} y", 0.0, 1.0, float(corner.y), 0.005, format="%.3f",
                                    key=f"q{index}y_{page.id}")
                values.append(Point(x=x, y=y))
            apply_quad = st.form_submit_button("Appliquer ces coins", disabled=busy)
        if apply_quad:
            _run("Recadrage…", "Recadrage manuel appliqué.",
                 lambda: scanning_service.set_manual_quad(project, page.id, values))


def _retouch_adjustments(page, record, busy: bool, has_straightened: bool) -> None:
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
            _run("Application…", "Réglages enregistrés et appliqués.",
                 lambda: scanning_service.render_final(project, page.id, adjustments, source="manual"))


def _retouch_orientation_kind(page, record, busy: bool) -> None:
    st.caption(f"Orientation automatique : {page.geometry.orientation_method or '—'}"
               f"{' (incertaine)' if page.geometry.orientation_uncertain else ''} ; quarts de tour appliqués : "
               f"{page.geometry.effective_quarter_turns()}.")
    turns = st.segmented_control("Rotation manuelle (après l'automatique)", [0, 1, 2, 3],
                                 default=page.geometry.manual_quarter_turns,
                                 format_func=lambda value: f"{value * 90}°", key=f"scan_turns_{page.id}")
    if st.button("Confirmer la rotation", disabled=busy or turns is None or turns == page.geometry.manual_quarter_turns):
        _run("Rotation…", "Rotation appliquée.", lambda: scanning_service.set_rotation(project, page.id, int(turns)))
    kind_options = [None, *PageKind]
    kind_choice = st.selectbox(
        "Type de page (statistiques et prix)", kind_options,
        index=kind_options.index(record.page_kind_override),
        format_func=lambda kind: (f"Automatique : {PAGE_KIND_LABELS[record.page_kind]}" if record.page_kind
                                  else "Automatique (après traitement)") if kind is None else PAGE_KIND_LABELS[kind],
        key=f"scan_kind_{page.id}", disabled=busy)
    if kind_choice != record.page_kind_override:
        _run("Enregistrement…", "Type de page enregistré.",
             lambda: scanning_service.set_page_kind(project, page.id, kind_choice))


def _retouch_analysis(page, record, busy: bool) -> None:
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
        if st.button("Appliquer la proposition", icon=":material/check:", disabled=busy):
            _run("Application…", "Proposition appliquée.", lambda: scanning_service.apply_proposal(project, page.id))


def _close_retouch() -> None:
    st.session_state.pop("_scan_dialog_page", None)


@st.dialog("Retoucher la page", width="large", on_dismiss=_close_retouch)
def retouch_dialog(page_id: str) -> None:
    page = project.page(page_id)
    record = scanning_service.load_record(project, page)
    paths = project_service.page_paths(project, page)
    final_path = project_service.project_dir(project) / record.final_file if record.final_file else None
    busy = running and scan_runner.status().current_page == page.id
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
            _close_retouch()
            st.rerun()
    if busy:
        st.info("Cette page est en cours de traitement en arrière-plan : les commandes sont désactivées.",
                icon=":material/hourglass_top:")
    for reason in record.review_reasons:
        st.warning(reason, icon=":material/rule:")
    if record.error:
        st.error(record.error, icon=":material/error:")

    tool = st.segmented_control("Outil", RETOUCH_TOOLS, default=RETOUCH_TOOLS[0], key="retouch_tool")
    if tool == "Recadrage":
        _retouch_crop(page, busy)
    elif tool == "Réglages":
        _retouch_adjustments(page, record, busy, has_straightened)
    elif tool == "Orientation et type":
        _retouch_orientation_kind(page, record, busy)
    elif tool == "Analyse Gemma":
        _retouch_analysis(page, record, busy)
    else:
        _retouch_preview(page, record, paths, final_path)
        _retouch_commands(page, record, busy, has_straightened)


with st.expander("Tableau des pages et sélection", icon=":material/table_rows:"):
    table = st.dataframe(
        [{**row, "état": STATE_LABELS[ScanState(row["état"])],
          "réglages": SOURCE_LABELS.get(row["réglages"], row["réglages"])} for row in overview],
        column_order=["numéro", "fichier", "état", "à jour", "validée", "réglages", "analyse", "proposition",
                      "OCR (mots)", "motifs"],
        hide_index=True, on_select="rerun", selection_mode="multi-row", key="scan_table", height=260,
        alt="Pages du livre et état de la numérisation")
    selected_ids = [overview[index]["page_id"] for index in table.selection.rows] if table.selection else []
    if st.button("Traiter la sélection", icon=":material/play_arrow:", disabled=running or not selected_ids):
        with common.user_action("Traitement lancé en arrière-plan."):
            scan_runner.start([project.id], selected_ids, use_ai=bool(use_ai))
            st.rerun()

# --------------------------------------------------------------------------- 5. PDF du livre

st.subheader("5. PDF du livre")
st.caption("Un PDF par livre : photographies finales en couleur, couche OCR invisible, ordre du livre. "
           "Toutes les pages doivent être traitées et à jour.")
with st.container(horizontal=True):
    uniform = st.toggle("Pages de même format (marge blanche)", value=True, key="scan_uniform",
                        help="Toutes les pages PDF ont le même format ; chaque page y est centrée. Avec « Centrer le "
                             "contenu » (étape 1), c'est le bloc de texte et d'images qui est centré.")
    include_unvalidated = st.toggle("Inclure les pages à vérifier non validées", value=True, key="scan_include")
if st.button("Exporter le PDF du livre", icon=":material/picture_as_pdf:", type="primary", disabled=running):
    with common.user_action() as result, st.spinner("Création et contrôle du PDF…"):
        report, validation = scanning_service.export_book_pdf(project, include_unvalidated=include_unvalidated,
                                                              uniform=uniform)
    if result.ok:
        st.session_state["scan_last_export"] = (report.model_dump_json(), validation.model_dump_json())
last = st.session_state.get("scan_last_export")
if last:
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

# --------------------------------------------------------------------------- 6. statistiques et prix

st.subheader("6. Statistiques et prix")


@st.fragment(run_every=10 if running else None)
def book_totals() -> None:
    stats = scanning_service.book_statistics(project)
    prices = pricing_service.load_prices()
    st.caption(f"Pages terminées : {stats.completed_pages} sur {stats.total_pages} · à vérifier : "
               f"{stats.review_pages} · en erreur : {stats.failed_pages} · en attente : {stats.pending_pages}. "
               "Seules les pages terminées (ou validées) sont comptées ; prix réglables dans l'onglet « Prix ».")
    price_summary([pricing_service.estimate(stats, prices)], prices)


book_totals()
st.page_link(common.SCREEN_PRICING, label="Régler les prix", icon=":material/payments:")

# --------------------------------------------------------------------------- fenêtre de retouche

if st.session_state.get("_scan_dialog_page") in by_id:
    retouch_dialog(st.session_state["_scan_dialog_page"])
