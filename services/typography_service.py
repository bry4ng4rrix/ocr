"""Typographie : résolution du style d'un bloc et changements globaux (livre entier ou sélection de pages).

Ordre de résolution (du plus faible au plus fort, `TypographySettings`) :
    valeurs par défaut -> block.estimated_style -> project.typography.kind_styles[kind]
    -> page.style_rules[kind] -> block.style_override

Un changement global écrit dans `kind_styles` (livre) ou dans `page.style_rules` (sélection) : il ne touche jamais
`estimated_style`. Les surcharges individuelles (`style_override`) sont conservées, sauf si
`override_individual=True`, auquel cas seuls les champs concernés par le changement en sont retirés.
Les fonctions modifient le projet en mémoire ; l'appelant l'enregistre (`project_service.save_project`).
"""
from __future__ import annotations

import statistics
from collections.abc import Iterable

from pydantic import BaseModel, Field, field_validator

from models import (HEADING_KINDS, Alignment, BlockKind, Page, Project, ResolvedStyle, TextBlock, TextStyle,
                    TypographyReport)

DEFAULT_BODY_PT = 11.0
DEFAULT_HEADING_RATIOS = {BlockKind.HEADING_1: 1.6, BlockKind.HEADING_2: 1.25}
DEFAULT_SMALL_RATIO = 0.85          # légende, note, en-tête, pied, numéro : taille relative au corps (repli)

ALL_KINDS = list(BlockKind)
BODY_FONT_KINDS = [kind for kind in BlockKind if kind not in HEADING_KINDS]
BODY_SIZE_KINDS = [BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.SPECIAL, BlockKind.TABLE]
PROPORTIONAL_SIZE_KINDS = [BlockKind.CAPTION, BlockKind.NOTE, BlockKind.HEADER, BlockKind.FOOTER,
                           BlockKind.PAGE_NUMBER]
LINE_SPACING_KINDS = [BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.NOTE, BlockKind.SPECIAL,
                      BlockKind.CAPTION, BlockKind.TABLE]
ALIGNMENT_KINDS = [BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.NOTE]
SPACE_AFTER_KINDS = [BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.NOTE, BlockKind.SPECIAL]
INDENT_KINDS = [BlockKind.PARAGRAPH]


class TypographyChange(BaseModel):
    """Changement demandé par l'utilisateur ; un champ `None` laisse la valeur actuelle.

    - `document_font` : police de tout le document (tous les types, sauf ceux visés par `heading_font` /
      `body_font` dans le même changement).
    - `heading_font`, `heading_1_size_pt`, `heading_2_size_pt` : titres.
    - `body_font` : tous les types hors titres ; `body_size_pt` : paragraphes, listes, citations, tableaux, et
      légendes, notes, en-têtes, pieds et numéros **proportionnellement** (rapport mesuré sur le livre).
    - `line_spacing` (multiple de l'interligne simple), `alignment`, `space_after_pt`, `first_line_indent_pt` :
      texte courant (voir les listes `*_KINDS` du module).
    - `margins_mm` (haut, droite, bas, gauche) : `project.export.margins_override_mm` (réglage du livre).
    """

    document_font: str | None = None
    heading_font: str | None = None
    body_font: str | None = None
    body_size_pt: float | None = Field(default=None, gt=0, le=400)
    heading_1_size_pt: float | None = Field(default=None, gt=0, le=400)
    heading_2_size_pt: float | None = Field(default=None, gt=0, le=400)
    line_spacing: float | None = Field(default=None, gt=0, le=5)
    alignment: Alignment | None = None
    space_after_pt: float | None = Field(default=None, ge=0, le=500)
    first_line_indent_pt: float | None = Field(default=None, ge=-200, le=500)
    margins_mm: tuple[float, float, float, float] | None = None

    @field_validator("document_font", "heading_font", "body_font")
    @classmethod
    def _font_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("nom de police vide")
        return value

    @field_validator("margins_mm")
    @classmethod
    def _margins(cls, value: tuple[float, float, float, float] | None):
        if value is not None and any(not 0 <= margin <= 200 for margin in value):
            raise ValueError("marges attendues entre 0 et 200 mm")
        return value


