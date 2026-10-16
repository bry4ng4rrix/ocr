"""Contrats des modèles : validation, aller-retour JSON, résolution des réglages."""
import pytest
from pydantic import ValidationError

from models import (Box, BlockKind, ComposedPage, GeometryCorrection, ImageBlock, ImageElement, ImageEnhancement,
                    Page, PageFormat, Point, Project, Rect, ReconstructionMode, ResolvedStyle, TextBlock,
                    TextElement, TextStyle)
from models.errors import BlockNotFoundError, PageNotFoundError


def _page() -> Page:
    page = Page(source_file="originals/p.jpg", source_sha256="0" * 64, original_name="p.jpg")
    page.text_blocks.append(TextBlock(box=Box(x=0.1, y=0.1, w=0.8, h=0.1), ocr_text="Bonjour à tous", order=1,
                                      estimated_style=TextStyle(font_size_pt=11, bold=False)))
    page.image_blocks.append(ImageBlock(box=Box(x=0.1, y=0.3, w=0.5, h=0.3), file="images/a.png", order=0))
    return page


def test_box_rejects_out_of_page():
    with pytest.raises(ValidationError):
        Box(x=0.8, y=0.0, w=0.3, h=0.1)


def test_box_pixels_roundtrip():
    box = Box.from_pixels(100, 200, 500, 400, width=1000, height=2000)
    assert box.to_pixels(1000, 2000) == (100, 200, 500, 400)
    assert box.fraction_inside(Box(x=0, y=0, w=1, h=1)) == pytest.approx(1.0)


def test_box_geometry_helpers():
    a, b = Box(x=0.0, y=0.0, w=0.5, h=0.5), Box(x=0.25, y=0.25, w=0.5, h=0.5)
    assert a.intersection_area(b) == pytest.approx(0.0625)
    assert a.union(b).right == pytest.approx(0.75)
    assert 0 < a.iou(b) < 1


def test_text_style_merge_and_hex():
    base = TextStyle(font_size_pt=10, bold=False, color="#112233")
    merged = base.merged_with(TextStyle(bold=True))
    assert merged.font_size_pt == 10 and merged.bold is True
    assert merged.defined_fields() == {"font_size_pt", "bold", "color"}
    with pytest.raises(ValidationError):
        TextStyle(color="rouge")


def test_text_block_text_prefers_correction():
    block = TextBlock(box=Box(x=0, y=0, w=1, h=1), ocr_text="Bonjovr")
    assert block.text == "Bonjovr"
    block.corrected_text = "Bonjour"
    assert block.text == "Bonjour" and block.ocr_text == "Bonjovr"


def test_geometry_signature_tracks_effective_values():
    geometry = GeometryCorrection(detected_quarter_turns=1, detected_skew_deg=1.5)
    signature = geometry.signature()
    geometry.manual_quarter_turns = 1
    assert geometry.effective_quarter_turns() == 2
    assert geometry.signature() != signature
    geometry.auto_deskew = False
    assert geometry.effective_skew() == 0.0
    with pytest.raises(ValidationError):
        GeometryCorrection(manual_quad=[Point(x=0, y=0)])


def test_enhancement_neutral():
    assert ImageEnhancement().is_neutral()
    assert not ImageEnhancement(contrast=10).is_neutral()
    assert ImageEnhancement(contrast=10, enabled=False).is_neutral()


def test_page_lookup_and_order():
    page = _page()
    assert [type(b).__name__ for b in page.blocks_in_order()] == ["ImageBlock", "TextBlock"]
    with pytest.raises(BlockNotFoundError):
        page.text_block("absent")


def test_project_json_roundtrip_is_lossless():
    project = Project(name="Livre", languages=["fra", "deu"])
    page = _page()
    page.geometry.manual_quad = [Point(x=0.1, y=0.1), Point(x=0.9, y=0.1), Point(x=0.9, y=0.9), Point(x=0.1, y=0.9)]
    page.style_rules[BlockKind.PARAGRAPH] = TextStyle(font_family="Liberation Sans")
    page.format_override = PageFormat(name="Perso", width_mm=120, height_mm=190)
    project.pages.append(page)
    project.typography.kind_styles[BlockKind.HEADING_1] = TextStyle(font_size_pt=20)
    project.export.mode = ReconstructionMode.HYBRID

    reloaded = Project.model_validate_json(project.model_dump_json())
    assert reloaded == project
    assert reloaded.page(page.id).text_blocks[0].text == "Bonjour à tous"
    with pytest.raises(PageNotFoundError):
        reloaded.page("absent")


def test_target_format_choices():
    project = Project(name="Livre")
    page = _page()
    page.detected_format = PageFormat(name="13 × 20 cm", width_mm=130, height_mm=200)
    assert project.target_format(page).width_mm == 130
    project.export.page_format = "A4"
    assert project.target_format(page).name == "A4"
    project.export.page_format = "custom"
    project.export.custom_width_mm = 100
    assert project.target_format(page).width_mm == 100


def test_composed_page_discriminated_union_roundtrip():
    style = ResolvedStyle(font_family="Liberation Serif", font_size_pt=11)
    page = ComposedPage(page_id="p", page_number=1, width_pt=420, height_pt=595, mode=ReconstructionMode.FIDELITY,
                        elements=[TextElement(block_id="t", kind=BlockKind.PARAGRAPH, text="x",
                                              rect=Rect(x_pt=10, y_pt=10, width_pt=100, height_pt=20), style=style),
                                  ImageElement(block_id="i", kind="photo", file="/tmp/a.png",
                                               rect=Rect(x_pt=10, y_pt=40, width_pt=100, height_pt=80))])
    reloaded = ComposedPage.model_validate_json(page.model_dump_json())
    assert len(reloaded.text_elements) == 1 and len(reloaded.image_elements) == 1
