"""Composition en points : modes, conversions, exclusions, fonds, bandes, déterminisme (D-06, D-08)."""
from __future__ import annotations

from functools import cache
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from models import (MM_TO_PT, STANDARD_FORMATS, BlockKind, Box, ImageBlock, LayoutSettings, Page, PageCategory,
                    PageStatus, Project, ReconstructionMode, TextBlock, TextStyle)
from services import composition_service, layout_service
from services.composition_service import compose_book, compose_page
from tests.fixtures import synthetic

MODES = list(ReconstructionMode)


def _write_png(path: Path, size: tuple[int, int] = (8, 8)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.full((size[1], size[0], 3), 128, np.uint8)).save(path)


@cache
def _analysis(factory):
    source = factory()
    return source, layout_service.analyse_page(source.truth_ocr(), source.image, LayoutSettings(languages=["fra"]))


def _page(factory, project_dir: Path) -> Page:
    """Page analysée comme le ferait processing_service (images et fonds « extraits » en fichiers factices)."""
    source, result = _analysis(factory)
    page = Page(source_file=f"originals/{source.name}.png", source_sha256="0" * 64,
                original_name=f"{source.name}.png", corrected_width=source.width, corrected_height=source.height)
    page = layout_service.merge_layout(page, result)
    page.status = PageStatus.ANALYSED
    for block in page.image_blocks:
        block.file = f"images/{block.id}.png"
        _write_png(project_dir / block.file)
    for index, _ in enumerate(page.background_regions):
        page.background_files.append(f"backgrounds/{page.id}_{index}.png")
        _write_png(project_dir / page.background_files[-1])
    _write_png(project_dir / "corrected" / f"{page.id}.png")
    return page


@pytest.fixture
def book(tmp_path) -> tuple[Project, Path]:
    project_dir = tmp_path / "projet"
    pages = [_page(factory, project_dir) for factory in (synthetic.text_page, synthetic.two_column_page,
                                                         synthetic.graphic_background_page,
                                                         synthetic.photo_text_page)]
    return Project(name="Livre", pages=pages), project_dir


def _kinds(composed, kind: BlockKind):
    return [element for element in composed.text_elements if element.kind == kind]


# --------------------------------------------------------------------------- modes


def test_fidelity_everything_absolute(book):
    project, folder = book
    for composed in compose_book(project, folder, ReconstructionMode.FIDELITY):
        assert composed.mode == ReconstructionMode.FIDELITY and composed.analysed
        assert composed.elements
        assert all(element.positioning == "absolute" for element in composed.elements)


def test_editable_flow_with_header_footer(book):
    project, folder = book
    text_page, two_columns, graphic, photo = compose_book(project, folder, ReconstructionMode.EDITABLE)
    for composed in (text_page, two_columns, graphic, photo):
        for element in composed.elements:
            if getattr(element, "kind", None) in (BlockKind.HEADER, BlockKind.FOOTER, BlockKind.PAGE_NUMBER):
                continue
            assert element.positioning == "flow"
            assert element.band_index is not None and element.column_index is not None
    assert _kinds(text_page, BlockKind.HEADER)[0].positioning == "header"
    assert _kinds(text_page, BlockKind.PAGE_NUMBER)[0].positioning == "footer"
    assert all(element.band_index is None for element in _kinds(text_page, BlockKind.PAGE_NUMBER))
    image, = photo.image_elements
    assert image.positioning == "flow" and image.z_index == 5
    assert all(element.positioning == "flow" for element in graphic.text_elements if element.on_image)


def test_hybrid_running_text_flows_overlays_absolute(book):
    project, folder = book
    graphic = compose_page(project, project.pages[2], folder, ReconstructionMode.HYBRID)
    overlays = [element for element in graphic.text_elements if element.on_image]
    assert len(overlays) == 2 and all(element.positioning == "absolute" for element in overlays)
    running = [element for element in graphic.text_elements if not element.on_image
               and element.kind == BlockKind.PARAGRAPH]
    assert running and all(element.positioning == "flow" for element in running)
    assert _kinds(graphic, BlockKind.PAGE_NUMBER)[0].positioning == "footer"
    photo = compose_page(project, project.pages[3], folder, ReconstructionMode.HYBRID)
    assert [element.positioning for element in photo.image_elements] == ["absolute"]


def test_mode_defaults_to_project_setting(book):
    project, folder = book
    project.export.mode = ReconstructionMode.HYBRID
    assert compose_page(project, project.pages[0], folder).mode == ReconstructionMode.HYBRID


