"""Typographie : résolution des styles et changements globaux (exigence 13)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from models import (Alignment, BlockKind, Box, LayoutSettings, Page, PageStatus, Project, TextBlock, TextStyle)
from models.errors import PageNotFoundError
from services import layout_service, typography_service
from services.typography_service import TypographyChange, apply_global_typography, preview_typography_change, \
    resolve_style
from tests.fixtures import synthetic


def _analysed_page(factory) -> Page:
    source = factory()
    result = layout_service.analyse_page(source.truth_ocr(), source.image, LayoutSettings(languages=["fra"]))
    page = Page(source_file=f"originals/{source.name}.png", source_sha256="0" * 64,
                original_name=f"{source.name}.png", status=PageStatus.ANALYSED)
    return layout_service.merge_layout(page, result)


@pytest.fixture(scope="module")
def pages() -> list[Page]:
    return [_analysed_page(synthetic.text_page), _analysed_page(synthetic.photo_text_page),
            _analysed_page(synthetic.two_column_page)]


@pytest.fixture
def project(pages) -> Project:
    return Project(name="Livre", pages=[page.model_copy(deep=True) for page in pages])


def _blocks(project: Project, kind: BlockKind) -> list[tuple[Page, TextBlock]]:
    return [(page, block) for page in project.pages for block in page.text_blocks if block.kind == kind]


# --------------------------------------------------------------------------- résolution


def test_resolution_order_layers(project):
    page = project.pages[0]
    block = next(b for b in page.text_blocks if b.kind == BlockKind.PARAGRAPH)
    style = resolve_style(project, page, block)
    assert style.font_family == project.typography.document_font
    assert style.font_size_pt == block.estimated_style.font_size_pt          # estimé > défaut
    assert style.first_line_indent_pt == pytest.approx(block.estimated_style.first_line_indent_pt)
    project.typography.kind_styles[BlockKind.PARAGRAPH] = TextStyle(font_size_pt=12, font_family="Livre")
    assert resolve_style(project, page, block).font_size_pt == 12              # type (livre) > estimé
    page.style_rules[BlockKind.PARAGRAPH] = TextStyle(font_size_pt=13)
    assert resolve_style(project, page, block).font_size_pt == 13              # page > livre
    assert resolve_style(project, page, block).font_family == "Livre"
    block.style_override = TextStyle(font_size_pt=9.5)
    assert resolve_style(project, page, block).font_size_pt == 9.5             # surcharge du bloc > page


def test_defaults_without_estimation():
    project = Project(name="Vide")
    page = Page(source_file="originals/p.png", source_sha256="0" * 64, original_name="p.png", body_font_size_pt=10)
    heading = TextBlock(kind=BlockKind.HEADING_1, box=Box(x=0.1, y=0.1, w=0.8, h=0.05), ocr_text="Titre")
    paragraph = TextBlock(box=Box(x=0.1, y=0.2, w=0.8, h=0.3), ocr_text="Texte")
    page.text_blocks = [heading, paragraph]
    style = resolve_style(project, page, heading)
    assert style.bold is True and style.font_size_pt == pytest.approx(16) and style.alignment == Alignment.CENTER
    style = resolve_style(project, page, paragraph)
    assert style.font_size_pt == 10 and style.bold is False
    assert style.line_spacing == project.typography.default_line_spacing
    page.body_font_size_pt = None
    assert resolve_style(project, page, paragraph).font_size_pt == 11


# --------------------------------------------------------------------------- changements globaux


def test_global_font_change(project):
    report = apply_global_typography(project, TypographyChange(body_font="Liberation Sans",
                                                               heading_font="DejaVu Serif"))
    for page in project.pages:
        for block in page.text_blocks:
            expected = "DejaVu Serif" if block.kind in (BlockKind.HEADING_1, BlockKind.HEADING_2) else "Liberation Sans"
            assert resolve_style(project, page, block).font_family == expected, block.kind
    total = sum(len(page.text_blocks) for page in project.pages)
    assert report.blocks_affected == total and report.pages_affected == 3 and report.blocks_preserved == 0
    # le style estimé n'est jamais modifié ; les autres attributs non plus
    for page in project.pages:
        for block in page.text_blocks:
            assert block.estimated_style.font_family is None
            assert resolve_style(project, page, block).font_size_pt == pytest.approx(block.estimated_style.font_size_pt)


def test_document_font_replaces_previous_kind_fonts(project):
    apply_global_typography(project, TypographyChange(body_font="Liberation Sans"))
    apply_global_typography(project, TypographyChange(document_font="DejaVu Sans"))
    assert project.typography.document_font == "DejaVu Sans"
    assert all(style.font_family is None for style in project.typography.kind_styles.values())
    page, block = _blocks(project, BlockKind.PARAGRAPH)[0]
    assert resolve_style(project, page, block).font_family == "DejaVu Sans"


def test_individual_override_preserved(project):
    page, special = _blocks(project, BlockKind.PARAGRAPH)[0]
    special.style_override = TextStyle(font_family="Liberation Mono", bold=True)
    report = apply_global_typography(project, TypographyChange(body_font="Liberation Sans", body_size_pt=12))
    style = resolve_style(project, page, special)
    assert style.font_family == "Liberation Mono" and style.bold is True     # surcharge gardée
    assert style.font_size_pt == 12                                          # champ non surchargé : appliqué
    assert report.blocks_preserved == 1 and report.preserved_block_ids == [special.id]
    others = [(p, b) for p, b in _blocks(project, BlockKind.PARAGRAPH) if b.id != special.id]
    assert others and all(resolve_style(project, p, b).font_family == "Liberation Sans" for p, b in others)


def test_individual_override_replaced_on_request(project):
    page, special = _blocks(project, BlockKind.PARAGRAPH)[0]
    special.style_override = TextStyle(font_family="Liberation Mono", bold=True)
    report = apply_global_typography(project, TypographyChange(body_font="Liberation Sans"),
                                     override_individual=True)
    assert special.style_override.font_family is None and special.style_override.bold is True   # seul le champ visé
    assert resolve_style(project, page, special).font_family == "Liberation Sans"
    assert report.blocks_preserved == 0


def test_selection_only_changes_listed_pages(project):
    first, second, third = project.pages
    report = apply_global_typography(project, TypographyChange(body_font="Liberation Sans", body_size_pt=12.5),
                                     page_ids=[second.id])
    assert project.typography.kind_styles == {}
    assert first.style_rules == {} and third.style_rules == {}
    paragraph = next(b for b in second.text_blocks if b.kind == BlockKind.PARAGRAPH)
    style = resolve_style(project, second, paragraph)
    assert style.font_family == "Liberation Sans" and style.font_size_pt == 12.5
    untouched = next(b for b in first.text_blocks if b.kind == BlockKind.PARAGRAPH)
    assert resolve_style(project, first, untouched).font_family == project.typography.document_font
    assert report.pages_affected == 1 and report.blocks_affected == len(second.text_blocks)
    with pytest.raises(PageNotFoundError):
        apply_global_typography(project, TypographyChange(body_font="X"), page_ids=["absent"])


def test_body_size_scales_small_kinds_proportionally(project):
    apply_global_typography(project, TypographyChange(body_size_pt=12, heading_1_size_pt=22))
    styles = project.typography.kind_styles
    assert styles[BlockKind.PARAGRAPH].font_size_pt == 12 and styles[BlockKind.LIST_ITEM].font_size_pt == 12
    assert styles[BlockKind.HEADING_1].font_size_pt == 22
    assert BlockKind.HEADING_2 not in styles or styles[BlockKind.HEADING_2].font_size_pt is None
    caption = styles[BlockKind.CAPTION].font_size_pt                # légende 9 pt pour un corps de 11 pt
    assert caption == pytest.approx(12 * 9 / 11, abs=0.6)
    assert styles[BlockKind.PAGE_NUMBER].font_size_pt < 12


def test_paragraph_settings_and_margins(project):
    change = TypographyChange(line_spacing=1.3, alignment=Alignment.JUSTIFY, space_after_pt=4,
                              first_line_indent_pt=0, margins_mm=(20, 15, 22, 18))
    apply_global_typography(project, change)
    page, paragraph = _blocks(project, BlockKind.PARAGRAPH)[0]
    style = resolve_style(project, page, paragraph)
    assert (style.line_spacing, style.alignment, style.space_after_pt, style.first_line_indent_pt) == \
        (1.3, Alignment.JUSTIFY, 4, 0)
    page, heading = _blocks(project, BlockKind.HEADING_1)[0]
    assert resolve_style(project, page, heading).alignment == Alignment.CENTER     # titres non réalignés
    assert project.export.margins_override_mm == (20, 15, 22, 18)


def test_preview_does_not_mutate(project):
    project.pages[0].text_blocks[3].style_override = TextStyle(font_family="Liberation Mono")
    before = project.model_dump()
    change = TypographyChange(document_font="DejaVu Sans", body_font="Liberation Sans", body_size_pt=12,
                              margins_mm=(10, 10, 10, 10))
    preview = preview_typography_change(project, change)
    assert project.model_dump() == before
    applied = apply_global_typography(project, change)
    assert preview == applied
    selection = [project.pages[1].id]
    before = project.model_dump()
    assert preview_typography_change(project, change, selection, override_individual=True).pages_affected == 1
    assert project.model_dump() == before


def test_change_validation():
    with pytest.raises(ValidationError):
        TypographyChange(body_size_pt=0)
    with pytest.raises(ValidationError):
        TypographyChange(body_font="  ")
    with pytest.raises(ValidationError):
        TypographyChange(margins_mm=(10, 10, -1, 10))
    assert typography_service.planned_rules(TypographyChange(), []) == {}
