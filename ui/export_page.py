"""PDF export, validation report, preview and download."""
from __future__ import annotations

import streamlit as st

from core.document_models import PageSize, PageStatus
from services import export_service

from . import state


def render(prj) -> None:
    st.header("Export PDF")
    if not prj.pages:
        st.info("Importez d’abord des pages (rubrique « Pages »).")
        return

    sizes = list(PageSize)
    page_size = st.selectbox(
        "Format des pages du livre PDF",
        sizes,
        index=sizes.index(prj.render.page_size),
        format_func=lambda value: "A4" if value == PageSize.A4 else (
            "A5" if value == PageSize.A5 else ("Lettre" if value == PageSize.LETTER else value.value)),
        key=state.k("export_page_size"),
    )
    if page_size != prj.render.page_size:
        prj.render.page_size = page_size
        state.save(prj)
    if page_size == PageSize.CUSTOM:
        st.caption(f"Format personnalisé actuel : {prj.render.custom_width_mm:g} × "
                   f"{prj.render.custom_height_mm:g} mm.")

    modes = list(export_service.MODE_LABELS)
    mode = st.radio("Format", modes, format_func=export_service.MODE_LABELS.get, horizontal=True,
                    key=state.k("export_mode"))
    selected = st.multiselect(
        "Pages à exporter",
        [page.id for page in prj.pages],
        default=[page.id for page in prj.pages],
        format_func=lambda page_id: state.page_label(prj, prj.page(page_id)),
        key=state.k("export_pages"),
    )
    pages = [page for page in prj.pages if page.id in selected]

    outdated = [page for page in pages if page.status != PageStatus.PROCESSED or page.ocr_outdated]
    if mode == "reconstruction" and outdated:
        st.warning(f"{len(outdated)} page(s) n’ont pas d’OCR à jour. "
                   "Elles ne contribueront pas au nouveau livre OCR.")
    if any(block.has_unvalidated_edits for page in pages for block in page.image_blocks):
        st.warning("Des réglages d’illustration ne sont pas validés : le PDF utilisera la dernière version validée.")

    preview_page_id = st.selectbox(
        "Aperçu d’une page",
        [page.id for page in pages],
        format_func=lambda page_id: state.page_label(prj, prj.page(page_id)),
        key=state.k("export_preview_page"),
        disabled=not pages,
    ) if pages else None
    if preview_page_id and st.button("Générer l’aperçu PDF", key=state.k("export_preview")):
        page = prj.page(preview_page_id)
        with state.guard("Aperçu PDF"), st.spinner("Rasterisation de l’aperçu…"):
            image, report = export_service.preview_png(prj, page, mode=mode)
            st.session_state["export_preview_result"] = (prj.id, page.id, mode, state.to_png(image),
                                                         report.warnings, report.overflows)
    preview = st.session_state.get("export_preview_result")
    if preview and preview[:3] == (prj.id, preview_page_id, mode):
        st.image(preview[3], caption="Aperçu rasterisé depuis le moteur PDF")
        for warning in preview[4]:
            st.warning(warning)
        for overflow in preview[5]:
            st.error(overflow)

    if st.button("Exporter le PDF", type="primary", disabled=not selected, key=state.k("export_run")):
        bar = st.progress(0.0, text="Préparation…")
        with state.guard("Export PDF"), st.status("Génération et vérification du PDF…", expanded=True) as status:
            def progress(fraction: float, message: str) -> None:
                bar.progress(min(max(fraction, 0.0), 1.0), text=message)
                status.write(message)

            result = export_service.export_pdf(prj, mode, selected, progress=progress)
            st.session_state["last_export"] = (prj.id, result.relative, result.mode, result.warnings,
                                                result.not_reconstructed, result.validation)
            status.update(label="PDF exporté et vérifié", state="complete")
            state.bump()
            st.rerun()

    _last_export(prj)
    _previous_exports(prj)


def _report(validation: dict) -> None:
    if not validation:
        st.info("Aucun rapport de validation n’est disponible.")
        return
    if validation.get("ok"):
        st.success(f"Validation réussie — {validation.get('page_count', 0)} page(s).")
    else:
        st.error("Le PDF comporte des erreurs de validation.")
    summary = validation.get("summary", {})
    columns = st.columns(4)
    columns[0].metric("Pages", validation.get("page_count", 0))
    columns[1].metric("Texte extrait", summary.get("text_chars", 0))
    columns[2].metric("Images", f"{summary.get('images_found', 0)} / {summary.get('images_expected', 0)}")
    similarity = summary.get("text_similarity_mean")
    columns[3].metric("Similarité du texte", f"{similarity:.0%}" if similarity is not None else "—")
    for error in validation.get("errors", []):
        st.error(error)
    for warning in validation.get("warnings", []):
        st.warning(warning)


def _last_export(prj) -> None:
    last = st.session_state.get("last_export")
    if not last or last[0] != prj.id:
        return
    _, relative, mode, warnings, not_reconstructed, validation = last
    st.subheader("Dernier export")
    for warning in warnings:
        st.warning(warning)
    if not_reconstructed:
        st.warning("Pages non reconstruites : " + ", ".join(str(page) for page in not_reconstructed))
    _report(validation)
    with state.guard("Téléchargement du PDF"):
        data = state.storage().ProjectStorage(prj.id).read_bytes(relative)
        st.download_button("Télécharger le PDF", data, file_name=relative.rsplit("/", 1)[-1],
                           mime="application/pdf", key=state.k("download_last", relative))
        report_relative = relative[:-4] + ".json"
        if state.storage().ProjectStorage(prj.id).exists(report_relative):
            report = state.storage().ProjectStorage(prj.id).read_bytes(report_relative)
            st.download_button("Télécharger le rapport JSON", report,
                               file_name=report_relative.rsplit("/", 1)[-1], mime="application/json",
                               key=state.k("download_report", relative))


def _previous_exports(prj) -> None:
    exports = export_service.list_exports(prj)
    if not exports:
        return
    st.subheader("Exports précédents")
    for item in exports:
        with st.expander(f"{item['name']} — {item['size'] / 1024:.0f} Ko"):
            if item.get("warnings"):
                for warning in item["warnings"]:
                    st.warning(warning)
            _report(item.get("validation", {}))
            with state.guard("Téléchargement d’un export précédent"):
                data = state.storage().ProjectStorage(prj.id).read_bytes(item["relative"])
                st.download_button("Télécharger", data, file_name=item["name"], mime="application/pdf",
                                   key=state.k("download_old", item["relative"]))
