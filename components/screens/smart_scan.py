"""Écran « Numérisation intelligente » : un livre = des photos → un PDF fidèle (photos traitées + OCR invisible).

Déroulé : 1. choisir ou créer le livre, donner toutes ses photos et les options de traitement ; 2. suivi du
traitement en arrière-plan ; 3. résultats (galerie avant/après ou tableau) et validation ; 4. retouche d'une
page ou de toutes les pages (fenêtres) ; 5. PDF du livre ; 6. statistiques des pages terminées (texte seul, avec
image, tableau, couverture) et montants selon les prix de l'onglet « Prix ».

Les boutons d'un même objectif sont regroupés sur une ligne (options de traitement, contrôle du traitement,
affichage des résultats, retouche, mise en page du PDF, création et téléchargement). L'écran n'appelle que des
services. Gemma (Ollama) est facultatif : son état affiché vient d'un test réel.
"""
from __future__ import annotations

import streamlit as st

from components import common
from components.price_summary import price_summary
from components.scan import bulk, retouch
from components.scan.shared import (FILTERS, FORMAT_LABELS, FORMAT_OPTIONS, GALLERY_PAGE_SIZE, GALLERY_SIDE,
                                    ORIENTATION_LABELS, SOURCE_LABELS, STATE_COLORS, STATE_LABELS, UPLOAD_TYPES,
                                    USE_AI_KEY, ai_status, cached_image, image_size, rerun_when_runner_stops,
                                    runner_caption, use_ai)
from models.scanning import PAGE_FORMAT_CUSTOM, PAGE_FORMAT_IMAGE, PAGE_KIND_LABELS, ScanBookSettings, ScanState
from repositories import scan_repository
from services import ocr_service, pricing_service, project_service, scanning_service
from workers import scan_runner

NEW_BOOK = "__nouveau__"
SOURCES = ["Téléverser les photos", "Dossier sur ce PC"]
RESULT_VIEWS = ["Galerie", "Tableau"]

ai_level, ai_message = ai_status()
ai_ready = ai_level == "ok"
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

