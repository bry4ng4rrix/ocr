"""Écran H — Export : mode de reconstruction (D-06), format, options, DOCX / PDF reconstruit / PDF fac-similé
recherchable / PDF via LibreOffice, rapport de validation, aperçu des pages du PDF, téléchargement."""
from __future__ import annotations

import shutil
from pathlib import Path

import streamlit as st
from PIL import Image

import config
from components import common
from models import PageStatus, ReconstructionMode
from models.errors import BookOcrError
from models.project import MODE_LABELS
from services import export_service, pdf_service, project_service

MODE_EXPLANATIONS = {
    ReconstructionMode.FIDELITY: (
        "Le texte est placé **à la position et à la taille détectées** (cadres), les images sont ancrées et le "
        "fond graphique est conservé derrière le texte. **Apparence la plus proche de l'original.** Un passage "
        "modifié et allongé peut déborder de son cadre (signalé dans le rapport) ; le rendu diffère un peu entre "
        "Word et LibreOffice."),
    ReconstructionMode.EDITABLE: (
        "Le texte forme un **flux de paragraphes stylés** dans l'ordre de lecture ; les images sont dans le flux "
        "à leur taille d'origine. **Le plus facile à modifier et à restyler**, mais les positions exactes sont "
        "perdues : le texte reflue."),
    ReconstructionMode.HYBRID: (
        "Le **texte courant est en flux** (modifiable) ; le texte posé sur un fond ou une image reste **en cadre**, "
        "images et fonds sont ancrés. **Compromis** entre apparence et édition ; certains habillages diffèrent "
        "selon le logiciel."),
}
FORMAT_LABELS = {"detected": "Format détecté de chaque page", "A4": "A4 (210 × 297 mm)", "A5": "A5 (148 × 210 mm)",
                 "custom": "Personnalisé"}
OVERFLOW_LABELS = {"shrink": "Réduire la police (jusqu'à 70 %)", "report": "Signaler seulement"}
SETTINGS_FIELDS = ["mode", "page_format", "custom_width_mm", "custom_height_mm", "scale_fonts_to_format",
                   "keep_backgrounds", "include_headers_footers", "include_page_numbers", "overflow_policy"]

project = common.require_project()
common.require_pages(project)
soffice_available = shutil.which(config.SOFFICE_CMD) is not None
by_id = {page.id: page for page in project.pages}


@st.cache_data(max_entries=24, show_spinner=False)
def _pdf_page(path: str, stamp: int, index: int, width: int) -> Image.Image:
    return pdf_service.rasterize_pdf_page(Path(path), index, width_px=width)


def _pdf_preview(path: Path, key: str) -> None:
    """Aperçu des pages d'un PDF exporté (rasterisé par pdf_service)."""
    try:
        count = pdf_service.pdf_page_count(path)
    except BookOcrError as error:
        st.error(error.user_message)
        return
    if count == 0:
        st.warning("Le PDF ne contient aucune page.")
        return
    number = st.slider("Page du PDF", 1, count, 1, key=key) if count > 1 else 1
    try:
        st.image(_pdf_page(str(path), path.stat().st_mtime_ns, int(number) - 1, 700), width=700,
                 alt=f"Page {number} du PDF exporté")
    except BookOcrError as error:
        st.error(error.user_message)
    st.caption(f"Page {number} sur {count}.")


def _download(path: Path, key: str) -> None:
    mime = "application/pdf" if path.suffix == ".pdf" else \
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    st.download_button(f"Télécharger {path.name}", data=path.read_bytes(), file_name=path.name, mime=mime,
                       icon=":material/download:", key=key, on_click="ignore", type="primary")


# --------------------------------------------------------------------------- réglages

settings = project.export
common.sync_widgets("export", settings.model_dump(include=set(SETTINGS_FIELDS)), settings.model_dump_json())

st.subheader("Mode de reconstruction")
mode = st.segmented_control("Mode", list(ReconstructionMode), format_func=common.MODE_SHORT_LABELS.get, required=True,
                            key="export_mode")
st.info(f"**{MODE_LABELS[mode]}** — {MODE_EXPLANATIONS[mode]}", icon=":material/info:")
with st.expander("Comparer les trois modes", icon=":material/compare:"):
    st.table([{"Mode": common.MODE_SHORT_LABELS[item], "Texte, images et compromis": MODE_EXPLANATIONS[item].replace("**", "")}
              for item in ReconstructionMode], hide_index=True)