# --------------------------------------------------------------------------- résolution


def _default_style(project: Project, page: Page, block: TextBlock) -> TextStyle:
    body = page.body_font_size_pt or DEFAULT_BODY_PT
    heading = block.kind in HEADING_KINDS
    size = body * DEFAULT_HEADING_RATIOS.get(block.kind, 1.0)
    typography = project.typography
    return TextStyle(font_family=typography.document_font, font_size_pt=round(size, 1), bold=heading,
                     italic=False, underline=False, color=typography.default_color,
                     alignment=Alignment.CENTER if heading else typography.default_alignment,
                     line_spacing=typography.default_line_spacing, space_before_pt=0.0, space_after_pt=0.0,
                     first_line_indent_pt=0.0, left_indent_pt=0.0, right_indent_pt=0.0)


def style_layers(project: Project, page: Page, block: TextBlock) -> list[tuple[str, TextStyle]]:
    """Couches de style du bloc, de la plus faible à la plus forte (nom, style partiel)."""
    return [
        ("default", _default_style(project, page, block)),
        ("estimated", block.estimated_style),
        ("book", project.typography.kind_styles.get(block.kind, TextStyle())),
        ("page", page.style_rules.get(block.kind, TextStyle())),
        ("override", block.style_override),
    ]


def resolve_style_with_sources(project: Project, page: Page, block: TextBlock) -> tuple[ResolvedStyle, dict[str, str]]:
    """Style complet et, pour chaque champ, la couche qui l'a fixé (`default`, `estimated`, `book`, `page`,
    `override`). `composition_service` s'en sert pour ne mettre à l'échelle que les valeurs estimées."""
    merged = TextStyle()
    sources: dict[str, str] = {}
    for name, layer in style_layers(project, page, block):
        merged = merged.merged_with(layer)
        for field_name in layer.defined_fields():
            sources[field_name] = name
    data = {name: value for name, value in merged if value is not None}
    return ResolvedStyle(**data), sources


def resolve_style(project: Project, page: Page, block: TextBlock) -> ResolvedStyle:
    """Style complet d'un bloc : défauts <- estimé <- type (livre) <- type (page) <- surcharge du bloc."""
    return resolve_style_with_sources(project, page, block)[0]


# --------------------------------------------------------------------------- changements globaux


def _measured_ratio(pages: Iterable[Page], kind: BlockKind) -> float:
    """Taille médiane estimée d'un type relativement au corps de sa page (repli : 0,85), bornée à 0,5–1,0."""
    ratios = []
    for page in pages:
        body = page.body_font_size_pt
        if not body:
            continue
        for block in page.text_blocks:
            if block.kind == kind and block.estimated_style.font_size_pt:
                ratios.append(block.estimated_style.font_size_pt / body)
    if not ratios:
        return DEFAULT_SMALL_RATIO
    return min(1.0, max(0.5, statistics.median(ratios)))