with st.container(border=True):
    with st.container(horizontal=True, vertical_alignment="bottom"):
        choice = st.selectbox("Livre", options, key="scan_book_choice", width=420,
                              format_func=lambda value: "Nouveau livre…" if value == NEW_BOOK
                              else f"{books[value].name} — {books[value].page_count} page(s)")
        source = st.segmented_control("Photos du livre", SOURCES, default=SOURCES[0], key="scan_source")
    if choice != NEW_BOOK and choice != current_id:
        common.select_page(None)
        st.session_state["project_id"] = choice
        st.rerun()
    new_name, languages = "", []
    if choice == NEW_BOOK:
        name_column, language_column = st.columns([2, 1])
        new_name = name_column.text_input("Nom du nouveau livre", key="scan_new_name",
                                          placeholder="Ex. Blickpunkt Geschichte")
        installed = ocr_service.available_languages() or ["fra", "deu", "eng"]
        languages = language_column.multiselect("Langues du texte", [lang for lang in installed if lang != "osd"],
                                                default=[lang for lang in ("fra", "deu") if lang in installed],
                                                key="scan_new_languages")
    uploads, folder = [], ""
    if source == "Dossier sur ce PC":
        folder = st.text_input("Dossier contenant toutes les photos du livre",
                               placeholder="/home/…/photos/livre-01", key="scan_folder",
                               help="Les photos sont lues une à une (recommandé pour les gros livres).")
    else:
        uploads = st.file_uploader("Toutes les photos du livre (ordre des noms de fichiers)", type=UPLOAD_TYPES,
                                   accept_multiple_files=True,
                                   key=f"scan_uploads_{st.session_state.get('_scan_upload_round', 0)}")

    # Options de traitement, regroupées : elles s'appliquent pendant le traitement des pages.
    existing = project_service.open_project(choice) if choice != NEW_BOOK else None
    settings = scanning_service.load_book_settings(existing) if existing else ScanBookSettings()
    st.markdown("**Options de traitement**")
    with st.container(horizontal=True):
        run_ocr = st.toggle("Reconnaître le texte (OCR)", value=settings.run_ocr, key=f"scan_run_ocr_{choice}",
                            help="Activé : couche de texte invisible, le PDF est recherchable. Désactivé : pages "
                                 "redressées et nettoyées seulement (plus rapide, environ 5 à 9 s de moins par "
                                 "page), PDF sans texte recherchable.")
        remove_background = st.toggle("Supprimer le fond (papier blanc), sauf les images",
                                      value=settings.remove_background, key=f"scan_remove_bg_{choice}",
                                      help="Le papier devient blanc ; texte, photos, illustrations et encadrés "
                                           "colorés sont conservés.")
        st.toggle("Analyse Gemma pendant le traitement", value=bool(st.session_state.get(USE_AI_KEY, False)),
                  key="scan_use_ai_widget", disabled=not ai_ready,
                  on_change=lambda: st.session_state.__setitem__(USE_AI_KEY, st.session_state["scan_use_ai_widget"]),
                  help="Facultatif. Environ 2 minutes par page sur ce poste (mesuré) ; sans IA : ≈ 17 s.")
    # Seuls les réglages de traitement changent ici : ceux du PDF (étape 5) sont conservés.
    wanted_settings = settings.model_copy(update={"run_ocr": run_ocr, "remove_background": remove_background})
    if existing is not None and wanted_settings != settings:
        scanning_service.save_book_settings(existing, wanted_settings)
        if existing.pages:
            st.session_state["_scan_settings_changed"] = existing.id
    settings_changed = st.session_state.get("_scan_settings_changed") == choice
    if settings_changed:
        st.info("Réglage du fond ou de l'OCR modifié : retraitez le livre pour l'appliquer aux pages déjà traitées "
                "(les réglages faits à la main sont conservés).", icon=":material/info:")
    has_input = bool(uploads) or bool(folder.strip())
    label = "Importer et traiter le livre" if has_input else "Traiter le livre"
    if existing is not None and not has_input and settings_changed:
        label = "Retraiter tout le livre"
    go = st.button(label, type="primary", icon=":material/play_arrow:",
                   disabled=running or (choice == NEW_BOOK and not has_input))

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
        scan_runner.start([project.id], use_ai=use_ai())
    if result.ok:
        st.session_state["project_id"] = project.id
        st.session_state["_scan_select_book"] = project.id
        st.session_state["_scan_upload_round"] = st.session_state.get("_scan_upload_round", 0) + 1
        if report is not None:
            common.flash("success", f"{len(report.added_page_ids)} photo(s) importée(s) ; traitement lancé en "
                         "arrière-plan." + (f" {len(report.issues)} remarque(s) d'import." if report.issues else ""))
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
by_id = {page.id: page for page in project.pages}

# --------------------------------------------------------------------------- 2. traitement


@st.fragment(run_every=3 if running else None)
def processing_status() -> None:
    progress = scan_repository.progress(project.id)
    library = scan_repository.progress()
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
    runner_caption()
    rerun_when_runner_stops()


st.subheader("2. Traitement")
processing_status()
with st.container(horizontal=True):                     # contrôle du traitement en arrière-plan
    if st.button("Relancer les pages en erreur", icon=":material/replay:", disabled=running, key="process_retry"):
        with common.user_action("Relance des pages en erreur lancée."):
            scan_runner.start([project.id], retry_failed=True, use_ai=use_ai())
            st.rerun()
    if st.button("Arrêter le traitement", icon=":material/stop:", disabled=not running, key="process_stop"):
        scan_runner.cancel()
        common.flash("info", "Arrêt demandé : la page en cours se termine.")
        st.rerun()
