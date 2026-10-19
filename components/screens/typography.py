"""Écran F — Typographie globale : polices, tailles, interligne, alignement, espacements et marges du livre ou
des pages choisies ; aperçu du rapport avant application."""
from __future__ import annotations

import streamlit as st

from components import common
from models import Alignment
from services import fonts, project_service, typography_service
from services.typography_service import TypographyChange

UNCHANGED = "(inchangé)"
SCOPE_LABELS = {"book": "Tout le livre", "pages": "Pages choisies"}

project = common.require_project()
common.require_pages(project)
locked = common.locked_page_ids(project.id)
families = sorted(fonts.available_families())
by_id = {page.id: page for page in project.pages}

typography = project.typography
st.caption(f"Police actuelle du document : {typography.document_font} · interligne par défaut : "
           f"{typography.default_line_spacing:g} · alignement par défaut : "
           f"{common.ALIGNMENT_LABELS[typography.default_alignment]}"
           + (f" · marges imposées (mm) : {project.export.margins_override_mm}"
              if project.export.margins_override_mm else ""))
if not families:
    st.error("Aucune police TrueType trouvée sur cette machine : les changements de police sont impossibles.")

font_options = [UNCHANGED, *families]
with st.form("typography_form"):
    st.markdown("**Polices**")
    font_columns = st.columns(3)
    document_font = font_columns[0].selectbox("Police du document", font_options, key="typo_document_font")
    heading_font = font_columns[1].selectbox("Police des titres", font_options, key="typo_heading_font")
    body_font = font_columns[2].selectbox("Police du texte courant", font_options, key="typo_body_font")
    st.markdown("**Tailles (pt)** — laissez vide pour ne pas changer")
    size_columns = st.columns(3)
    body_size = size_columns[0].number_input("Texte courant", min_value=4.0, max_value=72.0, step=0.5, value=None,
                                             key="typo_body_size", help="Légendes, notes, en-têtes et pieds suivent "
                                                                        "proportionnellement.")
    heading_1_size = size_columns[1].number_input("Titre 1", min_value=4.0, max_value=200.0, step=0.5, value=None,
                                                  key="typo_heading_1_size")
    heading_2_size = size_columns[2].number_input("Titre 2", min_value=4.0, max_value=200.0, step=0.5, value=None,
                                                  key="typo_heading_2_size")
    st.markdown("**Paragraphes**")
    paragraph_columns = st.columns(4)
    line_spacing = paragraph_columns[0].number_input("Interligne (multiple)", min_value=0.5, max_value=5.0, step=0.05,
                                                     value=None, key="typo_line_spacing")
    alignment = paragraph_columns[1].selectbox("Alignement", [None, *Alignment], key="typo_alignment",
                                               format_func=lambda value: UNCHANGED if value is None
                                               else common.ALIGNMENT_LABELS[value])
    space_after = paragraph_columns[2].number_input("Espace après (pt)", min_value=0.0, max_value=500.0, step=1.0,
                                                    value=None, key="typo_space_after")
    first_indent = paragraph_columns[3].number_input("Retrait de première ligne (pt)", min_value=-200.0,
                                                     max_value=500.0, step=1.0, value=None, key="typo_first_indent")
    st.markdown("**Marges du livre (mm)** — les quatre, ou aucune")
    margin_columns = st.columns(4)
    margins = [column.number_input(label, min_value=0.0, max_value=200.0, step=1.0, value=None,
                                   key=f"typo_margin_{index}")
               for index, (column, label) in enumerate(zip(margin_columns, ["Haut", "Droite", "Bas", "Gauche"]))]
    st.markdown("**Portée**")
    scope = st.segmented_control("Appliquer à", list(SCOPE_LABELS), format_func=SCOPE_LABELS.get, default="book",
                                 required=True, key="typo_scope")
    chosen = st.multiselect("Pages choisies (si « Pages choisies »)", list(by_id), key="typo_pages",
                            format_func=lambda page_id: common.page_label(project, by_id[page_id]))
    override_individual = st.checkbox("Remplacer aussi les styles individuels des blocs",
                                      key="typo_override_individual",
                                      help="Sinon, les blocs réglés à la main dans l'éditeur gardent leur style.")
    with st.container(horizontal=True):
        preview_clicked = st.form_submit_button("Aperçu de l'effet", icon=":material/preview:")
        apply_clicked = st.form_submit_button("Appliquer et enregistrer", type="primary", icon=":material/check:")