# --------------------------------------------------------------------------- conversions


def test_points_conversion_on_detected_format(book):
    project, folder = book
    page = project.pages[0]
    composed = compose_page(project, page, folder, ReconstructionMode.FIDELITY)
    a5 = STANDARD_FORMATS["A5"]
    assert composed.width_pt == pytest.approx(a5.width_pt, abs=0.01)
    assert composed.height_pt == pytest.approx(a5.height_pt, abs=0.01)
    ink = composition_service.box_to_rect(page.text_blocks[1].box, a5.width_pt, a5.height_pt)
    assert ink.x_pt == pytest.approx(page.text_blocks[1].box.x * a5.width_pt, abs=0.01)
    assert ink.height_pt == pytest.approx(page.text_blocks[1].box.h * a5.height_pt, abs=0.01)
    for block in page.text_blocks:
        element = next(e for e in composed.text_elements if e.block_id == block.id)
        assert element.style.font_size_pt == block.estimated_style.font_size_pt   # même format : pas d'échelle
        ink = composition_service.box_to_rect(block.box, a5.width_pt, a5.height_pt)
        frame = element.rect                     # cadre typographique : contient l'encre, lignes entières
        assert frame.x_pt <= ink.x_pt + 1e-6 and frame.right_pt >= ink.right_pt - 1e-6
        assert frame.y_pt <= ink.y_pt and frame.bottom_pt >= ink.bottom_pt
        pitch = element.style.line_spacing * composition_service.NATURAL_LINE_EM * element.style.font_size_pt
        assert frame.height_pt >= len(block.lines) * pitch - 1e-3
        assert frame.y_pt >= ink.y_pt - pitch                     # le cadre ne remonte pas d'une ligne entière
    paragraph = next(b for b in page.text_blocks if b.kind == BlockKind.PARAGRAPH)
    element = next(e for e in composed.text_elements if e.block_id == paragraph.id)
    assert element.rect.x_pt == pytest.approx(paragraph.box.x * a5.width_pt, abs=0.01)   # aligné à gauche
    assert composed.margins.left_pt == pytest.approx(page.content_box.x * a5.width_pt, abs=0.01)
    assert composed.margins.top_pt == pytest.approx(page.content_box.y * a5.height_pt, abs=0.01)
    assert composed.source_image == str((folder / "corrected" / f"{page.id}.png").resolve())


def test_font_scaling_when_format_changes(book):
    project, folder = book
    page = project.pages[0]
    paragraph = next(b for b in page.text_blocks if b.kind == BlockKind.PARAGRAPH)
    heading = next(b for b in page.text_blocks if b.kind == BlockKind.HEADING_1)
    project.typography.kind_styles[BlockKind.HEADING_1] = TextStyle(font_size_pt=24)    # choix de l'utilisateur
    project.export.page_format = "A4"
    composed = compose_page(project, page, folder, ReconstructionMode.EDITABLE)
    ratio = STANDARD_FORMATS["A4"].width_pt / STANDARD_FORMATS["A5"].width_pt
    assert composed.width_pt == pytest.approx(STANDARD_FORMATS["A4"].width_pt, abs=0.01)
    element = next(e for e in composed.text_elements if e.block_id == paragraph.id)
    assert element.style.font_size_pt == pytest.approx(paragraph.estimated_style.font_size_pt * ratio, abs=0.01)
    assert element.style.first_line_indent_pt == pytest.approx(
        paragraph.estimated_style.first_line_indent_pt * ratio, abs=0.01)
    assert element.rect.x_pt == pytest.approx(paragraph.box.x * STANDARD_FORMATS["A4"].width_pt, abs=0.01)
    assert element.rect.width_pt > paragraph.box.w * STANDARD_FORMATS["A4"].width_pt
    assert next(e for e in composed.text_elements if e.block_id == heading.id).style.font_size_pt == 24
    project.export.scale_fonts_to_format = False
    composed = compose_page(project, page, folder, ReconstructionMode.EDITABLE)
    element = next(e for e in composed.text_elements if e.block_id == paragraph.id)
    assert element.style.font_size_pt == paragraph.estimated_style.font_size_pt