with st.expander("État du système", icon=":material/monitor_heart:"):
    (st.success if ai_ready else st.warning if ai_level == "error" else st.info)(ai_message)
    tesseract = ocr_service.tesseract_version()
    if tesseract:
        missing = [lang for lang in project.languages if lang not in ocr_service.available_languages()]
        st.caption(f"OCR : Tesseract {tesseract} ; langues du livre : {', '.join(project.languages)}"
                   + (f" (absentes : {', '.join(missing)})" if missing else "") + ".")
    else:
        st.error("Tesseract est introuvable : la couche OCR du PDF ne peut pas être produite.")

# --------------------------------------------------------------------------- 3. résultats


def _validate_all() -> None:
    with common.user_action() as result:
        summary = scanning_service.validate_all(project)
    if result.ok:
        parts = [f"{len(summary.validated)} page(s) validée(s)"]
        if summary.already:
            parts.append(f"{len(summary.already)} déjà validée(s)")
        if summary.not_ready:
            shown = ", ".join(str(number) for number in summary.not_ready[:12])
            more = f" (+{len(summary.not_ready) - 12})" if len(summary.not_ready) > 12 else ""
            parts.append(f"{len(summary.not_ready)} pas prête(s) : page(s) {shown}{more}")
        common.flash("success" if not summary.not_ready else "warning", " · ".join(parts) + ".")
        st.rerun()


def _filtered_rows(wanted: list[ScanState] | None) -> list[dict]:
    return [row for row in scanning_service.book_overview(project)
            if wanted is None or ScanState(row["état"]) in wanted]


@st.fragment(run_every=5 if running else None)
def gallery(wanted: list[ScanState] | None) -> None:
    rows = _filtered_rows(wanted)
    if not rows:
        st.info("Aucune page dans ce filtre.")
        return
    pages_count = (len(rows) - 1) // GALLERY_PAGE_SIZE + 1
    shown_view = st.number_input(f"Vue (sur {pages_count})", 1, pages_count, 1, key="scan_gallery_view") \
        if pages_count > 1 else 1
    for row in rows[(shown_view - 1) * GALLERY_PAGE_SIZE: shown_view * GALLERY_PAGE_SIZE]:
        page = by_id.get(row["page_id"])
        if page is None:
            continue
        record = scanning_service.load_record(project, page)
        state = ScanState(row["état"])
        final_path = project_service.project_dir(project) / record.final_file if record.final_file else None
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"**Page {row['numéro']}** · {page.original_name}")
                st.badge(STATE_LABELS[state], color=STATE_COLORS[state])
                if record.validated:
                    st.badge("Validée", color="green", icon=":material/check:")
                if record.effective_kind is not None:
                    st.badge(PAGE_KIND_LABELS[record.effective_kind], color="violet",
                             icon=":material/edit:" if record.page_kind_override else None)
            before_column, after_column, changes_column = st.columns([1, 1, 1.2])
            page_paths = project_service.page_paths(project, page)
            original = (cached_image(page_paths.thumbnail, GALLERY_SIDE) if page_paths.thumbnail.is_file()
                        else cached_image(page_paths.original, GALLERY_SIDE, page.source_index))
            final = cached_image(final_path, GALLERY_SIDE)
            with before_column:
                if original is not None:
                    st.image(original, width="stretch", caption="Photo d'origine", alt=f"Page {row['numéro']} avant")
            with after_column:
                if final is not None:
                    st.image(final, width="stretch", caption=f"Page du PDF · {image_size(final_path)}",
                             alt=f"Page {row['numéro']} après")
                elif state in (ScanState.PENDING, ScanState.PROCESSING):
                    st.info("Traitement à venir…" if state == ScanState.PENDING else "Traitement en cours…",
                            icon=":material/hourglass_top:")
                else:
                    st.warning("Pas d'image finale.", icon=":material/image_not_supported:")
            with changes_column:
                if record.final_file:
                    st.markdown("**Modifications**")
                    for change in scanning_service.describe_changes(project, page, record):
                        st.markdown(f"- {change}")
                for reason in record.review_reasons:
                    st.warning(reason, icon=":material/rule:")
                if record.error:
                    st.error(record.error, icon=":material/error:")
                if st.button("Retoucher cette page", key=f"retouch_{page.id}", icon=":material/tune:"):
                    retouch.open_retouch(page.id)
    rerun_when_runner_stops()