def _change() -> TypographyChange | None:
    filled_margins = [value for value in margins if value is not None]
    if filled_margins and len(filled_margins) != 4:
        st.error("Indiquez les quatre marges, ou aucune.")
        return None
    change = TypographyChange(
        document_font=None if document_font == UNCHANGED else document_font,
        heading_font=None if heading_font == UNCHANGED else heading_font,
        body_font=None if body_font == UNCHANGED else body_font,
        body_size_pt=body_size, heading_1_size_pt=heading_1_size, heading_2_size_pt=heading_2_size,
        line_spacing=line_spacing, alignment=alignment, space_after_pt=space_after,
        first_line_indent_pt=first_indent, margins_mm=tuple(filled_margins) if filled_margins else None)
    if not change.model_dump(exclude_none=True):
        st.warning("Aucun changement demandé : remplissez au moins un champ.")
        return None
    return change


def _page_ids() -> list[str] | None | bool:
    if scope == "book":
        return None
    if not chosen:
        st.error("Choisissez au moins une page, ou appliquez au livre entier.")
        return False
    return [page.id for page in project.pages if page.id in set(chosen)]


def _show_report(report, applied: bool) -> None:
    verb = "modifié" if applied else "seraient modifiés"
    st.success(f"{report.blocks_affected} bloc(s) {verb} sur {report.pages_affected} page(s).",
               icon=":material/check_circle:")
    if report.blocks_preserved:
        st.info(f"{report.blocks_preserved} bloc(s) gardent leur style individuel (cochez « Remplacer aussi les "
                "styles individuels » pour les modifier).", icon=":material/lock:")


if preview_clicked or apply_clicked:
    change = _change()
    page_ids = _page_ids()
    if change is not None and page_ids is not False:
        if apply_clicked:
            busy = locked if page_ids is None else locked & set(page_ids)
            if busy:
                st.error("Des pages concernées sont en cours de traitement : attendez la fin du traitement.")
            else:
                with common.user_action() as result:
                    with common.editing(project.id) as fresh:
                        for page_id in page_ids if page_ids is not None else [page.id for page in fresh.pages]:
                            project_service.snapshot_page(fresh, page_id, "Typographie globale")
                        report = typography_service.apply_global_typography(fresh, change, page_ids,
                                                                            override_individual=override_individual)
                        project_service.save_project(fresh)
                if result.ok:
                    common.flash("success", f"Typographie appliquée : {report.blocks_affected} bloc(s) sur "
                                            f"{report.pages_affected} page(s)"
                                 + (f", {report.blocks_preserved} bloc(s) gardent leur style individuel."
                                    if report.blocks_preserved else "."))
                    st.rerun()
        else:
            with common.user_action() as result:
                report = typography_service.preview_typography_change(project, change, page_ids,
                                                                      override_individual=override_individual)
            if result.ok:
                _show_report(report, applied=False)

st.subheader("Styles du livre par type de bloc")
if typography.kind_styles:
    st.dataframe([{"Type": common.BLOCK_KIND_LABELS[kind], **{common.STYLE_FIELD_LABELS.get(name, name): str(value)
                                                           for name, value in style if value is not None}}
                  for kind, style in typography.kind_styles.items()], hide_index=True, alt="Styles par type de bloc")
else:
    st.caption("Aucun style de type fixé au niveau du livre : les styles estimés et la police du document "
               "s'appliquent.")