def test_margins_override(book):
    project, folder = book
    project.export.margins_override_mm = (20.0, 15.0, 25.0, 18.0)
    composed = compose_page(project, project.pages[0], folder, ReconstructionMode.EDITABLE)
    assert composed.margins.top_pt == pytest.approx(20 * MM_TO_PT, abs=1e-3)
    assert composed.margins.right_pt == pytest.approx(15 * MM_TO_PT, abs=1e-3)
    assert composed.margins.bottom_pt == pytest.approx(25 * MM_TO_PT, abs=1e-3)
    assert composed.margins.left_pt == pytest.approx(18 * MM_TO_PT, abs=1e-3)


# --------------------------------------------------------------------------- exclusions, images, fonds


def test_exclusions(book):
    project, folder = book
    page = project.pages[0]
    hidden = next(b for b in page.text_blocks if b.kind == BlockKind.PARAGRAPH)
    hidden.hidden = True
    project.export.include_headers_footers = False
    project.export.include_page_numbers = False
    for mode in MODES:
        composed = compose_page(project, page, folder, mode)
        ids = {element.block_id for element in composed.elements}
        assert hidden.id not in ids
        assert not _kinds(composed, BlockKind.HEADER) and not _kinds(composed, BlockKind.PAGE_NUMBER)
        assert len(composed.text_elements) == len(page.text_blocks) - 3


def test_text_is_corrected_text(book):
    project, folder = book
    block = next(b for b in project.pages[0].text_blocks if b.kind == BlockKind.PARAGRAPH)
    block.corrected_text = "Texte relu."
    composed = compose_page(project, project.pages[0], folder, ReconstructionMode.EDITABLE)
    assert next(e for e in composed.text_elements if e.block_id == block.id).text == "Texte relu."


def test_images_absolute_paths_and_missing_files(book):
    project, folder = book
    page = project.pages[3]
    composed = compose_page(project, page, folder, ReconstructionMode.FIDELITY)
    image, = composed.image_elements
    block = page.image_blocks[0]
    assert Path(image.file).is_absolute() and image.file == str((folder / block.file).resolve())
    assert image.caption_block_id == block.caption_block_id and image.kind == "photo"
    block.file = ""
    composed = compose_page(project, page, folder, ReconstructionMode.FIDELITY)
    assert not composed.image_elements
    assert any(issue.code == "composition_image_not_extracted" and issue.block_id == block.id
               for issue in composed.issues)
    assert not any(isinstance(e, type(image)) for e in composed.elements)


def test_backgrounds(book):
    project, folder = book
    graphic = project.pages[2]
    for mode in MODES:
        composed = compose_page(project, graphic, folder, mode)
        background, = composed.backgrounds
        assert background.kind == "background" and background.z_index == 0 and background.positioning == "absolute"
        assert background.block_id is None and Path(background.file).is_absolute()
        assert background.rect.width_pt == pytest.approx(graphic.background_regions[0].w * composed.width_pt, abs=0.01)
        assert all(not isinstance(e, type(background)) or e.kind != "background" for e in composed.elements)
    project.export.keep_backgrounds = False
    assert compose_page(project, graphic, folder, ReconstructionMode.HYBRID).backgrounds == []
    project.export.keep_backgrounds = True
    text_page = project.pages[0]
    text_page.background_regions = [Box(x=0, y=0, w=1, h=0.5)]
    text_page.background_files = [graphic.background_files[0]]
    assert text_page.effective_category == PageCategory.TEXT
    assert compose_page(project, text_page, folder, ReconstructionMode.FIDELITY).backgrounds == []   # cas A


# --------------------------------------------------------------------------- bandes et colonnes


def test_bands_title_then_two_columns(book):
    project, folder = book
    page = project.pages[1]
    composed = compose_page(project, page, folder, ReconstructionMode.EDITABLE)
    assert [band.columns for band in composed.bands] == [1, 2]
    title = _kinds(composed, BlockKind.HEADING_1)[0]
    assert (title.band_index, title.column_index) == (0, 0)
    paragraphs = _kinds(composed, BlockKind.PARAGRAPH)
    assert [(e.band_index, e.column_index) for e in paragraphs] == [(1, 0), (1, 0), (1, 1), (1, 1)]
    two = composed.bands[1]
    assert len(two.column_rects) == 2 and two.gap_pt > 0
    assert two.column_rects[0].right_pt < two.column_rects[1].x_pt
    assert composed.bands[0].bottom_pt <= two.top_pt
    # espace d'origine : premier paragraphe de chaque colonne mesuré depuis le bas du titre
    first_left, second_left, first_right, _ = paragraphs
    assert first_left.gap_before_pt == pytest.approx(first_left.rect.y_pt - composed.bands[0].bottom_pt, abs=0.01)
    assert first_right.gap_before_pt == pytest.approx(first_right.rect.y_pt - composed.bands[0].bottom_pt, abs=0.01)
    assert second_left.gap_before_pt == pytest.approx(second_left.rect.y_pt - first_left.rect.bottom_pt, abs=0.01)
    assert title.gap_before_pt == pytest.approx(max(0.0, title.rect.y_pt - composed.margins.top_pt), abs=0.01)
    assert composed.bands[0].top_pt == pytest.approx(title.rect.y_pt, abs=0.01)