def planned_rules(change: TypographyChange, pages: list[Page]) -> dict[BlockKind, TextStyle]:
    """Style partiel à écrire pour chaque type de bloc (calcul pur)."""
    rules: dict[BlockKind, dict] = {}

    def put(kinds: Iterable[BlockKind], field_name: str, value) -> None:
        for kind in kinds:
            rules.setdefault(kind, {})[field_name] = value

    if change.document_font:
        put(ALL_KINDS, "font_family", change.document_font)
    if change.body_font:
        put(BODY_FONT_KINDS, "font_family", change.body_font)
    if change.heading_font:
        put(HEADING_KINDS, "font_family", change.heading_font)
    if change.body_size_pt:
        put(BODY_SIZE_KINDS, "font_size_pt", change.body_size_pt)
        for kind in PROPORTIONAL_SIZE_KINDS:
            put([kind], "font_size_pt", round(change.body_size_pt * _measured_ratio(pages, kind), 1))
    if change.heading_1_size_pt:
        put([BlockKind.HEADING_1], "font_size_pt", change.heading_1_size_pt)
    if change.heading_2_size_pt:
        put([BlockKind.HEADING_2], "font_size_pt", change.heading_2_size_pt)
    if change.line_spacing:
        put(LINE_SPACING_KINDS, "line_spacing", change.line_spacing)
    if change.alignment:
        put(ALIGNMENT_KINDS, "alignment", change.alignment)
    if change.space_after_pt is not None:
        put(SPACE_AFTER_KINDS, "space_after_pt", change.space_after_pt)
    if change.first_line_indent_pt is not None:
        put(INDENT_KINDS, "first_line_indent_pt", change.first_line_indent_pt)
    return {kind: TextStyle(**values) for kind, values in rules.items()}


def _target_pages(project: Project, page_ids: list[str] | None) -> list[Page]:
    if page_ids is None:
        return list(project.pages)
    wanted = set(page_ids)
    for page_id in wanted:
        project.page(page_id)                  # PageNotFoundError si l'identifiant est inconnu
    return [page for page in project.pages if page.id in wanted]


def apply_global_typography(project: Project, change: TypographyChange, page_ids: list[str] | None = None,
                            override_individual: bool = False) -> TypographyReport:
    """Applique `change` au livre (`page_ids=None`) ou aux pages listées. Mutation en mémoire uniquement.

    Livre : `project.typography.document_font` et `kind_styles` ; une police de document efface les polices
    de type précédemment fixées au niveau du livre (sauf celles du même changement). Sélection :
    `page.style_rules` des pages listées. `margins_mm` va toujours dans `project.export.margins_override_mm`.
    """
    pages = _target_pages(project, page_ids)
    rules = planned_rules(change, list(project.pages))
    report = TypographyReport()

    if page_ids is None:
        typography = project.typography
        if change.document_font:
            # la police du document est la couche par défaut : les polices de type fixées auparavant au niveau
            # du livre sont retirées (celles du même changement sont réécrites juste après)
            typography.document_font = change.document_font
            for kind in list(typography.kind_styles):
                typography.kind_styles[kind] = typography.kind_styles[kind].without({"font_family"})
        written = planned_rules(change.model_copy(update={"document_font": None}), list(project.pages))
        for kind, rule in written.items():
            typography.kind_styles[kind] = typography.kind_styles.get(kind, TextStyle()).merged_with(rule)
        for kind in [kind for kind, style in typography.kind_styles.items() if not style.defined_fields()]:
            del typography.kind_styles[kind]
    else:
        for page in pages:
            for kind, rule in rules.items():
                page.style_rules[kind] = page.style_rules.get(kind, TextStyle()).merged_with(rule)

    if change.margins_mm is not None:
        project.export.margins_override_mm = tuple(float(value) for value in change.margins_mm)

    touched_pages: set[str] = set()
    for page in pages:
        for block in page.text_blocks:
            rule = rules.get(block.kind)
            if rule is None:
                continue
            fields = rule.defined_fields()
            protected = fields & block.style_override.defined_fields()
            if protected and override_individual:
                block.style_override = block.style_override.without(protected)
                protected = set()
            if protected:
                report.blocks_preserved += 1
                report.preserved_block_ids.append(block.id)
            if fields - protected:
                report.blocks_affected += 1
                touched_pages.add(page.id)
    if change.margins_mm is not None:
        touched_pages.update(page.id for page in pages)
    report.pages_affected = len(touched_pages)
    return report


def preview_typography_change(project: Project, change: TypographyChange, page_ids: list[str] | None = None,
                              override_individual: bool = False) -> TypographyReport:
    """Même calcul qu'`apply_global_typography`, sur une copie : le projet n'est pas modifié."""
    return apply_global_typography(project.model_copy(deep=True), change, page_ids, override_individual)