def table(wanted: list[ScanState] | None) -> None:
    rows = _filtered_rows(wanted)
    selection = st.dataframe(
        [{**row, "état": STATE_LABELS[ScanState(row["état"])],
          "réglages": SOURCE_LABELS.get(row["réglages"], row["réglages"])} for row in rows],
        column_order=["numéro", "fichier", "état", "à jour", "validée", "réglages", "analyse", "proposition",
                      "OCR (mots)", "motifs"],
        hide_index=True, on_select="rerun", selection_mode="multi-row", key="scan_table", height=320,
        alt="Pages du livre et état de la numérisation")
    selected_ids = [rows[index]["page_id"] for index in selection.selection.rows] if selection.selection else []
    with st.container(horizontal=True, vertical_alignment="center"):          # actions sur la sélection
        if st.button("Traiter la sélection", icon=":material/play_arrow:", type="primary",
                     disabled=running or not selected_ids, key="scan_table_process"):
            with common.user_action("Traitement lancé en arrière-plan."):
                scan_runner.start([project.id], selected_ids, use_ai=use_ai())
                st.rerun()
        if st.button("Retoucher la page sélectionnée", icon=":material/tune:", disabled=len(selected_ids) != 1,
                     key="scan_table_retouch"):
            retouch.open_retouch(selected_ids[0])
        st.caption(f"{len(selected_ids)} page(s) sélectionnée(s).")


st.subheader("3. Résultats")
with st.container(horizontal=True, vertical_alignment="bottom"):             # affichage et validation
    view = st.segmented_control("Affichage", RESULT_VIEWS, default=RESULT_VIEWS[0], key="scan_results_view") \
        or RESULT_VIEWS[0]
    filter_label = st.segmented_control("Filtre", list(FILTERS), default="Toutes", key="scan_filter") or "Toutes"
    if st.button("Valider toutes les pages", icon=":material/done_all:", key="validate_all",
                 help="Valide chaque page dont l'image finale et le texte sont à jour, y compris les pages "
                      "« à vérifier ». Les pages en attente, en erreur ou obsolètes ne sont pas validées ; une "
                      "page reste dévalidable dans sa retouche."):
        _validate_all()
if view == "Tableau":
    table(FILTERS.get(filter_label))
else:
    gallery(FILTERS.get(filter_label))

# --------------------------------------------------------------------------- 4. retouche

st.subheader("4. Retouche")
st.caption("Une page : aperçu, recadrage à la souris, réglages, orientation et type, analyse Gemma. Toutes les "
           "pages : recadrage commun, rognage des bords, rotation. Les deux s'ouvrent dans une fenêtre.")
with st.container(horizontal=True, vertical_alignment="bottom"):
    chosen_page = common.page_selector(project, key="smart_scan")
    if st.button("Retoucher cette page", icon=":material/tune:", type="primary", key="retouch_open"):
        retouch.open_retouch(chosen_page.id)
    if st.button("Modifier toutes les pages…", icon=":material/select_all:", key="bulk_open"):
        bulk.open_bulk()

# --------------------------------------------------------------------------- 5. PDF du livre


def _pdf_label(export) -> str:
    report = export.report
    origin = "automatiquement" if export.automatic else "à la demande"
    return (f"{report.page_count} page(s) · {report.file_size / 1_048_576:.1f} Mo · créé {origin} le "
            f"{export.created_at.astimezone():%d/%m/%Y à %H:%M} · validation "
            f"{'réussie' if export.validation.passed else 'échouée'}")