st.subheader("Format et options")
with st.container(border=True):
    left, right = st.columns(2)
    page_format = left.selectbox("Format des pages", list(FORMAT_LABELS), format_func=FORMAT_LABELS.get,
                                 key="export_page_format")
    with left.container(horizontal=True):
        st.number_input("Largeur (mm)", min_value=31.0, max_value=1000.0, step=1.0, key="export_custom_width_mm",
                        disabled=page_format != "custom")
        st.number_input("Hauteur (mm)", min_value=31.0, max_value=1000.0, step=1.0, key="export_custom_height_mm",
                        disabled=page_format != "custom")
    left.checkbox("Adapter la taille des polices au format", key="export_scale_fonts_to_format",
                  help="Les tailles sont proportionnelles si le format de sortie diffère du format détecté.")
    right.checkbox("Conserver les fonds graphiques", key="export_keep_backgrounds")
    right.checkbox("Inclure les en-têtes et pieds de page", key="export_include_headers_footers")
    right.checkbox("Inclure les numéros de page", key="export_include_page_numbers")
    right.segmented_control("Texte trop long pour sa zone (PDF)", list(OVERFLOW_LABELS),
                            format_func=OVERFLOW_LABELS.get, required=True, key="export_overflow_policy")
    values = common.widget_values("export", SETTINGS_FIELDS)
    pending = {name: value for name, value in values.items() if getattr(settings, name) != value}
    if st.button("Enregistrer les réglages d'export", icon=":material/save:", disabled=not pending):
        with common.user_action("Réglages d'export enregistrés.") as result:
            with common.editing(project.id) as fresh:
                fresh.export = fresh.export.model_validate({**fresh.export.model_dump(), **values})
                project_service.save_project(fresh)
        if result.ok:
            st.rerun()
    if pending:
        st.caption("Réglages modifiés : enregistrez-les avant d'exporter (l'export utilise les réglages enregistrés).")

# --------------------------------------------------------------------------- export

st.subheader("Exporter")
scope = st.segmented_control("Pages", ["all", "selection"], required=True, default="all", key="export_scope",
                             format_func={"all": "Tout le livre", "selection": "Pages choisies"}.get)
page_ids = None
if scope == "selection":
    chosen = st.multiselect("Pages à exporter", list(by_id), key="export_pages",
                            format_func=lambda page_id: common.page_label(project, by_id[page_id]))
    page_ids = [page.id for page in project.pages if page.id in set(chosen)]
not_analysed = sum(page.status != PageStatus.ANALYSED for page in project.pages
                   if page_ids is None or page.id in page_ids)
if not_analysed:
    st.warning(f"{not_analysed} page(s) ne sont pas analysées : elles seront exportées vides (signalées dans le "
               "rapport), jamais remplacées par la photo.", icon=":material/warning:")
blocked = bool(pending) or page_ids == []
EXPORTS = {
    "docx": ("DOCX modifiable", ":material/description:", lambda: export_service.export_docx(project, None, page_ids)),
    "pdf": ("PDF reconstruit", ":material/picture_as_pdf:", lambda: export_service.export_pdf(project, None, page_ids)),
    "facsimile": ("PDF fac-similé recherchable", ":material/image_search:",
                  lambda: export_service.export_pdf(project, None, page_ids, searchable_facsimile=True)),
    "libreoffice": ("PDF via LibreOffice (DOCX → PDF)", ":material/transform:",
                    lambda: export_service.export_pdf_via_docx(project, None, page_ids)),
}
with st.container(horizontal=True):
    for name, (label, icon, run) in EXPORTS.items():
        unavailable = name == "libreoffice" and not soffice_available
        if st.button(label, icon=icon, key=f"export_{name}", disabled=blocked or unavailable,
                     type="primary" if name == "docx" else "secondary"):
            with common.user_action() as result:
                with st.spinner(f"Export en cours : {label}…"):
                    outcome = run()
            if result.ok:
                st.session_state["export_result"] = {"kind": name, "outcome": outcome}