def test_single_column_page_has_one_band(book):
    project, folder = book
    composed = compose_page(project, project.pages[3], folder, ReconstructionMode.EDITABLE)
    assert len(composed.bands) == 1 and composed.bands[0].columns == 1
    order = [element.block_id for element in composed.elements]
    image = project.pages[3].image_blocks[0]
    assert order.index(image.id) < order.index(image.caption_block_id)


def test_elements_in_reading_order(book):
    project, folder = book
    page = project.pages[1]
    composed = compose_page(project, page, folder, ReconstructionMode.FIDELITY)
    expected = [block.id for block in page.blocks_in_order()]
    assert [element.block_id for element in composed.elements] == expected


# --------------------------------------------------------------------------- déterminisme, pages non analysées


def test_deterministic(book):
    project, folder = book
    for mode in MODES:
        first = [page.model_dump() for page in compose_book(project, folder, mode)]
        second = [page.model_dump() for page in compose_book(project, folder, mode)]
        assert first == second


def test_book_order_and_page_numbers(book):
    project, folder = book
    project.pages.reverse()
    composed = compose_book(project, folder, ReconstructionMode.EDITABLE)
    assert [page.page_id for page in composed] == [page.id for page in project.pages]
    assert [page.page_number for page in composed] == [1, 2, 3, 4]
    subset = compose_book(project, folder, ReconstructionMode.EDITABLE, page_ids=[project.pages[2].id,
                                                                                  project.pages[0].id])
    assert [page.page_id for page in subset] == [project.pages[0].id, project.pages[2].id]
    assert [page.page_number for page in subset] == [1, 3]


def test_unanalysed_page(tmp_path):
    page = Page(source_file="originals/p.jpg", source_sha256="0" * 64, original_name="p.jpg",
                status=PageStatus.OCR_DONE)
    project = Project(name="Livre", pages=[page])
    composed = compose_page(project, page, tmp_path, ReconstructionMode.EDITABLE)
    assert composed.analysed is False and composed.elements == [] and composed.backgrounds == []
    assert composed.issues[0].code == "composition_not_analysed"
    assert composed.width_pt == pytest.approx(STANDARD_FORMATS["A5"].width_pt, abs=1e-3)
    assert composed.source_image is None
    page.status = PageStatus.ANALYSED                    # page blanche analysée : composée, sans élément
    composed = compose_page(project, page, tmp_path, ReconstructionMode.EDITABLE)
    assert composed.analysed is True and composed.elements == []


def test_box_to_rect_helper():
    rect = composition_service.box_to_rect(Box(x=0.1, y=0.2, w=0.5, h=0.25), 400.0, 600.0)
    assert (rect.x_pt, rect.y_pt, rect.width_pt, rect.height_pt) == (40.0, 120.0, 200.0, 150.0)


def test_user_drawn_text_block_keeps_its_box(book):
    project, folder = book
    page = project.pages[0]
    drawn = TextBlock(box=Box(x=0.2, y=0.6, w=0.5, h=0.1), ocr_text="", corrected_text="Ajout", user_created=True,
                      order=50)
    page.text_blocks.append(drawn)
    composed = compose_page(project, page, folder, ReconstructionMode.FIDELITY)
    element = next(e for e in composed.text_elements if e.block_id == drawn.id)
    assert element.rect == composition_service.box_to_rect(drawn.box, composed.width_pt, composed.height_pt)


def test_user_image_block_without_file_is_reported(book):
    project, folder = book
    page = project.pages[0]
    page.image_blocks.append(ImageBlock(box=Box(x=0.2, y=0.6, w=0.3, h=0.2), user_created=True, order=99))
    composed = compose_page(project, page, folder, ReconstructionMode.HYBRID)
    assert any(issue.code == "composition_image_not_extracted" for issue in composed.issues)