def _download(export, *, primary: bool, label: str, key: str) -> None:
    path = scanning_service.book_pdf_path(project, export)
    st.download_button(label, data=lambda path=path: path.read_bytes(), file_name=path.name, mime="application/pdf",
                       icon=":material/download:", type="primary" if primary else "secondary", key=key,
                       on_click="ignore")


def _create_pdf() -> None:
    with common.user_action("PDF créé et enregistré : le téléchargement est prêt.") as result, \
            st.spinner("Création, enregistrement et contrôle du PDF…"):
        scanning_service.export_book_pdf(project)
    if result.ok:
        st.rerun()


def pdf_options() -> None:
    """Mise en page (format, orientation, marge, centrage) puis options d'export, enregistrées avec le livre."""
    book_settings = scanning_service.load_book_settings(project)
    with st.container(border=True):
        st.markdown("**Mise en page**")
        format_column, orientation_column, margin_column = st.columns([2, 3, 1], vertical_alignment="bottom")
        page_format = format_column.selectbox(
            "Format des pages du PDF", FORMAT_OPTIONS, index=FORMAT_OPTIONS.index(book_settings.page_format),
            format_func=lambda value: FORMAT_LABELS[value], key=f"scan_format_{project.id}",
            help="Taille de chaque page : la page PDF a la taille de la photo. Format commun : taille médiane du "
                 "livre. A4, A3… : chaque page est ajustée sans déformation dans le format, marges comprises.")
        fixed_size = page_format == PAGE_FORMAT_IMAGE
        orientation = orientation_column.segmented_control(
            "Orientation", list(ORIENTATION_LABELS), default=book_settings.orientation,
            format_func=lambda value: ORIENTATION_LABELS[value], key=f"scan_orientation_{project.id}",
            disabled=fixed_size) or book_settings.orientation
        margin_mm = margin_column.number_input("Marge (mm)", min_value=0.0, max_value=60.0, step=1.0,
                                               value=float(book_settings.margin_mm), key=f"scan_margin_{project.id}",
                                               disabled=fixed_size)
        custom_width, custom_height = book_settings.custom_width_mm, book_settings.custom_height_mm
        if page_format == PAGE_FORMAT_CUSTOM:
            width_column, height_column = st.columns(2)
            custom_width = width_column.number_input("Largeur (mm)", min_value=50.0, max_value=1200.0, step=1.0,
                                                     value=float(custom_width), key=f"scan_custom_w_{project.id}")
            custom_height = height_column.number_input("Hauteur (mm)", min_value=50.0, max_value=1200.0, step=1.0,
                                                       value=float(custom_height), key=f"scan_custom_h_{project.id}")
        center_content = st.toggle("Centrer le contenu sur la page", value=book_settings.center_content,
                                   key=f"scan_center_{project.id}", disabled=fixed_size,
                                   help="Sur les pages à fond supprimé, c'est le bloc de texte et d'images qui est "
                                        "centré (et non la photo entière).")
        st.markdown("**Options d'export**")
        with st.container(horizontal=True):
            include_unvalidated = st.toggle("Inclure les pages à vérifier non validées",
                                            value=book_settings.include_unvalidated,
                                            key=f"scan_include_{project.id}")
            auto_export = st.toggle("Créer le PDF automatiquement à la fin du traitement",
                                    value=book_settings.auto_export, key=f"scan_auto_export_{project.id}",
                                    help="Quand toutes les pages sont prêtes, le PDF est créé et enregistré sans clic.")
    wanted = book_settings.model_copy(update={
        "page_format": page_format, "orientation": orientation, "margin_mm": float(margin_mm),
        "custom_width_mm": float(custom_width), "custom_height_mm": float(custom_height),
        "center_content": center_content, "include_unvalidated": include_unvalidated, "auto_export": auto_export})
    if wanted != book_settings:
        scanning_service.save_book_settings(project, wanted)