st.caption("Fac-similé : chaque page est la photo corrigée, avec le texte reconnu invisible par-dessus "
           "(recherche et copie possibles). PDF reconstruit : texte réel, polices intégrées, même rendu que l'aperçu.")
if not soffice_available:
    st.caption("PDF via LibreOffice indisponible : LibreOffice (soffice) n'est pas installé sur cette machine "
               "(sudo pacman -S libreoffice-fresh).")

# --------------------------------------------------------------------------- rapport


def _show_outcome(kind: str, outcome: export_service.ExportOutcome) -> None:
    report, validation, inspection = outcome.report, outcome.validation, outcome.docx_inspection
    if outcome.ok:
        st.success(f"Export terminé : {outcome.path.name}", icon=":material/check_circle:")
    elif outcome.path is None:
        st.error("L'export n'a pas produit de fichier.", icon=":material/error:")
    else:
        st.error(f"Export produit avec des erreurs : {outcome.path.name} (voir le rapport).", icon=":material/error:")
    if report is not None:
        with st.container(horizontal=True):
            st.metric("Pages", report.page_count, border=True)
            st.metric("Blocs de texte", report.text_blocks, border=True)
            st.metric("Images", report.images, border=True)
            st.metric("Débordements", len(report.overflows), border=True)
            st.metric("Replis", len(report.fallbacks), border=True)
    if outcome.conversion is not None:
        st.caption(f"Conversion LibreOffice : {outcome.conversion.message} ({outcome.conversion.duration_s:.0f} s)")
    if validation is not None:
        st.markdown(f"**Validation du PDF relu** : {validation.page_count} page(s)"
                    + (f" pour {validation.expected_page_count} attendue(s)" if validation.expected_page_count
                       else ""))
        st.dataframe([{"Page": item.page_number, "Caractères": item.chars, "Images": item.images,
                       "Vide": "oui" if item.blank else "",
                       "Texte retrouvé": f"{item.text_similarity:.0%}" if item.text_similarity is not None else "—"}
                      for item in validation.pages], hide_index=True, alt="Validation page par page du PDF")
    if inspection is not None:
        st.markdown(f"**DOCX relu** : {inspection.sections} section(s), {inspection.non_empty_paragraphs} "
                    f"paragraphe(s), {inspection.frames} cadre(s), {inspection.inline_images} image(s) dans le flux, "
                    f"{inspection.anchored_images} image(s) ancrée(s).")
    problems = [*outcome.issues, *(report.issues if report else []), *(validation.issues if validation else [])]
    if report is not None and (report.overflows or report.fallbacks):
        with st.expander(f"Débordements et replis ({len(report.overflows) + len(report.fallbacks)})",
                         icon=":material/warning:"):
            common.show_issues([*report.overflows, *report.fallbacks])
    if problems:
        with st.expander(f"Signalements ({len(problems)})", icon=":material/report:", expanded=not outcome.ok):
            common.show_issues(problems)
    if outcome.path is not None and outcome.path.exists():
        _download(outcome.path, key=f"download_last_{kind}")
        if outcome.path.suffix == ".pdf":
            _pdf_preview(outcome.path, key="last_export_page")
        else:
            st.caption("L'aperçu des pages n'est disponible que pour les PDF (le DOCX s'ouvre dans Word ou "
                       "LibreOffice).")


last = st.session_state.get("export_result")
if last is not None and last["outcome"].path is not None and \
        Path(last["outcome"].path).parent != export_service.exports_dir(project):
    st.session_state["export_result"] = last = None          # résultat d'un autre projet
if last is not None:
    st.subheader("Dernier export")
    with st.container(border=True):
        _show_outcome(last["kind"], last["outcome"])

# --------------------------------------------------------------------------- exports précédents

st.subheader("Exports précédents")
previous = export_service.list_exports(project)
if not previous:
    st.caption("Aucun export pour ce projet.")
else:
    names = {path.name: path for path in previous}
    chosen_name = st.selectbox("Fichier", list(names), key="previous_export")
    chosen_path = names[chosen_name]
    st.caption(f"{chosen_path.stat().st_size / 1024:.0f} Ko · {chosen_path}")
    _download(chosen_path, key="download_previous")
    if chosen_path.suffix == ".pdf" and st.toggle("Afficher l'aperçu des pages", key="previous_preview"):
        _pdf_preview(chosen_path, key="previous_export_page")