@st.fragment(run_every=5 if running else None)
def book_pdf() -> None:
    settings = scanning_service.export_options(project)
    latest = scanning_service.latest_book_pdf(project)
    current = latest is not None and scanning_service.is_export_current(project, latest, settings)
    if current:
        (st.success if latest.validation.passed else st.warning)(_pdf_label(latest), title="PDF prêt",
                                                                    icon=":material/picture_as_pdf:")
        with st.container(horizontal=True, vertical_alignment="center"):           # télécharger ou recréer
            _download(latest, primary=True, label="Télécharger le PDF", key=f"pdf_download_{latest.signature}")
            if st.button("Recréer le PDF", icon=":material/refresh:", type="tertiary", disabled=running,
                         key="pdf_recreate"):
                _create_pdf()
        with st.expander("Contrôle du PDF", icon=":material/fact_check:"):
            validation = latest.validation
            st.table({"Contrôle": ["Nombre de pages", "Pages avec texte", "Texte invisible uniquement",
                                   "Images non recompressées", "Écart de couleur max", "Mots témoins trouvés"],
                      "Résultat": [f"{validation.page_count}/{validation.expected_pages}",
                                   str(validation.pages_with_text),
                                   "oui" if validation.invisible_text_only else "non",
                                   "oui" if validation.images_unrecompressed else "non",
                                   f"{validation.max_color_delta}",
                                   f"{sum(p.found for p in validation.probes)}/{len(validation.probes)}"]})
            common.show_issues(validation.issues + latest.report.issues)
    else:
        problems = scanning_service.export_problems(project, settings)
        if latest is not None:
            st.info(f"Le PDF précédent n'est plus à jour (pages, images ou options modifiées depuis) : "
                    f"{_pdf_label(latest)}.", icon=":material/update:")
        if problems:
            shown = "\n".join(f"- {problem}" for problem in problems[:8])
            more = f"\n- … et {len(problems) - 8} autre(s)" if len(problems) > 8 else ""
            st.warning(f"Le PDF sera créé quand toutes les pages seront prêtes :\n{shown}{more}",
                       icon=":material/hourglass_top:")
        with st.container(horizontal=True):                                         # créer ou reprendre l'ancien
            label = "Mettre à jour le PDF" if latest is not None else "Créer le PDF"
            if st.button(label, icon=":material/picture_as_pdf:", type="primary", disabled=running or bool(problems),
                         key="pdf_create"):
                _create_pdf()
            if latest is not None:
                _download(latest, primary=False, label="Télécharger l'ancien PDF", key=f"pdf_old_{latest.signature}")
    if running and scan_runner.status().export_message:
        st.caption(scan_runner.status().export_message)
    rerun_when_runner_stops()


st.subheader("5. PDF du livre")
st.caption("Un PDF par livre : photographies finales en couleur, couche OCR invisible, ordre du livre. "
           "Il est enregistré dans le projet ; le téléchargement s'affiche dès qu'il est prêt.")
pdf_options()
book_pdf()

# --------------------------------------------------------------------------- 6. statistiques et prix


@st.fragment(run_every=10 if running else None)
def book_totals() -> None:
    stats = scanning_service.book_statistics(project)
    prices = pricing_service.load_prices()
    st.caption(f"Pages terminées : {stats.completed_pages} sur {stats.total_pages} · à vérifier : "
               f"{stats.review_pages} · en erreur : {stats.failed_pages} · en attente : {stats.pending_pages}. "
               "Seules les pages terminées (ou validées) sont comptées ; prix réglables dans l'onglet « Prix ».")
    price_summary([pricing_service.estimate(stats, prices)], prices)


st.subheader("6. Statistiques et prix")
book_totals()
st.page_link(common.SCREEN_PRICING, label="Régler les prix", icon=":material/payments:")

# --------------------------------------------------------------------------- fenêtres (retouche, toutes les pages)

retouch.show_if_open(project)
bulk.show_if_open(project)
